"""`send --stop` 回归测试：挂住期间别的命令都拦，但工人回话（ask / say）那条必须放行。

运行：python3 -m unittest discover -s tests
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parent.parent / "scripts" / "codex_msg.py"


class Hold(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / ".git").mkdir()
        self.run_tool("register", "c", str(self.root))

    def tearDown(self):
        self._tmp.cleanup()

    def run_tool(self, *args):
        return subprocess.run([sys.executable, str(TOOL), "--root", str(self.root), *args],
                              capture_output=True, text=True, encoding="utf-8")

    def hook(self, command):
        # payload 结构照 Codex 0.160.0 实测：tool_name = "Bash"，命令在 tool_input.command
        env = dict(os.environ, CODEX_MSG_CH="c")
        payload = json.dumps({"hook_event_name": "PreToolUse", "cwd": str(self.root),
                              "tool_name": "Bash", "tool_input": {"command": command}})
        out = subprocess.run([sys.executable, str(TOOL), "--root", str(self.root), "--hook"],
                             input=payload, capture_output=True, text=True, encoding="utf-8",
                             env=env, cwd=self.root)
        return json.loads(out.stdout).get("hookSpecificOutput", {}).get("permissionDecision")

    def test_held_worker_can_still_reply(self):
        self.run_tool("send", "c", "--stop", "先停")
        self.assertEqual(self.hook("ls"), "deny")                 # 送达口信，挂住
        self.assertEqual(self.hook("pytest -q"), "deny")          # 挂住期间别的命令照拦
        for verb in ("ask", "say"):
            reply = f'python3 "{TOOL}" {verb} c "收到，已停"'
            self.assertIsNone(self.hook(reply), f"挂住时 {verb} 被拦，工人回不了话")
        self.run_tool("say", "c", "收到，已停")                    # 回话真正执行后解挂
        self.assertIsNone(self.hook("ls"))

    def test_reply_lookalike_is_still_held(self):
        self.run_tool("send", "c", "--stop", "先停")
        self.hook("ls")
        self.assertEqual(self.hook("grep ask codex_msg.py.bak"), "deny")


if __name__ == "__main__":
    unittest.main()
