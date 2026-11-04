"""Registered extractor launchers retain their runtime while stopped."""
import base64

import pytest

from pseudolife_memory import runtimes as rt


@pytest.mark.parametrize("windows", [False, True])
def test_stopped_autostart_runtime_is_pinned(tmp_path, monkeypatch, windows):
    layout = rt.Layout(tmp_path / "runtimes", tmp_path / "bin" / ("pseudolife-mcp.exe" if windows else "pseudolife-mcp"))
    python = layout.root / "000001" / ("Scripts/python.exe" if windows else "bin/python")
    for name in ("000001", "000002"):
        runtime = layout.root / name
        console = runtime / ("Scripts/pseudolife-mcp.exe" if windows else "bin/pseudolife-mcp")
        console.parent.mkdir(parents=True)
        console.write_text("", encoding="utf-8")
        (runtime / "runtime.json").write_text('{"version":"0.0.0"}', encoding="utf-8")
    unit_home = tmp_path / "config"
    unit = unit_home / "systemd/user/pseudolife-sonnet-shim.service"
    unit.parent.mkdir(parents=True)
    unit.write_text(f'ExecStart="{python}" "/checkout/ops/shim_autostart.py" run claude --foreground\n', encoding="utf-8")
    inner = f"$psi.FileName = '{str(python).replace(chr(39), chr(39)*2)}'\n$psi.Arguments = 'shim_autostart.py run claude'"
    encoded = base64.b64encode(inner.encode("utf-16-le")).decode("ascii")
    action = {"Execute": "powershell.exe", "Arguments": f"-NoProfile -EncodedCommand {encoded}"}
    monkeypatch.setenv("XDG_CONFIG_HOME", str(unit_home))
    reader = getattr(rt, "_autostart_interpreters", None)
    if reader is not None:
        monkeypatch.setattr(rt, "_autostart_interpreters", lambda: reader(windows=windows))
    # No task or live process is started; only the registration is read.
    monkeypatch.setattr(rt, "_scheduled_extractor_actions", lambda: [action], raising=False)
    result = rt.remove_unused(layout, processes=lambda: [])
    assert str(layout.root / "000001") in result["kept"]
    assert (layout.root / "000001").is_dir()


def test_a_service_running_another_interpreter_does_not_pin_a_runtime(tmp_path, monkeypatch):
    layout = rt.Layout(tmp_path / "runtimes", tmp_path / "bin/pseudolife-mcp")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(rt, "_scheduled_extractor_actions", lambda: [], raising=False)
    assert rt.pinned_runtimes(layout, []) == set()


@pytest.mark.parametrize("readable", [True, False])
def test_prune_removes_unregistered_runtime_but_preserves_uncertain_registrations(tmp_path, monkeypatch, readable):
    layout = rt.Layout(tmp_path / "runtimes", tmp_path / "bin/pseudolife-mcp")
    for name in ("000001", "000002"):
        runtime = layout.root / name
        (runtime / "bin").mkdir(parents=True)
        (runtime / "bin/pseudolife-mcp").write_text("", encoding="utf-8")
        (runtime / "runtime.json").write_text('{"version":"0.0.0"}', encoding="utf-8")
    monkeypatch.setattr(rt, "_autostart_interpreters", lambda: [str(tmp_path / "other/python")] if readable else None)
    result = rt.remove_unused(layout, processes=lambda: [])
    old = layout.root / "000001"
    assert old.exists() is not readable
    assert (layout.root / "000002").exists()
    if not readable:
        assert "no runtime was removed" in result["error"] and result["removed"] == []


@pytest.mark.parametrize("arguments", ["-EncodedCommand %%%", "-EncodedCommand eAA=", "-EncodedCommand"])
def test_an_unreadable_spawner_never_allows_pruning(monkeypatch, arguments):
    monkeypatch.setattr(rt, "_scheduled_extractor_actions", lambda: [{"Execute": "powershell.exe", "Arguments": arguments}])
    assert rt._autostart_interpreters(windows=True) is None
