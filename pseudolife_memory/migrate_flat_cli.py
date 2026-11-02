"""Opt-in lossless band collapse, fenced from the daemon and backed up first.

This is a data migration: entries and meta retain their existing schema. The
database commit precedes the atomic config replacement; the durable marker
lets a retry finish that replacement without another backup or row rewrite.
"""

from __future__ import annotations

import copy
import datetime
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
import tarfile
import uuid

import yaml

MARKER = "flat_band_migration_v1"
WRITER_LOCK = "pseudolife-bank-writer"
_PRESETS = {"flat", "continuum", "titans", "moneta", "yaad", "memora", "custom"}
_POLICIES = {"balanced", "recency_heavy", "surprise_heavy"}
_FINGERPRINT = "SELECT id, md5((to_jsonb(e)-'band')::text) FROM public.entries e ORDER BY id"
_HELP = ("usage: pseudolife-mcp migrate-flat --apply --data-dir PATH --external-recovery-verified\n\n"
         "Back up and flatten a stopped bank. Set PSEUDOLIFE_MCP_DATABASE_URL.\n"
         "Native execution also requires pseudolife-daemon on the bank host.\n"
         "--external-recovery-verified attests that you have rehearsed restoring\n"
         "this exact bank AND config with your own tooling; no automatic restore exists.\n")
_RECOVERY_REFUSAL = ("a paired restore for an explicit DSN is not available yet; nothing changed; "
                     "rehearse restoring this exact bank AND config with your own tooling, "
                     "then pass --external-recovery-verified")
_RECOVERY_WARNING = ("WARNING: external recovery acknowledged for this bank AND config; "
                     "use your rehearsed tooling to restore the backup pair; no automatic restore is available.")


class MigrationError(RuntimeError):
    """A refusal whose message contains no database credentials or YAML values."""


def _mapping(value, path):
    if not isinstance(value, dict):
        raise MigrationError(f"{path} must be a mapping; restore a valid config and retry migrate-flat --apply")
    return value


def flat_config(config: dict, entries: int) -> tuple[dict, int]:
    """Preserve custom capacity, including oversize banks, without running CMS."""
    _mapping(config, "config.yaml")
    memory = _mapping(config.get("memory", {}), "memory")
    miras = _mapping(memory.get("miras", {}), "memory.miras")
    preset = miras.get("preset", "flat")
    if not isinstance(preset, str) or preset not in _PRESETS:
        raise MigrationError("memory.miras.preset is unknown; restore a valid config and retry migrate-flat --apply")
    capacity = 5250
    if preset == "custom":
        bands = miras.get("bands")
        if not isinstance(bands, list) or not bands:
            raise MigrationError("memory.miras.bands must be a nonempty list for custom; restore a valid config and retry migrate-flat --apply")
        capacity = 0
        for band in bands:
            band = _mapping(band, "memory.miras.bands[]")
            size = band.get("max_entries", 5000)
            policy = band.get("retention_policy", "balanced")
            if type(size) is not int or size <= 0 or size > 2**63 - 1:
                raise MigrationError("memory.miras.bands[].max_entries must be a positive 64-bit integer; restore a valid config and retry migrate-flat --apply")
            if not isinstance(policy, str) or policy not in _POLICIES:
                raise MigrationError("memory.miras.bands[].retention_policy is unknown; restore a valid config and retry migrate-flat --apply")
            capacity += size
    capacity = max(capacity, entries, 5250)
    if capacity > 2**63 - 1:
        raise MigrationError("aggregate band capacity exceeds a 64-bit integer; nothing changed")
    result = copy.deepcopy(config)
    # Detach memory from any aliases elsewhere before replacing its layout.
    result["memory"] = copy.deepcopy(memory)
    result["memory"]["miras"] = {"preset": "flat"} if capacity == 5250 else {
        "preset": "custom", "bands": [{
            "name": "flat", "max_entries": capacity, "retention_policy": "balanced",
            "update_interval": 1000000000, "promotion_access_count": 1000000000,
            "promotion_surprise": 1.1,
        }]}
    return result, capacity


def _flat_layout(config, capacity):
    memory = config.get("memory", {}) if isinstance(config, dict) else None
    miras = memory.get("miras", {}) if isinstance(memory, dict) else None
    if not isinstance(miras, dict):
        return False
    if miras.get("preset", "flat") == "flat":
        return capacity <= 5250
    bands = miras.get("bands", [])
    return (miras.get("preset") == "custom" and isinstance(bands, list) and len(bands) == 1
            and isinstance(bands[0], dict)
            and bands[0].get("name") == "flat"
            and bands[0].get("retention_policy", "balanced") == "balanced"
            and type(bands[0].get("max_entries", 5000)) is int
            and bands[0].get("max_entries", 5000) >= capacity)


def _migration_config_path(data_dir):
    path = Path(data_dir).resolve() / "config.yaml"
    override = os.environ.get("PSEUDOLIFE_MCP_CONFIG")
    if override:
        try:
            matches = Path(override).resolve(strict=True) == path.resolve(strict=True)
        except OSError:
            matches = False
        if not matches:
            raise MigrationError("PSEUDOLIFE_MCP_CONFIG does not resolve to --data-dir/config.yaml; nothing changed; use a matching configuration path before retrying")
    return path


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def recovery_instructions(backup):
    return ("use your rehearsed external recovery tooling for this same bank AND config; bank dump "
            + shlex.quote(str(backup["dump"])) + "; state archive "
            + shlex.quote(str(backup["state"])) + "; no automatic restore is available")


def require_config_finalized(storage, path, bands):
    """Fence only an explicitly started migration, before hydration can evict."""
    marker = storage.get_meta(MARKER)
    if marker is None:
        return
    capacity = marker.get("capacity") if isinstance(marker, dict) else None
    if type(capacity) is not int or capacity < 5250:
        raise MigrationError("flat migration marker is invalid; keep the daemon stopped and restore the bank and config from the migration backup pair")
    if not (len(bands) == 1 and bands[0][0] == "flat" and bands[0][1] >= capacity and bands[0][2] == "balanced"):
        raise MigrationError("this bank was migrated but the loaded config is not the matching flat layout; restart with the migrated config or restore bank and config together: " + recovery_instructions(marker["backup"]))
    if marker.get("complete") is True:
        return
    try:
        matches = _hash(Path(path).read_bytes()) == marker["target_sha256"]
    except OSError:
        matches = False
    if not matches:
        raise MigrationError("flat migration config finalization is pending; keep the daemon stopped and run migrate-flat --apply; restore bank and config together if needed: " + recovery_instructions(marker["backup"]))


def _backup(data_dir):
    from pseudolife_memory.backup_cli import perform_backup
    from pseudolife_memory.update_cli import _dump_complete

    # Keep this recovery pair outside ordinary seven-day backup rotation.
    output = data_dir / "backups" / ("migrate-flat-" + uuid.uuid4().hex)
    result = perform_backup(data_dir, out=output)
    if result["dump"] is None or not _dump_complete(result["dump"]):
        raise MigrationError("the bank backup is incomplete; nothing changed; repair backup and retry migrate-flat --apply")
    config = data_dir / "config.yaml"
    with tarfile.open(result["state"], "r:gz") as archive:
        member = archive.getmember("config.yaml")
        if not member.isfile():
            raise MigrationError("state backup has no regular config.yaml; nothing changed")
        with archive.extractfile(member) as source:
            if source.read() != config.read_bytes():
                raise MigrationError("config.yaml changed during backup; nothing changed; retry migrate-flat --apply")
    try:
        for path in (result["dump"], result["state"]):
            # A writable handle lets Windows flush file data and metadata.
            with Path(path).open("r+b") as stream:
                os.fsync(stream.fileno())
        if os.name != "nt":
            for path in (output, output.parent, data_dir):
                descriptor = os.open(path, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
    except OSError:
        raise MigrationError("the bank backup could not be flushed to disk; nothing changed; repair the backup filesystem and retry migrate-flat --apply") from None
    return {"dump": str(result["dump"]), "state": str(result["state"])}


def _read_config(path):
    if path.is_symlink() or not path.is_file():
        raise MigrationError("config.yaml must be an existing regular file in --data-dir; nothing changed")
    raw = path.read_bytes()
    try:
        config = yaml.safe_load(raw.decode("utf-8-sig"))
        if config is None:
            config = {}
    except (yaml.YAMLError, UnicodeError):
        raise MigrationError("config.yaml is not valid UTF-8 YAML; restore a valid config and retry migrate-flat --apply") from None
    # Match the native config editor's rewrite domain before taking a backup.
    seen = set()

    def writable(value):
        if isinstance(value, (bytes, datetime.date)):
            raise MigrationError("config.yaml holds a YAML timestamp or binary value, which this daemon cannot rewrite; edit config.yaml on the daemon host instead")
        if type(value) is int and not -(2**63) <= value < 2**63:
            raise MigrationError("config.yaml holds an integer past 64 bits, which this daemon cannot rewrite; edit config.yaml on the daemon host instead")
        if isinstance(value, (dict, list)):
            if id(value) in seen:
                raise MigrationError("config.yaml shares a mapping or list through a YAML anchor and alias, which this daemon cannot rewrite; edit config.yaml on the daemon host instead")
            seen.add(id(value))
            for item in (list(value.keys()) + list(value.values()) if isinstance(value, dict) else value):
                writable(item)

    writable(config)
    return raw, _mapping(config, "config.yaml")


def _replace_config(path, raw):
    temporary = path.with_name(".migrate-flat.config." + uuid.uuid4().hex + ".part")
    # Exclusive create refuses stale or foreign files, including symlinks.
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _finish(conn, path, marker):
    raw, config = _read_config(path)
    if _hash(raw) == marker["original_sha256"]:
        target, capacity = flat_config(config, marker["capacity"])
        rendered = yaml.safe_dump(target, sort_keys=False).encode("utf-8")
        if capacity != marker["capacity"] or _hash(rendered) != marker["target_sha256"]:
            raise MigrationError("migration config no longer matches its committed plan; restore bank and config together: " + recovery_instructions(marker["backup"]))
        _replace_config(path, rendered)
    elif _hash(raw) != marker["target_sha256"] and not _flat_layout(config, marker["capacity"]):
        raise MigrationError("config.yaml changed after the migration committed; restore bank and config together: " + recovery_instructions(marker["backup"]))
    marker = {**marker, "complete": True}
    try:
        with conn.transaction():
            conn.execute("UPDATE public.meta SET value=%s::jsonb WHERE key=%s", (json.dumps(marker), MARKER))
    except Exception:
        raise MigrationError("migration marker finalization failed") from None


def migrate(conn, data_dir: Path, *, backup=None):
    """Use a dedicated idle connection; the caller owns closing it on failure."""
    data_dir = Path(data_dir).resolve()
    path = _migration_config_path(data_dir)
    original, config = _read_config(path)
    # Validate before backup, even for an empty bank.
    flat_config(config, 0)
    conn.commit()
    if not conn.execute("SELECT pg_try_advisory_lock(hashtextextended(%s,0))", (WRITER_LOCK,)).fetchone()[0]:
        conn.rollback()
        raise MigrationError("the bank writer is running; stop the daemon and retry migrate-flat --apply; nothing changed")
    conn.commit()
    committed = False
    marker = None
    try:
        found = conn.execute("SELECT value FROM public.meta WHERE key=%s", (MARKER,)).fetchone()
        conn.commit()
        if found:
            marker = found[0]
            if marker.get("complete"):
                if not _flat_layout(config, marker["capacity"]):
                    raise MigrationError("this bank was migrated but its config is not the matching flat layout; restore bank and config together: " + recovery_instructions(marker["backup"]))
                return {"status": "unchanged", **marker}
            committed = True
            _finish(conn, path, marker)
            return {"status": "resumed", **marker, "complete": True}
        with conn.transaction():
            # Allows pg_dump's ACCESS SHARE while preventing direct entry writers.
            conn.execute("SET LOCAL lock_timeout='5s'")
            conn.execute("LOCK TABLE public.entries IN SHARE ROW EXCLUSIVE MODE")
            rows = conn.execute(_FINGERPRINT).fetchall()
            target, capacity = flat_config(config, len(rows))
            nonflat = conn.execute("SELECT count(*) FROM public.entries WHERE band <> 'flat'").fetchone()[0]
            if not nonflat and _flat_layout(config, capacity):
                return {"status": "unchanged", "entries": len(rows), "capacity": capacity}
            pair = (backup or _backup)(data_dir)
            if path.read_bytes() != original:
                raise MigrationError("config.yaml changed during backup; nothing changed; retry migrate-flat --apply")
            rendered = yaml.safe_dump(target, sort_keys=False).encode("utf-8")
            marker = {"version": 1, "complete": False, "entries": len(rows),
                      "capacity": capacity, "original_sha256": _hash(original),
                      "target_sha256": _hash(rendered), "backup": pair}
            conn.execute("UPDATE public.entries SET band='flat' WHERE band <> 'flat'")
            if conn.execute(_FINGERPRINT).fetchall() != rows:
                raise MigrationError("entry verification failed; migration rolled back; restore backup if needed: " + recovery_instructions(pair))
            conn.execute("INSERT INTO public.meta (key,value) VALUES (%s,%s::jsonb)", (MARKER, json.dumps(marker)))
            conn.execute("INSERT INTO public.meta (key,value) VALUES ('writer_lease_epoch','1'::jsonb) "
                         "ON CONFLICT (key) DO UPDATE SET value=to_jsonb(COALESCE("
                         "CASE WHEN jsonb_typeof(meta.value)='number' "
                         "THEN (meta.value #>> '{}')::numeric::bigint END,0)+1)")
        committed = True
        _finish(conn, path, marker)
        return {"status": "migrated", **marker, "complete": True}
    except Exception as error:
        if committed:
            reason = str(error).replace("; nothing changed", "") if isinstance(error, MigrationError) else "config replacement failed"
            raise MigrationError("bank migration committed but config finalization failed; keep the daemon stopped and retry migrate-flat --apply; restore bank and config together if needed: " + recovery_instructions(marker["backup"]) + f" ({reason})") from error
        if isinstance(error, MigrationError):
            raise
        if marker is not None:
            raise MigrationError("migration did not confirm completion; keep the daemon stopped and retry migrate-flat --apply to reconcile; restore bank and config together if needed: " + recovery_instructions(marker["backup"])) from error
        raise MigrationError("migration could not finish; keep the daemon stopped and retry migrate-flat --apply; inspect the backup before restoring") from error
    finally:
        try:
            conn.rollback()
            conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (WRITER_LOCK,))
            conn.commit()
        except Exception:
            # Preserve the original outcome if the connection died during
            # COMMIT; closing also releases any surviving session lease.
            conn.close()


def main(argv=None):
    apply = False
    external_recovery = False
    data_dir = None
    args = iter(sys.argv[1:] if argv is None else argv)
    for argument in args:
        if argument in ("-h", "--help"):
            print(_HELP, end="")
            return 0
        if argument == "--apply":
            apply = True
        elif argument == "--external-recovery-verified":
            external_recovery = True
        elif argument == "--data-dir":
            data_dir = next(args, None)
        else:
            print("migrate-flat refused: use --apply --data-dir PATH", file=sys.stderr)
            return 2
    if not apply:
        print("migrate-flat refused: no changes made; stop the daemon and pass --apply", file=sys.stderr)
        return 1
    if data_dir is None:
        print("migrate-flat refused: --data-dir is required", file=sys.stderr)
        return 1
    if not external_recovery:
        print("migrate-flat refused: " + _RECOVERY_REFUSAL, file=sys.stderr)
        return 1
    dsn = os.environ.get("PSEUDOLIFE_MCP_DATABASE_URL")
    if not dsn:
        print("migrate-flat refused: set PSEUDOLIFE_MCP_DATABASE_URL to the stopped bank and retry", file=sys.stderr)
        return 1
    print(_RECOVERY_WARNING, file=sys.stderr)
    try:
        import psycopg
        _migration_config_path(data_dir)
        with psycopg.connect(dsn, autocommit=True) as conn:
            result = migrate(conn, Path(data_dir))
    except MigrationError as error:
        print(f"migrate-flat refused: {error}", file=sys.stderr)
        return 1
    except Exception:
        print("migrate-flat refused: database or state directory unavailable; check the connection and --data-dir, then retry", file=sys.stderr)
        return 1
    print(f"flat migration: {result['status']}; entries={result['entries']}; capacity={result['capacity']}")
    if "backup" in result:
        print(f"bank dump: {result['backup']['dump']}")
        print(f"state archive: {result['backup']['state']}")
        print("restore bank and config together: " + recovery_instructions(result["backup"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
