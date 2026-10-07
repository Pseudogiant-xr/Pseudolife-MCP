import base64
import copy

import pytest

from .cli_doorbell_fable125 import HOME_ERROR, expected_effect


@pytest.mark.parametrize("name,arm,exceptional", [
    ("nonstring-prompt-nohome", "oracle", True),
    ("nonstring-prompt-nohome", "candidate", False),
    ("invalid-session-nohome", "oracle", False),
    ("invalid-session-nohome", "candidate", False),
    ("nonstring-prompt-home", "oracle", False),
    ("nonstring-prompt-home", "candidate", False),
    ("symlink-parent", "oracle", False),
    ("symlink-parent", "candidate", False),
])
def test_named_nohome_control_checks_exact_exit_streams_and_no_writes(name, arm, exceptional):
    before = {"digests/fixture.bell-pending": "eA==", "digests/fixture.bell-prompt-seen": "eQ=="}
    stderr = "Traceback (most recent call last):\nfixture frames\n" + HOME_ERROR + "\n" if exceptional else ""
    observation = dict(exit_code=int(exceptional), stdout_b64="",
                       stderr_b64=base64.b64encode(stderr.encode()).decode(), post_files_b64=before,
                       pre_directories=["digests"], post_directories=["digests"])
    expected_effect(name, arm, observation, before)
    for field, value in [("exit_code", 1 - int(exceptional)), ("stdout_b64", "eA=="),
                         ("stderr_b64", "eA=="), ("post_files_b64", dict(before, mutation="eA==")),
                         ("post_directories", ["digests", "unexpected"])]:
        changed = copy.deepcopy(observation)
        changed[field] = value
        with pytest.raises(AssertionError):
            expected_effect(name, arm, changed, before)
