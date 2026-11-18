"""Cold imports for tests of the Rust harness's package independence."""
from contextlib import contextmanager
from importlib.abc import MetaPathFinder
from pathlib import Path
import sys
from unittest.mock import patch


def _is_product(name):
    return name == "pseudolife_memory" or name.startswith("pseudolife_memory.")


class _BlockPackage(MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if _is_product(fullname):
            raise AssertionError(f"unexpected product package import: {fullname}")


@contextmanager
def cold_harness_imports():
    """Re-execute harness modules and their imports even when pytest cached them."""
    support = Path(__file__).resolve().parent
    harness = support.parent / "daemon" / "harness"

    def owned(name, module):
        if _is_product(name):
            return True
        filename = getattr(module, "__file__", None)
        if filename is None:
            return False
        path = Path(filename).resolve()
        return (path.is_relative_to(harness) or path.is_relative_to(support)) and not path.name.startswith("test_")

    saved = {name: module for name, module in sys.modules.copy().items() if owned(name, module)}
    for name in saved:
        del sys.modules[name]
    with patch.object(sys, "meta_path", [_BlockPackage(), *sys.meta_path]), \
            patch.object(sys, "path", [str(harness), str(support.parents[1]), *sys.path]):
        try:
            yield
        finally:
            for name, module in sys.modules.copy().items():
                if owned(name, module):
                    del sys.modules[name]
            sys.modules.update(saved)
            rust = sys.modules.get("rust")
            if rust is not None:
                if "rust.harness_support" in saved:
                    rust.harness_support = saved["rust.harness_support"]
                else:
                    rust.__dict__.pop("harness_support", None)
