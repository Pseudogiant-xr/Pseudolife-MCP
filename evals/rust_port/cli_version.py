"""Installer-schema version cases with minimal, genuine runtime relocation."""
import json
import os
from pathlib import Path
import shutil
import sys
import tomllib


def cases():
    rows = [
        ("bare-version", ["version"], "bare", "commit"),
        ("bare-flag", ["--version"], "bare", "commit"),
        ("default-commit", ["version"], "default", "commit"),
        ("default-commit-flag-tail", ["--version", "ignored"], "default", "commit"),
        ("default-requirement", ["version"], "default", "requirement"),
        ("default-checkout-no-git", ["version"], "default", "checkout-no-git"),
        ("override-commit", ["version"], "override", "commit"),
        ("override-requirement", ["--version"], "override", "requirement"),
        ("half-root", ["version"], "half-root", "commit"),
        ("half-launcher", ["version"], "half-launcher", "commit"),
        ("wrong-platform-suffix", ["version"], "wrong-suffix", "commit"),
    ]
    return [{"id": "version-" + name, "mode": "version", "argv": argv, "stdin_b64": "",
             "environment_deltas": {}, "pre_files_b64": {}, "normalizations": [],
             "layout_kind": layout, "manifest_kind": marker}
            for name, argv, layout, marker in rows]


def seed_context(source_root):
    """Observe the selected installed interpreter; never override its identity."""
    runtime = Path(sys.prefix)
    scripts = runtime / ("Scripts" if os.name == "nt" else "bin")
    console = scripts / ("pseudolife-mcp.exe" if os.name == "nt" else "pseudolife-mcp")
    config = runtime / "pyvenv.cfg"
    if sys.prefix == sys.base_prefix or not console.is_file() or not config.is_file():
        raise RuntimeError("version preparation requires the genuine installed oracle venv")
    # The verified oracle can reuse dependency directories through a .pth file.
    # Relocated children receive these same locations explicitly, without copying
    # dependency trees or installing anything into the disposable runtime.
    dependencies = list(dict.fromkeys(str(Path(path).resolve()) for path in sys.path
                                     if Path(path).is_dir()
                                     and Path(path).name in ("site-packages", "dist-packages")))
    return {"runtime": runtime, "python": Path(sys.executable), "console": console,
            "config": config, "base_interpreter": str(sys._base_executable),
            "pythonpath": os.pathsep.join([str(source_root), *dependencies]),
            "version": tomllib.loads((source_root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]}


def make_prepare(source_root, source_pin):
    """Use the reviewed prepare callback with identical files in both arms."""
    source_root = Path(source_root).resolve()
    seed = seed_context(source_root)
    windows = os.name == "nt"
    scripts_name, python_name = ("Scripts", "python.exe") if windows else ("bin", "python")
    console_name = "pseudolife-mcp.exe" if windows else "pseudolife-mcp"
    native_name = "pseudolife-stdio.exe" if windows else "pseudolife-stdio"

    def manifest(kind, *, commit=source_pin):
        # Installer-format fixture date; capture wall times come from the runner.
        return {"version": seed["version"], "installed_at": "2000-01-01T00:00:00+0000",
                "source": "pseudolife-mcp==" + seed["version"] if kind == "requirement" else str(source_root),
                "source_commit": commit if kind == "commit" else None,
                "base_interpreter": seed["base_interpreter"]}

    def seed_files(runtime, marker, commands, *, native):
        scripts = runtime / scripts_name
        scripts.mkdir(parents=True, exist_ok=True)
        shutil.copy2(commands["oracle"][0], scripts / python_name)
        shutil.copy2(seed["console"], scripts / console_name)
        shutil.copy2(seed["config"], runtime / "pyvenv.cfg")
        if native:
            shutil.copy2(commands["candidate"][0], runtime / native_name)
        (runtime / "runtime.json").write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")

    def prepare(case, home, env, command, commands):
        if case.get("mode") != "version":
            return command
        if Path(commands["oracle"][0]).resolve() != seed["python"].resolve():
            raise ValueError("version preparation must use the verified oracle interpreter")
        home = Path(home)
        default = (Path(env["LOCALAPPDATA"]) if windows else
                   Path(env.get("XDG_DATA_HOME", str(home / ".local/share")))) / "pseudolife-mcp" / "runtimes"
        override = home / "override" / "runtimes"
        launcher = home / "override" / "bin" / console_name
        kind = case["layout_kind"]
        for key in ("PSEUDOLIFE_SHIM_RUNTIMES", "PSEUDOLIFE_SHIM_LAUNCHER"):
            env.pop(key, None)
        if kind in ("override", "half-root", "wrong-suffix"):
            env["PSEUDOLIFE_SHIM_RUNTIMES"] = str(override)
        if kind in ("override", "half-launcher", "wrong-suffix"):
            wrong = launcher.with_suffix("") if windows else Path(str(launcher) + ".exe")
            env["PSEUDOLIFE_SHIM_LAUNCHER"] = str(wrong if kind == "wrong-suffix" else launcher)
        env["PYTHONPATH"] = seed["pythonpath"]
        marker = manifest(case["manifest_kind"])
        if kind == "bare":
            runtime = home / "bare-python"
            seed_files(runtime, marker, commands, native=False)
            native = home / "bare-native" / native_name
            native.parent.mkdir(parents=True)
            shutil.copy2(commands["candidate"][0], native)
            seed_files(default / "000001", marker, commands, native=False)
        else:
            selected = override if kind == "override" else default
            runtime = selected / "000001"
            seed_files(runtime, marker, commands, native=True)
            # Own identity must survive the presence of a newer complete runtime.
            seed_files(selected / "000002", manifest("commit", commit="0" * 39 + "1"), commands, native=False)
            native = runtime / native_name
        copied = runtime / scripts_name / python_name if command == commands["oracle"] else native
        return [str(copied), *command[1:]]

    return prepare
