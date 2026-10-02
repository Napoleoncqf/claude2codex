# claude2codex

[English](README.md) | **简体中文**

让 [Claude Code](https://claude.com/claude-code) 把活派给 [Codex CLI](https://github.com/openai/codex),
并且在 Codex 干活的**过程中还能继续和它对话**。

它是一个 Claude Code 的 *skill*(装好后文件夹名叫 `dispatch-codex`),由三个小脚本组成,**不需要常驻后台服务**。

---

## 目录
1. [它解决什么问题](#1-它解决什么问题)
2. [原理(大白话版)](#2-原理大白话版)
3. [开始之前](#3-开始之前)
4. [安装,一步一步来](#4-安装一步一步来)
5. [第一次派单(完整示例)](#5-第一次派单完整示例)
6. [命令速查表](#6-命令速查表)
7. [常见问题排查](#7-常见问题排查)
8. [安全与局限](#8-安全与局限)
9. [卸载](#9-卸载)
10. [同类项目](#10-同类项目)
11. [FAQ](#11-faq)

---

## 1. 它解决什么问题

Claude Code 擅长规划和审查,Codex 擅长按明确的任务埋头干活、跑命令。常见搭配是:
**Claude 出方案 → Codex 动手做 → Claude 审结果。**

把任务交给 Codex 很简单:`codex exec "做这件事"` 就能以*无头模式*(没有交互界面)运行。
问题是,无头的工人**听不见你说话**:

- 做到一半你想起来"等等,改用方案 B"——晚了,它已经在跑了。
- 工人走到岔路口("A 还是 B?"),身边没人可问,只能**瞎猜**。

`claude2codex` 补上了这条缺失的通道:

| 你想… | 命令 | 效果 |
|---|---|---|
| 运行中给工人捎一句话 | `send` | 话在它执行下一条命令前送到 |
| 让工人先停下来回答你 | `send --stop` | 它回话之前,每条命令都会被拦住 |
| 让工人反过来问你 | 工人运行 `ask` | 它原地暂停,你 `answer` 后在**同一次运行里**接着干 |
| 工人一出声就通知你 | `wait` / 收件钩子 | 就算 Claude 当时闲着也会被叫醒 |

## 2. 原理(大白话版)

两个小事实让这一切成为可能:

1. **Codex 钩子(hook)。** Codex 可以在工人**每条命令执行之前**先运行你的脚本(`PreToolUse` 钩子)。
   脚本如果回答"拒绝",这条命令就不会执行,拒绝理由会展示给模型看。
   我们把拒绝理由当成**送信的信使**:你的话就写在理由里。并不是出错了,工人读完话后会把命令重新跑一遍。
   没有话要送时,钩子只输出 `{}`,一个 token 也不花。
2. **用文件当信箱。** 每次派单会开一条*频道*,就是一个文件夹
   `<项目>/.harness-tmp/codex-msg/<频道>/`,里面有 `inbox.jsonl`(你 → 工人)和
   `outbox.jsonl`(工人 → 你)。没有服务器、没有数据库,不需要常驻任何进程。

```
        你 / Claude Code                                    Codex 工人(无头)
   ┌────────────────────────┐                         ┌──────────────────────────────┐
   │ codex_msg.py send  ────┼──▶ inbox.jsonl ──钩子──▶│ 下一条命令被拦一次,           │
   │                        │                         │ 理由里带着你的话              │
   │ codex_msg.py wait  ◀───┼─── outbox.jsonl ◀───────┼─ codex_msg.py ask "A 还是 B?" │
   │ codex_msg.py answer ───┼──▶ (answer 字段) ──────▶│ ……解除阻塞,继续干活          │
   └────────────────────────┘                         └──────────────────────────────┘
```

叫醒 Claude 有两层保险,因为 Claude 可能闲着,也可能正忙:

- **`wait`**——你把它放到后台跑。工人一出声它就退出,Claude Code 会在后台任务退出时通知你。
  这样能叫醒*闲着*的 Claude。
- **收件钩子**(可选)——一个 Claude Code 钩子,每次调用工具前,悄悄把工人未读的提问塞进 Claude 的上下文。
  这样能触达*正忙*的 Claude。

## 3. 开始之前

你需要:

| 依赖 | 怎么检查 | 没有的话 |
|---|---|---|
| **Claude Code** | `claude --version` | https://claude.com/claude-code |
| **Codex CLI** | `codex --version` | `npm i -g @openai/codex`(需要 [Node.js](https://nodejs.org)) |
| **Codex 已登录** | `codex login status` | `codex login`(ChatGPT 账号或 API key) |
| **Python 3.9+** | `python3 --version` | https://www.python.org |
| **bash 和 git** | `bash --version`、`git --version` | macOS/Linux 自带;Windows 请装 Git for Windows |

你的项目必须是一个 **git 仓库**(脚本靠找 `.git` 来确定项目根目录)。

已在 macOS + Codex CLI 0.160.0(ChatGPT 登录)+ Python 3.14 上实测通过。
代码里有 Windows 专用分支,但**没有测过**。

## 4. 安装,一步一步来

**第 1 步 —— 把 skill 放到 Claude Code 找 skill 的地方。**
文件夹**必须**叫 `dispatch-codex`:

```bash
git clone https://github.com/Napoleoncqf/claude2codex.git
mkdir -p ~/.claude/skills
cp -r claude2codex ~/.claude/skills/dispatch-codex
```

**第 2 步 ——(可选)在 skill 文件夹里固定一份私有的 Codex**,这样你以后升级全局 Codex 也不会影响派单。
不做这一步就使用 `PATH` 里的 `codex`。

```bash
npm i --prefix ~/.claude/skills/dispatch-codex/codex-cli @openai/codex
```

也可以用 `export CODEX_TASK_BIN=/path/to/codex` 指定任意 Codex 可执行文件。

**第 3 步 —— 在每个会派单的项目里忽略临时目录。**
日志、brief、频道都放在 `.harness-tmp/` 里:

```bash
echo ".harness-tmp/" >> .gitignore
```

**第 4 步 ——(推荐)安装 Claude 侧的收件钩子。**
打开 `~/.claude/settings.json`,加入下面内容(如果已有 `"hooks"`,请合并进去):

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "python3 \"$HOME/.claude/skills/dispatch-codex/scripts/codex_inbox_hook.py\""
          }
        ]
      }
    ]
  }
}
```

这个钩子是*失败即放行*的(出任何问题都保持沉默,绝不会拦住 Claude),并且在没用过对讲机的项目里什么也不做。
不装它一切照常工作,只是你要靠 `wait` 来获知工人的提问,而不是在忙的时候被即时打断。
Windows 上请写 Python 和脚本的完整路径。

**第 5 步 —— 重启 Claude Code**,让它加载新的 skill 和钩子。

Codex 那一侧**不用你装任何东西**:每次派单都会自动把 Codex 钩子写进
`<工作目录>/.codex/hooks.json`(已有文件会先备份成 `hooks.json.pre-codex-msg`)。

## 5. 第一次派单(完整示例)

最简单的用法:直接用大白话对 Claude 说——*"把这个重构派给 Codex"*,skill 会接管。
下面是背后发生的事,让你知道自己在看什么。

**A. 写一份简短的 brief**(做什么、动哪些文件、怎样算完成):

```markdown
# brief-demo.md
目标:给 src/auth.py 里的 signup() 加输入校验。
只许改:src/auth.py、tests/test_auth.py。
完成判据:`pytest tests/test_auth.py` 通过。
```

**B. 派单**(放后台跑,因为可能要几分钟):

```bash
S=~/.claude/skills/dispatch-codex/scripts
bash $S/codex_task.sh -f brief-demo.md --tag demo
```

它打印的最前面几行会告诉你频道名:

```
CHANNEL=demo-1002-092405  ...
WAKEUP=demo-1002-092405   ...
```

**C. 工人干活时和它对话:**

```bash
M="python3 $S/codex_msg.py"
$M send demo-1002-092405 "另外,用户名为空也要拒绝"   # 在它下一条命令前送到
$M wait demo-1002-092405                              # 阻塞,直到工人出声
```

假设工人问:"校验失败应该抛异常还是返回 None?"。`wait` 会以退出码 `0` 退出并打印这个问题,然后:

```bash
$M answer demo-1002-092405 "抛 ValueError"
```

工人大约 2 秒内收到答复,在**同一次运行里**继续干,不用重启。

**D. 干完之后**会打印最终答复,最后一行是 session id:

```
SESSION=01a0fd6e-... LOG=.harness-tmp/codex/demo-....log EXIT=0
```

**E. 信任之前先审查。** 工人的产出是*未审草稿*:请看 `git diff` 并跑测试。

如果要在同一会话里追问或返工(必须重新传模型):

```bash
bash $S/codex_task.sh --resume <session-id> -m <model> -p "有两个测试挂了:<粘贴输出>。修掉。"
```

## 6. 命令速查表

**派单** —— `codex_task.sh`

| 参数 | 含义 |
|---|---|
| `-f brief.md` / `-p "文本"` | 任务来自文件(推荐)或直接写在命令里 |
| `--tag NAME` | 日志文件名和频道名的前缀 |
| `--ro` | 只读沙箱(用于评审)。此模式下工人没法 `ask`——只有单向 |
| `--net` | 沙箱内允许联网(默认关) |
| `--nosearch` | 关闭 Codex 内置的联网检索(默认开) |
| `--full` | 完全不沙箱。除非你真的清楚在做什么,别用 |
| `--add-dir DIR` | 多给一个可写目录 |
| `-i IMG` | 附一张图(可重复) |
| `-C DIR` | 在别的目录工作,例如并行任务用单独的 `git worktree` |
| `-m MODEL`、`-e EFFORT` | 模型和思考档位(`minimal`…`max`,默认 `xhigh`) |
| `--resume ID` | 接着某个会话干(只接受明确的 id;`--last` 被故意拒绝) |
| `--no-channel` | 这一单不开对讲频道 |

完整列表:`bash codex_task.sh --help`。

**对讲** —— `codex_msg.py`

| 命令 | 谁用 | 含义 |
|---|---|---|
| `ls` | 你 | 列出频道、未读口信、待答提问 |
| `send CH "话"` | 你 | 在工人下一条命令前送达一句话 |
| `send CH --stop "话"` | 你 | 同上,并且让工人在回话前不许动手 |
| `poll [CH]` | 你 | 看工人问了什么、报了什么 |
| `answer CH "答复"` | 你 | 回答最新一条待答的提问 |
| `wait CH [--timeout 秒]` | 你 | 阻塞到工人出声或收工 |
| `ask CH "问题"` | 工人 | 提问并暂停(默认最多 15 分钟)直到有人答复 |
| `say CH "话"` | 工人 | 汇报进度,不等回复 |

`wait` 的退出码:`0` 工人在等你的答复 · `10` 只有报告 · `20` 工人收工 · `30` 超时 · `40` 找不到频道。
**请单独运行 `wait`,不要接管道**,否则会丢掉退出码。

## 7. 常见问题排查

| 现象 | 可能原因和解决办法 |
|---|---|
| `codex: command not found` | 安装它(`npm i -g @openai/codex`)或设置 `CODEX_TASK_BIN`。如果是卸载了 cask 后残留的 Homebrew 链接,也是同样的报错 |
| `need python3` | 安装 Python 3 |
| 工人日志里出现 `ERROR ... Command blocked by PreToolUse hook` | **正常。** 这就是口信在送达。工人读完后会重跑那条命令 |
| `send` 之后没反应 | 口信是在工人*下一条命令之前*送达的。如果它正卡在一条很长的命令里(如 `sleep 600`),就得等。用 `ls` 确认频道是否存在 |
| 工人始终收不到话 | 确认没有用 `--no-channel`,并且 `<工作目录>/.codex/hooks.json` 存在 |
| 工人提问时我没被打断 | 你跳过了第 4 步,或没重启 Claude Code。`wait` 依然可用 |
| `--ro` 的工人没法提问 | 设计如此:只读沙箱写不了发件箱。需要提问请用默认模式 |
| 运行中途报 "at capacity" | 脚本会自动续跑(默认 3 次,可用 `CODEX_TASK_CAPACITY_RETRIES` 调整) |

诊断:`.harness-tmp/codex-msg/hook-trace.log` 会记录 Codex 钩子每次被触发的情况(只记元数据,不记命令内容)。

## 8. 安全与局限

- **工人由 Codex 的沙箱约束。** 默认 `workspace-write`:能改项目里的文件,但它的 shell 没有网络。
  `--ro` 只读。`--full` 会去掉保护——尽量别用。
- 开了频道的派单会加上 **`--dangerously-bypass-hook-trust`**,让 Codex 在钩子路径被刷新后不必每次重新确认就运行对讲钩子。
  它只作用于*这一次*派单。想避开就用 `--no-channel`。
- **你自己的 Codex 会话不会被拦截。** Codex 钩子只在带有派单脚本设置的 `CODEX_MSG_CH` 环境变量的进程里生效,
  即使你在同一个仓库里工作也不会受影响。
- **仅限本机、同一用户。** 频道就是普通文件,能写你项目目录的人就能写口信。这不是多用户或联网工具。
- **把工人的产出当成不可信的。** 看 diff,跑测试。
- 不要提交 `.harness-tmp/`、派单生成的 `.codex/hooks.json`,更不要提交任何 `auth.json`。
- 如实说明能力边界:口信只在命令与命令之间送达,没法打断一条已经在执行的命令。

## 9. 卸载

```bash
# 1. 对每个派过单的项目:移除 Codex 侧钩子和临时文件
python3 ~/.claude/skills/dispatch-codex/scripts/codex_msg.py uninstall <项目目录>
rm -rf <项目目录>/.harness-tmp

# 2. 删除 skill 本体
rm -rf ~/.claude/skills/dispatch-codex

# 3. 把第 4 步加进 ~/.claude/settings.json 的 PreToolUse 条目删掉
```

## 10. 同类项目

"从 Claude Code 委派给 Codex"这个领域已经很拥挤,本项目只想把一件事做好。

- [OpenAI 官方 `codex-plugin-cc`](https://community.openai.com/t/introducing-codex-plugin-for-claude-code/1378186):
  `/codex:review`、`/codex:rescue` 等命令。一行命令安装,适合评审和一次性救场。
  据我所见,它没有对无头工人的运行中通信。
- [`dpemmons/intercom`](https://pkg.go.dev/github.com/dpemmons/intercom):用 broker 加 MCP 适配器,
  在运行中的 Claude Code 会话和 Codex app-server 会话之间互发消息。更重(一个 Go 服务),
  但也覆盖交互式会话;它不针对无头的 `codex exec`。
- 一些更小的 `codex-skill` 仓库,把任务派给 Codex(单向)。

**本项目的定位:** 你想继续用朴素的 `codex exec` 工人,**不要常驻进程、不要 MCP 服务、不要 Go 工具链**
——只要三个脚本加一个文件夹——同时还能随时给它们纠偏。
如果你只需要评审和"派出去就不管",用官方插件就好。

## 11. FAQ

**会额外花 token 吗?** 只有真的有口信或提问时才花。静默的钩子只输出 `{}`。

**能同时跑好几个工人吗?** 能。每次派单都有自己的频道。并行任务请用单独的 `git worktree`(`-C`),避免改同一批文件。

**Codex 知道有这套对讲机吗?** 知道——派单脚本会在 prompt 前面加几行说明:
被拦下、理由里带"主线口信"的命令是消息,以及怎么用 `ask` / `say`。

**既然是双向的,为什么叫 `claude2codex`?** 因为这是你的出发方向:Claude 把活派给 Codex。
对讲机是派出去之后让对话继续下去的方式。

## 许可证

[MIT](LICENSE) © Napoleoncqf
