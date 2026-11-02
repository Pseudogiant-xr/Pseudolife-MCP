"""Batch launchers must preserve the system role, newlines and prompt bytes."""
import importlib.util
import os
import shutil
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("claude_prompt_fixture", ROOT / "evals/claude_shim.py")
shim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shim)


class BatchPrompt(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "real Windows batch launcher")
    def test_real_batch_launcher_preserves_multiline_prompt(self):
        with tempfile.TemporaryDirectory(prefix="shim prompt with spaces ") as tmp:
            script = Path(tmp) / "stub.py"
            script.write_text('import json,sys\nfrom pathlib import Path\n'
                              'a=sys.argv\n'
                              'text=(Path(a[a.index("--system-prompt-file")+1]).read_text(encoding="utf-8") '
                              'if "--system-prompt-file" in a else a[a.index("--system-prompt")+1])\n'
                              'print(json.dumps({"result":text}))\n')
            batch = Path(tmp) / "stub.cmd"
            batch.write_text(f'@"{sys.executable}" "{script}" %*\n')
            cli = shim.ClaudeCli(batch, "fixture-model", 30)
            system = 'first\nsecond 雪\nlast'
            self.assertEqual(cli.chat(system, "user"), system)

    def test_batch_system_uses_file_and_preserves_user_stdin(self):
        for suffix in (".cmd", ".BAT"):
            for system in ('first\nsecond "quoted" & %PATH% 雪\r\nlast', "x" * 24001):
                with self.subTest(suffix=suffix, length=len(system)):
                    cli = shim.ClaudeCli(Path("fixture" + suffix), "fixture-model", 5)
                    paths = []
                    def run(argv, stdin):
                        self.assertNotIn("--system-prompt", argv)
                        path = Path(argv[argv.index("--system-prompt-file") + 1])
                        paths.append(path)
                        self.assertEqual(path.read_bytes(), system.encode("utf-8"))
                        self.assertEqual(stdin, b"user input")
                        return 0, b'{"result":"OK"}', b""
                    with patch.object(shim.sys, "platform", "win32"), patch.object(cli, "_run", run):
                        self.assertEqual(cli.chat(system, "user input"), "OK")
                    self.assertEqual(len(paths), 1)
                    self.assertFalse(paths[0].exists())
                    self.assertFalse(paths[0].parent.exists())

    def test_failure_removes_prompt_file(self):
        cli = shim.ClaudeCli(Path("fixture.cmd"), "fixture-model", 5)
        paths = []
        def fail(argv, stdin):
            paths.append(Path(argv[argv.index("--system-prompt-file") + 1]))
            raise TimeoutError("fixture timeout")
        with patch.object(shim.sys, "platform", "win32"), patch.object(cli, "_run", fail):
            with self.assertRaisesRegex(TimeoutError, "fixture timeout"):
                cli.chat("first\nsecond", "user")
        self.assertFalse(paths[0].exists())

    def test_locked_prompt_cleanup_preserves_success_and_failure(self):
        unlink = os.unlink
        for fails in (False, True):
            with self.subTest(fails=fails):
                cli = shim.ClaudeCli(Path("fixture.cmd"), "fixture-model", 5)
                paths = []
                def run(argv, stdin):
                    paths.append(Path(argv[argv.index("--system-prompt-file") + 1]))
                    if fails:
                        raise TimeoutError("fixture timeout")
                    return 0, b'{"result":"OK"}', b""
                def locked(path, *args, **kwargs):
                    if Path(path).name == "system.txt":
                        raise PermissionError("fixture file is held open")
                    return unlink(path, *args, **kwargs)
                try:
                    with patch.object(shim.sys, "platform", "win32"), patch.object(cli, "_run", run), patch.object(os, "unlink", locked):
                        if fails:
                            with self.assertRaisesRegex(TimeoutError, "fixture timeout"):
                                cli.chat("first\nsecond", "user")
                        else:
                            self.assertEqual(cli.chat("first\nsecond", "user"), "OK")
                    self.assertTrue(paths[0].exists(), "fixture did not exercise failed cleanup")
                finally:
                    if paths:
                        shutil.rmtree(paths[0].parent, ignore_errors=True)

    def test_native_executable_keeps_existing_argv(self):
        cli = shim.ClaudeCli(Path("fixture.exe"), "fixture-model", 5)
        calls = []
        def run(argv, stdin):
            calls.append((argv, stdin))
            return 0, b'{"result":"OK"}', b""
        with patch.object(shim.sys, "platform", "win32"), patch.object(cli, "_run", run):
            cli.chat("first\nsecond", "user")
        argv, stdin = calls[0]
        self.assertEqual(argv[-2:], ["--system-prompt", "first\nsecond"])
        self.assertEqual(stdin, b"user")


if __name__ == "__main__":
    unittest.main()
