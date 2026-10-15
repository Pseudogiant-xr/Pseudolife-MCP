"""Accepted production blobs shared by CI preflight and byte-pinned judges."""
import hashlib
from pathlib import Path
import subprocess


# These are Git blob IDs, so an unchanged file needs no re-pin when master
# moves. #766 accepted both cli.py versions: eb0c13e9's wait-mail evidence and
# 39e8d7ad's unrelated delegate-watch dispatch. Keep that historical acceptance.
ACCEPTED_BLOBS = {
    "pseudolife_memory/cli.py": (
        "80d778afd7fd79d0d1cd31aedf9be9a2bbbffa0f",
        "4226b8a3fe003ab05406309c44a57ec471cfbc55",
    ),
    "pseudolife_memory/wait_mail_cli.py": ("236636957e2f5affbe2eec21d9e125c35ffae5f2",),
}


def require_accepted(relative, observed):
    if observed not in ACCEPTED_BLOBS[relative]:
        raise ValueError(
            f"unaccepted Python oracle blob: {relative} ({observed}); "
            "review the dependent parity judges and update ACCEPTED_BLOBS in "
            "evals/rust_port/oracle_pins.py with the accepted Git blob in the same PR")


def require_pinned_head(root, head):
    """Check immutable candidate/merge bytes, independently of oracle selection."""
    raw = subprocess.check_output(
        ["git", "ls-tree", "-r", "-z", head, "--", *ACCEPTED_BLOBS], cwd=root, timeout=10)
    entries = {}
    for record in raw.split(b"\0"):
        if record:
            metadata, path = record.split(b"\t", 1)
            mode, kind, blob = metadata.decode("ascii").split()
            if mode in ("100644", "100755") and kind == "blob":
                entries[path.decode("utf-8")] = blob
    for relative in ACCEPTED_BLOBS:
        require_accepted(relative, entries.get(relative, "missing or non-regular file"))


def require_pinned_files(root):
    """Retain the judge's canonical-LF byte check, including dirty source."""
    for relative in ACCEPTED_BLOBS:
        raw = (Path(root) / relative).read_bytes().replace(b"\r\n", b"\n")
        blob = hashlib.sha1(b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw,
                            usedforsecurity=False).hexdigest()
        require_accepted(relative, blob)
