"""The daemon image must ship a CPU-only torch.

Found 2026-07-29 during the v0.11.0 deploy: the running container reported
``torch 2.13.0+cu130`` with the full CUDA stack installed (19 ``nvidia-*`` /
``cuda-*`` / ``triton`` distributions, ~4 GB) despite the daemon never
touching a GPU — ``CUDA_VISIBLE_DEVICES=-1`` is set in the image.

The failure was silent and non-obvious. ``ops/Dockerfile.daemon`` installs a
pinned CPU torch from PyTorch's CPU wheel index first, on the assumption that
the constrained install afterwards would see torch already satisfied. pip
*did* see it satisfied — and then backtracked anyway:

    Requirement already satisfied: torch>=2.1.0 ... (2.12.0+cpu)
    INFO: pip is looking at multiple versions of torch ...
    Collecting torch>=2.1.0 (from pseudolife-mcp==0.11.0)
      Downloading torch-2.13.0-cp312-...whl
    Collecting setuptools>=77.0.3 (from torch>=2.1.0->pseudolife-mcp==0.11.0)

The trigger was **not** a dependency raising the torch floor — nothing in the
graph requires torch > 2.12.0. It was a *ceiling* collision in the other
direction: ``torch 2.12.0`` requires ``setuptools<82``, and a Dependabot bump
(``17b97180``, 2026-07-24) moved the lockfile's pin to ``setuptools==83.0.0``.
Irreconcilable, so pip discarded the pinned CPU torch and walked forward until
it found a torch with no setuptools ceiling — 2.13.0, whose default PyPI build
on linux is the CUDA one. The bitter detail: the Dockerfile forces setuptools
back to 78.1.1 on the very next line, so the constraint that poisoned the
resolution never even survived into the image.

These guards encode the invariant that was previously only a comment.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from importlib import metadata
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

_REPO = Path(__file__).resolve().parents[1]
_LOCK = _REPO / "ops" / "requirements.lock.txt"
_DOCKERFILE = _REPO / "ops" / "Dockerfile.daemon"
_COMPOSE = _REPO / "ops" / "docker-compose.yml"

# Distribution-name prefixes that only ever arrive with a CUDA torch build.
_GPU_PREFIXES = ("nvidia-", "nvidia_", "cuda-", "cuda_", "triton")


def _lock_pins() -> dict[str, str]:
    """``{normalized name: version}`` for every pin in the lockfile."""
    pins: dict[str, str] = {}
    for raw in _LOCK.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "==" not in line:
            continue
        name, _, version = line.partition("==")
        pins[name.strip().lower().replace("_", "-")] = version.strip()
    return pins


# ── the lockfile pins torch, and pins the CPU build ───────────────────────


def test_lockfile_pins_a_cpu_only_torch() -> None:
    """torch used to be deliberately absent from the lock, on the theory that
    the earlier CPU install satisfied it. That left the single largest
    dependency in the image unpinned — so when pip did decide to re-resolve
    it, nothing held it to the CPU wheel and PyPI's CUDA build won."""
    pins = _lock_pins()
    assert "torch" in pins, (
        "ops/requirements.lock.txt does not pin torch. An unpinned torch is "
        "free to re-resolve to PyPI's CUDA build (~4 GB of nvidia-* wheels) "
        "the moment anything perturbs the resolution."
    )
    assert pins["torch"].endswith("+cpu"), (
        f"lockfile pins torch=={pins['torch']}, which is not a CPU build. "
        f"The daemon is a CPU-only service (the image sets "
        f"CUDA_VISIBLE_DEVICES=-1); the pin must carry the '+cpu' local "
        f"version so a rebuild cannot silently substitute the CUDA wheel."
    )


def test_lockfile_carries_no_cuda_distributions() -> None:
    """The direct symptom, asserted directly: if these ever appear in the
    lock, a CUDA torch has been captured into it."""
    offenders = sorted(
        name for name in _lock_pins() if name.startswith(_GPU_PREFIXES)
    )
    assert not offenders, (
        f"ops/requirements.lock.txt pins GPU-only distributions {offenders} — "
        f"the lock was regenerated from an environment carrying a CUDA torch."
    )


# ── the Dockerfile and the lock cannot drift apart ────────────────────────


def test_dockerfile_torch_pin_matches_the_lockfile() -> None:
    """Two files name the torch version; they must never disagree. If step 1
    installs one version and the constraint pins another, pip resolves the
    difference by *reinstalling* torch — which is exactly the failure mode
    this whole file exists to prevent."""
    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    match = re.search(r'"torch==([^"]+)"', dockerfile)
    assert match, (
        "ops/Dockerfile.daemon no longer installs a pinned torch; the image "
        "would resolve torch from PyPI (CUDA build on linux)."
    )
    docker_pin = match.group(1)
    lock_pin = _lock_pins()["torch"]
    # The Dockerfile pin may omit the '+cpu' local label (PEP 440 '==2.12.0'
    # matches '2.12.0+cpu'); compare on the release segment.
    assert Version(docker_pin).base_version == Version(lock_pin).base_version, (
        f"ops/Dockerfile.daemon installs torch=={docker_pin} but "
        f"ops/requirements.lock.txt pins torch=={lock_pin}. A constraint that "
        f"disagrees with the installed version forces pip to reinstall torch."
    )


def test_ci_pins_the_same_torch_as_the_image() -> None:
    """Three files name a torch version — the Dockerfile, the lockfile, and
    CI (which installs CPU torch first "mirroring the daemon image"). A CI
    that validates a different torch than the image ships is a test suite
    reporting green about a stack nobody runs."""
    ci = (_REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    match = re.search(r'"torch==([^"]+)"', ci)
    assert match, (
        ".github/workflows/ci.yml no longer pins torch; the runner would "
        "resolve it from PyPI and validate a different build than we ship."
    )
    lock_pin = _lock_pins()["torch"]
    assert Version(match.group(1)).base_version == Version(lock_pin).base_version, (
        f".github/workflows/ci.yml pins torch=={match.group(1)} but the image "
        f"ships torch=={lock_pin} — CI is validating a stack we do not ship."
    )


def test_dockerfile_installs_torch_from_the_cpu_wheel_index() -> None:
    """The CPU build exists only on PyTorch's own index — plain PyPI serves
    the CUDA build under the same name and version."""
    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    assert "https://download.pytorch.org/whl/cpu" in dockerfile, (
        "ops/Dockerfile.daemon no longer installs torch from the CPU wheel "
        "index; PyPI's linux torch wheel is the CUDA build."
    )


def test_source_changes_do_not_invalidate_dependencies_or_baked_models() -> None:
    """CSS/JS/Python edits must not trigger the multi-GB model downloads.

    Docker invalidates every layer after a changed ``COPY``.  Keep the pinned
    runtime dependency install and both network-bound model bakes ahead of the
    application-source copy, then install the local package without resolving
    dependencies again.  This makes ordinary code deploys rebuild only the
    small package layer.
    """
    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    lock_install = dockerfile.index(
        "subprocess.check_call"
    )
    qwen_bake = dockerfile.index(
        "SentenceTransformer('Qwen/Qwen3-Embedding-0.6B')"
    )
    minilm_bake = dockerfile.index(
        "SentenceTransformer('all-MiniLM-L6-v2')"
    )
    source_copy = dockerfile.index("COPY pseudolife_memory /app/pseudolife_memory")
    assert lock_install < qwen_bake < minilm_bake < source_copy, (
        "dependency/model layers must precede the source COPY or every code "
        "edit redownloads the embedding models"
    )
    assert 'pip install --no-deps "/app"' in dockerfile[source_copy:], (
        "the post-COPY app install must not resolve the already-pinned runtime "
        "dependencies again"
    )


def test_model_downloads_survive_interrupted_builds() -> None:
    """A slow/failed model fetch must resume from BuildKit's cache mount."""
    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    cache_mount = "--mount=type=cache,id=pseudolife-hf,target=/tmp/hf-cache"
    assert cache_mount + ",sharing=locked" in dockerfile
    assert "HF_HOME=/tmp/hf-cache" in dockerfile
    assert "cp -a /tmp/hf-cache/." not in dockerfile
    assert "models--Qwen--Qwen3-Embedding-0.6B /opt/hf/hub/" in dockerfile
    assert "models--sentence-transformers--all-MiniLM-L6-v2 /opt/hf/hub/" in dockerfile


def test_image_installs_declared_dependencies_with_constraints(monkeypatch):
    """Exercise the Docker dependency command without network or installing."""
    import builtins
    tomllib = pytest.importorskip("tomllib", reason="image command uses Python 3.12")
    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    command = re.search(r'python -c "(import subprocess,sys,tomllib;.*?)"',
                        dockerfile, re.S).group(1).replace("\\\n", "")
    calls = []
    monkeypatch.setattr(subprocess, "check_call", calls.append)
    real_open = builtins.open
    monkeypatch.setattr(builtins, "open", lambda path, *a, **kw:
                        real_open(_REPO / "pyproject.toml" if path ==
                                  "/app/pyproject.toml" else path, *a, **kw))
    exec(command, {})
    project = tomllib.loads((_REPO / "pyproject.toml").read_text())["project"]
    assert len(calls) == 1
    assert calls[0][3:6] == ["install", "-c", "/app/requirements.lock.txt"]
    assert calls[0][6:] == (project["dependencies"] +
                            project["optional-dependencies"]["onnx"] +
                            ["setuptools==83.0.0"])
    assert "pip check" in dockerfile
    assert "PIP_NO_CACHE_DIR=0" not in dockerfile
    assert "env -u PIP_NO_CACHE_DIR" in dockerfile


# ── the actual regression: a ceiling collision, not a floor ───────────────


def test_lockfile_respects_the_pinned_torchs_setuptools_ceiling() -> None:
    """THE 2026-07-29 REGRESSION, pinned.

    ``torch 2.12.0`` declares ``setuptools<82``. Dependabot moved the lock to
    ``setuptools==83.0.0``, and because a constraint file cannot be
    negotiated, pip resolved the conflict by throwing away the pinned torch
    rather than the setuptools pin — landing on the CUDA build.

    Any future setuptools bump that crosses the pinned torch's ceiling must
    fail here, loudly, instead of silently adding 4 GB of CUDA to the image.
    Bumping past the ceiling means bumping torch in the same change.
    """
    pins = _lock_pins()
    lock_torch, lock_setuptools = pins["torch"], pins.get("setuptools")
    if lock_setuptools is None:
        pytest.skip("lockfile does not pin setuptools")

    # Read the ceiling off the real torch metadata. Only meaningful when the
    # locally installed torch IS the pinned one, otherwise we would assert
    # against a different release's requirements.
    try:
        installed = metadata.version("torch")
    except metadata.PackageNotFoundError:  # pragma: no cover - env-dependent
        pytest.skip("torch is not installed in this environment")
    if Version(installed).base_version != Version(lock_torch).base_version:
        pytest.skip(
            f"installed torch {installed} is not the pinned {lock_torch}; "
            f"cannot read the pinned release's setuptools ceiling"
        )

    ceilings = [
        Requirement(r)
        for r in (metadata.requires("torch") or [])
        if Requirement(r).name.lower() == "setuptools"
    ]
    if not ceilings:
        pytest.skip(f"torch {installed} declares no setuptools requirement")

    for req in ceilings:
        assert req.specifier.contains(lock_setuptools, prereleases=True), (
            f"ops/requirements.lock.txt pins setuptools=={lock_setuptools}, "
            f"which violates torch {installed}'s own requirement "
            f"'{req}'. pip cannot satisfy both, and it resolves the conflict "
            f"by discarding the pinned CPU torch and re-resolving from PyPI "
            f"— which on linux means the CUDA build and ~4 GB of nvidia-* "
            f"wheels. Bump torch in the same change, or hold setuptools."
        )


# ── the built image itself (opt-in; needs docker + a built image) ──────────


def _daemon_image_tag() -> str:
    compose = _COMPOSE.read_text(encoding="utf-8")
    match = re.search(r"^\s*image:\s*(pseudolife-daemon:\S+)\s*$", compose, re.M)
    assert match, "no pseudolife-daemon image tag in ops/docker-compose.yml"
    return match.group(1)


@pytest.mark.slow
def test_built_image_ships_cpu_torch_and_no_cuda_packages() -> None:
    """The end-to-end assertion the static guards above stand in for.

    Skipped unless docker is available and the image has been built — the
    static guards are the ones that run everywhere. This is what to run after
    a rebuild, and it is the check that actually failed on 2026-07-29.
    """
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    tag = _daemon_image_tag()
    if subprocess.run(
        ["docker", "image", "inspect", tag],
        capture_output=True,
    ).returncode:
        pytest.skip(f"{tag} has not been built")

    probe = (
        "import torch, importlib.metadata as m;"
        "gpu=sorted(d.metadata['Name'] for d in m.distributions()"
        " if (d.metadata['Name'] or '').lower()"
        f".startswith({_GPU_PREFIXES!r}));"
        "print(torch.__version__);print(torch.version.cuda);print(len(gpu));"
        "print(','.join(gpu))"
    )
    out = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "python", tag, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    version, cuda, gpu_count, gpu_names = out[0], out[1], int(out[2]), out[3]

    assert version.endswith("+cpu"), (
        f"{tag} ships torch {version} (torch.version.cuda={cuda}) — expected "
        f"a '+cpu' build. The daemon runs with CUDA_VISIBLE_DEVICES=-1 and "
        f"has no GPU code path; the CUDA build is dead weight."
    )
    assert gpu_count == 0, (
        f"{tag} carries {gpu_count} GPU-only distributions ({gpu_names}) — "
        f"roughly 4 GB of CUDA runtime in a CPU-only service."
    )
