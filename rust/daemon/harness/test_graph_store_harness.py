"""Rejecting controls for the store harness's semantic clock comparison."""
import copy
import unittest

from graph_store import ClockState, catalog_shape, digest, response_diff, strict_json


def state(clock=1.0):
    return {"catalog": {"sequences": [["public", "entities_id_seq", "bigint", 1, 1, 1]]},
            "rows": {"public.entities": {"columns": ["id", "created_at"],
                                          "rows": [[1, clock]]}}}


class ClockControls(unittest.TestCase):
    def test_unchanged_seed_value_stays_exact(self):
        clock = ClockState()
        clock.initialize(state())
        self.assertEqual(clock.state(state(), 0, (10, 11)), state())

    def test_changed_clock_requires_own_window(self):
        clock = ClockState()
        clock.initialize(state())
        with self.assertRaisesRegex(AssertionError, "outside operation window"):
            clock.state(state(9), 0, (10, 11))

    def test_changed_clock_cannot_compare_with_unchanged(self):
        clocks = [ClockState(), ClockState()]
        for clock in clocks:
            clock.initialize(state())
        changed = clocks[0].state(state(10), 0, (10, 11))
        unchanged = clocks[1].state(state(), 0, (10, 11))
        self.assertNotEqual(digest(changed), digest(unchanged))

    def test_equal_semantic_changes_compare_inside_each_window(self):
        clocks = [ClockState(), ClockState()]
        for clock in clocks:
            clock.initialize(state())
        a = clocks[0].state(state(10), 3, (10, 11))
        b = clocks[1].state(state(20), 3, (20, 21))
        self.assertEqual(digest(a), digest(b))

    def test_response_clock_must_equal_durable_value(self):
        clock = ClockState()
        clock.initialize(state())
        with self.assertRaisesRegex(AssertionError, "response disagrees"):
            clock.response({"id": 1, "created_at": 2})

    def test_nonfinite_clock_refuses(self):
        clock = ClockState()
        clock.initialize(state())
        with self.assertRaises(AssertionError):
            clock.state(state(float("nan")), 1, (10, 11))

    def test_portable_digest_keeps_sequence_advance(self):
        a, b = state(), state()
        b["catalog"]["sequences"][0][-1] = 2
        self.assertNotEqual(digest(a), digest(b))
        self.assertEqual(catalog_shape(a), catalog_shape(b))

    def test_catalog_shape_keeps_nonsequence_mutation(self):
        a, b = state(), state()
        b["catalog"]["extra"] = "unexpected table"
        self.assertNotEqual(catalog_shape(a), catalog_shape(b))

    def test_row_multiplicity_is_preserved(self):
        a, b = state(), copy.deepcopy(state())
        b["rows"]["public.entities"]["rows"].append([1, 1.0])
        self.assertNotEqual(digest(a), digest(b))

    def test_clock_scalar_type_is_not_erased(self):
        clock = ClockState()
        clock.initialize(state())
        with self.assertRaisesRegex(AssertionError, "response disagrees"):
            clock.response({"id": 1, "created_at": True})

    def test_response_list_scalar_types_stay_distinct(self):
        self.assertTrue(response_diff({"nodes": [{"id": 1}]}, {"nodes": [{"id": True}]}))

    def test_duplicate_keys_refuse(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            strict_json('{"value":1,"value":2}')

    def test_nonfinite_json_refuses(self):
        with self.assertRaisesRegex(ValueError, "non-finite"):
            strict_json('{"value":NaN}')


if __name__ == "__main__":
    unittest.main()
