import base64
import json
import sys

import pytest

from evals.rust_port.harness import Policy, compare
from evals.rust_port.pytest_plugin import public_shim_arguments
from evals.rust_port.stdio_judge import StdioPolicy, judge


def transcript(raw, *, stderr=b"", exit_code=0):
    return {"stdout_frames_b64": [base64.b64encode(raw).decode()],
            "stderr_b64": base64.b64encode(stderr).decode(), "exit_code": exit_code}


def test_raw_spacing_member_order_and_numeric_spelling_remain_load_bearing():
    expected = transcript(b'{"id":1,"result":2}\n')
    for raw in (b'{"result":2,"id":1}\n', b'{"id":1, "result":2}\n', b'{"id":1,"result":2.0}\n'):
        assert judge(expected, transcript(raw), StdioPolicy())


def test_duplicate_stdio_rpc_keys_reach_generic_comparator():
    left = transcript(b'{"id":1}\n')
    right = transcript(b'{"id":1,"id":1}\n')
    assert judge(left, right, StdioPolicy()) == [{"path": "/", "reason": "duplicate_json_key"}]
    assert compare({"body": {}}, {"boundary_error": "DuplicateJSONKey"}, Policy())[0]["reason"] == "duplicate_json_key"


def test_source_lf_normalization_changes_only_named_token():
    left = transcript(b'{"result":{"instructions":"a\\r\\nb"},"other":"x\\r\\ny"}\n')
    right = transcript(b'{"result":{"instructions":"a\\nb"},"other":"x\\r\\ny"}\n')
    assert judge(left, right, StdioPolicy()) == []
    assert judge(left, transcript(b'{"result":{"instructions":"a\\nb"},"other":"x\\ny"}\n'), StdioPolicy())


def test_stderr_allowlist_is_exact_and_exit_is_compared():
    left = transcript(b'{"id":1}\n')
    assert judge(left, transcript(b'{"id":1}\n', stderr=b"named diagnostic\n"),
                 StdioPolicy(stderr_allowlist=(b"named diagnostic\\n",))) == []
    assert judge(left, transcript(b'{"id":1}\n', stderr=b"unexpected\n"), StdioPolicy())
    assert judge(left, transcript(b'{"id":1}\n', exit_code=1), StdioPolicy())


@pytest.mark.parametrize("args", [[], ["shim"], ["channel"]])
def test_public_adapter_accepts_only_modes(args):
    assert public_shim_arguments(sys.executable, ["-m", "pseudolife_memory.cli", *args]) == args
    assert public_shim_arguments("pseudolife-mcp", args) == args


@pytest.mark.parametrize("args", [["-c", "import private"], ["-m", "pseudolife_memory.cli", "serve"],
                                    ["-m", "pseudolife_memory.cli", "doctor"]])
def test_public_adapter_refuses_private_and_other_cli_modes(args):
    with pytest.raises(pytest.UsageError):
        public_shim_arguments(sys.executable, args)
