"""``/api/config`` (B2d): its ``config_path`` names each side's own home.

The Python and Rust daemons run in separate homes, so the path the config
editor answers differs by its home alone; it compares as
``<home>/data/config.yaml`` (a recorded golden's path is scrubbed to
``<machine path>`` the same way).
"""
import re

CONFIG_PATH = re.compile(r".+[\\/]data[\\/]config\.yaml")


def register(harness):
    def scrub(resp):
        body = resp.get("json")
        if (isinstance(body, dict) and isinstance(body.get("config_path"), str)
                and CONFIG_PATH.fullmatch(body["config_path"])):
            body["config_path"] = "<home>/data/config.yaml"
        return resp

    harness.register_response_scrub("/api/config", scrub)
    return []
