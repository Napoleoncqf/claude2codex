# claude2codex

**English** | [简体中文](README.zh-CN.md)

Let [Claude Code](https://claude.com/claude-code) hand work to the [Codex CLI](https://github.com/openai/codex)
— and keep **talking to Codex while it works**.

It is a Claude Code *skill* (installed folder name: `dispatch-codex`) made of three small scripts and no
background service.

---

## Table of contents
1. [What problem does this solve?](#1-what-problem-does-this-solve)
2. [How it works (in plain words)](#2-how-it-works-in-plain-words)
3. [Before you start](#3-before-you-start)
4. [Install, step by step](#4-install-step-by-step)
5. [Your first dispatch (a worked example)](#5-your-first-dispatch-a-worked-example)
6. [Command cheat sheet](#6-command-cheat-sheet)
7. [Troubleshooting](#7-troubleshooting)
8. [Safety and limits](#8-safety-and-limits)
9. [Uninstall](#9-uninstall)
10. [Related projects](#10-related-projects)
11. [FAQ](#11-faq)

---

## 1. What problem does this solve?

Claude Code is good at planning and reviewing. Codex is good at grinding through a well-defined job and
running commands. A common setup: **Claude plans, Codex does the work, Claude reviews the result.**

Handing a job to Codex is easy: `codex exec "do the thing"` runs it *headless* (no interactive screen).
The catch is that a headless worker **cannot hear you**:

- You realise halfway through "oh wait, use approach B" — too late, it is already running.
- The worker hits a fork in the road ("A or B?") and has to **guess**, because nobody is there to ask.

`claude2codex` adds the missing channel:

| You want to… | Command | What happens |
|---|---|---|
| Tell the worker something mid-run | `send` | The note reaches it before its next command |
| Make the worker stop and answer you | `send --stop` | Every command is blocked until it replies |
| Let the worker ask *you* a question | worker runs `ask` | It pauses, you `answer`, it continues in the same run |
| Get notified when the worker speaks | `wait` / inbox hook | Claude is woken up, even if it was idle |

## 2. How it works (in plain words)

Two tiny facts make this possible:

1. **Codex hooks.** Codex can run your script *before every command the worker executes* (a `PreToolUse` hook).
   If that script says "deny", the command is blocked and the denial reason is shown to the model.
   We use the denial reason as a **delivery truck for your note**. Nothing is wrong; the command is just
   re-run after the worker reads the note. When no note is waiting, the hook prints `{}` and costs zero tokens.
2. **Files as mailboxes.** Each dispatch opens a *channel*, which is just a folder
   `<project>/.harness-tmp/codex-msg/<channel>/` with an `inbox.jsonl` (you → worker) and an
   `outbox.jsonl` (worker → you). No server, no database, nothing to keep running.

```
        you / Claude Code                                   Codex worker (headless)
   ┌────────────────────────┐                         ┌──────────────────────────────┐
   │ codex_msg.py send  ────┼──▶ inbox.jsonl ──hook──▶│ next command is blocked once │
   │                        │                         │ and carries your note        │
   │ codex_msg.py wait  ◀───┼─── outbox.jsonl ◀───────┼─ codex_msg.py ask "A or B?"  │
   │ codex_msg.py answer ───┼──▶ (answer field) ─────▶│ ...unblocks and continues    │
   └────────────────────────┘                         └──────────────────────────────┘
```

Waking Claude up has two layers, because Claude might be idle *or* busy:

- **`wait`** — you run it in the background. It exits as soon as the worker speaks, and Claude Code
  notifies you when a background task exits. This wakes an *idle* Claude.
- **Inbox hook** (optional) — a Claude Code hook that, before each tool call, quietly slips any unread worker
  questions into Claude's context. This reaches a *busy* Claude.

## 3. Before you start

You need:

| Requirement | How to check | If missing |
|---|---|---|
| **Claude Code** | `claude --version` | https://claude.com/claude-code |
| **Codex CLI** | `codex --version` | `npm i -g @openai/codex` (needs [Node.js](https://nodejs.org)) |
| **A Codex login** | `codex login status` | `codex login` (ChatGPT account or API key) |
| **Python 3.9+** | `python3 --version` | https://www.python.org |
| **bash and git** | `bash --version`, `git --version` | macOS/Linux have them; on Windows use Git for Windows |

Your project must be a **git repository** (the scripts find the project root by looking for `.git`).

Tested on macOS with Codex CLI 0.160.0 (ChatGPT login) and Python 3.14. Windows-specific code paths exist
but are **untested**.

## 4. Install, step by step

**Step 1 — Put the skill where Claude Code looks for skills.**
The folder **must** be named `dispatch-codex`:

```bash
git clone https://github.com/Napoleoncqf/claude2codex.git
mkdir -p ~/.claude/skills
cp -r claude2codex ~/.claude/skills/dispatch-codex
```

**Step 2 — (Optional) pin a private copy of Codex** inside the skill folder, so updates to your global Codex
never break dispatching. Skip this to use the `codex` on your `PATH`.

```bash
npm i --prefix ~/.claude/skills/dispatch-codex/codex-cli @openai/codex
```

You can also point at any Codex binary with `export CODEX_TASK_BIN=/path/to/codex`.

**Step 3 — Ignore the scratch folder in every project you dispatch from.**
Logs, briefs and channels live in `.harness-tmp/`:

```bash
echo ".harness-tmp/" >> .gitignore
```

**Step 4 — (Recommended) install the Claude-side inbox hook.**
Open `~/.claude/settings.json` and add this (merge it into any existing `"hooks"` you already have):

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

This hook is *fail-open* (if anything goes wrong it stays silent and never blocks Claude) and does
nothing in projects that never used the intercom. Without it everything still works; you just rely on
`wait` instead of being interrupted mid-task.
On Windows use the full path to your Python and the script.

**Step 5 — Restart Claude Code** so it picks up the new skill (and the hook).

You do **not** need to install anything on the Codex side: every dispatch writes the Codex hook into
`<workdir>/.codex/hooks.json` for you (an existing file is backed up as `hooks.json.pre-codex-msg`).

## 5. Your first dispatch (a worked example)

The easy way: just ask Claude in plain language — *"Send this refactor to Codex"* — and the skill takes over.
Below is what happens underneath, so you know what you are looking at.

**A. Write a short brief** (what to do, which files, how to know it is done):

```markdown
# brief-demo.md
Goal: add input validation to `signup()` in src/auth.py.
Only touch: src/auth.py, tests/test_auth.py.
Done when: `pytest tests/test_auth.py` passes.
```

**B. Dispatch it** (in the background, because it may take minutes):

```bash
S=~/.claude/skills/dispatch-codex/scripts
bash $S/codex_task.sh -f brief-demo.md --tag demo
```

The first lines it prints tell you the channel name:

```
CHANNEL=demo-1002-092405  ...
WAKEUP=demo-1002-092405   ...
```

**C. Talk to the worker while it runs:**

```bash
M="python3 $S/codex_msg.py"
$M send demo-1002-092405 "Also reject empty usernames"   # arrives before its next command
$M wait demo-1002-092405                                  # blocks until the worker says something
```

Suppose the worker asks "Should validation errors raise or return None?". `wait` exits with code `0` and
prints the question, then:

```bash
$M answer demo-1002-092405 "Raise ValueError"
```

The worker receives the answer within about 2 seconds and carries on — in the **same run**, no restart.

**D. When it finishes**, the final answer is printed and the last line shows a session id:

```
SESSION=01a0fd6e-... LOG=.harness-tmp/codex/demo-....log EXIT=0
```

**E. Review before trusting it.** Worker output is an *unreviewed draft*: run `git diff` and your tests.

To follow up on the same session (the model must be passed again):

```bash
bash $S/codex_task.sh --resume <session-id> -m <model> -p "Two tests fail: <paste>. Fix them."
```

## 6. Command cheat sheet

**Dispatch** — `codex_task.sh`

| Flag | Meaning |
|---|---|
| `-f brief.md` / `-p "text"` | Task from a file (preferred) or inline |
| `--tag NAME` | Prefix for log files and the channel name |
| `--ro` | Read-only sandbox (for reviews). The worker can't `ask` in this mode — one-way only |
| `--net` | Allow network inside the sandbox (off by default) |
| `--nosearch` | Turn off Codex's built-in web search (on by default) |
| `--full` | No sandbox at all. Only if you really mean it |
| `--add-dir DIR` | One more writable directory |
| `-i IMG` | Attach an image (repeatable) |
| `-C DIR` | Work in another directory, e.g. a separate `git worktree` for parallel jobs |
| `-m MODEL`, `-e EFFORT` | Model and reasoning effort (`minimal`…`max`, default `xhigh`) |
| `--resume ID` | Continue a session (explicit id only; `--last` is refused on purpose) |
| `--no-channel` | Don't open an intercom channel for this run |

Full list: `bash codex_task.sh --help`.

**Intercom** — `codex_msg.py`

| Command | Who uses it | Meaning |
|---|---|---|
| `ls` | you | List channels, unread notes, unanswered questions |
| `send CH "note"` | you | Deliver a note before the worker's next command |
| `send CH --stop "note"` | you | Same, and block the worker until it replies |
| `poll [CH]` | you | Show what the worker asked or reported |
| `answer CH "text"` | you | Answer the latest open question |
| `wait CH [--timeout S]` | you | Block until the worker speaks or finishes |
| `ask CH "question"` | worker | Ask and pause (default 15 min) until answered |
| `say CH "text"` | worker | Report progress, don't wait |

`wait` exit codes: `0` worker is waiting for your answer · `10` report only · `20` worker finished ·
`30` timed out · `40` channel not found. **Run `wait` on its own, not piped**, or you lose the exit code.

## 7. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `codex: command not found` | Install it (`npm i -g @openai/codex`) or set `CODEX_TASK_BIN`. A leftover Homebrew link to a removed cask gives the same message |
| `need python3` | Install Python 3 |
| Worker log shows `ERROR ... Command blocked by PreToolUse hook` | **Normal.** That is the note being delivered. The worker reads it and re-runs the command |
| Nothing happens when I `send` | The note is delivered before the worker's *next command*. If it is inside one long command (e.g. `sleep 600`), it waits. Check `ls` for the channel |
| Worker never receives anything | Make sure you did not use `--no-channel`, and that `<workdir>/.codex/hooks.json` exists |
| I'm not being interrupted when the worker asks | You skipped Step 4, or didn't restart Claude Code. `wait` still works |
| `--ro` worker can't ask | By design: a read-only sandbox can't write the outbox. Use the default mode if you need questions |
| Run died with "at capacity" | The script retries automatically (3 times by default, `CODEX_TASK_CAPACITY_RETRIES`) |

Diagnostics: `.harness-tmp/codex-msg/hook-trace.log` records every time the Codex hook fired (metadata
only, no command text).

## 8. Safety and limits

- **The worker is sandboxed by Codex.** Default `workspace-write`: it can edit files in the project but
  its shell has no network. `--ro` is read-only. `--full` removes protection — avoid it.
- **`--dangerously-bypass-hook-trust`** is added to dispatches that open a channel, so Codex runs the intercom
  hook without asking again each time its path is refreshed. It only covers *that* dispatch. Use
  `--no-channel` to avoid it.
- **Your own Codex sessions are never intercepted.** The Codex hook acts only in a process that carries the
  `CODEX_MSG_CH` variable set by the dispatcher, even when you work in the same repo.
- **Local and same-user only.** Channels are plain files; anyone who can write to your project folder can
  write a note. This is not a multi-user or networked tool.
- **Treat worker output as untrusted.** Read the diff, run the tests.
- Don't commit `.harness-tmp/`, `.codex/hooks.json` from dispatches, or any `auth.json`.
- Honest scope: notes are delivered only between commands; there is no way to interrupt a command that is
  already running.

## 9. Uninstall

```bash
# 1. For each project you dispatched from: remove the Codex-side hook and scratch files
python3 ~/.claude/skills/dispatch-codex/scripts/codex_msg.py uninstall <project-dir>
rm -rf <project-dir>/.harness-tmp

# 2. Remove the skill itself
rm -rf ~/.claude/skills/dispatch-codex

# 3. Delete the PreToolUse entry you added to ~/.claude/settings.json (Step 4)
```

## 10. Related projects

Delegating to Codex from Claude Code is a crowded space; this repo only tries to do one thing well.

- [OpenAI's official `codex-plugin-cc`](https://community.openai.com/t/introducing-codex-plugin-for-claude-code/1378186):
  `/codex:review`, `/codex:rescue` and friends. One-command install, great for review and one-shot rescue.
  It has no mid-run messaging to a headless worker that I could find.
- [`dpemmons/intercom`](https://pkg.go.dev/github.com/dpemmons/intercom): a broker plus MCP adapters for
  messaging between running Claude Code sessions and Codex app-server sessions. Heavier (a Go service) but
  also covers live interactive sessions; it does not target headless `codex exec` runs.
- Several smaller `codex-skill` repos that dispatch tasks to Codex (one-way).

**Where this one fits:** you want to keep using plain `codex exec` workers, with *no daemon, no MCP server
and no Go toolchain* — just three scripts and a folder of files — and still be able to steer them.
Use the official plugin if all you need is review and fire-and-forget.

## 11. FAQ

**Does it cost extra tokens?** Only when a note or question actually exists. Silent hooks print `{}`.

**Can several workers run at once?** Yes. Each dispatch gets its own channel. Give parallel jobs separate
`git worktree`s (`-C`) so they don't edit the same files.

**Does Codex "know" about the intercom?** Yes — the dispatcher prepends a few lines to the prompt explaining
that blocked commands carrying a "主线口信" (main-line note) are messages, and how to `ask`/`say`.
(The strings the worker sees are in Chinese; the models handle that fine. Edit `codex_task.sh` and the
two Python files if you want English.)

**Why call it `claude2codex` if it's two-way?** Because that's the direction you start from: Claude dispatches
to Codex. The intercom is how the conversation continues after that.

## License

[MIT](LICENSE) © Napoleoncqf
