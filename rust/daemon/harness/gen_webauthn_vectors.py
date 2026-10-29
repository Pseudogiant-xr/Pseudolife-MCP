"""Generate WebAuthn parity vectors from the Python oracle.

Output: ``goldens/webauthn-vectors.json``, which the Rust daemon's
``maintainer_webauthn`` tests replay. Run with ``python -I
gen_webauthn_vectors.py``. Every vector records its inputs
(bytes as hex) and what ``pseudolife_memory.maintainer_webauthn`` returned:
the parsed fields on success, the ``check`` of the ``AssertionInvalid`` it
raised, or the name of any other exception. Keys are generated per run and
stay in the vector file only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO), str(REPO / "tests")]

import pseudolife_memory.maintainer_webauthn as w  # noqa: E402
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature  # noqa: E402
from maintainer_authenticator import ORIGIN, RP_ID, RawPairs, SoftAuthenticator, cbor  # noqa: E402

OUT = Path(__file__).resolve().parent / "goldens" / "webauthn-vectors.json"
VECTORS: list[dict] = []
NAMES: set[str] = set()

P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
P256_P = 2**256 - 2**224 + 2**192 + 2**96 - 1
ED_L = 2**252 + 27742317777372353535851937790883648493
ED_P = 2**255 - 19


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def hx(value):
    return None if value is None else bytes(value).hex()


def enc_cbor(v):
    if v is None:
        return {"null": None}
    if isinstance(v, bool):
        return {"bool": v}
    if isinstance(v, int):
        return {"int": str(v)}
    if isinstance(v, bytes):
        return {"bytes": v.hex()}
    if isinstance(v, str):
        return {"text": v}
    if isinstance(v, list):
        return {"array": [enc_cbor(x) for x in v]}
    if isinstance(v, dict):
        return {"map": [[enc_cbor(k), enc_cbor(x)] for k, x in v.items()]}
    raise TypeError(type(v))


def auth_fields(p):
    return {"flags": p.flags, "sign_count": p.sign_count,
            "credential_id": hx(p.credential_id), "public_key": hx(p.public_key)}


def b(args, key):
    return bytes.fromhex(args[key])


CALLS = {
    "b64url_decode": lambda a: w.b64url_decode(a["value"], a["check"]).hex(),
    "cbor_loads": lambda a: enc_cbor(w.cbor_loads(b(a, "data"))),
    "cbor_decode": lambda a: (lambda r: [enc_cbor(r[0]), r[1]])(
        w.cbor_decode(b(a, "data"), a["pos"])),
    "cose_public_key": lambda a: w.cose_public_key(b(a, "cose"))[0],
    "check_client_data": lambda a: (w.check_client_data(
        b(a, "raw"), expected_type=a["expected_type"], challenge=b(a, "challenge"),
        origin=a["origin"]), True)[1],
    "parse_authenticator_data": lambda a: auth_fields(w.parse_authenticator_data(
        b(a, "raw"), rp_id=a["rp_id"], registration=a["registration"])),
    "verify_assertion": lambda a: auth_fields(w.verify_assertion(
        public_key=b(a, "public_key"), alg=a["alg"], client_data_json=b(a, "client_data_json"),
        authenticator_data=b(a, "authenticator_data"), signature=b(a, "signature"),
        challenge=b(a, "challenge"), origin=a["origin"], rp_id=a["rp_id"])),
    "verify_registration": lambda a: (lambda r: {
        "credential_id": r.credential_id.hex(), "public_key": r.public_key.hex(),
        "alg": r.alg, "sign_count": r.sign_count})(w.verify_registration(
            raw_id=b(a, "raw_id"), client_data_json=b(a, "client_data_json"),
            attestation_object=b(a, "attestation_object"), challenge=b(a, "challenge"),
            origin=a["origin"], rp_id=a["rp_id"])),
}


def add(name, fn, **args):
    assert name not in NAMES, name
    NAMES.add(name)
    args = {k: (v.hex() if isinstance(v, (bytes, bytearray)) else v) for k, v in args.items()}
    try:
        expect = {"ok": CALLS[fn](args)}
    except w.AssertionInvalid as exc:
        expect = {"check": exc.check}
    except Exception as exc:  # noqa: BLE001 - recorded, not hidden
        expect = {"exception": type(exc).__name__}
    VECTORS.append({"name": name, "fn": fn, "args": args, "expect": expect})
    return expect


def want(expect, check):
    """Fail loudly when a vector meant to hit ``check`` hits something else."""
    got = expect.get("check", "ok" if "ok" in expect else expect.get("exception"))
    assert got == check, (check, expect)


# ── base64url ──────────────────────────────────────────────────────────────

for value, check in [("", "base64url"), ("A", "base64url"), ("AA", "base64url"),
                     ("AB", "base64url"), ("AAA", "base64url"), ("AAB", "base64url"),
                     ("AAAA", "base64url"), ("AAAAA", "base64url"), ("AA==", "base64url"),
                     ("AA=", "base64url"), ("-_8", "base64url"), ("+/8", "base64url"),
                     ("A A", "base64url"), (" AA", "base64url"), ("AA\n", "base64url"),
                     ("\u00e9AA", "base64url"), ("_w", "base64url"), ("_x", "base64url"),
                     ("__8", "base64url"), ("__9", "base64url"), ("YWJj", "client_data"),
                     ("YWJjZA", "client_data"), ("YWJjZB", "client_data"),
                     ("YWJjZGU", "attestation"), ("YWJjZGV", "attestation"),
                     ("Y2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2M", "base64url"),
                     ("Y2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2NjY2N", "base64url")]:
    add(f"b64/{value!r}/{check}", "b64url_decode", value=value, check=check)
for n in range(0, 9):
    raw = bytes(range(250, 250 - n, -1)) if n else b""
    add(f"b64/roundtrip/{n}", "b64url_decode", value=w.b64url_encode(raw) or "", check="x")


# ── CBOR ───────────────────────────────────────────────────────────────────

def cl(name, data):
    return add(f"cbor/{name}", "cbor_loads", data=data)


for v in [0, 1, 23, 24, 255, 256, 65535, 65536, 2**32 - 1, 2**32, 2**64 - 1,
          -1, -24, -25, -256, -257, -2**32, -2**64]:
    cl(f"int/{v}", cbor(v))
cl("int/nonminimal-1byte", b"\x18\x01")
cl("int/nonminimal-8byte", b"\x1b" + (5).to_bytes(8, "big"))
cl("neg/nonminimal", b"\x39\x00\x00")
cl("bytes/empty", b"\x40")
cl("bytes/short", cbor(b"abc"))
cl("bytes/nonminimal-len", b"\x58\x03abc")
cl("text/empty", b"\x60")
cl("text/ascii", cbor("hello"))
cl("text/multibyte", cbor("h\u00e9llo \u20ac \U0001F600"))
cl("text/nul", b"\x61\x00")
cl("text/fffe", b"\x63\xef\xbf\xbe")
cl("text/max-scalar", b"\x64\xf4\x8f\xbf\xbf")
for name, raw in [("surrogate-high", b"\xed\xa0\x80"), ("surrogate-low", b"\xed\xbf\xbf"),
                  ("overlong-nul", b"\xc0\x80"), ("overlong-3", b"\xe0\x80\xaf"),
                  ("above-max", b"\xf4\x90\x80\x80"), ("truncated", b"\xe2\x82"),
                  ("lone-continuation", b"\x80"), ("f5", b"\xf5\x80\x80\x80"),
                  ("ff", b"\xff"), ("five-byte", b"\xf8\x88\x80\x80\x80"),
                  ("latin1", b"\xe9"), ("c1", b"\xc1\xbf")]:
    want(cl(f"text/{name}", bytes([0x60 + len(raw)]) + raw), "cbor_text")
cl("bool/false", b"\xf4")
cl("bool/true", b"\xf5")
cl("null", b"\xf6")
for info in [0, 19, 23, 24, 25, 26, 27, 28, 31]:
    want(cl(f"simple/f{0xe0 + info:02x}", bytes([0xe0 + info]) + b"\x00" * 8), "cbor_simple")
want(cl("simple/break-only", b"\xff"), "cbor_simple")
want(cl("simple/undefined", b"\xf7"), "cbor_simple")
want(cl("simple/float16", b"\xf9\x3c\x00"), "cbor_simple")
want(cl("simple/float64-truncated", b"\xfb"), "cbor_simple")
for major in range(7):
    for info in (28, 29, 30, 31):
        want(cl(f"indefinite/m{major}i{info}", bytes([(major << 5) | info]) + b"\x00\xff"),
             "cbor_indefinite")
for major in range(7):
    for info, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
        want(cl(f"truncated-arg/m{major}i{info}",
                bytes([(major << 5) | info]) + b"\x00" * (size - 1)), "cbor_truncated")
want(cl("truncated/empty", b""), "cbor_truncated")
want(cl("truncated/bytes", b"\x43ab"), "cbor_truncated")
want(cl("truncated/text", b"\x63ab"), "cbor_truncated")
want(cl("truncated/bytes-huge", b"\x5b" + b"\xff" * 8), "cbor_truncated")
want(cl("truncated/text-huge", b"\x7b" + b"\xff" * 8), "cbor_truncated")
want(cl("truncated/array-count", b"\x82\x00"), "cbor_truncated")
want(cl("truncated/array-huge", b"\x9b" + b"\xff" * 8), "cbor_truncated")
want(cl("truncated/array-item", b"\x82\x00\x18"), "cbor_truncated")
want(cl("truncated/array-item-missing", b"\x83\x18\x01\x00"), "cbor_truncated")
want(cl("truncated/map-count", b"\xa1\x00"), "cbor_truncated")
want(cl("truncated/map-count-odd", b"\xa2\x00\x00\x00"), "cbor_truncated")
want(cl("truncated/map-huge", b"\xbb" + b"\xff" * 8), "cbor_truncated")
want(cl("truncated/map-value", b"\xa1\x00\x18"), "cbor_truncated")
cl("array/empty", b"\x80")
cl("array/mixed", cbor([1, -2, b"x", "y", [True, None], {1: False}]))
cl("map/empty", b"\xa0")
cl("map/int-and-text", cbor({1: 2, "1": 3, -1: 4}))
cl("map/big-keys", b"\xa2\x1b" + b"\xff" * 8 + b"\x00\x3b" + b"\xff" * 8 + b"\x00")
want(cl("map/key-bytes", b"\xa1\x41a\x00"), "cbor_key")
want(cl("map/key-true", b"\xa1\xf5\x00"), "cbor_key")
want(cl("map/key-false", b"\xa1\xf4\x00"), "cbor_key")
want(cl("map/key-null", b"\xa1\xf6\x00"), "cbor_key")
want(cl("map/key-array", b"\xa1\x80\x00"), "cbor_key")
want(cl("map/key-map", b"\xa1\xa0\x00"), "cbor_key")
want(cl("map/key-tag", b"\xa1\xc0\x00\x00"), "cbor_tag")
want(cl("map/key-float", b"\xa1\xf9\x00\x00\x00"), "cbor_simple")
want(cl("map/dup-int", cbor(RawPairs([(1, 2), (1, 3)]))), "cbor_duplicate_key")
want(cl("map/dup-text", cbor(RawPairs([("a", 2), ("a", 3)]))), "cbor_duplicate_key")
want(cl("map/dup-neg", cbor(RawPairs([(-7, 2), (3, 1), (-7, 3)]))), "cbor_duplicate_key")
want(cl("map/dup-nonminimal", b"\xa2\x01\x00\x18\x01\x00"), "cbor_duplicate_key")
want(cl("map/dup-big", b"\xa2\x1b" + b"\xff" * 8 + b"\x00\x1b" + b"\xff" * 8 + b"\x00"),
     "cbor_duplicate_key")
want(cl("map/dup-before-bad-value", b"\xa2\x01\x00\x01\xff"), "cbor_duplicate_key")
cl("map/int-1-vs-text-1", cbor(RawPairs([(1, 0), ("\x01", 0)])))
want(cl("map/key-ok-then-key-bytes", b"\xa2\x01\x00\x40\x00"), "cbor_key")
want(cl("tag/0", b"\xc0\x00"), "cbor_tag")
want(cl("tag/24", b"\xd8\x18\x41\x00"), "cbor_tag")
want(cl("tag/no-content", b"\xc0"), "cbor_tag")
want(cl("tag/27-truncated", b"\xdb\x00"), "cbor_truncated")
want(cl("trailing/one", b"\x00\x00"), "cbor_trailing")
want(cl("trailing/map", cbor({1: 2}) + b"\xff"), "cbor_trailing")
cl("depth/8-scalar", b"\x81" * 8 + b"\x00")
want(cl("depth/9-scalar", b"\x81" * 9 + b"\x00"), "cbor_depth")
cl("depth/8-empty", b"\x81" * 8 + b"\x80")
want(cl("depth/9-empty", b"\x81" * 9 + b"\x80"), "cbor_depth")
cl("depth/maps-8", b"\xa1\x00" * 8 + b"\x00")
want(cl("depth/maps-9", b"\xa1\x00" * 9 + b"\x00"), "cbor_depth")
want(cl("depth/key-at-9", b"\x81" * 8 + b"\xa1\x00\x00"), "cbor_depth")
want(cl("depth/count-check-before-depth", b"\x81" * 9), "cbor_truncated")
want(cl("depth/before-truncation", b"\x81" * 9 + b"\x18"), "cbor_depth")
cl("depth/truncation-at-8", b"\x81" * 8)
want(cl("depth/before-simple", b"\x81" * 9 + b"\xf7"), "cbor_depth")
for pos in (0, 1, 2, 3, 4, 9):
    add(f"cbor_decode/pos-{pos}", "cbor_decode", data=b"\x01\x82\x02\x03\xff", pos=pos)
add("cbor_decode/trailing-allowed", "cbor_decode", data=b"\xa0\xa0\x00", pos=0)


# ── clientDataJSON ─────────────────────────────────────────────────────────

CH = os.urandom(32)
CH64 = w.b64url_encode(CH)
BASE = '"type":"webauthn.get","challenge":"%s","origin":"%s"' % (CH64, ORIGIN)


def cd(name, raw, *, expected_type="webauthn.get", challenge=CH, origin=ORIGIN):
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "surrogatepass")
    return add(f"client_data/{name}", "check_client_data", raw=raw,
               expected_type=expected_type, challenge=challenge, origin=origin)


want(cd("valid", "{%s}" % BASE), "ok")
want(cd("valid-cross-false", '{%s,"crossOrigin":false}' % BASE), "ok")
cd("valid-reordered", '{"origin":"%s","crossOrigin":false,"challenge":"%s","type":"webauthn.get"}'
   % (ORIGIN, CH64))
cd("valid-extra-fields", '{%s,"other_keys_can_be_added_here":"x","tokenBinding":{"status":"present"}}' % BASE)
cd("valid-whitespace", ' \t\r\n{ "type" : "webauthn.get" ,\n"challenge":"%s",\t"origin":"%s" } \n'
   % (CH64, ORIGIN))
cd("ws-formfeed", "\x0c{%s}" % BASE)
cd("ws-vtab", "\x0b{%s}" % BASE)
cd("ws-nbsp-trailing", "{%s}\u00a0" % BASE)
cd("ws-ideographic", "\u3000{%s}" % BASE)
cd("ws-nel", "{%s}\u0085" % BASE)
cd("bom", b"\xef\xbb\xbf{" + BASE.encode() + b"}")
cd("escaped-type", '{"type":"\\u0077ebauthn.get","challenge":"%s","origin":"%s"}' % (CH64, ORIGIN))
cd("escaped-type-upper", '{"type":"\\u0057ebauthn.get","challenge":"%s","origin":"%s"}' % (CH64, ORIGIN))
cd("escaped-key", '{"\\u0074ype":"webauthn.get","challenge":"%s","origin":"%s"}' % (CH64, ORIGIN))
cd("escaped-origin-slashes", '{"type":"webauthn.get","challenge":"%s","origin":"%s"}'
   % (CH64, ORIGIN.replace("/", "\\/")))
cd("escaped-challenge", '{"type":"webauthn.get","challenge":"\\u%04x%s","origin":"%s"}'
   % (ord(CH64[0]), CH64[1:], ORIGIN))
cd("lone-high-extra", '{%s,"x":"\\ud800"}' % BASE)
cd("lone-low-extra", '{%s,"x":"\\udfff"}' % BASE)
cd("lone-high-key", '{%s,"\\udbff":1}' % BASE)
cd("lone-high-type", '{"type":"webauthn.get\\ud800","challenge":"%s","origin":"%s"}' % (CH64, ORIGIN))
cd("lone-high-origin", '{"type":"webauthn.get","challenge":"%s","origin":"%s\\ud800"}' % (CH64, ORIGIN))
cd("lone-high-challenge", '{"type":"webauthn.get","challenge":"%s\\ud800","origin":"%s"}' % (CH64, ORIGIN))
cd("lone-dup-keys", '{%s,"\\ud800":1,"\\ud800":2}' % BASE)
cd("lone-high-vs-pair-distinct", '{%s,"\\ud800":1,"\\ud800\\udc00":2}' % BASE)
cd("pair-vs-raw-dup", '{%s,"\\ud83d\\ude00":1,"\U0001F600":2}' % BASE)
cd("pair-upper-vs-raw-dup", '{%s,"\\uD83D\\uDE00":1,"\U0001F600":2}' % BASE)
cd("hi-hi-lo", '{%s,"x":"\\ud800\\ud800\\udc00"}' % BASE)
cd("hi-hi-lo-dup", '{%s,"\\ud800\\ud800\\udc00":1,"\\ud800\U00010000":2}' % BASE)
cd("lo-hi", '{%s,"x":"\\udc00\\ud800"}' % BASE)
cd("hi-then-ascii-escape", '{%s,"x":"\\ud800\\u0041"}' % BASE)
cd("hi-then-bad-hex", '{%s,"x":"\\ud800\\u00G1"}' % BASE)
cd("hi-then-backslash-n", '{%s,"x":"\\ud800\\n"}' % BASE)
cd("hi-at-end", '{%s,"x":"\\ud800"' % BASE)
cd("pair-at-end", '{%s,"x":"\\ud800\\udc00' % BASE)
cd("bad-hex", '{%s,"x":"\\u12G4"}' % BASE)
cd("short-hex", '{%s,"x":"\\u12"}' % BASE)
cd("bad-escape-x", '{%s,"x":"\\x41"}' % BASE)
cd("bad-escape-quote", '{%s,"x":"\\\'"}' % BASE)
cd("bad-escape-U", '{%s,"x":"\\U00000041"}' % BASE)
cd("all-escapes", '{%s,"x":"\\"\\\\\\/\\b\\f\\n\\r\\t"}' % BASE)
cd("escape-nul", '{%s,"x":"\\u0000"}' % BASE)
for c in (0x00, 0x01, 0x09, 0x0a, 0x0d, 0x1f):
    cd(f"raw-control-{c:02x}", '{%s,"x":"%s"}' % (BASE, chr(c)))
cd("raw-del", '{%s,"x":"\x7f"}' % BASE)
cd("raw-non-ascii", '{%s,"x":"\u00e9\u20ac\U0001F600"}' % BASE)
cd("invalid-utf8-overlong", b"{" + BASE.encode() + b',"x":"\xc0\x80"}')
cd("invalid-utf8-surrogate", b"{" + BASE.encode() + b',"x":"\xed\xa0\x80"}')
cd("invalid-utf8-latin1", b"{" + BASE.encode() + b',"x":"\xe9"}')
cd("invalid-utf8-above-max", b"{" + BASE.encode() + b',"x":"\xf4\x90\x80\x80"}')
for name, num in [("zero", "0"), ("neg-zero", "-0"), ("int", "123"), ("neg", "-7"),
                  ("frac", "1.5"), ("exp", "1e5"), ("exp-upper", "1E5"), ("exp-plus", "1e+5"),
                  ("exp-minus", "-1.5e-3"), ("huge-exp", "1e400"), ("tiny", "1e-400"),
                  ("big-int", "9" * 400), ("leading-zero", "01"), ("dot-end", "1."),
                  ("dot-start", ".5"), ("minus-only", "-"), ("exp-no-digits", "1e"),
                  ("exp-sign-no-digits", "1e+"), ("plus", "+1"), ("hex", "0x10"),
                  ("nan", "NaN"), ("inf", "Infinity"), ("neg-inf", "-Infinity"),
                  ("nan-lower", "nan"), ("true", "true"), ("false", "false"), ("null", "null"),
                  ("tru", "tru"), ("nul", "nul"), ("True", "True"), ("double-minus", "--1"),
                  ("frac-exp", "0.0e0"), ("minus-frac", "-0.5"), ("dot-exp", "1.e5")]:
    cd(f"number/{name}", '{%s,"x":%s}' % (BASE, num))
    cd(f"number-in-array/{name}", '{%s,"x":[%s]}' % (BASE, num))
cd("trailing-comma-object", '{%s,}' % BASE)
cd("trailing-comma-array", '{%s,"x":[1,]}' % BASE)
cd("leading-comma-array", '{%s,"x":[,1]}' % BASE)
cd("single-quotes", "{'type':'webauthn.get'}")
cd("unquoted-key", '{%s,x:1}' % BASE)
cd("comment", '{%s/*c*/}' % BASE)
cd("missing-colon", '{%s,"x" 1}' % BASE)
cd("missing-comma", '{%s "x":1}' % BASE)
cd("unterminated-object", '{%s' % BASE)
cd("unterminated-string", '{%s,"x":"abc' % BASE)
cd("extra-data", '{%s} x' % BASE)
cd("extra-object", '{%s}{}' % BASE)
cd("empty-object", "{}")
cd("whitespace-only", "   ")
cd("top-array", "[%s]" % ("{%s}" % BASE))
cd("top-string", '"webauthn.get"')
cd("top-number", "1")
cd("top-null", "null")
cd("dup-type", '{%s,"type":"webauthn.get"}' % BASE)
cd("dup-escaped", '{%s,"\\u0078":1,"x":2}' % BASE)
cd("dup-nested", '{%s,"a":{"b":1,"b":2}}' % BASE)
cd("dup-in-array", '{%s,"a":[{"b":1,"b":2}]}' % BASE)
cd("dup-top-array", '[{"b":1,"b":2}]')
cd("dup-then-syntax", '{%s,"a":1,"a":2,' % BASE)
cd("dup-then-extra", '{%s,"a":1,"a":2} x' % BASE)
cd("dup-nested-then-nan", '{%s,"a":{"b":1,"b":2},"c":NaN}' % BASE)
cd("nan-then-dup-nested", '{%s,"c":NaN,"a":{"b":1,"b":2}}' % BASE)
cd("dup-outer-then-wrong-type", '{"type":"x","type":"y"}')
cd("dup-empty-key", '{%s,"":1,"":2}' % BASE)
cd("type-wrong", '{"type":"webauthn.create","challenge":"%s","origin":"%s"}' % (CH64, ORIGIN))
cd("type-number", '{"type":1,"challenge":"%s","origin":"%s"}' % (CH64, ORIGIN))
cd("type-null", '{"type":null,"challenge":"%s","origin":"%s"}' % (CH64, ORIGIN))
cd("type-missing", '{"challenge":"%s","origin":"%s"}' % (CH64, ORIGIN))
cd("type-array", '{"type":["webauthn.get"],"challenge":"%s","origin":"%s"}' % (CH64, ORIGIN))
cd("type-case", '{"type":"WebAuthn.get","challenge":"%s","origin":"%s"}' % (CH64, ORIGIN))
cd("type-and-challenge-wrong", '{"type":"x","challenge":"!!","origin":"y"}')
cd("type-ok-create-expected", "{%s}" % BASE, expected_type="webauthn.create")


def chal(name, value_json):
    cd(f"challenge/{name}", '{"type":"webauthn.get","challenge":%s,"origin":"%s"}'
       % (value_json, ORIGIN))


chal("missing-key", "null")
cd("challenge/absent", '{"type":"webauthn.get","origin":"%s"}' % ORIGIN)
chal("number", "123")
chal("bool", "true")
chal("array", '["%s"]' % CH64)
chal("empty", '""')
chal("padded", '"%s="' % CH64)
chal("std-alphabet", '"%s"' % CH64.replace("-", "+").replace("_", "/") + "")
chal("plus", '"%s+"' % CH64[:-1])
chal("non-canonical", '"%s"' % (CH64[:-1] + chr(ord(CH64[-1]) + 1)
                                 if CH64[-1] not in "z9_-" else CH64[:-1] + "B"))
chal("len-mod-4-is-1", '"%s"' % (CH64 + "A" * ((1 - len(CH64)) % 4)))
chal("other-bytes", '"%s"' % w.b64url_encode(os.urandom(32)))
chal("prefix", '"%s"' % w.b64url_encode(CH[:31]))
chal("longer", '"%s"' % w.b64url_encode(CH + b"\x00"))
chal("non-ascii", '"%s\u00e9"' % CH64)
chal("space", '" %s"' % CH64)
cd("challenge/empty-expected", '{"type":"webauthn.get","challenge":"AA","origin":"%s"}' % ORIGIN,
   challenge=b"")
cd("challenge/zero-byte", '{"type":"webauthn.get","challenge":"AA","origin":"%s"}' % ORIGIN,
   challenge=b"\x00")


def orig(name, value_json, **kw):
    cd(f"origin/{name}", '{"type":"webauthn.get","challenge":"%s","origin":%s}'
       % (CH64, value_json), **kw)


orig("wrong", '"https://evil.example.com"')
orig("trailing-slash", '"%s/"' % ORIGIN)
orig("case", '"%s"' % ORIGIN.upper())
orig("no-port", '"https://console.example.com"')
orig("number", "1")
orig("null", "null")
orig("array", '["%s"]' % ORIGIN)
cd("origin/missing", '{"type":"webauthn.get","challenge":"%s"}' % CH64)
orig("unicode-expected", '"https://b\u00fccher.example"', origin="https://b\u00fccher.example")
orig("unicode-escaped", '"https://b\\u00fccher.example"', origin="https://b\u00fccher.example")
for name, value in [("true", "true"), ("string-false", '"false"'), ("null", "null"),
                    ("zero", "0"), ("empty-array", "[]"), ("empty-object", "{}"),
                    ("false", "false")]:
    cd(f"cross_origin/{name}", '{%s,"crossOrigin":%s}' % (BASE, value))
for name, value in [("string", '"%s"' % ORIGIN), ("null", "null"), ("false", "false"),
                    ("empty", '""')]:
    cd(f"top_origin/{name}", '{%s,"topOrigin":%s}' % (BASE, value))
cd("top_origin/with-cross-true", '{%s,"crossOrigin":true,"topOrigin":"x"}' % BASE)
cd("order/origin-before-cross", '{"type":"webauthn.get","challenge":"%s","origin":"x","crossOrigin":true}'
   % CH64)
cd("size/empty", b"")
pad = 4096 - len(('{%s,"p":""}' % BASE).encode())
want(cd("size/4096", '{%s,"p":"%s"}' % (BASE, "a" * pad)), "ok")
want(cd("size/4097", '{%s,"p":"%s"}' % (BASE, "a" * (pad + 1))), "client_data_size")
cd("size/4096-multibyte", '{%s,"p":"%s"}' % (BASE, "\u00e9" * (pad // 2) + "a" * (pad % 2)))
for depth in (2, 8, 64, 128, 129, 511, 512, 513, 900, 980, 989, 990, 991, 992, 993, 1000, 1500):
    if depth <= 2000:
        cd(f"depth/arrays-{depth}", '{%s,"x":%s}' % (BASE, "[" * (depth - 1) + "]" * (depth - 1)))
for depth in (128, 512, 513, 1000):
    inner = '{"a":' * (depth - 1) + "1" + "}" * (depth - 1)
    cd(f"depth/objects-{depth}", '{%s,"x":%s}' % (BASE, inner))
cd("depth/2047-arrays", "[" * 2047 + "]" * 2047)
cd("depth/dup-before-deep", '{%s,"a":{"b":1,"b":2},"x":%s}' % (BASE, "[" * 600 + "]" * 600))


# ── ceremonies ─────────────────────────────────────────────────────────────

def register(name, auth, *, cdj=None, att=None, ad=None, raw_id=None, flags=0x45, cose=None,
             credential_id=None, fmt="none", att_stmt=None, extensions=None, trailing=b"",
             challenge=CH, origin=ORIGIN, rp_id=RP_ID, extra=None, cd_origin=None,
             kind="webauthn.create", sign_count=None):
    if cdj is None:
        cdj = auth.client_data(CH, kind, origin=cd_origin, extra=extra)
    if att is None:
        if ad is None:
            ad = auth.auth_data(flags=flags, sign_count=sign_count,
                                attested=auth.attested(credential_id=credential_id, cose=cose),
                                extensions=extensions, trailing=trailing)
        att = cbor({"fmt": fmt, "attStmt": {} if att_stmt is None else att_stmt, "authData": ad})
    if raw_id is None:
        raw_id = auth.credential_id if credential_id is None else credential_id
    return add(f"register/{name}", "verify_registration", raw_id=raw_id, client_data_json=cdj,
               attestation_object=att, challenge=challenge, origin=origin, rp_id=rp_id)


def assertion(name, auth, *, public_key=None, alg=None, cdj=None, ad=None, sig=None,
              challenge=CH, origin=ORIGIN, rp_id=RP_ID, flags=0x05, extra=None, extensions=None,
              trailing=b"", sign_count=None, kind="webauthn.get", cd_origin=None, sign=None):
    if cdj is None:
        cdj = auth.client_data(CH, kind, origin=cd_origin, extra=extra)
    if ad is None:
        ad = auth.auth_data(flags=flags, sign_count=sign_count, extensions=extensions,
                            trailing=trailing)
    if sig is None:
        sig = (sign or auth.sign)(ad + sha256(cdj))
    return add(f"assert/{name}", "verify_assertion",
               public_key=auth.cose_key() if public_key is None else public_key,
               alg=auth.alg if alg is None else alg, client_data_json=cdj,
               authenticator_data=ad, signature=sig, challenge=challenge, origin=origin,
               rp_id=rp_id)


def flip(raw: bytes, index: int, bit: int = 0x01) -> bytes:
    out = bytearray(raw)
    out[index] ^= bit
    return bytes(out)


AUTHS = {
    "es256": SoftAuthenticator(-7, sign_count=5),
    "eddsa": SoftAuthenticator(-8),
    "rs2048": SoftAuthenticator(-257, rsa_bits=2048, sign_count=1),
    "rs3072": SoftAuthenticator(-257, rsa_bits=3072),
}

for tag, auth in AUTHS.items():
    key = auth.cose_key()
    # registration: valid shapes
    want(register(f"{tag}/valid", auth), "ok")
    register(f"{tag}/valid-ext-empty", auth, extensions={})
    register(f"{tag}/valid-ext", auth, extensions={"credProtect": 2, "hmac-secret": True})
    register(f"{tag}/valid-packed-stmt", auth, fmt="packed",
             att_stmt={"alg": auth.alg, "sig": b"\x00" * 8, "x5c": [b"\x01"]})
    register(f"{tag}/valid-flags-be-bs", auth, flags=0x5D)
    register(f"{tag}/valid-sign-count", auth, sign_count=2**32 - 1)
    register(f"{tag}/valid-cid-1", auth, credential_id=b"\x07")
    register(f"{tag}/valid-cid-1023", auth, credential_id=os.urandom(1023))
    register(f"{tag}/valid-cross-false", auth, extra={"crossOrigin": False})
    # registration: client data
    register(f"{tag}/cd-type-get", auth, kind="webauthn.get")
    register(f"{tag}/cd-origin", auth, cd_origin="https://evil.example.com")
    register(f"{tag}/cd-challenge", auth, challenge=os.urandom(32))
    register(f"{tag}/cd-cross-true", auth, extra={"crossOrigin": True})
    register(f"{tag}/cd-cross-string", auth, extra={"crossOrigin": "false"})
    register(f"{tag}/cd-cross-null", auth, extra={"crossOrigin": None})
    register(f"{tag}/cd-top-origin", auth, extra={"topOrigin": ORIGIN})
    register(f"{tag}/cd-dup", auth, cdj=('{"type":"webauthn.create","challenge":"%s","origin":"%s",'
                                         '"origin":"%s"}' % (CH64, ORIGIN, ORIGIN)).encode())
    register(f"{tag}/cd-empty", auth, cdj=b"")
    register(f"{tag}/cd-before-attestation-size", auth, kind="webauthn.get", att=b"\x00" * 5000)
    # registration: attestation object
    register(f"{tag}/att-4097", auth, att=b"\xa0" + b"\x00" * 4096)
    register(f"{tag}/att-empty", auth, att=b"")
    register(f"{tag}/att-not-map", auth, att=cbor([1, 2, 3]))
    ad_ok = auth.auth_data(flags=0x45, attested=auth.attested())
    register(f"{tag}/att-extra-key", auth,
             att=cbor({"fmt": "none", "attStmt": {}, "authData": ad_ok, "x": 1}))
    register(f"{tag}/att-missing-key", auth, att=cbor({"fmt": "none", "authData": ad_ok}))
    register(f"{tag}/att-int-key", auth, att=cbor({"fmt": "none", "attStmt": {}, 1: ad_ok}))
    register(f"{tag}/att-fmt-int", auth, att=cbor({"fmt": 1, "attStmt": {}, "authData": ad_ok}))
    register(f"{tag}/att-stmt-array", auth,
             att=cbor({"fmt": "none", "attStmt": [], "authData": ad_ok}))
    register(f"{tag}/att-authdata-text", auth,
             att=cbor({"fmt": "none", "attStmt": {}, "authData": "x"}))
    register(f"{tag}/att-trailing", auth,
             att=cbor({"fmt": "none", "attStmt": {}, "authData": ad_ok}) + b"\x00")
    register(f"{tag}/att-indefinite", auth,
             att=b"\xbf" + cbor("fmt") + cbor("none") + b"\xff")
    register(f"{tag}/att-dup-key", auth,
             att=cbor(RawPairs([("fmt", "none"), ("attStmt", {}), ("fmt", "none")])))
    register(f"{tag}/att-tag", auth, att=b"\xd8\x18" + cbor({"fmt": "none"}))
    register(f"{tag}/att-float-fmt", auth,
             att=b"\xa3" + cbor("fmt") + b"\xf9\x00\x00" + cbor("attStmt") + b"\xa0"
             + cbor("authData") + cbor(ad_ok))
    # pad attStmt so the whole attestation is exactly 4096 bytes
    base_len = len(cbor({"fmt": "none", "attStmt": {"p": b""}, "authData": ad_ok}))
    filler = 4096 - base_len
    for size in (filler - 2, filler - 3, filler - 1):
        att = cbor({"fmt": "none", "attStmt": {"p": b"\x00" * size}, "authData": ad_ok})
        if len(att) == 4096:
            want(register(f"{tag}/att-exactly-4096", auth, att=att), "ok")
            att2 = cbor({"fmt": "none", "attStmt": {"p": b"\x00" * (size + 1)}, "authData": ad_ok})
            register(f"{tag}/att-4097-real", auth, att=att2)
            break
    else:
        raise AssertionError("no 4096-byte attestation for " + tag)
    # registration: authenticator data
    register(f"{tag}/ad-rp", auth, rp_id="other.example.com")
    register(f"{tag}/ad-rp-case", auth, rp_id=RP_ID.upper())
    register(f"{tag}/ad-no-up", auth, flags=0x44)
    register(f"{tag}/ad-no-uv", auth, flags=0x41)
    register(f"{tag}/ad-no-up-uv", auth, flags=0x40)
    register(f"{tag}/ad-no-at", auth, flags=0x05)
    register(f"{tag}/ad-short", auth, ad=ad_ok[:36])
    register(f"{tag}/ad-37", auth, ad=ad_ok[:37])
    register(f"{tag}/ad-54", auth, ad=ad_ok[:54])
    register(f"{tag}/ad-55", auth, ad=ad_ok[:55])
    register(f"{tag}/ad-cid-0", auth, credential_id=b"")
    register(f"{tag}/ad-cid-1024", auth, credential_id=os.urandom(1024))
    register(f"{tag}/ad-cid-65535", auth, ad=ad_ok[:53] + b"\xff\xff" + ad_ok[55:])
    register(f"{tag}/ad-cid-past-end", auth,
             ad=ad_ok[:53] + struct.pack(">H", len(ad_ok) - 55 + 1) + ad_ok[55:])
    register(f"{tag}/ad-cid-exact-end", auth,
             ad=ad_ok[:53] + struct.pack(">H", len(ad_ok) - 55) + ad_ok[55:])
    register(f"{tag}/ad-key-truncated", auth, ad=ad_ok[:-1])
    register(f"{tag}/ad-key-missing", auth, ad=ad_ok[:55 + 16])
    register(f"{tag}/ad-trailing", auth, trailing=b"\x00")
    register(f"{tag}/ad-ed-missing", auth, flags=0xC5)
    register(f"{tag}/ad-ed-array", auth, ad=auth.auth_data(flags=0xC5, attested=auth.attested())
             + cbor([1]))
    register(f"{tag}/ad-ed-bytes", auth, ad=auth.auth_data(flags=0xC5, attested=auth.attested())
             + cbor(b"x"))
    register(f"{tag}/ad-ed-trailing", auth, extensions={"a": 1}, trailing=b"\x00")
    register(f"{tag}/ad-ed-dup", auth, ad=auth.auth_data(flags=0xC5, attested=auth.attested())
             + cbor(RawPairs([("a", 1), ("a", 2)])))
    register(f"{tag}/ad-ed-deep", auth, ad=auth.auth_data(flags=0xC5, attested=auth.attested())
             + b"\xa1\x00" * 9 + b"\x00")
    register(f"{tag}/ad-ed-indefinite", auth,
             ad=auth.auth_data(flags=0xC5, attested=auth.attested()) + b"\xbf\xff")
    register(f"{tag}/ad-trailing-ignored-without-ed", auth,
             ad=auth.auth_data(flags=0x45, attested=auth.attested()) + cbor({}))
    register(f"{tag}/raw-id-mismatch", auth, raw_id=os.urandom(16))
    register(f"{tag}/raw-id-prefix", auth, raw_id=auth.credential_id[:-1])
    register(f"{tag}/raw-id-longer", auth, raw_id=auth.credential_id + b"\x00")
    register(f"{tag}/raw-id-empty", auth, raw_id=b"")
    register(f"{tag}/cid-mismatch-before-cose", auth, raw_id=b"\x01", cose=cbor([1]))
    register(f"{tag}/key-not-map", auth, cose=cbor([1, 2]))
    register(f"{tag}/key-bytes", auth, cose=cbor(b"\x00" * 32))
    register(f"{tag}/key-tag", auth, cose=b"\xc0" + key)
    register(f"{tag}/key-deep", auth, cose=b"\x81" * 9 + b"\x00")
    register(f"{tag}/key-text-label", auth, cose=cbor({**auth.cose_map(), "x": 1}))
    register(f"{tag}/key-dup", auth, cose=cbor(RawPairs(list(auth.cose_map().items())
                                                       + [(3, auth.alg)])))

    # assertions: valid shapes
    want(assertion(f"{tag}/valid", auth), "ok")
    assertion(f"{tag}/valid-count-0", auth, sign_count=0)
    assertion(f"{tag}/valid-count-max", auth, sign_count=2**32 - 1)
    assertion(f"{tag}/valid-ext", auth, extensions={"appid": True})
    assertion(f"{tag}/valid-flags-be-bs", auth, flags=0x1D)
    assertion(f"{tag}/valid-reserved-flags", auth, flags=0x27)
    assertion(f"{tag}/valid-cross-false", auth, extra={"crossOrigin": False})
    # assertions: client data
    assertion(f"{tag}/cd-type-create", auth, kind="webauthn.create")
    assertion(f"{tag}/cd-origin", auth, cd_origin="https://evil.example.com")
    assertion(f"{tag}/cd-origin-arg", auth, origin="https://evil.example.com")
    assertion(f"{tag}/cd-challenge", auth, challenge=os.urandom(32))
    assertion(f"{tag}/cd-cross-true", auth, extra={"crossOrigin": True})
    assertion(f"{tag}/cd-cross-string", auth, extra={"crossOrigin": "false"})
    assertion(f"{tag}/cd-cross-null", auth, extra={"crossOrigin": None})
    assertion(f"{tag}/cd-top-origin", auth, extra={"topOrigin": ORIGIN})
    assertion(f"{tag}/cd-nan", auth, cdj=('{"type":"webauthn.get","challenge":"%s","origin":"%s",'
                                         '"x":NaN}' % (CH64, ORIGIN)).encode())
    assertion(f"{tag}/cd-too-big", auth, cdj=b" " * 4097)
    assertion(f"{tag}/cd-before-auth-data", auth, kind="webauthn.create", ad=b"")
    # assertions: authenticator data
    assertion(f"{tag}/ad-rp", auth, rp_id="other.example.com")
    assertion(f"{tag}/ad-no-up", auth, flags=0x04)
    assertion(f"{tag}/ad-no-uv", auth, flags=0x01)
    assertion(f"{tag}/ad-up-only-signed", auth, flags=0x01)
    assertion(f"{tag}/ad-at", auth, flags=0x45)
    assertion(f"{tag}/ad-at-with-data", auth,
              ad=auth.auth_data(flags=0x45, attested=auth.attested()))
    assertion(f"{tag}/ad-short", auth, ad=auth.auth_data()[:36])
    assertion(f"{tag}/ad-empty", auth, ad=b"")
    assertion(f"{tag}/ad-trailing", auth, trailing=b"\x00")
    assertion(f"{tag}/ad-ed-missing", auth, flags=0x85)
    assertion(f"{tag}/ad-ed-array", auth, ad=auth.auth_data(flags=0x85) + cbor([]))
    assertion(f"{tag}/ad-ed-trailing", auth, extensions={}, trailing=b"\x00")
    assertion(f"{tag}/ad-ed-two-maps", auth, ad=auth.auth_data(flags=0x85) + b"\xa0\xa0")
    assertion(f"{tag}/ad-ed-simple", auth, ad=auth.auth_data(flags=0x85) + b"\xf7")
    assertion(f"{tag}/ad-rp-before-flags", auth, rp_id="x", flags=0x00)
    # assertions: stored key and alg
    for other in (-7, -8, -257):
        if other != auth.alg:
            assertion(f"{tag}/stored-alg-{other}", auth, alg=other)
    assertion(f"{tag}/stored-alg-unsupported", auth, alg=-37)
    assertion(f"{tag}/stored-alg-zero", auth, alg=0)
    assertion(f"{tag}/stored-key-garbage", auth, public_key=b"\xff")
    assertion(f"{tag}/stored-key-trailing", auth, public_key=key + b"\x00")
    assertion(f"{tag}/stored-key-empty", auth, public_key=b"")
    assertion(f"{tag}/stored-key-before-sig", auth, public_key=b"\xa0", sig=b"")
    # assertions: signatures
    ad = auth.auth_data()
    cdj = auth.client_data(CH, "webauthn.get")
    good = auth.sign(ad + sha256(cdj))
    assertion(f"{tag}/sig-empty", auth, ad=ad, cdj=cdj, sig=b"")
    assertion(f"{tag}/sig-empty-before-alg", auth, ad=ad, cdj=cdj, sig=b"", alg=0)
    for i in (0, 1, len(good) // 2, len(good) - 1):
        assertion(f"{tag}/sig-flip-{i}", auth, ad=ad, cdj=cdj, sig=flip(good, i))
    assertion(f"{tag}/sig-high-bit-flip", auth, ad=ad, cdj=cdj, sig=flip(good, 0, 0x80))
    assertion(f"{tag}/sig-truncated", auth, ad=ad, cdj=cdj, sig=good[:-1])
    assertion(f"{tag}/sig-appended", auth, ad=ad, cdj=cdj, sig=good + b"\x00")
    assertion(f"{tag}/sig-prepended", auth, ad=ad, cdj=cdj, sig=b"\x00" + good)
    assertion(f"{tag}/sig-other-message", auth, ad=ad, cdj=cdj,
              sig=auth.sign(ad + sha256(cdj + b" ")))
    assertion(f"{tag}/sig-over-cd-not-hash", auth, ad=ad, cdj=cdj, sig=auth.sign(ad + cdj))
    assertion(f"{tag}/sig-zeros", auth, ad=ad, cdj=cdj, sig=b"\x00" * len(good))
    assertion(f"{tag}/sig-mutated-ad", auth, ad=flip(ad, 36), cdj=cdj, sig=good)


# ── ES256 signature encodings ──────────────────────────────────────────────

es = AUTHS["es256"]
es_ad = es.auth_data(sign_count=9)
es_cdj = es.client_data(CH, "webauthn.get")
es_msg = es_ad + sha256(es_cdj)
while True:
    der_sig = es.sign(es_msg)
    r, s = decode_dss_signature(der_sig)
    if r.bit_length() == 256 and s.bit_length() == 256:
        break


def der_len(n, long=False):
    if long or n >= 128:
        body = n.to_bytes(max(1, (n.bit_length() + 7) // 8), "big")
        return bytes([0x80 | len(body)]) + body
    return bytes([n])


def der_int(v, *, pad=0, long=False, raw=None, tag=0x02):
    body = raw if raw is not None else b"\x00" * pad + v.to_bytes(v.bit_length() // 8 + 1, "big")
    return bytes([tag]) + der_len(len(body), long) + body


def der_seq(*parts, long=False, tag=0x30, extra=b""):
    body = b"".join(parts) + extra
    return bytes([tag]) + der_len(len(body), long) + body


def es_sig(name, sig):
    return assertion(f"es256-der/{name}", es, ad=es_ad, cdj=es_cdj, sig=sig)


want(es_sig("reencoded", der_seq(der_int(r), der_int(s))), "ok")
assert der_seq(der_int(r), der_int(s)) == der_sig
want(es_sig("high-s", der_seq(der_int(r), der_int(P256_N - s))), "ok")
es_sig("raw-r-s", r.to_bytes(32, "big") + s.to_bytes(32, "big"))
es_sig("seq-long-len", der_seq(der_int(r), der_int(s), long=True))
es_sig("seq-long-len-2", b"\x30\x82\x00" + bytes([len(der_sig) - 2]) + der_sig[2:])
es_sig("int-long-len", der_seq(der_int(r, long=True), der_int(s)))
es_sig("r-extra-zero", der_seq(der_int(r, pad=1), der_int(s)))
es_sig("s-extra-zero", der_seq(der_int(r), der_int(s, pad=1)))
es_sig("r-negative", der_seq(der_int(0, raw=r.to_bytes(32, "big")), der_int(s)))
es_sig("s-negative", der_seq(der_int(r), der_int(0, raw=s.to_bytes(32, "big"))))
es_sig("trailing-outside", der_sig + b"\x00")
es_sig("trailing-inside", der_seq(der_int(r), der_int(s), extra=b"\x00"))
es_sig("third-int", der_seq(der_int(r), der_int(s), der_int(1)))
es_sig("set-tag", der_seq(der_int(r), der_int(s), tag=0x31))
es_sig("constructed-int-tag", der_seq(der_int(r, tag=0x22), der_int(s)))
es_sig("bitstring-tag", der_seq(der_int(r, tag=0x03), der_int(s)))
es_sig("indefinite", b"\x30\x80" + der_int(r) + der_int(s) + b"\x00\x00")
es_sig("r-zero", der_seq(der_int(0), der_int(s)))
es_sig("s-zero", der_seq(der_int(r), der_int(0)))
es_sig("r-empty", der_seq(der_int(0, raw=b""), der_int(s)))
es_sig("r-n", der_seq(der_int(P256_N), der_int(s)))
es_sig("s-n", der_seq(der_int(r), der_int(P256_N)))
es_sig("r-plus-n", der_seq(der_int(r + P256_N), der_int(s)))
es_sig("s-plus-n", der_seq(der_int(r), der_int(s + P256_N)))
es_sig("swapped", der_seq(der_int(s), der_int(r)))
es_sig("empty-seq", b"\x30\x00")
es_sig("only-r", der_seq(der_int(r)))
es_sig("truncated", der_sig[:-1])
es_sig("seq-len-short", bytes([0x30, der_sig[1] - 1]) + der_sig[2:])
es_sig("seq-len-long", bytes([0x30, der_sig[1] + 1]) + der_sig[2:])
es_sig("s-65-bytes", der_seq(der_int(r), der_int(0, raw=b"\x00" * 33 + s.to_bytes(32, "big"))))
es_sig("explicit-wrapper", b"\xa0" + der_len(len(der_sig)) + der_sig)

# small r and s (DER short integers), a vanishingly unlikely but legal shape
es_sig("tiny-r-s", der_seq(der_int(1), der_int(1)))


# ── Ed25519 edge keys and signatures ───────────────────────────────────────

ed = AUTHS["eddsa"]
ed_ad = ed.auth_data()
ed_cdj = ed.client_data(CH, "webauthn.get")
ed_msg = ed_ad + sha256(ed_cdj)
ed_good = ed.sign(ed_msg)
ed_s = int.from_bytes(ed_good[32:], "little")


def ed_key(x: bytes) -> bytes:
    return cbor({1: 1, 3: -8, -1: 6, -2: x})


def ed_sig(name, sig, x=None):
    return assertion(f"eddsa-edge/{name}", ed, ad=ed_ad, cdj=ed_cdj, sig=sig,
                     public_key=None if x is None else ed_key(x))


IDENTITY = (1).to_bytes(32, "little")
NONCANON_IDENTITY = (ED_P + 1).to_bytes(32, "little")
ed_sig("s-plus-l", ed_good[:32] + (ed_s + ED_L).to_bytes(32, "little"))
ed_sig("s-high-bits", ed_good[:32] + bytes(ed_good[32:-1]) + bytes([ed_good[63] | 0xe0]))
ed_sig("identity-key-zero-sig", IDENTITY + b"\x00" * 32, IDENTITY)
ed_sig("identity-key-s-l", IDENTITY + ED_L.to_bytes(32, "little"), IDENTITY)
ed_sig("noncanonical-identity-key", IDENTITY + b"\x00" * 32, NONCANON_IDENTITY)
ed_sig("noncanonical-identity-r", NONCANON_IDENTITY + b"\x00" * 32, IDENTITY)
ed_sig("zero-key", IDENTITY + b"\x00" * 32, b"\x00" * 32)
ed_sig("ff-key", ed_good, b"\xff" * 32)
ed_sig("not-on-curve-key", ed_good, (2).to_bytes(32, "little"))
ed_sig("len-63", ed_good[:63])
ed_sig("len-65", ed_good + b"\x00")
for name, x in [("ff", b"\xff" * 32), ("zero", b"\x00" * 32), ("identity", IDENTITY),
                ("two", (2).to_bytes(32, "little")), ("noncanon-identity", NONCANON_IDENTITY)]:
    register(f"eddsa-edge/key-{name}", ed, cose=ed_key(x))
    add(f"cose/eddsa-x-{name}", "cose_public_key", cose=ed_key(x))


# ── RS256 encodings ────────────────────────────────────────────────────────

rs = AUTHS["rs2048"]
rs_ad = rs.auth_data(sign_count=7)
rs_cdj = rs.client_data(CH, "webauthn.get")
rs_msg = rs_ad + sha256(rs_cdj)
rs_priv = rs.private.private_numbers()
rs_n, rs_d = rs_priv.public_numbers.n, rs_priv.d
rs_k = (rs_n.bit_length() + 7) // 8
SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")
SHA256_PREFIX_NO_NULL = bytes.fromhex("302f300b0609608648016503040201" "0420")
SHA1_OID_PREFIX = bytes.fromhex("3021300906052b0e03021a05000420")


def raw_sign(em: bytes) -> bytes:
    return pow(int.from_bytes(em, "big"), rs_d, rs_n).to_bytes(rs_k, "big")


def pkcs1(t: bytes, *, block=b"\x00\x01", fill=b"\xff", tail=b"") -> bytes:
    ps = fill * (rs_k - len(block) - 1 - len(t) - len(tail))
    return block + ps + b"\x00" + t + tail


def rs_sig(name, sig):
    return assertion(f"rs256-enc/{name}", rs, ad=rs_ad, cdj=rs_cdj, sig=sig)


digest_ = sha256(rs_msg)
want(rs_sig("raw-standard", raw_sign(pkcs1(SHA256_PREFIX + digest_))), "ok")
rs_sig("no-null-params", raw_sign(pkcs1(SHA256_PREFIX_NO_NULL + digest_)))
rs_sig("sha1-oid", raw_sign(pkcs1(SHA1_OID_PREFIX + digest_)))
rs_sig("bare-digest", raw_sign(pkcs1(digest_)))
rs_sig("block-type-2", raw_sign(pkcs1(SHA256_PREFIX + digest_, block=b"\x00\x02")))
rs_sig("block-type-0", raw_sign(pkcs1(SHA256_PREFIX + digest_, block=b"\x00\x00")))
rs_sig("fill-fe", raw_sign(pkcs1(SHA256_PREFIX + digest_, fill=b"\xfe")))
rs_sig("garbage-tail", raw_sign(pkcs1(SHA256_PREFIX + digest_, tail=b"\x00\x01\x02")))
rs_sig("short-ps-garbage", raw_sign(b"\x00\x01\xff\x00" + SHA256_PREFIX + digest_
                                     + b"\x00" * (rs_k - 4 - len(SHA256_PREFIX) - 32)))
rs_sig("wrong-digest", raw_sign(pkcs1(SHA256_PREFIX + sha256(rs_msg + b"x"))))
rs_sig("digest-of-sha384", raw_sign(pkcs1(SHA256_PREFIX + hashlib.sha384(rs_msg).digest()[:32])))
rs_sig("pss", rs.private.sign(rs_msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                  salt_length=32), hashes.SHA256()))
rs_sig("pkcs1-sha384", rs.private.sign(rs_msg, padding.PKCS1v15(), hashes.SHA384()))
rs_sig("pkcs1-sha1", rs.private.sign(rs_msg, padding.PKCS1v15(), hashes.SHA1()))
rs_sig("s-equals-n", rs_n.to_bytes(rs_k, "big"))
rs_sig("s-n-minus-1", (rs_n - 1).to_bytes(rs_k, "big"))
rs_sig("s-one", (1).to_bytes(rs_k, "big"))
rs_sig("s-zero", b"\x00" * rs_k)
rs_sig("s-all-ff", b"\xff" * rs_k)
good_rs = rs.sign(rs_msg)
rs_sig("plus-n", (int.from_bytes(good_rs, "big") + rs_n).to_bytes(rs_k + 1, "big"))

rs_numbers = rs.private.public_key().public_numbers()
n_bytes = rs_numbers.n.to_bytes(rs_k, "big")


def rsa_key(n: bytes, e: bytes, *, kty=3, alg=-257, extra=None) -> bytes:
    m = {1: kty, 3: alg, -1: n, -2: e}
    m.update(extra or {})
    return cbor(m)


for name, cose in [("n-leading-zero", rsa_key(b"\x00" + n_bytes, b"\x01\x00\x01")),
                   ("n-two-leading-zeros", rsa_key(b"\x00\x00" + n_bytes, b"\x01\x00\x01")),
                   ("e-leading-zeros", rsa_key(n_bytes, b"\x00\x00\x01\x00\x01")),
                   ("e-4-bytes", rsa_key(n_bytes, b"\x00\x01\x00\x01"))]:
    want(assertion(f"rs256-key/{name}", rs, ad=rs_ad, cdj=rs_cdj, sig=good_rs, public_key=cose),
         "ok")
    register(f"rs256-key/{name}", rs, cose=cose)

# A modulus above aws-lc-rs's 8192-bit cap that OpenSSL still verifies.
big = SoftAuthenticator(-257, rsa_bits=8704)
register("rs8704/valid", big)
assertion("rs8704/valid", big)
assertion("rs8704/sig-flip", big, sig=flip(big.sign(big.auth_data() + sha256(
    big.client_data(CH, "webauthn.get"))), 5))
rs8192 = SoftAuthenticator(-257, rsa_bits=8192)
want(assertion("rs8192/valid", rs8192), "ok")
# A modulus past OpenSSL's 16384-bit cap: registers, never verifies.
huge_n = b"\xc5" + os.urandom(2050) + b"\x01"
add("cose/rsa-16400-bits", "cose_public_key", cose=rsa_key(huge_n, b"\x01\x00\x01"))
assertion("rs16400/any-sig", rs, public_key=rsa_key(huge_n, b"\x01\x00\x01"),
          ad=rs_ad, cdj=rs_cdj, sig=b"\x01" * len(huge_n))


# ── COSE keys ──────────────────────────────────────────────────────────────

esm, edm, rsm = AUTHS["es256"].cose_map(), AUTHS["eddsa"].cose_map(), AUTHS["rs2048"].cose_map()


def cose(name, value):
    data = value if isinstance(value, bytes) else cbor(value)
    return add(f"cose/{name}", "cose_public_key", cose=data)


def with_(m, **changes):
    out = dict(m)
    for k, v in changes.items():
        k = int(k.replace("m", "-"))
        if v is ...:
            out.pop(k)
        else:
            out[k] = v
    return out


for tag, m in (("es256", esm), ("eddsa", edm), ("rs256", rsm)):
    want(cose(f"{tag}/valid", m), "ok")
    want(cose(f"{tag}/reordered", dict(reversed(list(m.items())))), "ok")
    cose(f"{tag}/trailing", cbor(m) + b"\x00")
    cose(f"{tag}/dup", RawPairs(list(m.items()) + [(1, m[1])]))
    cose(f"{tag}/text-key", {**m, "3": m[3]})
    cose(f"{tag}/alg-missing", with_(m, **{"3": ...}))
    cose(f"{tag}/alg-text", with_(m, **{"3": str(m[3])}))
    cose(f"{tag}/alg-true", with_(m, **{"3": True}))
    cose(f"{tag}/alg-ps256", with_(m, **{"3": -37}))
    cose(f"{tag}/alg-es384", with_(m, **{"3": -35}))
    cose(f"{tag}/alg-big", with_(m, **{"3": 2**64 - 1}))
    cose(f"{tag}/alg-big-neg", with_(m, **{"3": -2**64}))
    cose(f"{tag}/extra-label", {**m, 4: b"kid"})
    cose(f"{tag}/extra-big-label", {**m, 2**64 - 1: 0})
    cose(f"{tag}/missing-kty", with_(m, **{"1": ...}))
    cose(f"{tag}/missing-m2", with_(m, m2=...))
    cose(f"{tag}/kty-true", with_(m, **{"1": True}))
    cose(f"{tag}/kty-false", with_(m, **{"1": False}))
    cose(f"{tag}/kty-2", with_(m, **{"1": 2}))
    cose(f"{tag}/kty-1", with_(m, **{"1": 1}))
    cose(f"{tag}/kty-3", with_(m, **{"1": 3}))
    cose(f"{tag}/kty-4", with_(m, **{"1": 4}))
    cose(f"{tag}/kty-text", with_(m, **{"1": "EC2"}))
    cose(f"{tag}/kty-null", with_(m, **{"1": None}))
    cose(f"{tag}/m1-true", with_(m, m1=True))
    cose(f"{tag}/m1-false", with_(m, m1=False))
    cose(f"{tag}/m1-6", with_(m, m1=6))
    cose(f"{tag}/m1-text", with_(m, m1="x"))
    cose(f"{tag}/m2-text", with_(m, m2="x"))
    cose(f"{tag}/m2-array", with_(m, m2=[m[-2]]))
cose("not-map-array", [1, 2])
cose("not-map-int", 7)
cose("not-map-bytes", b"\x41\x00")
cose("empty-map", {})
cose("empty-bytes", b"")
cose("deep", b"\x81" * 9 + b"\x00")
for name, change in [("x-31", {"m2": esm[-2][:31]}), ("x-33", {"m2": esm[-2] + b"\x00"}),
                     ("y-31", {"m3": esm[-3][:31]}), ("y-text", {"m3": "y"}),
                     ("crv-p384", {"m1": 2}), ("x-off-curve", {"m2": flip(esm[-2], 31)}),
                     ("y-off-curve", {"m3": flip(esm[-3], 0)}), ("x-ff", {"m2": b"\xff" * 32}),
                     ("x-p", {"m2": P256_P.to_bytes(32, "big")}),
                     ("zero-point", {"m2": b"\x00" * 32,
                                                                 "m3": b"\x00" * 32})]:
    m = dict(esm)
    for k, v in change.items():
        m[int(k.replace("m", "-"))] = v
    cose(f"es256/{name}", m)
cose("es256/neg-y", {**esm, -3: (P256_P - int.from_bytes(esm[-3], "big")).to_bytes(32, "big")})
cose("es256/okp-labels", {1: 1, 3: -7, -1: 6, -2: edm[-2], -3: edm[-2]})
cose("eddsa/x-31", {**edm, -2: edm[-2][:31]})
cose("eddsa/crv-x448", {**edm, -1: 7})
cose("eddsa/with-y", {**edm, -3: edm[-2]})
cose("eddsa/ec2-kty", {**edm, 1: 2})
rn = rsm[-1]
cose("rs256/n-2047-bits", {**rsm, -1: (int.from_bytes(rn, "big") >> 1).to_bytes(256, "big")})
cose("rs256/n-2^2047", {**rsm, -1: (2**2047).to_bytes(256, "big")})
cose("rs256/n-2^2047+1", {**rsm, -1: (2**2047 + 1).to_bytes(256, "big")})
cose("rs256/n-even", {**rsm, -1: rn[:-1] + bytes([rn[-1] & 0xfe])})
cose("rs256/n-1024", {**rsm, -1: rn[:128]})
cose("rs256/n-empty", {**rsm, -1: b""})
cose("rs256/n-zeros", {**rsm, -1: b"\x00" * 300})
cose("rs256/e-3", {**rsm, -2: b"\x03"})
cose("rs256/e-65537-padded", {**rsm, -2: b"\x00\x01\x00\x01"})
cose("rs256/e-empty", {**rsm, -2: b""})
cose("rs256/e-65537-le", {**rsm, -2: b"\x01\x00\x01\x00"})
cose("rs256/e-huge", {**rsm, -2: b"\x01" * 300})
cose("rs256/short-and-bad-e", {**rsm, -1: rn[:128], -2: b"\x03"})
cose("rs256/n-text", {**rsm, -1: "n"})
cose("rs256/e-int", {**rsm, -2: 65537})
cose("rs256/with-crv", {**rsm, -3: b""})
cose("rs256/ec2-kty-and-short", {**rsm, 1: 2, -1: rn[:128]})
cose("rs256/ps256-alg", {**rsm, 3: -37})
cose("rs256/alg-kty-okp", {**rsm, 1: 1})
cose("es256/crv-true", {**esm, -1: True})
cose("es256/crv-false", {**esm, -1: False})
cose("eddsa/kty-true-crv-true", {**edm, 1: True, -1: True})


# ── parse_authenticator_data direct ────────────────────────────────────────

pa = AUTHS["es256"]
for registration in (False, True):
    for name, raw in [("assert-shape", pa.auth_data()),
                      ("attested-shape", pa.auth_data(flags=0x45, attested=pa.attested())),
                      ("ed-only", pa.auth_data(flags=0x85) + cbor({"x": [1, 2]})),
                      ("at-ed", pa.auth_data(flags=0xC5, attested=pa.attested()) + cbor({})),
                      ("36", pa.auth_data()[:36]), ("empty", b""),
                      ("flags-ff", pa.auth_data(flags=0xFF, attested=pa.attested()) + cbor({})),
                      ("count-bytes", pa.auth_data(sign_count=0x01020304)),
                      ("key-any-item", pa.auth_data(flags=0x45, attested=pa.attested(cose=cbor(7)))),
                      ("key-nested-ok", pa.auth_data(flags=0x45,
                                                     attested=pa.attested(cose=b"\x81" * 8 + b"\x00")))]:
        add(f"auth_data/{name}/{'reg' if registration else 'get'}", "parse_authenticator_data",
            raw=raw, rp_id=RP_ID, registration=registration)
add("auth_data/rp-unicode", "parse_authenticator_data",
    raw=SoftAuthenticator(-7, rp_id="b\u00fccher.example").auth_data(), rp_id="b\u00fccher.example",
    registration=False)


# ── coverage and output ────────────────────────────────────────────────────

source = Path(w.__file__).read_text(encoding="utf-8")
module_checks = set(re.findall(r'AssertionInvalid\("(\w+)"\)', source)) | {"base64url"}
seen = {v["expect"].get("check") for v in VECTORS}
unreachable = {"alg"}  # _verify_signature's fallback: alg was already matched to the key
missing = module_checks - seen - unreachable
assert not missing, f"no vector raises: {sorted(missing)}"
OUT.write_text(json.dumps({"module_checks": sorted(module_checks), "unreachable": sorted(unreachable),
                           "vectors": VECTORS}, indent=1) + "\n", encoding="utf-8")
counts: dict[str, int] = {}
for v in VECTORS:
    counts[v["fn"]] = counts.get(v["fn"], 0) + 1
print(f"{len(VECTORS)} vectors -> {OUT}")
print(json.dumps(counts))
print("exceptions:", [v["name"] for v in VECTORS if "exception" in v["expect"]])
