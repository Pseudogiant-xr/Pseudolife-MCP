"""pgvector-python 0.5.0 changed psycopg reads to return ``Vector`` objects
(0.4.x returned numpy arrays); ``np.asarray(Vector)`` raises TypeError, which
broke 19 tests on master (2026-07-10 CI). The storage read path must hydrate
both shapes. No DB needed — exercises the helper directly."""

import numpy as np
from pgvector import Vector

from pseudolife_memory.storage.postgres import _embedding_out


def test_embedding_out_handles_vector_objects():
    out = _embedding_out(Vector([1.0, 2.0, 3.0]))
    assert isinstance(out, np.ndarray)
    assert out.dtype == np.float32
    assert out.tolist() == [1.0, 2.0, 3.0]


def test_embedding_out_passes_through_arrays_lists_and_none():
    arr = _embedding_out(np.asarray([0.5, 1.5], dtype=np.float64))
    assert isinstance(arr, np.ndarray)
    assert arr.dtype == np.float32
    assert arr.tolist() == [0.5, 1.5]

    lst = _embedding_out([0.25, 0.75])
    assert lst.dtype == np.float32
    assert lst.tolist() == [0.25, 0.75]

    assert _embedding_out(None) is None
