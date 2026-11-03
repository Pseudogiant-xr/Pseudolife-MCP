"""Store auto-promotion, including resident reads and persisted restarts."""


def register(h):
    enabled = "memory:\n  cortex:\n    auto_promote: true\n  dream:\n    enabled: false\n"
    get = lambda entity, attribute: h.tool("memory_fact_get", entity=entity, attribute=attribute)
    sentence = "I have a Ragdoll cat named Jacque"
    return {
        "cortex-auto-promote": {
            "config": enabled,
            # Keep multi-slot HLC ticks deterministic across cached Python
            # embeddings and Rust ONNX: both clocks must advance from the
            # same persisted future high-water mark, including after restart.
            "seed": [h.fact("clock fixture", "anchor", "causal ordering"),
                     h.sql("UPDATE facts SET hlc_phys=4102444800000, hlc_logical=7 "
                           "WHERE entity='clock fixture'", after_stop=True)],
            "ops": [
                h.store(sentence, [h.sess("promotion"), ("X-PL-Writer", "promotion-writer")],
                        source="conversation"),
                get("Jacque", "type"), get("Jacque", "breed"),
                # An admitted restatement still confirms both facts.
                h.store(sentence, [h.sess("promotion")], source="conversation"),
                h.store("my editor is neovim", source="tool"),
                get("user", "editor"),
                h.store("my editor is emacs", source="assistant"),
                get("user", "editor"),
                # Explicit origin wins over the source's lower tier.
                h.store("my editor is vim", source="assistant", origin="user"),
                get("user", "editor"),
                h.store("The relay port is 4001", source="notes"),
                get("relay", "port"),
                # An invalid explicit origin is refused before promotion.
                h.store("The gateway port is 4002", source="conversation", origin=""),
                get("gateway", "port"),
                h.store("my database is postgres", source="user", authority="quoted",
                        distortion_tolerance="constraint"),
                h.store("my database is sqlite", source="user"),
                get("user", "database"),
                h.store("   "),
                h.store("I should remember to use memory tools to store facts."),
                {"op": "restart", "config": enabled},
                get("Jacque", "type"), get("user", "editor"), get("user", "database"),
            ],
        },
        "cortex-auto-promote-set": {
            "config": enabled,
            "seed": [h.set_add("user", "database", "postgres")],
            "ops": [h.store("my database is mysql", source="conversation"),
                    get("user", "database")],
        },
        "cortex-auto-promote-confidence": {
            "config": enabled.replace("auto_promote: true", "auto_promote: true\n    promote_confidence: 0.4")
                             .replace("enabled: false", "enabled: false\n    write_dedup_min_jaccard: 0.4"),
            "seed": [h.fact("relay bridge", "host", "local")],
            "ops": [h.store("The relay port is 4001", source="action"), get("relay", "port")],
        },
        "cortex-auto-promote-disabled": {
            "live_only": "restart changes the enabled cortex; compared live in both daemons",
            "config": enabled.replace("auto_promote: true", "auto_promote: false"),
            "seed": [],
            "ops": [h.store(sentence), get("Jacque", "type"),
                    {"op": "restart", "config": enabled.replace("auto_promote: true",
                         "auto_promote: true\n    enabled: false")},
                    h.store("I have a Siamese cat named Maple"), get("Maple", "type")],
        },
        "cortex-auto-promote-eligibility": {
            "config": enabled.replace("memory:\n", "memory:\n  surprise_threshold: 0.99\n"
                                      "  meta_filter:\n    enabled: true\n"),
            "seed": [h.fact("clock fixture", "anchor", "causal ordering"),
                     h.sql("UPDATE facts SET hlc_phys=4102444800000, hlc_logical=7 "
                           "WHERE entity='clock fixture'", after_stop=True)],
            "ops": [h.store(sentence, source="conversation"),
                    # A near restatement has nonzero surprise below the threshold.
                    h.store(sentence + ".", source="conversation"),
                    get("Jacque", "breed"),
                    h.store("I don't have stored information; my editor is notepad",
                            source="assistant"),
                    get("user", "editor")],
        },
    }
