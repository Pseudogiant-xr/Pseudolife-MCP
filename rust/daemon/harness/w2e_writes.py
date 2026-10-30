"""Console write scenarios for the write-path harness (CUTOVER B2b).

The episode harness's ``rest`` operation compares HTTP status and body;
``w2e`` compares the full bank after every request. Success-path SQL
expectations also require the intended mutation on both sides.
"""
from __future__ import annotations

from w2e_episodes import rest


def checked(path: str, body: dict, *queries: str) -> dict:
    return {**rest(path, body), "expect": list(queries)}


def register(h) -> dict[str, dict]:
    set_fact = "/api/facts/set"
    resolve = "/api/facts/resolve"
    forget = "/api/facts/forget"
    reinforce = "/api/reinforce"
    delete = "/api/delete"
    supersede = "/api/supersede"
    consolidate = "/api/consolidate"

    def write(entity, attribute, value, **kw):
        return rest(set_fact, {"entity": entity, "attribute": attribute, "value": value, **kw})

    return {
        "console-facts-null": {
            "config": None,
            # A deterministic database refusal replaces PostgreSQL's failing-row
            # detail (which embeds run-specific timestamps and vector bytes).
            # The trigger fires only for NULL: coercing to an empty string evades it.
            "seed": [h.sql("CREATE FUNCTION console_null_fact() RETURNS trigger LANGUAGE plpgsql AS $$ "
                           "BEGIN IF NEW.value IS NULL THEN RAISE EXCEPTION 'null fact value rejected'; "
                           "END IF; RETURN NEW; END $$; "
                           "CREATE TRIGGER console_null_fact BEFORE INSERT ON facts "
                           "FOR EACH ROW EXECUTE FUNCTION console_null_fact()")],
            "ops": [
                checked(set_fact, {"entity": "Console Null", "attribute": "value", "value": None},
                        "SELECT count(*) = 0 FROM facts WHERE entity_norm = 'console-null'"),
                {"op": "rest", "method": "GET", "path": "/api/facts"},
                rest(forget, {"entity": "Console Null"}),
                checked(set_fact, {"entity": "Console Null", "attribute": "value", "value": "valid"},
                        "SELECT count(*) = 1 FROM facts WHERE entity_norm = 'console-null' AND value = 'valid'"),
            ],
        },
        "console-facts-set": {
            "config": None,
            "seed": [h.set_add("Console Team", "tools", "editor")],
            "ops": [
                rest(set_fact),
                rest(set_fact, {"attribute": "port", "value": "8765"}),
                rest(set_fact, {"entity": "Console DB", "value": "8765"}),
                rest(set_fact, {"entity": "Console DB", "attribute": "port"}),
                write(7, "port", "8765"),
                write("Console DB", ["port"], "8765"),
                write("Console DB", "port", {"value": "8765"}),
                write("Console DB", "port", "8765", confidence="invalid"),
                write("Console DB", "port", "8765", confidence=None),
                write("Console DB", "port", "8765", confidence=[]),
                write("Console DB", "port", "8765", authority="invalid"),
                write(7, "port", "8765", authority="invalid"),
                write("Console DB", "port", "8765", authority=True),
                write("Console DB", "port", "8765", distortion_tolerance=["constraint"]),
                write("Console DB", "port", "8765", distortion_tolerance="invalid"),
                write("Console DB", "port", "8765", origin=["user"]),
                write("Console DB", "port", "8765", freshness_class=["slow"]),
                # A scalar write cannot replace set members.
                write("Console Team", "tools", "terminal"),
                checked(set_fact, {"entity": "Console DB", "attribute": "port", "value": "8765"},
                        "SELECT count(*) = 1 FROM facts WHERE entity_norm = 'console-db' "
                        "AND attribute_norm = 'port' AND status = 'current' AND value = '8765'"),
                # The route converts a string confidence and passes both labels.
                checked(set_fact, {"entity": "Console DB", "attribute": "host", "value": "10.0.0.5",
                                   "confidence": "0.9", "origin": "user", "freshness_class": "volatile",
                                   "authority": "directive", "distortion_tolerance": "constraint"},
                        "SELECT count(*) = 1 FROM facts WHERE entity_norm = 'console-db' "
                        "AND attribute_norm = 'host' AND status = 'current' AND abs(confidence - 0.9) < 0.000001 "
                        "AND origin = 'user' AND freshness_class = 'volatile' "
                        "AND authority = 'directive' AND distortion_tolerance = 'constraint'"),
                write("console_db", "Host", "10.0.0.5", origin="action"),
                checked(set_fact, {"entity": "Console DB", "attribute": "host", "value": "10.0.0.6"},
                        "SELECT count(*) = 1 FROM facts WHERE entity_norm = 'console-db' "
                        "AND attribute_norm = 'host' AND status = 'contested' AND value = '10.0.0.6'"),
                checked(set_fact, {"entity": "Console DB", "attribute": "host", "value": "10.0.0.7",
                                   "confidence": 0.9, "origin": "user"},
                        "SELECT count(*) = 1 FROM facts WHERE entity_norm = 'console-db' "
                        "AND attribute_norm = 'host' AND status = 'current' AND value = '10.0.0.7'"),
                # Falsey optional fields take the route's documented defaults.
                write("Console DB", "region", "west", origin="", freshness_class=None,
                      authority="", distortion_tolerance=None, confidence=True),
                write("Console DB", "numeric", "decimal", confidence="٠.٨"),
            ],
        },
        "console-facts-resolve": {
            "config": None,
            "seed": [
                h.fact("Console DB", "host", "10.0.0.5", origin="user", confidence=0.9),
                h.fact("Console DB", "host", "10.0.0.6"),
                h.fact("Console DB", "port", "8765", origin="user", confidence=0.9),
                h.fact("Console DB", "port", "8766"),
                h.fact("Console DB", "region", "west", origin="user", confidence=0.9),
                h.fact("Console DB", "region", "east"),
            ],
            "ops": [
                rest(resolve),
                rest(resolve, {"attribute": "host", "accept": True}),
                rest(resolve, {"entity": "Console DB", "accept": True}),
                rest(resolve, {"entity": 7, "attribute": "host", "accept": True}),
                rest(resolve, {"entity": "Console DB", "attribute": ["host"], "accept": False}),
                rest(resolve, {"entity": "No such entity", "attribute": "host", "accept": True}),
                # bool() is the REST contract: even the string "false" accepts.
                checked(resolve, {"entity": "console_db", "attribute": "Host", "accept": "false"},
                        "SELECT count(*) = 1 FROM facts WHERE entity_norm = 'console-db' "
                        "AND attribute_norm = 'host' AND status = 'current' AND value = '10.0.0.6'"),
                checked(resolve, {"entity": "Console DB", "attribute": "port"},
                        "SELECT count(*) = 0 FROM facts WHERE entity_norm = 'console-db' "
                        "AND attribute_norm = 'port' AND status = 'contested'",
                        "SELECT count(*) = 1 FROM facts WHERE entity_norm = 'console-db' "
                        "AND attribute_norm = 'port' AND status = 'current' AND value = '8765'"),
                rest(resolve, {"entity": "Console DB", "attribute": "region", "accept": []}),
                rest(resolve, {"entity": "Console DB", "attribute": "host", "accept": None}),
            ],
        },
        "console-facts-forget": {
            "config": None,
            "seed": [
                h.fact("Console DB", "host", "10.0.0.5", origin="user", confidence=0.9),
                h.fact("Console DB", "host", "10.0.0.6", origin="user", confidence=0.9),
                h.fact("Console DB", "host", "10.0.0.7"),
                h.fact("Console DB", "port", "8765"),
                h.set_add("Console DB", "tools", "editor"),
                h.fact("Console Peer", "port", "8766"),
                h.sql("INSERT INTO slot_reads (entity_norm, attribute_norm, read_count) VALUES "
                      "('console-db', 'host', 3), ('console-db', 'port', 2)"),
            ],
            "ops": [
                rest(forget),
                rest(forget, {"attribute": "host"}),
                rest(forget, {"entity": 7}),
                rest(forget, {"entity": "Console DB", "attribute": ["host"]}),
                rest(forget, {"entity": "No such entity"}),
                checked(forget, {"entity": "console_db", "attribute": "Host"},
                        "SELECT count(*) = 0 FROM facts WHERE entity_norm = 'console-db' "
                        "AND attribute_norm = 'host'",
                        "SELECT count(*) = 0 FROM slot_reads WHERE entity_norm = 'console-db' "
                        "AND attribute_norm = 'host'",
                        "SELECT count(*) = 1 FROM facts WHERE entity_norm = 'console-db' "
                        "AND attribute_norm = 'port'"),
                rest(forget, {"entity": "Console DB", "attribute": "host"}),
                checked(forget, {"entity": "Console DB", "attribute": None},
                        "SELECT count(*) = 0 FROM facts WHERE entity_norm = 'console-db'",
                        "SELECT count(*) = 0 FROM slot_reads WHERE entity_norm = 'console-db'",
                        "SELECT count(*) = 1 FROM facts WHERE entity_norm = 'console-peer'"),
            ],
        },
        "console-reinforce": {
            "config": None,
            "seed": [h.store("Console reinforcement target.")],
            "ops": [
                rest(reinforce),
                rest(reinforce, {}),
                *[rest(reinforce, {"entry_id": value})
                  for value in (None, "invalid", [], {}, "1.5", "--1")],
                # int() accepts numeric strings, floats and booleans at this route.
                *[rest(reinforce, {"entry_id": value})
                  for value in (0, -1, 999999, "999999", 999999.5, False)],
                checked(reinforce, {"entry_id": "١"},
                        "SELECT reinforcements = 1 AND explicit_reinforcements = 1 FROM entries "
                        "WHERE text = 'Console reinforcement target.'"),
                checked(reinforce, {"entry_id": "@id:Console reinforcement target."},
                        "SELECT reinforcements = 2 AND explicit_reinforcements = 2 FROM entries "
                        "WHERE text = 'Console reinforcement target.'"),
                checked(delete, {"text": "Console reinforcement target."},
                        "SELECT count(*) = 0 FROM entries WHERE text = 'Console reinforcement target.'"),
                rest(reinforce, {"entry_id": 999999}),
            ],
        },
        "console-delete": {
            "config": "memory:\n  delete_confirm_threshold: 2\n",
            "seed": [
                h.store("Console delete exact.", source="status", tags=["keep"]),
                h.store("Console delete tagged.", source="status", tags=["remove"]),
                h.store("Console bulk first.", source="status"),
                h.store("Console bulk second.", source="status"),
                h.store("Console bulk third.", source="status"),
                h.store("Console session note.", [h.sess("console-delete")], source="user"),
                h.session_title("Console deletion session", [h.sess("console-delete")]),
            ],
            "ops": [
                rest(delete),
                rest(delete, {"text": "", "source": None}),
                rest(delete, {"substring": 7}),
                rest(delete, {"text": ["Console delete exact."]}),
                rest(delete, {"text": "Console delete exact.", "source": "user"}),
                checked(delete, {"text": "Console delete exact.", "source": "status", "tag": " KEEP "},
                        "SELECT count(*) = 0 FROM entries WHERE text = 'Console delete exact.'",
                        "SELECT count(*) = 1 FROM entries WHERE text = 'Console delete tagged.'"),
                checked(delete, {"source": "status", "tag": "remove"},
                        "SELECT count(*) = 0 FROM entries WHERE text = 'Console delete tagged.'"),
                # A string false must leave all three rows in place; true permits the bulk deletion.
                checked(delete, {"substring": "Console bulk", "confirm_bulk": "false"},
                        "SELECT count(*) = 3 FROM entries WHERE text LIKE 'Console bulk%'"),
                checked(delete, {"substring": "Console bulk", "confirm_bulk": True},
                        "SELECT count(*) = 0 FROM entries WHERE text LIKE 'Console bulk%'"),
                checked(delete, {"episode": "@ep:Console deletion session", "source": "user"},
                        "SELECT count(*) = 0 FROM entries WHERE text = 'Console session note.'"),
                rest(delete, {"text": "No such console note."}),
            ],
        },
        "console-supersede": {
            "config": None,
            "seed": [
                h.store("Console correction by id."),
                h.store("Console correction by text."),
            ],
            "ops": [
                rest(supersede),
                rest(supersede, {"old_text": "Console correction by id."}),
                *[rest(supersede, {"entry_id": "@id:Console correction by id.", "new_text": value})
                  for value in (None, 7, [], "  ")],
                rest(supersede, {"new_text": "No target supplied."}),
                rest(supersede, {"old_text": "Console correction by id.",
                                 "entry_id": "@id:Console correction by id.", "new_text": "Ambiguous selector."}),
                *[rest(supersede, {"entry_id": value, "new_text": "Invalid id replacement."})
                  for value in ("7", True, 1.5, 0, -1, [], 999999)],
                rest(supersede, {"old_text": 7, "new_text": "Invalid text selector."}),
                rest(supersede, {"old_text": "Missing console note.", "new_text": "Missing target."}),
                checked(supersede, {"entry_id": "@id:Console correction by id.",
                                   "new_text": "  Console corrected using id.  "},
                        "SELECT superseded_at IS NOT NULL AND "
                        "superseded_by_text = 'Console corrected using id.' FROM entries "
                        "WHERE text = 'Console correction by id.'",
                        "SELECT count(*) = 1 FROM entries WHERE text = 'Console corrected using id.' "
                        "AND source = 'correction' AND superseded_at IS NULL"),
                rest(supersede, {"entry_id": "@id:Console correction by id.", "new_text": "Retired target."}),
                checked(supersede, {"old_text": "Console correction by text.",
                                   "new_text": "Console corrected using exact text."},
                        "SELECT superseded_at IS NOT NULL FROM entries WHERE text = 'Console correction by text.'"),
            ],
        },
        "console-consolidate": {
            "config": None,
            "seed": [
                h.store("Console cluster first note."),
                h.store("Console cluster second note."),
                h.store("Console exact cluster first note."),
                h.store("Console exact cluster second note."),
                h.store("Console string tags target."),
                h.store("Console object tags target."),
            ],
            "ops": [
                rest(consolidate),
                rest(consolidate, {"entry_ids": ["@id:Console cluster first note."]}),
                *[rest(consolidate, {"entry_ids": ["@id:Console cluster first note."], "new_text": value})
                  for value in (None, 7, [], "  ")],
                rest(consolidate, {"new_text": "No selection."}),
                rest(consolidate, {"entry_ids": [], "new_text": "Empty selection."}),
                rest(consolidate, {"replaces": [], "new_text": "Empty selection."}),
                rest(consolidate, {"entry_ids": ["@id:Console cluster first note."],
                                   "replaces": ["Console cluster first note."], "new_text": "Both selectors."}),
                *[rest(consolidate, {"entry_ids": value, "new_text": "Invalid id selection."})
                  for value in (7, "7", [True], [1.5], [0], [-1], [[1]])],
                *[rest(consolidate, {"replaces": value, "new_text": "Invalid text selection."})
                  for value in ("Console cluster first note.", [7], ["  "])],
                *[checked(consolidate, {"replaces": ["Console cluster first note."],
                                       "new_text": "Invalid tags replacement.", "tags": tags},
                          "SELECT superseded_at IS NULL FROM entries WHERE text = 'Console cluster first note.'")
                  for tags in (7, True, 1.5)],
                checked(consolidate, {"replaces": ["Console string tags target."], "new_text": "String tags result.", "tags": "AbA"},
                        "SELECT tags = jsonb_build_array('a', 'b') FROM entries WHERE text = 'String tags result.'"),
                checked(consolidate, {"replaces": ["Console object tags target."], "new_text": "Object tags result.", "tags": {" Mixed ": 1}},
                        "SELECT tags = jsonb_build_array('mixed') FROM entries WHERE text = 'Object tags result.'"),
                # One missing target must not retire the valid member of the request.
                checked(consolidate, {"entry_ids": ["@id:Console cluster first note.", 999999],
                                      "new_text": "Atomic refusal."},
                        "SELECT superseded_at IS NULL FROM entries WHERE text = 'Console cluster first note.'"),
                checked(consolidate, {"entry_ids": ["@id:Console cluster first note.",
                                                    "@id:Console cluster second note.",
                                                    "@id:Console cluster first note."],
                                      "new_text": "  Console combined cluster.  ",
                                      "tags": [" Console ", "console", "", "Merged"]},
                        "SELECT count(*) = 2 FROM entries WHERE text IN "
                        "('Console cluster first note.', 'Console cluster second note.') AND superseded_at IS NOT NULL",
                        "SELECT count(*) = 1 FROM entries WHERE text = 'Console combined cluster.' "
                        "AND source = 'consolidation' AND superseded_at IS NULL"),
                rest(consolidate, {"entry_ids": ["@id:Console cluster first note."], "new_text": "Retired target."}),
                checked(consolidate, {"replaces": ["Console exact cluster first note.",
                                                   "Console exact cluster second note."],
                                      "new_text": "Console combined exact cluster.", "source": "user"},
                        "SELECT count(*) = 2 FROM entries WHERE text IN "
                        "('Console exact cluster first note.', 'Console exact cluster second note.') "
                        "AND superseded_at IS NOT NULL",
                        "SELECT count(*) = 1 FROM entries WHERE text = 'Console combined exact cluster.' "
                        "AND source = 'user' AND superseded_at IS NULL"),
            ],
        },
    }
