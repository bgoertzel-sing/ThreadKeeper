"""Stdlib tests for ThreadKeeper subagent sandbox hardening.

Run with either:
    python3 tests/test_subagent_sandbox.py
    python3 -m unittest tests.test_subagent_sandbox
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "src"))

import subagent  # noqa: E402


class SubagentSandboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tk-subagent-sandbox-"))
        self.root = self.tmp / "sandbox"
        self.root.mkdir()
        (self.root / "readme.txt").write_text("inside", encoding="utf-8")
        self.outside = self.tmp / "outside.txt"
        self.outside.write_text("outside", encoding="utf-8")
        self.policy = subagent.SubagentPolicy(str(self.root))

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_read_file_preserves_current_in_sandbox_behavior(self):
        self.assertEqual(subagent._tool_read_file("readme.txt", policy=self.policy), "inside")

    def test_write_and_append_are_root_scoped(self):
        self.assertEqual(subagent._tool_write_file("notes/new.txt", "hello", policy=self.policy), "WRITE-FILE-SUCCESS")
        self.assertEqual(subagent._tool_append_file("notes/new.txt", "world", policy=self.policy), "APPEND-FILE-SUCCESS")
        self.assertEqual((self.root / "notes" / "new.txt").read_text(encoding="utf-8"), "helloworld\n")

    def test_path_traversal_is_rejected(self):
        result = subagent._tool_read_file("../outside.txt", policy=self.policy)
        self.assertIn("escapes subagent sandbox", result)

    def test_absolute_path_escape_is_rejected(self):
        result = subagent._tool_write_file(str(self.outside), "nope", policy=self.policy)
        self.assertIn("escapes subagent sandbox", result)
        self.assertEqual(self.outside.read_text(encoding="utf-8"), "outside")

    @unittest.skipIf(not hasattr(os, "symlink"), "symlink unavailable on this platform")
    def test_symlink_escape_is_rejected(self):
        link = self.root / "link-out"
        try:
            link.symlink_to(self.outside)
        except OSError as exc:
            self.skipTest(f"symlink creation failed: {exc}")
        result = subagent._tool_read_file("link-out", policy=self.policy)
        self.assertIn("escapes subagent sandbox", result)

    def test_shell_disabled_by_default_even_if_called_directly(self):
        result = subagent._tool_shell("echo hello", policy=self.policy)
        self.assertIn("shell tool disabled", result)

    def test_run_tools_rejects_tools_outside_dispatch_subset(self):
        result = subagent.run_tools([("write-file", ["x.txt", "x"])], ["read-file"], policy=self.policy)
        self.assertIn("SKILL_REJECTED", result)
        self.assertFalse((self.root / "x.txt").exists())

    def test_shell_subset_requires_explicit_policy(self):
        cfg = {"sandbox_root": str(self.root)}
        policy = subagent.SubagentPolicy.from_config(cfg)
        self.assertFalse(policy.allow_shell)

        cfg["allow_shell"] = True
        policy = subagent.SubagentPolicy.from_config(cfg)
        self.assertTrue(policy.allow_shell)


if __name__ == "__main__":
    unittest.main()
