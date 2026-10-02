#!/usr/bin/env python3
"""codex_msg.py —— 给正在干活的 Codex 工人发消息 / 让它回话（双向对讲机）。

为什么要有它：`codex exec` 派出去的工人是无头的，一旦开跑就没有输入口。
Codex 内建的 `codex queue` 只对交互 / daemon 会话有效（实测往 exec 会话 queue 一条，
工人跑完全程都没收到），所以自己搭一条。

两个方向各走各的路：

  主线 → 工人：Codex 的 `PreToolUse` 钩子在工人**每条命令**前都会触发。
      钩子读信箱，有信就把这条命令拦下来（deny），把口信当拦截理由塞回去 ——
      模型必然读到，且没信时零 token、零打扰。

  工人 → 主线：工人跑 `codex_msg.py ask <频道> "问题"`，本进程把问题写进发件箱后
      **原地阻塞**轮询答复。主线答一句它就接着干，不用结束回合再找人。
      发件箱写完还得有人去看 —— 叫醒主线的两层保险见下。

叫醒主线：
  第一层 `wait`（主线闲着也能被叫醒）：主线派完单顺手挂一个
      `python codex_msg.py wait <频道>`（Claude Code 的 Bash run_in_background）。
      它阻塞到工人有提问 / 报告 / 收工，一退出主线就收到后台任务通知。
      答完再挂一个。退出码见 WAIT_* 常量。
  第二层 `codex_inbox_hook.py`（主线正在干活时即时看到）：挂在 Claude Code 的
      `PreToolUse` 上，主线每次调工具前把各活频道的未读提问 / 报告塞进上下文。
      它只塞不拦（`additionalContext`，不是 deny），没未读时输出 `{}`。

额度与缓存：钩子没消息时不写 stdout（模型侧零字节）；有消息时只追加一条工具结果，
不改写历史 —— prompt cache 前缀不受影响。

频道目录 = <工位>/.harness-tmp/codex-msg/<频道>/（必须在工人的沙箱可写范围内，
所以跟着工位走）；项目根 .harness-tmp/codex-msg/registry.json 登记 频道 → 绝对路径，
主线照它找。

项目根怎么定：`--root <目录>` 最优先，其次环境变量 CODEX_MSG_ROOT，再其次从当前目录
往上找第一个带 .git 的目录（worktree 里 .git 是文件，照样认），都找不到就用当前目录。

用法（主线）：
    python codex_msg.py ls
    python codex_msg.py send <频道> "口信"            # 下一条命令前送到
    python codex_msg.py send <频道> --stop "停手，…"   # 送到并挂住：回话前不许再动手
    python codex_msg.py poll [<频道>]                 # 看工人问了什么 / 报了什么
    python codex_msg.py answer <频道> "答复"          # 答最新一条待答的问
    python codex_msg.py wait <频道> [--timeout 秒]    # 挂后台守着，工人一出声就退出（= 叫醒）

用法（工人，派单开头里会给它抄好整行）：
    python codex_msg.py ask <频道> "问题" [--wait 900]   # 问一句并等答复
    python codex_msg.py say <频道> "进度 / 发现"          # 只报不等

用法（钩子，hooks.json 里接；codex_task.sh 派单时会自动 install）：
    python codex_msg.py --hook
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


def find_root(start) -> "Path | None":
    """项目根 = 从 start 往上第一个带 .git 的目录。

    worktree 里 .git 是**文件**不是目录，所以判据用 exists() 不用 is_dir()。
    找不到返回 None —— 调用方自己决定是退回当前目录（CLI）还是安静退出（钩子）。
    """
    try:
        p = Path(start).resolve()
    except Exception:
        return None
    for cand in [p, *p.parents]:
        try:
            if (cand / ".git").exists():
                return cand
        except Exception:
            continue
    return None


ROOT = find_root(os.environ.get("CODEX_MSG_ROOT") or Path.cwd()) or Path.cwd()
REGISTRY = ROOT / ".harness-tmp" / "codex-msg" / "registry.json"


PYX = Path(sys.executable).as_posix() if sys.executable else "python3"


def tool_hint() -> str:
    """打给人看的命令里写哪条路径：本文件的绝对路径。"""
    return str(Path(__file__).resolve()).replace("\\", "/")


def set_root(path) -> None:
    global ROOT, REGISTRY
    ROOT = Path(path).resolve()
    REGISTRY = ROOT / ".harness-tmp" / "codex-msg" / "registry.json"


DEFAULT_WAIT = 900          # 工人问完默认最多等 15 分钟，超时就自己拿主意继续
POLL_EVERY = 2.0            # 工人 `ask` 阻塞时的轮询间隔 = 主线 `answer` 后工人几秒内拿到；别调大

# —— `wait` 守候器的退出码（主线把它挂后台，进程一退主线就收到通知，看码即知发生了什么）——
WAIT_ASK = 0                # 工人在等答复（最要紧：它原地阻塞着，不答它就干等到超时）
WAIT_SAY = 10               # 只有报告，工人没在等，看一眼就行
WAIT_GONE = 20              # 工人收工了（频道已关），没有留话
WAIT_TIMEOUT = 30           # 等到超时都没动静（工人还在干，要接着守就再挂一个）
WAIT_NOCHAN = 40            # 频道始终没出现（频道名打错 / 派单没起来）
WAIT_POLL_EVERY = 1.0       # 守候器自己的轮询间隔：只 stat 两个小文件，1 秒一次不心疼
WAIT_DEFAULT_TIMEOUT = 3600


def now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%m-%d %H:%M:%S")


def read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    return rows


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as out:
        out.write(json.dumps(row, ensure_ascii=False) + "\n")


def rewrite_jsonl(path: Path, rows: list) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    tmp.replace(path)


# ——————————————————————————— 频道定位 ———————————————————————————

def registry() -> dict:
    if REGISTRY.exists():
        try:
            return json.loads(REGISTRY.read_text(encoding="utf-8"))
        except ValueError:
            return {}
    return {}


def register(channel: str, workdir: Path) -> Path:
    """派单时调用：登记频道并建目录，返回频道目录。"""
    chdir = Path(workdir).resolve() / ".harness-tmp" / "codex-msg" / channel   # 工位钉成绝对路径：登记表里若是相对路径，钩子从别的 cwd 就找不到频道
    chdir.mkdir(parents=True, exist_ok=True)
    (chdir / "live").write_text(now(), encoding="utf-8")
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    reg = registry()
    reg[channel] = {"dir": str(chdir), "workdir": str(Path(workdir).resolve()), "opened": now()}
    REGISTRY.write_text(json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")
    return chdir


def resolve(channel: str) -> Path:
    """主线 / 工人侧：频道名 → 频道目录。登记表优先，其次按当前目录猜。"""
    hit = registry().get(channel)
    if hit:
        return Path(hit["dir"])
    # 工人在自己的工位里跑：登记表在派单方的项目根、它看不见，就按目录猜。
    tried = []
    for base in (Path.cwd(), find_root(Path.cwd()), ROOT):
        if base is None:
            continue
        guess = Path(base) / ".harness-tmp" / "codex-msg" / channel
        if guess.exists():
            return guess
        tried.append(str(guess))
    raise SystemExit(f"找不到频道 {channel}（登记表 {REGISTRY} 里没有，这些落点也不存在：{tried}）")


def resolve_for_hook(payload: dict) -> "Path | None":
    """钩子侧：只认环境变量 `CODEX_MSG_CH`（派单时 export，实测能传到钩子进程）。

    故意不做「按目录猜唯一活频道」的兜底：你自己的交互 Codex 常在同一个仓库里
    干活，猜出来就会让他的会话收到发给工人的口信。拿不到频道 = 不归这把钩子管，
    安静退出。
    """
    ch = os.environ.get("CODEX_MSG_CH")
    if not ch:
        return None
    for base in (payload.get("cwd"), Path.cwd(), find_root(Path.cwd())):
        if not base:
            continue
        d = Path(base) / ".harness-tmp" / "codex-msg" / ch
        if d.exists():
            return d
    return None


# ——————————————————————————— 钩子挂载 ———————————————————————————

MARK = "codex_msg.py"


def hook_command() -> str:
    py = Path(sys.executable).as_posix()
    cmd = f'"{py}" "{Path(__file__).resolve().as_posix()}" --hook'
    # Codex 在 Windows 上用 PowerShell 跑钩子，PowerShell 把以引号开头的行当字符串表达式，
    # 不加 `&` 会语法错、钩子每次记成 Failed 然后 fail-open（看着像没挂上）。
    # macOS / Linux 走 sh，开头的 `&` 反而是语法错，所以只在 Windows 加。
    return ("& " + cmd) if sys.platform == "win32" else cmd


def install(workdir: Path) -> Path:
    """把对讲钩子挂进 <工位>/.codex/hooks.json（幂等，保留已有的别的钩子）。

    只能走项目级这一条路：`hooks.managed_dir` 是企业托管策略字段、`-c` 塞不进去，
    插件那条要装到机器上。工位（worktree）里 `.codex/` 不存在就新建一份。
    """
    cfg = Path(workdir) / ".codex" / "hooks.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    doc = {"hooks": {}}
    if cfg.exists():
        try:
            doc = json.loads(cfg.read_text(encoding="utf-8-sig"))
        except ValueError:
            doc = {"hooks": {}}
        backup = cfg.with_suffix(".json.pre-codex-msg")
        if not backup.exists():
            backup.write_bytes(cfg.read_bytes())
    hooks = doc.setdefault("hooks", {})
    pre = hooks.setdefault("PreToolUse", [])
    for group in pre:
        for h in group.get("hooks", []):
            if MARK in str(h.get("command", "")):
                h["command"] = hook_command()      # 路径可能变了，顺手刷新
                cfg.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
                return cfg
    pre.append({"matcher": "*", "hooks": [
        {"type": "command", "command": hook_command(), "timeout": 10}]})
    cfg.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def uninstall(workdir: Path) -> None:
    cfg = Path(workdir) / ".codex" / "hooks.json"
    if not cfg.exists():
        return
    doc = json.loads(cfg.read_text(encoding="utf-8-sig"))
    pre = doc.get("hooks", {}).get("PreToolUse", [])
    keep = [g for g in pre
            if not any(MARK in str(h.get("command", "")) for h in g.get("hooks", []))]
    doc["hooks"]["PreToolUse"] = keep
    cfg.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


# ——————————————————————————— 钩子面（主线 → 工人）———————————————————————————

HOW_TO_REPLY = (
    "——要回主线就跑这行（问完会原地等答复，答了就接着干；小事自己问，别攒到收工）："
    "\n  {PYX} {tool} ask {ch} \"你的问题\""
)


def trace(payload: dict, chdir, raw: str = "") -> None:
    """钩子有没有真被叫起来、当时看见的是什么 —— 唯一的诊断口，出问题先看它。

    只记元数据（不记命令正文 / 提示词），文件在主树 .harness-tmp 下、不入库。
    不归本钩子管的会话（没设 CODEX_MSG_CH）一行都不写，免得给你的交互 Codex
    每条命令刷一行。
    """
    if not os.environ.get("CODEX_MSG_CH"):
        return
    try:
        line = {"at": now(), "cwd": str(Path.cwd()),
                "env_ch": os.environ.get("CODEX_MSG_CH"),
                "resolved": str(chdir) if chdir else None,
                "session": payload.get("session_id"), "tool": payload.get("tool_name"),
                "event": payload.get("hook_event_name"), "raw_len": len(raw)}
        p = ROOT / ".harness-tmp" / "codex-msg" / "hook-trace.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as out:
            out.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:
        pass


def hook() -> int:
    # 任何异常都当没发生：钩子坏掉不能拖垮工人（fail-open 是这里的铁律）。
    raw = ""
    try:
        raw = sys.stdin.buffer.read().decode("utf-8-sig")
        payload = json.loads(raw or "{}")
    except ValueError:
        payload = {}
    chdir = resolve_for_hook(payload) if payload else None
    trace(payload, chdir, raw)
    if payload.get("hook_event_name") != "PreToolUse" or chdir is None:
        sys.stdout.write("{}")
        return 0
    inbox = chdir / "inbox.jsonl"
    state_file = chdir / "state.json"
    rows = read_jsonl(inbox)
    pending = [r for r in rows if not r.get("read_at")]
    held = False
    if state_file.exists():
        try:
            held = bool(json.loads(state_file.read_text(encoding="utf-8")).get("hold"))
        except ValueError:
            held = False
    if not pending and not held:
        # 快路：没信就回一个空决定。必须打印点什么 —— Codex 把「钩子没输出」记成
        # `hook: PreToolUse Failed`（fail-open 所以不拦活，但日志会被刷满）。
        # 空对象不携带任何决定，模型侧一个字节都不多花。
        sys.stdout.write("{}")
        return 0

    tool = tool_hint()
    ch = chdir.name
    if pending:
        for r in pending:
            r["read_at"] = now()
        rewrite_jsonl(inbox, rows)
        body = "\n\n".join(f"【主线口信 {r['at']}】{r['text']}" for r in pending)
        head = ("（这不是报错。你刚才那条命令被拦下了、没有执行 —— 拦它只是为了把下面"
                "这条口信塞给你。读完照口信办；如果口信没让你改做法，就把刚才那条命令"
                "重跑一遍接着干。）")
        if any(r.get("mode") == "stop" for r in pending):
            head = ("（停手。主线要你先回话再动手：在你用下面那行 ask 回话之前，"
                    "每条命令都会被拦下。）")
            state_file.write_text(json.dumps({"hold": True}), encoding="utf-8")
        reason = f"{head}\n\n{body}\n\n{HOW_TO_REPLY.format(PYX=PYX, tool=tool, ch=ch)}"
    else:
        reason = ("主线让你先回话再继续动手。\n\n"
                  + HOW_TO_REPLY.format(PYX=PYX, tool=tool, ch=ch))
    sys.stdout.write(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}, ensure_ascii=False))
    return 0


# ——————————————————————————— 工人面（工人 → 主线）———————————————————————————

def worker_post(channel: str, text: str, kind: str, wait: float) -> int:
    chdir = resolve(channel)
    outbox = chdir / "outbox.jsonl"
    entry = {"id": uuid.uuid4().hex[:8], "at": now(), "kind": kind, "text": text}
    append_jsonl(outbox, entry)
    (chdir / "state.json").write_text(json.dumps({"hold": False}), encoding="utf-8")  # 回话即解挂
    if kind == "say" or wait <= 0:
        print(f"[codex_msg] 已送达主线（{entry['id']}）。不等答复，继续干。")
        return 0
    print(f"[codex_msg] 已送达主线（{entry['id']}），等答复中，最多 {int(wait)} 秒…")
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(POLL_EVERY)
        for r in read_jsonl(outbox):
            if r.get("id") == entry["id"] and r.get("answer"):
                print(f"\n【主线答复 {r.get('answered_at')}】\n{r['answer']}")
                return 0
    print("\n[codex_msg] 主线没回（超时）。按你自己的判断继续干，"
          "并在最终报告里单独写一行：问过什么、没等到答复、你先按什么假设做了。")
    return 0


# ——————————————————————————— 主线面 ———————————————————————————

def cmd_ls() -> int:
    reg = registry()
    if not reg:
        print("（还没有登记过的频道）")
        return 0
    for ch, info in reg.items():
        d = Path(info["dir"])
        pend_in = len([r for r in read_jsonl(d / "inbox.jsonl") if not r.get("read_at")])
        pend_out = len([r for r in read_jsonl(d / "outbox.jsonl")
                        if r.get("kind") == "ask" and not r.get("answer")])
        alive = "活" if (d / "live").exists() else "关"
        print(f"{ch}  [{alive}]  未读口信 {pend_in}  待答提问 {pend_out}  {info['dir']}")
    return 0


def cmd_send(channel: str, text: str, stop: bool) -> int:
    chdir = resolve(channel)
    append_jsonl(chdir / "inbox.jsonl",
                 {"id": uuid.uuid4().hex[:8], "at": now(), "text": text,
                  "mode": "stop" if stop else "note"})
    print(f"已投递到 {channel}：工人下一条命令前会看到"
          + ("（并会被挂住直到回话）" if stop else ""))
    return 0


def unread(outbox: Path) -> tuple[list, list]:
    """(待答的提问, 没列过的报告) —— `poll` 与 `wait` 共用这一个判据，免得两处各算各的。

    「报告」主线列过一次就算收到，已读标记走**追加一行** `seen` 而不是改写文件：工人那头
    随时可能正在往同一个文件追加新行，改写会把它刚写的那行吃掉（ask 被吃 = 工人干等到超时）。
    「提问」不标已读 —— 答了才算完，没答就该一直显眼。
    """
    rows = read_jsonl(outbox)
    seen = {r.get("of") for r in rows if r.get("kind") == "seen"}
    asks = [r for r in rows if r.get("kind") == "ask" and not r.get("answer")]
    says = [r for r in rows if r.get("kind") == "say" and r.get("id") not in seen]
    return asks, says


def cmd_poll(channel: str | None) -> int:
    chans = [channel] if channel else list(registry().keys())
    anything = False
    for ch in chans:
        try:
            d = resolve(ch)
        except SystemExit:
            continue
        outbox = d / "outbox.jsonl"
        asks, says = unread(outbox)
        for r in asks:
            anything = True
            print(f"[{ch}] {r['at']} 提问 {r['id']}（等答复中）：\n  {r['text']}\n")
        for r in says:
            anything = True
            print(f"[{ch}] {r['at']} 报告：\n  {r['text']}\n")
            append_jsonl(outbox, {"kind": "seen", "of": r["id"], "at": now()})
    if not anything:
        print("（没有待答提问 / 新报告）")
    return 0


def cmd_wait(channel: str, timeout: float) -> int:
    """阻塞到工人出声（提问 / 报告）或收工，把内容原样打出来后退出。

    这是「工人 → 主线」方向的叫醒器：主线把它挂在 Claude Code 的后台 Bash 上，
    进程一退出主线就收到后台任务通知，于是闲着也能被叫醒。答完再挂一个。
    """
    chdir = None
    grace = time.time() + min(30.0, timeout)   # 频道可能还没建（派单脚本刚起），给一段宽限
    while True:
        try:
            chdir = resolve(channel)
            break
        except SystemExit:
            if time.time() >= grace:
                print(f"[wait] 频道 {channel} 始终没出现（名字打错？派单没起来？）")
                return WAIT_NOCHAN
            time.sleep(WAIT_POLL_EVERY)
    outbox = chdir / "outbox.jsonl"
    tool = tool_hint()
    deadline = time.time() + timeout
    print(f"[wait] 守着频道 {channel}，最多 {int(timeout)} 秒；工人一出声就退出并打在这里。")
    sys.stdout.flush()
    while True:
        # 先看有没有话，再看人还在不在：工人收工前最后一秒留的话不能漏。
        asks, says = unread(outbox)
        if asks or says:
            for r in says:
                append_jsonl(outbox, {"kind": "seen", "of": r["id"], "at": now()})
            print()
            for r in asks:
                print(f"[{channel}] {r['at']} 提问 {r['id']}（工人原地等答复中）：\n  {r['text']}\n")
            for r in says:
                print(f"[{channel}] {r['at']} 报告：\n  {r['text']}\n")
            if asks:
                print(f"→ 答它（工人 {int(POLL_EVERY)} 秒内收到）："
                      f"{PYX} {tool} answer {channel} \"你的答复\"")
                print(f"→ 答完再挂一个：{PYX} {tool} wait {channel}")
                return WAIT_ASK
            print(f"→ 接着守：{PYX} {tool} wait {channel}")
            return WAIT_SAY
        if not (chdir / "live").exists():
            print(f"\n[wait] 工人已收工（频道 {channel} 已关），没有留话。")
            return WAIT_GONE
        if time.time() >= deadline:
            print(f"\n[wait] {int(timeout)} 秒内没动静，工人还在干。要接着守就再挂一个："
                  f"{PYX} {tool} wait {channel}")
            return WAIT_TIMEOUT
        time.sleep(WAIT_POLL_EVERY)


def cmd_answer(channel: str, text: str, msg_id: str | None) -> int:
    d = resolve(channel)
    outbox = d / "outbox.jsonl"
    rows = read_jsonl(outbox)
    target = None
    for r in rows:
        if r.get("kind") == "ask" and not r.get("answer"):
            if msg_id is None or r.get("id") == msg_id:
                target = r
                if msg_id is None:
                    continue        # 不指定就答最后一条待答的
                break
    if target is None:
        print("没有待答的提问")
        return 1
    target["answer"] = text
    target["answered_at"] = now()
    rewrite_jsonl(outbox, rows)
    print(f"已答复 {target['id']}：工人最多 {int(POLL_EVERY)} 秒内收到")
    return 0


def main(argv: list[str]) -> int:
    # Windows 默认 stdout 是 GBK：不强制 UTF-8 的话，口信里的中文到工人手里就是乱码。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    argv = list(argv)
    if "--root" in argv:                       # 显式指到哪个项目（codex_task.sh 派单时会传）
        i = argv.index("--root")
        if i + 1 < len(argv):
            set_root(argv[i + 1])
        del argv[i:i + 2]
    if "--hook" in argv:
        return hook()
    if not argv:
        print(__doc__)
        return 2
    verb, rest = argv[0], argv[1:]

    def opt(name: str, default=None):
        if name in rest:
            i = rest.index(name)
            val = rest[i + 1] if i + 1 < len(rest) else default
            del rest[i:i + 2]
            return val
        return default

    if verb == "ls":
        return cmd_ls()
    if verb == "register":                      # codex_task.sh 派单时调用
        print(register(rest[0], Path(rest[1])))
        return 0
    if verb == "install":
        print(install(Path(rest[0]) if rest else ROOT))
        return 0
    if verb == "uninstall":
        uninstall(Path(rest[0]) if rest else ROOT)
        return 0
    if verb == "close":
        (resolve(rest[0]) / "live").unlink(missing_ok=True)
        return 0
    if verb in {"ask", "say"}:
        wait = float(opt("--wait", DEFAULT_WAIT) or DEFAULT_WAIT)
        if len(rest) < 2:
            print("用法：codex_msg.py ask <频道> \"问题\" [--wait 秒]")
            return 2
        return worker_post(rest[0], " ".join(rest[1:]), verb, wait)
    if verb == "send":
        stop = "--stop" in rest
        if stop:
            rest.remove("--stop")
        if len(rest) < 2:
            print("用法：codex_msg.py send <频道> [--stop] \"口信\"")
            return 2
        return cmd_send(rest[0], " ".join(rest[1:]), stop)
    if verb == "poll":
        return cmd_poll(rest[0] if rest else None)
    if verb == "wait":
        secs = float(opt("--timeout", WAIT_DEFAULT_TIMEOUT) or WAIT_DEFAULT_TIMEOUT)
        if not rest:
            print("用法：codex_msg.py wait <频道> [--timeout 秒]")
            return 2
        return cmd_wait(rest[0], secs)
    if verb == "answer":
        msg_id = opt("--id")
        if len(rest) < 2:
            print("用法：codex_msg.py answer <频道> [--id <id>] \"答复\"")
            return 2
        return cmd_answer(rest[0], " ".join(rest[1:]), msg_id)
    print(f"未知子命令 {verb}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
