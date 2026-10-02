#!/usr/bin/env bash
# codex_task.sh — 把本机 Codex CLI 当无头 subagent 派活。走你已登录的 Codex 账号（codex login）。
#
# 项目根 = 从当前目录往上第一个带 .git 的目录（worktree 里 .git 是文件，照样认），
# 日志与对讲频道落 <项目根>/.harness-tmp/。不绑任何项目，任何 git 仓库直接用。
#
# Usage:
#   codex_task.sh -p "prompt" [opts]              # 短任务内联派单
#   codex_task.sh -f brief.md [opts]              # 长 brief 从文件派单（主用法）
#   codex_task.sh --resume <session-id> -m <model> -p "..."   # 对既有会话追问/返工
# Options:
#   --ro          只读沙箱（分析/评审任务；默认 workspace-write 可改文件）
#   --net         沙箱内放开网络（codex 默认 workspace-write 里 shell 是断网的）
#   --nosearch    关掉原生 web_search（默认开着）
#   --search      反向开关（给包装脚本用：包装先写 --nosearch，调用方可再用它开回来）
#   --no-channel  不开对讲频道（默认开；开着才有主线↔工人捎话/反问）
#   --full        danger-full-access：不沙箱（只在你明确要时用）
#   --add-dir <d> 额外可写目录（默认只有工作根可写）
#   --codex-arg <a> 原样透传一个参数给 codex（可重复）
#   -i <file>     给初始 prompt 附图（可重复）
#   --tag <name>  日志文件名前缀（默认 task）
#   -m <model>    覆盖模型（默认走你的 ~/.codex/config.toml）
#   -e <effort>   思考档位 minimal|low|medium|high|xhigh|max（默认 xhigh）
#   -C <dir>      工作目录（并行派单给独立 git worktree 用；默认当前目录）
# Environment:
#   CODEX_TASK_BIN               指定 codex 可执行文件（默认：skill 目录下 codex-cli/ → PATH 里的 codex）
#   CODEX_TASK_WD_GRACE          看门狗宽限秒数（默认 120）
#   CODEX_TASK_CAPACITY_RETRIES  模型满载被掐断时的自动续跑次数（默认 3）
#   CODEX_TASK_CAPACITY_SLEEP    续跑前等待秒数（默认 90）
# Output:
#   stdout = codex 的最终答复；stderr 一行 SESSION=<id> LOG=<path> EXIT=<rc>
#
# 铁则：绝不 `resume --last` —— 你的交互 codex 客户端共用同一会话存储，
# --last 会接进你的活会话。追问只用显式 session id。

set -uo pipefail

# 项目根 = 往上第一个带 .git 的目录;找不到就用当前目录(codex 本来也要求在仓库里跑)。
find_root() {
  local cur="$1"
  while [ -n "$cur" ] && [ "$cur" != "/" ]; do
    [ -e "$cur/.git" ] && { echo "$cur"; return 0; }
    cur="$(dirname "$cur")"
  done
  return 1
}
PROJECT="$(find_root "$PWD" || echo "$PWD")"
OUTDIR="$PROJECT/.harness-tmp/codex"
mkdir -p "$OUTDIR"

SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLROOT="$(cd "$SCRIPTS/.." && pwd)"
CODEX_BIN="${CODEX_TASK_BIN:-}"
[ -n "$CODEX_BIN" ] && [ -x "$CODEX_BIN" ] || CODEX_BIN="$SKILLROOT/codex-cli/node_modules/.bin/codex"
[ -x "$CODEX_BIN" ] || CODEX_BIN="codex"
PY="$(command -v python3 || command -v python || true)"
[ -n "$PY" ] || { echo "need python3 (used by the intercom)" >&2; exit 2; }
IS_WIN=0; case "$(uname -s 2>/dev/null)" in MINGW*|MSYS*|CYGWIN*) IS_WIN=1;; esac

prompt=""; pfile=""; resume_id=""; sandbox="workspace-write"; tag="task"; model=""; effort="xhigh"; workdir=""
net=0; search=1; images=(); adddirs=(); extra_args=(); channel=1
while [ $# -gt 0 ]; do
  case "$1" in
    -p) prompt="$2"; shift 2;;
    -f) pfile="$2"; shift 2;;
    --resume) resume_id="$2"; shift 2;;
    --help) sed -n '/^# Usage:/,/^# 铁则:/{ /^# 铁则:/d; s/^# \{0,1\}//; p; }' "${BASH_SOURCE[0]}"; exit 0;;
    --ro) sandbox="read-only"; shift;;
    --full) sandbox="danger-full-access"; shift;;
    --net) net=1; shift;;
    --nosearch) search=0; shift;;
    --search) search=1; shift;;
    --no-channel) channel=0; shift;;
    --add-dir) adddirs+=("$2"); shift 2;;
    -i) images+=("$2"); shift 2;;
    --tag) tag="$2"; shift 2;;
    -m) model="$2"; shift 2;;
    -e) effort="$2"; shift 2;;
    -C) workdir="$2"; shift 2;;
    --codex-arg) extra_args+=("$2"); shift 2;;   # 原样透传给 codex 的一个参数(可重复;如 -c mcp_servers.x.command=…)
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

case "$resume_id" in
  --last|last)
    echo "REFUSED: resume --last 会劫持你的交互会话;传显式 session id" >&2
    exit 2;;
esac
# resume 不继承原会话的模型 / 档位，漏 -m 就掉回 config 默认模型，所以强制重传。
if [ -n "$resume_id" ] && [ -z "$model" ]; then
  echo "REFUSED: --resume 必须重传 -m <model>(原会话的模型不继承;还应重传 -e 与 -C)" >&2
  exit 2
fi

ts="$(date +%m%d-%H%M%S)"
log="$OUTDIR/$tag-$ts.log"
last="$OUTDIR/$tag-$ts.last.md"

# —— 对讲机（同目录 codex_msg.py）：无头工人跑起来后仍能收主线口信、也能反过来问主线 ——
# 送达走 Codex 的 PreToolUse 钩子（工人每条命令前触发；没口信时零 token），
# 回话走 `codex_msg.py ask`（工人原地阻塞等答复，不必结束回合再找人）。
# 频道目录跟着工位走（必须在工人沙箱可写范围内）；--ro 单只有主线→工人这一向。
MSGTOOL="$SCRIPTS/codex_msg.py"
MSGCMD="$PY \"$MSGTOOL\""
CH=""
if [ "$channel" = 1 ]; then
  CH="$tag-$ts"
  "$PY" "$MSGTOOL" --root "$PROJECT" register "$CH" "${workdir:-$PROJECT}" >/dev/null 2>&1
  "$PY" "$MSGTOOL" --root "$PROJECT" install "${workdir:-$PROJECT}" >/dev/null 2>&1
  export CODEX_MSG_CH="$CH"
fi

args=(exec)
if [ -n "$resume_id" ]; then
  # resume 子命令不吃 -s,沙箱继承原会话
  args+=(resume "$resume_id")
else
  args+=(-s "$sandbox")
fi
[ -n "$model" ] && args+=(-m "$model")
[ -n "$effort" ] && args+=(-c "model_reasoning_effort=\"$effort\"")
[ "$net" = 1 ] && args+=(-c "sandbox_workspace_write.network_access=true")
[ "$search" = 1 ] && args+=(-c "tools.web_search=true")
# Windows：若 ~/.codex 里配了 sandbox = "elevated"，headless 每条命令都会弹管理员授权窗。
# 逐次覆写成 unelevated（沙箱照旧在，只是不再要管理员；不改你的 ~/.codex）。
[ "$IS_WIN" = 1 ] && args+=(-c 'windows.sandbox="unelevated"')
# 对讲机每次派单都会刷新工位 .codex/hooks.json 里的路径，信任哈希必然对不上 ⇒ 逐次放行。
# 只在开频道时加(不开频道的单不碰工位钩子,也就不需要放行)。
[ "$channel" = 1 ] && args+=(--dangerously-bypass-hook-trust)
for x in ${extra_args+"${extra_args[@]}"}; do args+=("$x"); done
for d in ${adddirs+"${adddirs[@]}"}; do args+=(--add-dir "$d"); done
for im in ${images+"${images[@]}"}; do
  args+=(-i "$(cd "$(dirname "$im")" && pwd)/$(basename "$im")")
done
args+=(-o "$last")

# brief 路径在 cd 前转绝对(-C worktree 时相对路径会指错地方)
if [ -n "$pfile" ]; then
  pfile="$(cd "$(dirname "$pfile")" && pwd)/$(basename "$pfile")"
fi
cd "${workdir:-$PROJECT}"
if [ -z "$pfile" ] && [ -z "$prompt" ]; then
  echo "need -p or -f" >&2
  exit 2
fi

# —— 终点线看门狗：工人报告写完后进程偶尔挂死不退 ——
# codex 后台跑；尾消息文件（-o）落盘 = 报告已交，之后 GRACE 秒仍不退就杀「本次启动的」进程
# （Windows 用 taskkill /T 连子进程一起收；绝不按进程名杀，你的交互客户端无恙）。
WD_GRACE="${CODEX_TASK_WD_GRACE:-120}"

# 对讲说明只拼给 Codex(频道号是本单一次性的,塞不进静态文件)。resume 不拼:原会话已经知道。
intercom=""
if [ "$channel" = 1 ] && [ -z "$resume_id" ]; then
  intercom="【和主线对讲(本单频道 = $CH)】
干活途中主线可以给你捎话:某条命令突然被拦下、理由开头写着「主线口信」,那不是报错 ——
照口信办,如果口信没让你改做法,就把被拦的那条命令重跑一遍继续。
你也能随时反问主线,别把小疑问攒到收工、也别为一句话就停工交单:
  $MSGCMD ask $CH \"你的问题\"      <- 问完原地等答复(最多 15 分钟),答了就接着干
  $MSGCMD say $CH \"阶段性发现\"    <- 只报一句,不等回复
拿不准该不该问就问:一次 ask 比一次跑偏便宜得多。超时没人应答就自己拿主意继续,
并在最终报告里单写一行:问过什么、没等到答复、先按什么假设做的。
"
fi

if [ -n "$pfile" ]; then
  { [ -z "$intercom" ] || printf '%s\n' "$intercom"; cat "$pfile"; } | "$CODEX_BIN" "${args[@]}" - > "$log" 2>&1 &
else
  { [ -z "$intercom" ] || printf '%s\n' "$intercom"; printf '%s\n' "$prompt"; } | "$CODEX_BIN" "${args[@]}" - > "$log" 2>&1 &
fi
cpid=$!
if [ -n "$CH" ]; then
  # 频道号 = 日志文件名的词干,所以随时能从 log 路径反推。早点打出来:派单是阻塞的,
  # 主线要在它跑着的时候捎话,得先拿到这一行。
  echo "CHANNEL=$CH  捎话: $MSGCMD send $CH \"…\"  |  看它问什么: $MSGCMD poll $CH" >&2
  echo "WAKEUP=$CH  挂守候器(Claude Code 的 Bash run_in_background),工人一出声就叫醒你: $MSGCMD wait $CH" >&2
fi
winpid=""
[ "$IS_WIN" = 1 ] && winpid="$(ps -p "$cpid" 2>/dev/null | awk 'NR==2{print $4}')"
wd_fired=0; wd_deadline=0
while kill -0 "$cpid" 2>/dev/null; do
  if [ "$wd_deadline" = 0 ] && [ -s "$last" ]; then
    wd_deadline=$(( $(date +%s) + WD_GRACE ))
  elif [ "$wd_deadline" != 0 ] && [ "$(date +%s)" -ge "$wd_deadline" ]; then
    echo "[codex_task] WATCHDOG: 尾消息已落盘 ${WD_GRACE}s 后进程仍未退出，收尸本次进程 (pid=$cpid win=$winpid)" >> "$log"
    if [ -n "$winpid" ]; then
      taskkill //PID "$winpid" //T //F >> "$log" 2>&1
    else
      kill -9 "$cpid" 2>/dev/null
    fi
    wd_fired=1
    break
  fi
  sleep 5
done
wait "$cpid" 2>/dev/null
rc=$?
# 报告已交付时看门狗收尸不算失败
[ "$wd_fired" = 1 ] && [ -s "$last" ] && rc=0

wd_note=""; [ "$wd_fired" = 1 ] && wd_note=" WATCHDOG=killed"
sid="$(grep -m1 '^session id:' "$log" | awk '{print $3}')"

# —— 容量掐断自动续跑：高峰期可能被服务端回「Selected model is at capacity」半路掐死 ——
# 判据 = 非零退出 + 日志尾部有那句话 + 拿得到 session id；等一会儿再 resume 同一会话。
# 别的失败原因不重试（那是活本身的问题，该人看）。
CAP_RETRIES="${CODEX_TASK_CAPACITY_RETRIES:-3}"
CAP_SLEEP="${CODEX_TASK_CAPACITY_SLEEP:-90}"
attempt=0
while [ "$rc" != 0 ] && [ "$attempt" -lt "$CAP_RETRIES" ] && [ -n "$sid" ] \
    && tail -c 4000 "$log" | grep -q 'at capacity'; do
  attempt=$((attempt + 1))
  echo "[codex_task] CAPACITY: 模型满载被掐断,${CAP_SLEEP}s 后第 $attempt/$CAP_RETRIES 次续跑 session $sid" >&2
  sleep "$CAP_SLEEP"
  rlog="$OUTDIR/$tag-$ts.retry$attempt.log"
  rargs=(exec resume "$sid")
  [ -n "$model" ] && rargs+=(-m "$model")
  [ -n "$effort" ] && rargs+=(-c "model_reasoning_effort=\"$effort\"")
  [ "$IS_WIN" = 1 ] && rargs+=(-c 'windows.sandbox="unelevated"')
  # 续跑同样要放行对讲钩子（信任哈希对不上），否则续跑后工人收不到口信
  [ "$channel" = 1 ] && rargs+=(--dangerously-bypass-hook-trust)
  rargs+=(-o "$last")
  "$CODEX_BIN" "${rargs[@]}" "上一轮被服务端容量错误打断。从被打断处接着做完原派工单,交付格式照旧;已做完的不重做。" > "$rlog" 2>&1 < /dev/null
  rc=$?
  log="$rlog"
done
[ "$attempt" -gt 0 ] && wd_note="$wd_note CAPACITY_RETRIES=$attempt"

if [ -n "$CH" ]; then
  "$PY" "$MSGTOOL" --root "$PROJECT" close "$CH" >/dev/null 2>&1
  # 工人可能在收工前留了话(say / 没等到答复的 ask):带一行提醒,免得主线漏看。
  pend="$("$PY" "$MSGTOOL" --root "$PROJECT" poll "$CH" 2>/dev/null | head -1)"
  case "$pend" in ""|"（没有待答提问 / 新报告）") ;; *) echo "CHANNEL=$CH 有未读留言,跑 $MSGCMD poll $CH" >&2;; esac
fi

echo "SESSION=${sid:-unknown} LOG=$log EXIT=$rc$wd_note" >&2
cat "$last" 2>/dev/null
exit $rc
