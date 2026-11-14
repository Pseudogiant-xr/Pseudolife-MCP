"""The lifecycle suite drives only the native shim (#1314)."""
import contextlib
from pathlib import Path
import unittest
from unittest.mock import patch

import run

NATIVE = Path("native-shim")


class Started(Exception):
    pass


class NativeLifecycle(unittest.TestCase):
    def test_the_first_shim_the_lifecycle_starts_is_native(self):
        started = []

        @contextlib.contextmanager
        def server(binary, *args, **kwargs):
            started.append(binary)
            raise Started
            yield

        with patch.object(run, "server", server), self.assertRaises(Started):
            run.lifecycle(NATIVE, Path("unused"))
        self.assertEqual(started, [NATIVE])

    def test_every_lifecycle_slice_runs_the_native_binary(self):
        with patch.object(run, "lifecycle_arm", return_value=3) as arm, \
                patch.object(run, "health_refresh", return_value=3) as refresh:
            self.assertEqual(run.lifecycle(NATIVE, Path("root")), 9)
        self.assertEqual([c.args for c in arm.call_args_list],
                         [(NATIVE, "claude", Path("root")), (NATIVE, "codex", Path("root"))])
        self.assertEqual([c.args for c in refresh.call_args_list], [(NATIVE, Path("root"))])

    def test_the_expected_timeout_body_is_the_bytes_both_shims_returned(self):
        # Captured from both arms of the lifecycle suite at 05d0ff81d, where
        # the Python and native bodies were compared and equal.
        cases = [
            (['<cli>', '-p', '--model', 'claude-sonnet-5', '--output-format', 'json', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--tools', ''],
             b'{"error": "Command \'[\'<cli>\', \'-p\', \'--model\', \'claude-sonnet-5\', \'--output-format\', \'json\', \'--strict-mcp-config\', \'--mcp-config\', \'{\\"mcpServers\\":{}}\', \'--tools\', \'\']\' timed out after 5.0 seconds"}'),
            (['<cli>', 'exec', '--model', 'gpt-5.6-terra', '--json', '--sandbox', 'read-only', '--ephemeral', '--ignore-user-config', '--skip-git-repo-check', '-c', 'features.hooks=false', '-c', 'web_search=disabled', '-c', 'features.shell_tool=false', '-'],
             b'{"error": "Command \'[\'<cli>\', \'exec\', \'--model\', \'gpt-5.6-terra\', \'--json\', \'--sandbox\', \'read-only\', \'--ephemeral\', \'--ignore-user-config\', \'--skip-git-repo-check\', \'-c\', \'features.hooks=false\', \'-c\', \'web_search=disabled\', \'-c\', \'features.shell_tool=false\', \'-\']\' timed out after 5.0 seconds"}'),
        ]
        for argv, body in cases:
            self.assertEqual(run.timeout_body(argv), body)


if __name__ == "__main__":
    unittest.main()
