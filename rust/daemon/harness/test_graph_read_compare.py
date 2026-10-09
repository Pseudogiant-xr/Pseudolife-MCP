"""Reject changes outside the graph read comparator's permitted freedoms."""
import copy
import unittest

import graph_store
from graph_read_compare import compare


def edge(src, relation, dst, *, derived=False, via=None, **metadata):
    row = {"src": src, "relation": relation, "dst": dst,
           "derived": derived, **metadata}
    if via is not None:
        row["via"] = via
    return row


class GraphReadCompareTests(unittest.TestCase):
    def setUp(self):
        self.request = {
            "op": "derive_edges",
            "relations": {"r-a": {"inverse_of": "r-b"}},
            "edges": [edge(1, "r-a", 2), edge(2, "r-a", 3)],
        }
        self.asserted = [
            edge(1, "r-a", 2, confidence=0.75, origin="fixture",
                 evidence={"ids": [4, 5], "enabled": True}),
            edge(2, "r-a", 3, confidence=0.5, origin="fixture"),
        ]
        self.derived = [
            edge(2, "r-b", 1, derived=True, via=["inverse:r-a"],
                 confidence=0.75, evidence={"ids": [4, 5], "enabled": True}),
            edge(3, "r-b", 2, derived=True, via=["inverse:r-a"],
                 confidence=0.5),
        ]
        self.rows = self.asserted + self.derived

    def assert_matches(self, request, expected, actual):
        self.assertEqual(compare(request, expected, actual,
                                 graph_store.response_diff), [])

    def assert_rejects(self, request, expected, actual):
        self.assertTrue(compare(request, expected, actual,
                                graph_store.response_diff))

    def rows_match(self, actual, expected=None, request=None):
        self.assert_matches(request or self.request,
                            {"value": self.rows if expected is None else expected},
                            {"value": actual})

    def rows_reject(self, actual, expected=None, request=None):
        self.assert_rejects(request or self.request,
                            {"value": self.rows if expected is None else expected},
                            {"value": actual})

    def test_derived_order_is_permitted(self):
        self.rows_match(self.asserted + list(reversed(self.derived)))

    def test_derived_multiset_preserves_duplicate_rows(self):
        expected = self.rows + [copy.deepcopy(self.derived[0])]
        self.rows_match(self.asserted + [self.derived[0], self.derived[0],
                                        self.derived[1]], expected)

    def test_missing_derived_duplicate_rejects(self):
        self.rows_reject(self.rows, self.rows + [self.derived[0]])

    def test_extra_derived_duplicate_rejects(self):
        self.rows_reject(self.rows + [self.derived[0]])

    def test_derived_triple_changes_reject(self):
        for field, value in (("src", 9), ("relation", "r-other"), ("dst", 9)):
            with self.subTest(field=field):
                actual = copy.deepcopy(self.rows)
                actual[2][field] = value
                self.rows_reject(actual)

    def test_full_derived_metadata_changes_reject(self):
        for field, value in (("confidence", 0.9),
                             ("evidence", {"ids": [5, 4], "enabled": True}),
                             ("via", ["inverse:r-other"]),
                             ("via", ["inverse:r-a", "inverse:r-a"])):
            with self.subTest(field=field, value=value):
                actual = copy.deepcopy(self.rows)
                actual[2][field] = value
                self.rows_reject(actual)

    def test_derived_unknown_field_addition_and_removal_reject(self):
        actual = copy.deepcopy(self.rows)
        actual[2]["extra"] = None
        self.rows_reject(actual)
        actual = copy.deepcopy(self.rows)
        del actual[2]["evidence"]
        self.rows_reject(actual)

    def test_derived_metadata_scalar_types_reject(self):
        for before, after in ((True, 1), (1, 1.0), (1, "1"), (None, False)):
            with self.subTest(before=before, after=after):
                expected = copy.deepcopy(self.rows)
                actual = copy.deepcopy(self.rows)
                expected[2]["extra"] = before
                actual[2]["extra"] = after
                self.rows_reject(actual, expected)

    def test_asserted_order_changes_reject(self):
        self.rows_reject(list(reversed(self.asserted)) + self.derived)

    def test_asserted_edge_after_derived_rejects_even_on_both_arms(self):
        interleaved = [self.asserted[0], self.derived[0],
                       self.asserted[1], self.derived[1]]
        self.rows_reject(interleaved)
        self.rows_reject(interleaved, interleaved)

    def test_asserted_full_metadata_changes_reject(self):
        actual = copy.deepcopy(self.rows)
        actual[0]["evidence"]["ids"].reverse()
        self.rows_reject(actual)

    def test_asserted_unknown_scalar_type_changes_reject(self):
        for before, after in ((True, 1), (1, 1.0), (1, "1"), (None, False)):
            with self.subTest(before=before, after=after):
                expected = copy.deepcopy(self.rows)
                actual = copy.deepcopy(self.rows)
                expected[0]["extra"] = before
                actual[0]["extra"] = after
                self.rows_reject(actual, expected)

    def test_derived_flag_requires_boolean(self):
        for index in (0, 2):
            for flag in (0, 1, "false", "true", None):
                with self.subTest(index=index, flag=flag):
                    actual = copy.deepcopy(self.rows)
                    actual[index]["derived"] = flag
                    self.rows_reject(actual)
                    self.rows_reject(actual, actual)

    def test_missing_derived_flag_rejects(self):
        actual = copy.deepcopy(self.rows)
        del actual[2]["derived"]
        self.rows_reject(actual)

    def test_derived_boolean_value_change_rejects(self):
        actual = copy.deepcopy(self.rows)
        actual[2]["derived"] = False
        self.rows_reject(actual)

    def graph_response(self):
        return {"value": {"nodes": [1, 2, 3], "edges": self.rows,
                          "paths": [[1, 2, 3]],
                          "extra": {"enabled": True, "count": 1}}}

    def subgraph_request(self):
        return {**self.request, "op": "build_subgraph", "root": 1,
                "depth": 1, "to": 3}

    def test_node_order_and_derived_edge_order_are_permitted(self):
        expected = self.graph_response()
        actual = copy.deepcopy(expected)
        actual["value"]["nodes"] = [3, 1, 2]
        actual["value"]["edges"] = self.asserted + list(reversed(self.derived))
        request = self.subgraph_request()
        self.assert_matches(request, expected, actual)

    def test_duplicate_nodes_reject_even_on_both_arms(self):
        request = self.subgraph_request()
        expected = self.graph_response()
        actual = copy.deepcopy(expected)
        actual["value"]["nodes"] = [1, 2, 3, 3]
        self.assert_rejects(request, expected, actual)
        self.assert_rejects(request, actual, actual)

    def test_nodes_require_integer_ids(self):
        request = self.subgraph_request()
        expected = self.graph_response()
        for node in (True, 1.0, "1", None):
            with self.subTest(node=node):
                actual = copy.deepcopy(expected)
                actual["value"]["nodes"][0] = node
                self.assert_rejects(request, expected, actual)
                self.assert_rejects(request, actual, actual)

    def test_node_addition_and_removal_reject(self):
        request = self.subgraph_request()
        expected = self.graph_response()
        for nodes in ([1, 2], [1, 2, 3, 4]):
            with self.subTest(nodes=nodes):
                actual = copy.deepcopy(expected)
                actual["value"]["nodes"] = nodes
                self.assert_rejects(request, expected, actual)

    def test_unknown_subgraph_fields_and_scalar_types_are_preserved(self):
        request = self.subgraph_request()
        expected = self.graph_response()
        for extra in ({"enabled": 1, "count": 1},
                      {"enabled": True, "count": 1.0},
                      {"enabled": True, "count": 1, "new": None}):
            with self.subTest(extra=extra):
                actual = copy.deepcopy(expected)
                actual["value"]["extra"] = extra
                self.assert_rejects(request, expected, actual)

    def test_unknown_response_fields_force_exact_comparison(self):
        expected = {"value": self.rows, "extra": True}
        actual = {"value": self.asserted + list(reversed(self.derived)),
                  "extra": True}
        self.assert_rejects(self.request, expected, actual)
        self.assert_rejects(self.request, expected, {"value": self.rows})

    def collision_fixture(self):
        request = {
            "op": "derive_edges",
            "relations": {"r-a": {"inverse_of": "r-b"},
                          "r-c": {"inverse_of": "r-b"}},
            "edges": [edge(1, "r-a", 2), edge(1, "r-c", 2)],
        }
        asserted = copy.deepcopy(request["edges"])
        derived = edge(2, "r-b", 1, derived=True, via=["inverse:r-a"])
        return request, asserted, derived

    def test_inverse_collision_accepts_either_oracle_source(self):
        request, asserted, derived = self.collision_fixture()
        for source in ("inverse:r-a", "inverse:r-c"):
            with self.subTest(source=source):
                oracle = {**derived, "via": [source]}
                self.rows_match(asserted + [derived], asserted + [oracle], request)

    def test_inverse_collision_requires_smallest_native_source(self):
        request, asserted, derived = self.collision_fixture()
        native = {**derived, "via": ["inverse:r-c"]}
        self.rows_reject(asserted + [native], asserted + [derived], request)
        self.rows_reject(asserted + [native], asserted + [native], request)

    def test_inverse_collision_impossible_and_extra_provenance_reject(self):
        request, asserted, derived = self.collision_fixture()
        for via in (["inverse:r-z"], [], ["inverse:r-a", "inverse:r-c"],
                    ["inverse:r-a", "inverse:r-a"], "inverse:r-a", None):
            with self.subTest(via=via):
                native = {**derived, "via": via}
                self.rows_reject(asserted + [native], asserted + [derived], request)

    def test_inverse_collision_triples_and_multiplicity_remain_exact(self):
        request, asserted, derived = self.collision_fixture()
        self.rows_reject(asserted + [derived, derived], asserted + [derived], request)
        self.rows_reject(asserted, asserted + [derived], request)
        for field, value in (("src", 3), ("relation", "r-other"), ("dst", 3)):
            with self.subTest(field=field):
                native = {**derived, field: value}
                self.rows_reject(asserted + [native], asserted + [derived], request)

    def test_inverse_collision_does_not_normalize_metadata(self):
        request, asserted, derived = self.collision_fixture()
        oracle = {**derived, "via": ["inverse:r-c"], "extra": True}
        native = {**derived, "extra": 1}
        self.rows_reject(asserted + [native], asserted + [oracle], request)

    def test_noncollision_inverse_provenance_is_exact(self):
        actual = copy.deepcopy(self.rows)
        actual[2]["via"] = ["inverse:r-c"]
        self.rows_reject(actual)

    def test_reverse_inverse_map_can_produce_a_collision(self):
        request = {
            "op": "derive_edges",
            "relations": {"r-b": {"inverse_of": "r-a"},
                          "r-c": {"inverse_of": "r-b"}},
            "edges": [edge(1, "r-a", 2), edge(1, "r-c", 2)],
        }
        asserted = copy.deepcopy(request["edges"])
        oracle = edge(2, "r-b", 1, derived=True, via=["inverse:r-c"])
        native = {**oracle, "via": ["inverse:r-a"]}
        self.rows_match(asserted + [native], asserted + [oracle], request)

    def test_existing_asserted_inverse_does_not_permit_collision_freedom(self):
        request, asserted, derived = self.collision_fixture()
        asserted_inverse = edge(2, "r-b", 1)
        request["edges"].append(asserted_inverse)
        asserted.append(asserted_inverse)
        oracle = {**derived, "via": ["inverse:r-c"]}
        self.rows_reject(asserted + [derived], asserted + [oracle], request)

    def path_request(self, src=1, dst=8):
        return {"op": "shortest_path", "src_id": src, "dst_id": dst,
                "edges": [{"src_id": a, "relation": "r-a", "dst_id": b}
                          for a, b in ((1, 4), (4, 6), (6, 8),
                                       (1, 3), (3, 5), (5, 8), (5, 6))]}

    def test_shortest_path_accepts_alternative_minimum_path(self):
        self.assert_matches(self.path_request(), {"value": [1, 4, 6, 8]},
                            {"value": [1, 3, 5, 8]})

    def test_shortest_path_accepts_reverse_undirected_hops(self):
        self.assert_matches(self.path_request(8, 1), {"value": [8, 6, 4, 1]},
                            {"value": [8, 5, 3, 1]})

    def test_shortest_path_rejects_wrong_endpoints_length_and_absent_hop(self):
        expected = {"value": [1, 4, 6, 8]}
        for path in ([3, 1, 4, 8], [1, 4, 6, 5], [1, 3, 5, 6, 8],
                     [1, 4, 8], [1, 4, 5, 8], []):
            with self.subTest(path=path):
                self.assert_rejects(self.path_request(), expected, {"value": path})

    def test_shortest_path_requires_integer_ids(self):
        expected = {"value": [1, 4, 6, 8]}
        for path in ([True, 4, 6, 8], [1, 4.0, 6, 8], [1, "4", 6, 8],
                     [1, None, 6, 8], None):
            with self.subTest(path=path):
                self.assert_rejects(self.path_request(), expected, {"value": path})

    def test_unreachable_and_isolated_paths_require_none(self):
        for request in (self.path_request(1, 9),
                        {"op": "shortest_path", "src_id": 9,
                         "dst_id": 10, "edges": []}):
            with self.subTest(request=request):
                self.assert_matches(request, {"value": None}, {"value": None})
                for actual in ([], [9, 10], False):
                    self.assert_rejects(request, {"value": None}, {"value": actual})

    def test_same_node_path_requires_exact_singleton_even_if_isolated(self):
        request = {"op": "shortest_path", "src_id": 9, "dst_id": 9,
                   "edges": []}
        self.assert_matches(request, {"value": [9]}, {"value": [9]})
        for actual in (None, [], [9, 9], [10], [9.0]):
            with self.subTest(actual=actual):
                self.assert_rejects(request, {"value": [9]}, {"value": actual})

    def tie_subgraph(self):
        pairs = [(1, 4), (4, 6), (6, 8), (1, 3), (3, 5), (5, 8)]
        request = {"op": "build_subgraph", "root": 1, "depth": 1,
                   "to": 8, "relations": {"uses": {}},
                   "edges": [{"src": a, "relation": "uses", "dst": b}
                             for a, b in pairs]}
        rows = [edge(a, "uses", b) for a, b in pairs]
        # Oracle traversal follows input insertion order, not sorted neighbors.
        value = {"nodes": [1, 3, 4, 6, 8],
                 "edges": [rows[i] for i in (0, 1, 2, 3)],
                 "paths": [[1, 4, 6, 8]]}
        return request, {"value": value}

    def test_subgraph_valid_minimum_tie_with_different_selection_rejects(self):
        request, expected = self.tie_subgraph()
        actual = {"value": {"nodes": [1, 3, 4, 5, 8],
                            "edges": [edge(**request["edges"][i])
                                      for i in (0, 3, 4, 5)],
                            "paths": [[1, 3, 5, 8]]}}
        self.assert_rejects(request, expected, actual)

    def test_subgraph_paths_are_exact_even_when_nodes_and_edges_match(self):
        request, expected = self.tie_subgraph()
        actual = copy.deepcopy(expected)
        actual["value"]["paths"] = [[1, 3, 5, 8]]
        self.assert_rejects(request, expected, actual)

    def test_subgraph_kept_edge_selection_is_exact(self):
        request, expected = self.tie_subgraph()
        for edges in (expected["value"]["edges"][:-1],
                      expected["value"]["edges"] + [edge(**request["edges"][4])],
                      list(reversed(expected["value"]["edges"]))):
            with self.subTest(edges=edges):
                actual = copy.deepcopy(expected)
                actual["value"]["edges"] = edges
                self.assert_rejects(request, expected, actual)

    def test_compare_does_not_mutate_request_or_responses(self):
        request, asserted, derived = self.collision_fixture()
        expected = {"value": asserted + [{**derived, "via": ["inverse:r-c"]}]}
        actual = {"value": asserted + [derived]}
        before = copy.deepcopy((request, expected, actual))
        self.assert_matches(request, expected, actual)
        self.assertEqual((request, expected, actual), before)


if __name__ == "__main__":
    unittest.main()
