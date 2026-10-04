"""Additive Phase 2 CLI cases; outputs come only from the pinned process."""


def corpus():
    arguments = (
        ("help-long", ["--help"]),
        ("help-short", ["-h"]),
        ("help-mode", ["help"]),
        ("help-trailing", ["help", "anything"]),
        ("help-invalid-trailing", ["--help", "--invalid"]),
        ("unknown-basic", ["bogus"]),
        ("unknown-trailing", ["bogus", "--help"]),
        ("unknown-empty", [""]),
        ("unknown-apostrophe", ["isn't-a-mode"]),
        ("unknown-quotes", ["'\"mode"]),
        ("unknown-backslash", ["mode\\name"]),
        ("unknown-controls", ["mode\t\n\r\x01"]),
        ("unknown-unicode", ["mémoire—🧠"]),
        ("unknown-separators", ["mode\u00a0\u2028\u3000"]),
        ("unknown-category-c", ["mode\u200b\ue000\U000f0000"]),
    )
    return {"schema": 1, "fixture": {"kind": "isolated-cli-dispatch",
            "oracle_head": "f709abb54f7912ae9cd767998d0926ca33df4bcd"},
            "cases": [{"id": name, "surface": "cli", "request": {"argv": argv},
                       "policy": {"source_text_paths": [], "ignored_values": []}}
                      for name, argv in arguments]}
