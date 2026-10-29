## Configuration sections

One row per dataclass in utils/config.py, including nested band specs and aggregate sections; every declared field is recorded in contract-inventory.json. All rows preserve missing/YAML/default/env precedence, type/error/coercion and validation behavior at the source reader.

| Section | Declared fields | Phase | Status |
|---|---:|---|---|
