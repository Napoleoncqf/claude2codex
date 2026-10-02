# codex-intercom

A Claude Code skill (`dispatch-codex`) that dispatches work to the local **Codex CLI** as a headless subagent — plus a
**two-way intercom** so you can talk to the worker *while it runs*.

> Claude Code skill：把活派给本机 Codex CLI 当无头 subagent，并且**运行中可以双向对话**。

## Why this one

Delegating to Codex is well covered already (OpenAI's official
[codex-plugin-cc](https://community.openai.com/t/introducing-codex-plugin-for-claude-code/1378186),
and several `codex-skill` repos). What a `codex exec` worker can't do is *listen*: once it starts there
is no input channel, and the built-in `codex queue` doesn't reach `exec` sessions. This repo adds that:

| direction | what | mechanism |
|---|---|---|
| Claude → Codex | drop a note / stop the worker mid-run | Codex `PreToolUse` hook denies the next command and carries the note as the reason |
| Codex → Claude | worker asks a question and blocks for the answer | `codex_msg.py ask` polls an outbox file |
| wake Claude | notice the question even when idle or busy | background `wait` (idle) + Claude `PreToolUse` hook (busy) |

Zero tokens are spent when nobody is talking (hooks print `{}`).

## Requirements
- Codex CLI (`npm i -g @openai/codex`, then `codex login`), Python 3.9+, bash, git.
- macOS / Linux / Windows (git-bash). Verified end-to-end on macOS with `@openai/codex` 0.160.0
  (ChatGPT login): a note sent mid-run was delivered by the hook, the worker's `ask` woke `wait`
  (exit 0), and the answer came back in the same turn. Windows-specific branches are untested.

## Install
1. `cp -r . ~/.claude/skills/dispatch-codex` (the skill folder must be named `dispatch-codex`; hooks reference scripts by absolute path)
2. (optional) pin a local Codex: `npm i --prefix ~/.claude/skills/dispatch-codex/codex-cli @openai/codex`.
   Otherwise the `codex` on your `PATH` is used; override with `CODEX_TASK_BIN`.
3. Add `.harness-tmp/` to the `.gitignore` of every project you dispatch from.
4. **Claude-side inbox hook** — add to `~/.claude/settings.json` so worker questions are injected while Claude is busy:
   ```json
   {
     "hooks": {
       "PreToolUse": [
         { "matcher": "*",
           "hooks": [ { "type": "command",
             "command": "python3 /ABS/PATH/TO/.claude/skills/dispatch-codex/scripts/codex_inbox_hook.py" } ] }
       ]
     }
   }
   ```
   It is fail-open and prints nothing in projects that never used the intercom. Skip it and you still
   have `wait` (below); you just won't be interrupted mid-task.
   The Codex-side hook is written automatically into `<workdir>/.codex/hooks.json` on every dispatch
   (an existing file is backed up to `hooks.json.pre-codex-msg`; remove with `codex_msg.py uninstall <dir>`).

## Use
```bash
S=~/.claude/skills/dispatch-codex/scripts
bash $S/codex_task.sh -f brief.md --tag refactor          # stderr: CHANNEL=<id> SESSION=<id> ...
M="python3 $S/codex_msg.py"
$M send  <id> "prefer approach B"      # delivered before the worker's next command
$M send  <id> --stop "hold on, answer me first"
$M poll  <id>                          # what did the worker ask / report?
$M answer <id> "go with A"             # worker gets it within ~2s and continues
$M wait  <id>                          # run in background; exits when the worker speaks
```
`wait` exit codes: `0` worker is blocked waiting for an answer · `10` report only · `20` worker finished ·
`30` timeout · `40` channel not found. Run it bare (no pipe) or you lose the code.

See [SKILL.md](SKILL.md) for the flags and the rules the skill follows (never `resume --last`, review every diff, …).
Script comments are in Chinese.

## Safety notes
- The worker uses Codex's own sandbox (`workspace-write` by default; `--ro` read-only; `--full` is opt-in).
- Dispatch adds `--dangerously-bypass-hook-trust` so Codex runs the intercom hook without re-prompting
  after its path is refreshed. That only applies to dispatches that opened a channel; use `--no-channel` to avoid it.
- The Codex hook only fires for the process that has `CODEX_MSG_CH` set, so your own interactive Codex
  sessions in the same repo are never intercepted.
- Never commit `.harness-tmp/` or any `auth.json`.

## License
MIT
