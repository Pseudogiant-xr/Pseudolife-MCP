"""Search cleanup must enumerate only its own disposable namespace."""
from pathlib import Path
import os
import subprocess
import sys
import unittest


class SearchCleanupNamespace(unittest.TestCase):
    def test_owned_template_is_visible_to_cleanup(self):
        probe = '''
import w2d_search as search
pg = search.pg
owned = pg.name("pl_cf_w2d_template")
class Admin:
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, statement, parameters):
        return [(name,) for name in (owned, "pl_cf_w2d_foreign")
                if name.startswith(parameters[0])]
pg._admin = Admin
assert pg.existing() == [owned], (pg.PREFIX, pg.existing())
'''
        env = dict(os.environ, PL_HARNESS_SLICE="w1a")
        result = subprocess.run([sys.executable, "-c", probe],
                                cwd=Path(__file__).resolve().parent, env=env,
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cortex_cleanup_refuses_foreign_database_before_connect(self):
        probe = '''
import w2d_cortex_check as cortex
def forbidden():
    raise AssertionError("connected before ownership validation")
if hasattr(cortex, "pg"):
    cortex.pg._admin = forbidden
else:
    cortex._admin = forbidden
try:
    cortex.drop("pl_cf_w2d_cortex_foreign")
except ValueError:
    pass
else:
    raise AssertionError("foreign name admitted")
'''
        result = subprocess.run([sys.executable, "-c", probe],
                                cwd=Path(__file__).resolve().parent,
                                capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
