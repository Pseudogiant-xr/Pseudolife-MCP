"""Prove container ownership migration and privilege dropping on real Linux.

Run with a locally available Python daemon image (no pulls or builds):
    python tests/container_user_smoke.py --image <daemon-image>
Only uniquely named disposable volumes are mounted. No daemon is deployed.
"""

import argparse
import json
import subprocess
import uuid
from pathlib import Path

import yaml


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    service = yaml.safe_load((repo / "ops/docker-compose.yml").read_text(encoding="utf-8"))[
        "services"]["pseudolife-daemon"]

    def docker(*argv, success=True):
        result = subprocess.run(["docker", *argv], capture_output=True, text=True, timeout=90)
        if success and result.returncode:
            raise RuntimeError(f"docker exited {result.returncode}: {result.stderr}")
        return result

    # Require the image to be cached: this proof never downloads a runtime.
    docker("image", "inspect", "--format", "{{.Id}}", args.image)
    prefix = f"pseudolife-user-proof-{uuid.uuid4().hex}"
    volumes = []
    try:
        for name in [prefix + "-state", prefix + "-outside"]:
            docker("volume", "create", name)
            volumes.append(name)
        common = ["run", "--rm", "--network", "none", "--memory", "256m", "--cpus", "1",
                  "--mount", f"type=volume,src={volumes[0]},dst=/data",
                  "--mount", f"type=volume,src={volumes[1]},dst=/outside",
                  "--mount", f"type=bind,src={repo},dst=/proof,readonly",
                  "--workdir", "/proof", "-e", "PYTHONPATH=/proof", "--entrypoint", "python"]
        setup = """
import os
from pathlib import Path
Path('/data/nested').mkdir(mode=0o700)
p = Path('/data/nested/bank')
p.write_text('original')
p.chmod(0o600)
Path('/outside/private').write_text('outside')
Path('/outside/private').chmod(0o600)
Path('/data/link').symlink_to('/outside', target_is_directory=True)
Path('/data/custom').touch()
os.chown('/data/custom', 1234, 1234)
"""
        docker(*common, args.image, "-c", setup)
        hardened = []
        for value in service["security_opt"]:
            hardened += ["--security-opt", value]
        for value in service["cap_drop"]:
            hardened += ["--cap-drop", value]
        for value in service["cap_add"]:
            hardened += ["--cap-add", value]
        probe = """
import json, os, sys
from pathlib import Path
assert os.getuid() == os.geteuid() == 10001
assert os.getgid() == os.getegid() == 10001 and os.getgroups() == []
assert os.getpid() == 1
status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines())
assert int(status['CapEff'], 16) == int(status['CapPrm'], 16) == 0
assert status['NoNewPrivs'].strip() == '1'
assert Path('/data/nested/bank').read_text() == sys.argv[1]
Path('/data/nested/bank').write_text('updated')
assert Path('/data/custom').stat().st_uid == 1234
assert Path('/outside/private').stat().st_uid == 0
try:
    Path('/outside/private').read_text()
except PermissionError:
    pass
else:
    raise AssertionError('outside root-only file became readable')
model_files = list(Path('/opt/hf/hub').glob('models--*/snapshots/*/config.json'))
assert model_files, 'use a daemon image with the baked embedding models'
for model in model_files:
    json.loads(model.read_text())
print(json.dumps({'uid': os.getuid(), 'gid': os.getgid(), 'pid': os.getpid(),
                  'effective_capabilities': 0, 'permitted_capabilities': 0,
                  'no_new_privileges': True, 'baked_models_readable': len(model_files)}))
"""
        for expected in ["original", "updated"]:
            result = docker(*common, *hardened, args.image,
                            "-m", "pseudolife_memory.container_entrypoint", "python", "-c", probe, expected)
            print(result.stdout.strip(), flush=True)
        readonly = [part.replace("dst=/data", "dst=/data,readonly") for part in common]
        # Force a migration failure rather than exec on a read-only volume:
        # a fully migrated volume needs no chown, so restore root ownership first.
        docker(*common, args.image, "-c", "import os; os.chown('/data/nested/bank', 0, 0)")
        result = docker(*readonly, *hardened, args.image,
                        "-m", "pseudolife_memory.container_entrypoint", "python", "-c", "print('UNSAFE')",
                        success=False)
        assert result.returncode != 0 and "startup refused" in result.stderr
        assert "UNSAFE" not in result.stdout
        print("read-only migration refused before exec", flush=True)
    finally:
        for name in reversed(volumes):
            docker("volume", "rm", name)
        print(json.dumps({"disposable_volumes_removed": len(volumes)}), flush=True)


if __name__ == "__main__":
    main()
