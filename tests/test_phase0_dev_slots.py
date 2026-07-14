"""Dev-fact extraction: lexicon-gated 'X is Y', possessives, key: value."""
from pseudolife_memory.memory.slots import extract_slots


def _slots(text):
    return {
        (s.entity.lower(), s.attribute.lower(), s.value)
        for s in extract_slots(text)
    }


# ── Positives ────────────────────────────────────────────────────────────

def test_subject_form_with_compound_attribute():
    s = _slots("The zanthar build system default timeout is 4500 seconds")
    assert ("zanthar build system", "default timeout", "4500 seconds") in s


def test_my_form_maps_to_user_entity():
    assert ("user", "editor", "neovim") in _slots("my editor is neovim")


def test_possessive_form():
    s = _slots("GND-Share's default branch is master")
    assert ("gnd-share", "default branch", "master") in s


def test_of_form():
    s = _slots("the host of the relay server is 192.168.1.30")
    assert ("relay server", "host", "192.168.1.30") in s


def test_colon_form_with_entity():
    s = _slots("edge gateway host: 192.168.1.30:1236")
    assert ("edge gateway", "host", "192.168.1.30:1236") in s


def test_equals_form():
    s = _slots("pseudolife daemon port = 8765")
    assert ("pseudolife daemon", "port", "8765") in s


# ── Precision guards (must NOT extract) ─────────────────────────────────

def test_question_is_skipped():
    assert not _slots("Is the zanthar default timeout 4500 seconds?")


def test_negation_is_skipped():
    assert not _slots("The api host is not reachable today")


def test_non_lexicon_attribute_skipped():
    assert not _slots("my point is that we should ship sooner")


def test_entityless_subject_skipped():
    # No entity before the attribute — too ambiguous to promote.
    assert not _slots("The default branch is master")


def test_bare_colon_key_skipped():
    assert not _slots("note: remember to ship the release")
    assert not _slots("port: 8080")


def test_code_fence_skipped():
    text = "Set it up like this:\n```\nedge gateway host: 10.0.0.1\n```"
    assert not _slots(text)


def test_value_stopword_skipped():
    assert not _slots("the gnd-share status is that we are blocked")


# ── Legacy patterns still work ───────────────────────────────────────────

def test_legacy_named_pattern_survives():
    s = extract_slots("I have a Ragdoll cat named Jacque")
    kinds = {(x.entity, x.attribute, x.value) for x in s}
    assert ("Jacque", "type", "cat") in kinds
    assert ("Jacque", "breed", "Ragdoll") in kinds
