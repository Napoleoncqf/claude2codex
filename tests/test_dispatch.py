"""codex_task.sh 回归测试：用一个假的 codex（只记参数、写最终答复）跑真实的派单脚本。

运行：python3 -m unittest discover -s tests
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TASK = REPO / "scripts" / "codex_task.sh"
TOOL = REPO / "scripts" / "codex_msg.py"

FAKE_CODEX = '''#!/usr/bin/env python3
import os, pathlib, subprocess, sys
args = sys.argv[1:]
if os.environ.get("FAKE_SAY"):
    subprocess.run([sys.executable, os.environ["FAKE_MSG_TOOL"], "say",
                    os.environ["CODEX_MSG_CH"], os.environ["FAKE_SAY"]], check=True)
pathlib.Path(args[args.index("-o") + 1]).write_text("FINAL " + pathlib.Path.cwd().name + "\\n")
print("session id: fake-session")
'''


class Dispatch(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / ".git").mkdir()
        fake = self.root / "fake-codex"
        fake.write_text(FAKE_CODEX)
        fake.chmod(0o755)
        self.env = dict(os.environ, CODEX_TASK_BIN=str(fake), FAKE_MSG_TOOL=str(TOOL))

    def tearDown(self):
        self._tmp.cleanup()

    def test_help_prints_only_the_usage_block(self):
        out = subprocess.run(["bash", str(TASK), "--help"], capture_output=True, text=True).stdout
        self.assertIn("Usage:", out)
        self.assertNotIn("exit $rc", out, "--help 一路打到了脚本末尾")
        self.assertLess(len(out.splitlines()), 60)

    def test_note_left_at_close_reaches_the_main_line_in_full(self):
        env = dict(self.env, FAKE_SAY="收工前的报告正文：第三步没跑")
        r = subprocess.run(["bash", str(TASK), "-p", "work"], cwd=self.root, env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("收工前的报告正文：第三步没跑", r.stderr)

    def test_same_second_dispatches_get_separate_channels(self):
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        date = bin_dir / "date"                    # 把派单用的时间戳钉在同一秒
        date.write_text('#!/bin/sh\nif [ "$1" = "+%m%d-%H%M%S" ]; then echo 1018-120000; '
                        'else exec "' + shutil.which("date") + '" "$@"; fi\n')
        date.chmod(0o755)
        env = dict(self.env, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        procs = []
        for name in ("one", "two"):
            wd = self.root / name
            wd.mkdir()
            procs.append(subprocess.Popen(["bash", str(TASK), "-p", "work", "-C", str(wd)],
                                          cwd=self.root, env=env, text=True,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        outs = [p.communicate(timeout=30) for p in procs]
        channels = [re.search(r"CHANNEL=(\S+)", err).group(1) for _, err in outs]
        self.assertNotEqual(channels[0], channels[1])
        self.assertIn("FINAL one", outs[0][0])
        self.assertIn("FINAL two", outs[1][0])


class SkillExample(unittest.TestCase):
    """SKILL.md 里的对讲示例照抄就要能跑：Claude Code 在 macOS 上用 zsh，别处常是 bash。"""

    def test_intercom_example_passes_an_absolute_script_path(self):
        lines = (REPO / "SKILL.md").read_text(encoding="utf-8").splitlines()
        assign = next(l for l in lines if l.startswith("M="))
        call = next(l for l in lines if " send <频道>" in l and "$M" in l)
        call = call.replace("<频道>", "c").split("   #", 1)[0]
        script = 'python3() { printf "%s\\n" "$1"; }\n' + assign + "\n" + call
        for shell in ("bash", "zsh"):
            if not shutil.which(shell):
                continue
            with self.subTest(shell=shell):
                r = subprocess.run([shell, "-c", script], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertTrue(r.stdout.strip().startswith("/"), r.stdout)
                self.assertTrue(r.stdout.strip().endswith("codex_msg.py"), r.stdout)


if __name__ == "__main__":
    unittest.main()
