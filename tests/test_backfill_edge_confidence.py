import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))
import backfill_edge_confidence as bf  # noqa: E402


def test_recompute_rows():
    # rows: (id, src_display, relation, dst_display, old_conf)
    rows = [
        (1, "the daemon", "runs-on", "docker-desktop", 0.6),   # clean -> 0.70
        (2, "user", "runs-on", "windows 11", 0.6),             # violation -> 0.175
        (3, "x", "related-to", "y", 0.6),                       # related-to -> 0.45
    ]
    assert bf.recompute_rows(rows) == [(1, 0.70), (2, 0.175), (3, 0.45)]
