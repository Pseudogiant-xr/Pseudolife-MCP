"""Compare daemon TLS with psycopg on a newly created disposable PostgreSQL.

Linux development proof: python rust/daemon/harness/pg_tls.py --image IMAGE
Add --sudo when the development Docker socket requires it. The image must
already exist locally; this script never pulls, uses an existing server, or
reads a configured bank. Certificate keys and DSNs remain temporary.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from unittest.mock import patch
from urllib.parse import urlencode

import psycopg
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

REPO = Path(__file__).resolve().parents[3]
GOLDEN = Path(__file__).resolve().parent / "goldens/pg_tls.json"
MODES = ("default", "disable", "allow", "prefer", "require", "verify-ca", "verify-full")


def certificates(root):
    """Ephemeral fixture CA, SAN server and client-auth identities."""
    now = datetime.now(timezone.utc)

    def issue(name, *, issuer=None, client=False):
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        authority, signing = issuer if issuer else (None, key)
        builder = (x509.CertificateBuilder().subject_name(subject)
                   .issuer_name(authority.subject if authority else subject)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now-timedelta(days=1)).not_valid_after(now+timedelta(days=1))
                   .add_extension(x509.BasicConstraints(ca=issuer is None, path_length=None), True))
        if issuer:
            builder = builder.add_extension(x509.ExtendedKeyUsage([
                ExtendedKeyUsageOID.CLIENT_AUTH if client else ExtendedKeyUsageOID.SERVER_AUTH]), False)
            if not client:
                builder = builder.add_extension(x509.SubjectAlternativeName([
                    x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
        cert = builder.sign(signing, hashes.SHA256())
        (root/f"{name}.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (root/f"{name}-key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        (root/f"{name}-key.pem").chmod(0o600)
        return cert, key

    ca = issue("ca")
    issue("other-ca")
    issue("server", issuer=ca)
    issue("fixture", issuer=ca, client=True)
    (root/"invalid.pem").write_text("not a certificate\n")


@contextmanager
def server(docker, image, root, policy):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    hba = root/f"{policy}.conf"
    rule = {"both": "host all all 127.0.0.1/32 trust\n",
            "plain": "host all all 127.0.0.1/32 trust\n",
            "tls-only": "hostssl all all 127.0.0.1/32 trust\nhostnossl all all 127.0.0.1/32 reject\n",
            "client-cert": "hostssl all all 127.0.0.1/32 cert\nhostnossl all all 127.0.0.1/32 reject\n"}[policy]
    hba.write_text("local all all trust\n"+rule)
    # The container only exposes loopback, uses a private tmpfs and owns its
    # generated data directory. No host database or named volume is attached.
    command = ("cp /fixture/server.pem /tmp/server.pem; cp /fixture/server-key.pem /tmp/server-key.pem; "
               "chown postgres:postgres /tmp/server*.pem; chmod 600 /tmp/server-key.pem; "
               f"exec docker-entrypoint.sh postgres -c listen_addresses=127.0.0.1 -c port={port} "
               f"-c hba_file=/fixture/{policy}.conf -c ssl={'off' if policy == 'plain' else 'on'} "
               "-c ssl_cert_file=/tmp/server.pem -c ssl_key_file=/tmp/server-key.pem -c ssl_ca_file=/fixture/ca.pem")
    cid = subprocess.check_output([*docker, "run", "--pull=never", "--detach", "--rm", "--network=host",
        "--tmpfs", "/var/lib/postgresql", "--mount", f"type=bind,source={root},target=/fixture,readonly",
        "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "-e", "POSTGRES_USER=fixture", "-e", "POSTGRES_DB=pl_cf_tls",
        "--entrypoint", "sh", image, "-c", command], text=True).strip()
    try:
        guard = f"host=localhost port={port} user=fixture dbname=pl_cf_tls"
        ready = guard+f" sslmode=require sslcert='{root/'fixture.pem'}' sslkey='{root/'fixture-key.pem'}'"
        if policy == "plain": ready = guard+" sslmode=disable"
        for _ in range(60):
            try:
                with psycopg.connect(ready, connect_timeout=2):
                    break
            except psycopg.OperationalError:
                time.sleep(0.25)
        else:
            raise RuntimeError(f"disposable {policy} server did not start")
        yield guard
    finally:
        subprocess.run([*docker, "rm", "--force", cid], check=True, stdout=subprocess.DEVNULL)


def compare(docker, image, root, evidence, oracle_only):
    certificates(root)
    # System trust is private to this proof process and its cargo child.
    environment = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    # libpq reads HOME/.postgresql even when PG* variables are absent. Keep
    # both readiness and the oracle inside a fresh empty client home. Retain
    # the caller's Rust toolchain locations for the cargo subprocess.
    environment.setdefault("CARGO_HOME", str(Path.home()/".cargo"))
    environment.setdefault("RUSTUP_HOME", str(Path.home()/".rustup"))
    client_home = root/"client-home"
    client_home.mkdir()
    environment["HOME"] = str(client_home)
    environment.update(SSL_CERT_FILE=str(root/"ca.pem"), SSL_CERT_DIR=str(root/"empty-roots"))
    (root/"empty-roots").mkdir()
    stray_home = root/"stray-crl-home"
    (stray_home/".postgresql").mkdir(parents=True)
    (stray_home/".postgresql/root.crl").write_text("unused CRL fixture\n")
    records = []
    with patch.dict(os.environ, environment, clear=True):
        for policy in ("both", "plain", "tls-only", "client-cert"):
            with server(docker, image, root, policy) as guard:
                cases = []
                for mode in MODES:
                    roots = (None, "ca.pem", "other-ca.pem", "missing.pem", "invalid.pem") if policy in ("both", "plain") else ("ca.pem",)
                    identities = (False, True) if policy == "client-cert" else (False,)
                    for ca in roots:
                        for identity in identities:
                            options = "" if mode == "default" else f" sslmode={mode}"
                            if ca: options += f" sslrootcert='{root/ca}'"
                            if identity: options += f" sslcert='{root/'fixture.pem'}' sslkey='{root/'fixture-key.pem'}'"
                            cases.append(dict(name=f"{policy}/{mode}/{ca}/client={identity}", guard_dsn=guard, dsn=guard+options))
                if policy == "both":
                    port = psycopg.conninfo.conninfo_to_dict(guard)["port"]
                    for mode in MODES:
                        options = {"port": port, "sslrootcert": str(root/"ca.pem")}
                        if mode != "default":
                            options["sslmode"] = mode
                        for override_host in (False, True):
                            query = dict(options)
                            if override_host:
                                query["host"] = "localhost"
                            authority = "unused.invalid" if override_host else "localhost"
                            cases.append(dict(name=f"uri-override/{mode}/host={override_host}",
                                              guard_dsn=guard,
                                              dsn=f"postgresql://fixture@{authority}:1/pl_cf_tls?{urlencode(query)}"))
                    cases.append(dict(name="system/implicit-verify-full",guard_dsn=guard,dsn=guard+" sslrootcert=system"))
                    for mode in ("prefer", "require"):
                        cases.append(dict(name=f"unused-crl/{mode}/no-file-root",guard_dsn=guard,
                                          dsn=guard+f" sslmode={mode}",client_home=str(stray_home)))
                    cases.append(dict(name="unused-crl/system-root",guard_dsn=guard,
                                      dsn=guard+" sslrootcert=system",client_home=str(stray_home)))
                    for name, value in (("PGSSLMINPROTOCOLVERSION", "TLSv1.2"),
                                        ("PGKRBSRVNAME", "postgres"), ("PGGSSDELEGATION", "0"),
                                        ("PGREQUIRESSL", "0")):
                        cases.append(dict(name=f"environment-default/{name}",guard_dsn=guard,
                                          dsn=guard+" sslmode=require",environment={name:value}))
                    address_guard = guard.replace("host=localhost", "hostaddr=127.0.0.1")
                    for mode in MODES:
                        options = "" if mode == "default" else f" sslmode={mode}"
                        cases.append(dict(name=f"hostaddr/{mode}", guard_dsn=address_guard,
                                          dsn=address_guard+options+f" sslrootcert='{root/'ca.pem'}'"))
                    for ambient, explicit in (("require", None), ("disable", None),
                                              ("require", "disable"), ("disable", "require")):
                        options = " sslmode=require"
                        if explicit: options += f" channel_binding={explicit}"
                        cases.append(dict(name=f"channel-binding/env={ambient}/dsn={explicit}",
                                          guard_dsn=guard, dsn=guard+options,
                                          environment={"PGCHANNELBINDING": ambient}))
                for case in cases:
                    try:
                        case_environment = dict(case.get("environment", {}))
                        if "client_home" in case:
                            case_environment["HOME"] = case["client_home"]
                        with patch.dict(os.environ, case_environment):
                            with psycopg.connect(case["dsn"], connect_timeout=3) as conn:
                                case.update(accepted=True,tls=conn.execute("SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()").fetchone()[0])
                    except psycopg.Error:
                        case.update(accepted=False,tls=None)
                records.extend({k:v for k,v in c.items() if k not in ("dsn","guard_dsn","environment","client_home")} for c in cases)
                if not oracle_only:
                    casefile=root/"cases.json"
                    casefile.write_text(json.dumps(cases))
                    casefile.chmod(0o600)
                    child=dict(environment,PSEUDOLIFE_TEST_TLS_CASES=str(casefile))
                    log=evidence/f"{policy}.log"
                    with log.open("w") as output:
                        result=subprocess.run(["cargo","test","--manifest-path",str(REPO/"rust/Cargo.toml"),
                            "-p","pseudolife-daemon","--bin","pseudolife-daemon","storage::tls_tests::disposable_postgres_tls_matrix",
                            "--locked","--","--exact","--nocapture"],env=child,stdout=output,stderr=subprocess.STDOUT)
                    (evidence/f"{policy}.exit").write_text(str(result.returncode)+"\n")
                    print(json.dumps({"policy":policy,"cases":len(cases),"rust_exit":result.returncode}),flush=True)
                    if result.returncode:
                        print(log.read_text()[-3000:])
                        (evidence/"oracle.json").write_text(json.dumps(records,indent=2)+"\n")
                        return result.returncode
    (evidence/"oracle.json").write_text(json.dumps(records,indent=2)+"\n")
    if records != json.loads(GOLDEN.read_text(encoding="utf-8")):
        raise RuntimeError("TLS oracle differs from goldens/pg_tls.json; inspect the disposable recording before updating it")
    print(json.dumps({"cases":len(records),"oracle_only":oracle_only}),flush=True)
    return 0


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image",required=True)
    parser.add_argument("--sudo",action="store_true")
    parser.add_argument("--oracle-only",action="store_true")
    parser.add_argument("--out",type=Path,required=True)
    args=parser.parse_args()
    args.out.mkdir(parents=True,exist_ok=True)
    docker=["sudo","-n","docker"] if args.sudo else ["docker"]
    with tempfile.TemporaryDirectory(prefix="pl-tls-") as directory:
        root=Path(directory)
        root.chmod(0o755)  # only public certs/HBA readable by container postgres
        return compare(docker,args.image,root,args.out,args.oracle_only)


if __name__ == "__main__":
    raise SystemExit(main())
