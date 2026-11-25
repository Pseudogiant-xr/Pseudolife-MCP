"""The real user PATH guard of the whole-installer tests, free of the frozen
Python package.

A fixture home does not contain the Windows registry, so a whole-installer
test wraps its run in ``user_path_unchanged``: on Windows it fails when the
real user PATH (HKCU\\Environment Path) changed during the block, and takes
back only the test's own entries. Its tests are in
tests/test_installer_existing_upgrade_native.py.
"""
from __future__ import annotations

import contextlib
import os


REG_EXPAND_SZ = 2   # winreg.REG_EXPAND_SZ, named here so the helpers run off Windows


def _inside(entry: str, home: str, *, expand_vars: bool = True) -> bool:
    """Whether a user PATH entry names ``home`` or a path under it, in
    Windows terms: case-insensitive, either separator, surrounding quotes
    dropped, and ``%VAR%`` expanded only when ``expand_vars`` (a REG_EXPAND_SZ
    value; Windows expands nothing in a REG_SZ one, and never ``$VAR``)."""
    import ntpath
    import re

    def expand(path: str) -> str:
        path = path.strip()
        if len(path) >= 2 and path[0] == path[-1] == '"':
            path = path[1:-1]
        if not expand_vars:
            return path
        return re.sub(r"%([^%]+)%", lambda match: os.environ.get(match[1], match[0]), path)

    def norm(path: str) -> str:
        return ntpath.normcase(ntpath.normpath(path))

    if not entry.strip():
        return False
    entry = expand(entry)
    # A %VAR% that expands to several entries is not one entry inside home.
    # Checked before normpath, which can fold "home\bin;C:\.." back to home.
    if ";" in entry:
        return False
    entry, home = norm(entry), norm(expand(home))
    return entry == home or entry.startswith(home.rstrip("\\") + "\\")


def _path_entries(value) -> list[str]:
    """A (data, type) user PATH value's entries; an absent or empty value has
    none (ensure_user_path writes just its directory over an empty one)."""
    return [] if value is None or str(value[0]) == "" else str(value[0]).split(";")


def _leaked_entries(before, value, home: str) -> list[str] | None:
    """The entries ``value`` adds to ``before`` when that is all it does: the
    old entries all kept, in their order, and every added one inside ``home``
    (the leak's own signature). None for any other difference, an equal
    value included. Values are (data, type) or None."""
    if value is None or value == before:
        return None
    if before is not None and value[1] != before[1]:
        return None
    old, added, kept = _path_entries(before), [], 0
    expand_vars = value[1] == REG_EXPAND_SZ
    for entry in _path_entries(value):
        if kept < len(old) and entry == old[kept]:
            kept += 1
        elif _inside(entry, home, expand_vars=expand_vars):
            added.append(entry)
        else:
            return None
    if kept != len(old) or not added:
        return None
    return added


def _path_difference(before, value) -> str:
    """What differs between two user PATH values, for a failure message: the
    entries added and removed, and whether the entries both keep changed
    order or the value's registry type changed."""
    from collections import Counter

    old, new = _path_entries(before), _path_entries(value)
    # Added and removed as counts; the entries both have, in their new order,
    # are reordered only if they are not a subsequence of the old entries.
    pool, kept, added = Counter(old), [], []
    for entry in new:
        if pool[entry]:
            pool[entry] -= 1
            kept.append(entry)
        else:
            added.append(entry)
    removed = list((Counter(old) - Counter(kept)).elements())
    remaining = iter(old)
    in_order = all(entry in remaining for entry in kept)
    parts = [f"added {added!r}", f"removed {removed!r}"]
    if not in_order:
        parts.append("the entries both values have are in a different order")
    if before is None and value is not None:
        parts.append("the value was created")
    elif before is not None and value is None:
        parts.append("the value was deleted")
    elif before is not None and value[1] != before[1]:
        parts.append(f"the registry type changed from {before[1]} to {value[1]}")
    return ", ".join(parts)


@contextlib.contextmanager
def user_path_unchanged(fixture_home, broadcast=None):
    """On Windows, fail when the real user PATH (HKCU\\Environment Path)
    changed during the block; elsewhere a no-op. A whole-installer test wraps
    its run in it, because a fixture home does not contain the registry.

    It changes the value back only on the leak's own signature: the new value
    is the old one plus entries inside ``fixture_home``, none removed, the type
    kept. It rereads the value just before writing and writes the old one back
    only if the reread still has that signature; then it announces the change
    (WM_SETTINGCHANGE, as the leaking write did) and fails. Any other
    difference may be someone else's legitimate change on a shared machine
    (another session, a real installer run): nothing is written, and the test
    fails naming the entries added and removed, so a person can look."""
    if os.name != "nt":
        yield
        return
    import winreg

    if broadcast is None:
        broadcast = _broadcast_environment_change
    home = str(fixture_home)

    def read():
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            try:
                return winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                return None

    def not_ours(value):
        return AssertionError("the real user PATH changed during the test in a way this test did not "
                              f"cause (nothing was changed back): {_path_difference(before, value)}")

    before = read()
    try:
        yield
    finally:
        after = read()
        if after != before:
            if _leaked_entries(before, after, home) is None:
                raise not_ours(after)
            current = read()   # the value now: another writer may have moved it since
            leaked = _leaked_entries(before, current, home)
            if leaked is None:
                raise not_ours(current)
            # current is exactly before plus the test's own entries, so the
            # old value is what removing them leaves, byte for byte.
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
                if before is None:
                    winreg.DeleteValue(key, "Path")
                else:
                    winreg.SetValueEx(key, "Path", 0, before[1], before[0])
            broadcast()
            raise AssertionError(f"the test put {leaked!r} on the real user PATH; removed them again")


def _broadcast_environment_change() -> None:
    """Tell running programs (Explorer, and so every console opened after)
    that the user environment changed: ``WM_SETTINGCHANGE`` with
    ``"Environment"``, as the System Properties dialog sends it.

    The frozen Python client's own announcement (its runtimes module's
    ``_broadcast_environment_change``), copied so the guard needs no package."""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPCWSTR,
                                               wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
        result = ctypes.c_size_t()
        # HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG, 5 s per window
        user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(result))
    except Exception:  # noqa: BLE001 - a missed broadcast only delays the change to the next logon
        pass
