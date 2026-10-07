"""Positive delivery control for the retained warm CLI measurement controller."""
import base64
import copy

from .cli_wait_mail import cases
from .harness import Policy, compare
from .wait_mail_policy import LEDGER, PREFIX, delivery_projection


def measurement_case():
    return next(case for case in cases() if case["id"] == "wait-mail-unicode-delivery")


def project_invocation(case, response, before, after, window, platform):
    """Check successful delivery and all state, projecting only bounded clocks."""
    if case != measurement_case() or platform not in ("windows", "linux"):
        raise ValueError("wait-mail measurement requires the retained positive delivery case")
    if any(before.get(path) != value for path, value in case["pre_files_b64"].items()):
        raise ValueError("wait-mail measurement seed differs from recorded inputs")
    raw = {**response, "post_files_b64": after}
    projected, instance = delivery_projection(raw, window, LEDGER)
    newline = b"\r\n" if platform == "windows" else b"\n"
    encode = lambda value: base64.b64encode(value).decode("ascii")
    body = "peer — café 🧠\n".encode("utf-8")
    files = copy.deepcopy(before)
    if LEDGER in files:
        raise ValueError("wait-mail measurement requires a fresh ledger")
    files[".pseudolife-mcp/digests/" + "a" * 64 + ".seen"] = encode(b"12\n")
    files[LEDGER] = encode(b"<epoch>\twait\taaaaaaaa\t12\t"
                           + str(len(body.decode("utf-8"))).encode("ascii") + b"\trung anyone" + newline)
    expected = {"exit_code": 0, "stdout_b64": encode(body),
                "stderr_b64": encode(PREFIX + b"<local-clock> (rung anyone, watermark 12, 0 s after arming):" + newline),
                "post_files_b64": files}
    if compare(expected, projected, Policy(source_text_paths=(), ignored_values=())):
        raise ValueError("wait-mail measurement must deliver exact mail and advance only seen/ledger")
    return projected, instance
