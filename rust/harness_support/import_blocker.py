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


def _evict(modules):
    """Detach child bindings from parents that stay cached."""
    bindings = []
    for name in modules:
        parent, _, child = name.rpartition(".")
        if parent not in modules and (package := sys.modules.get(parent)) is not None:
            attrs = vars(package)
            if child in attrs:
                bindings.append((attrs, child, attrs.pop(child)))
    for name in modules:
        del sys.modules[name]
    return bindings


@contextmanager
def cold_harness_imports():
    """Re-execute harness modules and their imports even when pytest cached them."""
    support = Path(__file__).resolve().parent
    harness = support.parent / "daemon" / "harness"

    def owned(name, module):
        if _is_product(name) or name in {
            "cli_harness.producers",
            "rust.daemon.harness",
            "rust.cli_harness.pg_create",
            "rust.cli_harness.producers",
            "rust.cli_harness.run_names",
        }:
            return True
        filename = getattr(module, "__file__", None)
        if filename is None:
            return False
        path = Path(filename).resolve()
        return (path.is_relative_to(harness) or path.is_relative_to(support)) and not path.name.startswith("test_")

    saved = {name: module for name, module in sys.modules.copy().items() if owned(name, module)}
    bindings = _evict(saved)
    with patch.object(sys, "meta_path", [_BlockPackage(), *sys.meta_path]), \
            patch.object(sys, "path", [str(harness), str(support.parents[1]), *sys.path]):
        try:
            yield
        finally:
            _evict({name for name, module in sys.modules.copy().items() if owned(name, module)})
            sys.modules.update(saved)
            for attrs, child, original in bindings:
                attrs[child] = original
