"""The extractor ladder's fixed synthetic inputs and text scoring helpers."""

import re

PAIRS = [
    {"entity": "checkout-service", "attribute": "default port",
     "stale": "8080", "gold": "9090",
     "initial": "For reference, the checkout-service listens on its default port 8080.",
     "update": "Update: the checkout-service default port is now 9090 after the migration.",
     "question": "What is the checkout-service default port?"},
    {"entity": "payments-db", "attribute": "host",
     "stale": "db-prod-1", "gold": "db-prod-2",
     "initial": "The payments database currently runs on host db-prod-1.",
     "update": "We migrated the payments database to db-prod-2 over the weekend.",
     "question": "Which host serves the payments database?"},
    {"entity": "api-gateway", "attribute": "timeout",
     "stale": "30s", "gold": "60s",
     "initial": "The api-gateway request timeout is set to 30s.",
     "update": "Bumped the api-gateway timeout up to 60s yesterday.",
     "question": "What is the api-gateway request timeout?"},
    {"entity": "auth-service", "attribute": "version",
     "stale": "1.4.2", "gold": "2.0.0",
     "initial": "auth-service is pinned at version 1.4.2.",
     "update": "Rolled auth-service forward to version 2.0.0 this morning.",
     "question": "What version is auth-service?"},
    {"entity": "cache-layer", "attribute": "engine",
     "stale": "redis", "gold": "valkey",
     "initial": "Our cache-layer uses the redis engine.",
     "update": "We've since swapped the cache-layer engine over to valkey.",
     "question": "What engine does the cache-layer use?"},
    {"entity": "build-runner", "attribute": "os",
     "stale": "ubuntu-20.04", "gold": "ubuntu-24.04",
     "initial": "The build-runner image is based on ubuntu-20.04.",
     "update": "Upgraded the build-runner to ubuntu-24.04 in the latest pipeline.",
     "question": "What OS does the build-runner use?"},
    {"entity": "metrics-store", "attribute": "retention",
     "stale": "7 days", "gold": "30 days",
     "initial": "metrics-store retention is 7 days.",
     "update": "We extended metrics-store retention to 30 days.",
     "question": "What is the metrics-store retention?"},
    {"entity": "search-index", "attribute": "shards",
     "stale": "4", "gold": "12",
     "initial": "The search-index runs with 4 shards.",
     "update": "Scaled the search-index out to 12 shards last sprint.",
     "question": "How many shards does the search-index run?"},
    {"entity": "cdn-provider", "attribute": "vendor",
     "stale": "fastly", "gold": "cloudflare",
     "initial": "Our cdn-provider vendor is fastly.",
     "update": "Cut the cdn-provider over from fastly to cloudflare.",
     "question": "Who is the cdn-provider vendor?"},
    {"entity": "job-queue", "attribute": "broker",
     "stale": "rabbitmq", "gold": "kafka",
     "initial": "The job-queue broker is rabbitmq.",
     "update": "Replaced the job-queue broker with kafka recently.",
     "question": "What is the job-queue broker?"},
]

DISTRACTORS = [
    "Our internal wiki lives at wiki.corp.local.",
    "The frontend bundle size is about 2MB after tree-shaking.",
    "Daily standups are at 9:30am in the main channel.",
    "db-prod-3 is provisioned but reserved for future use.",
    "The staging cluster autoscaler kicks in above 70% CPU.",
    "Release notes are published to the changelog every Friday.",
]

# Unanswerable probes — never stated (or entity stated but attribute absent).
# For the abstention sub-sweep: these SHOULD return low_confidence=True.
UNANSWERABLE = [
    ("billing-service", "port", "What port does the billing-service use?"),
    ("notification-service", "host", "Where is the notification-service hosted?"),
    ("ml-pipeline", "framework", "What framework does the ml-pipeline use?"),
    ("user-db", "engine", "What engine backs the user-db?"),
    ("cdn-provider", "price", "What is the monthly cdn-provider price?"),
    ("payments-db", "password", "What is the payments-db password?"),
]

TOP_K = 5

# Same-entity/different-attribute and different-entity/same-attribute pairs that
# must remain DISTINCT slots after consolidation. A resolver false-merge collapses
# one of these onto the other -> measured as false_merge in the supersession sweep.
#
# IMPORTANT: these entities/attributes must NOT collide with PAIRS or UNANSWERABLE —
# otherwise a no-merge text re-asserts a pair's value and contaminates the
# supersession measurement (and an UNANSWERABLE probe would stop being unanswerable).
NO_MERGE = [
    # same entity, different attribute -> must remain two slots
    {"a": ("invoice-service", "port"), "b": ("invoice-service", "region"),
     "a_text": "The invoice-service port is 7000.",
     "b_text": "The invoice-service region is us-west-2."},
    # different entity, same attribute (adversarial: 'ledger-db engine' vs
    # 'ledger-cache engine' are lexically close) -> must remain two slots
    {"a": ("ledger-db", "engine"), "b": ("ledger-cache", "engine"),
     "a_text": "The ledger-db uses the postgres engine.",
     "b_text": "The ledger-cache uses the memcached engine."},
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def approx_tokens(text: str) -> int:
    """Cheap, dependency-free token estimate (~chars/4, the common GPT proxy)."""
    return max(1, len(text or "") // 4)


def value_present(text: str, value: str) -> bool:
    """Word-boundary match so short values ('4') don't match '24'."""
    if not text or not value:
        return False
    return re.search(r"(?<![\w.])" + re.escape(value) + r"(?![\w.])",
                     text, re.IGNORECASE) is not None
