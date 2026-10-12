"""The delegate's narrow graph-order and inverse-provenance rulings."""
from collections import Counter, defaultdict
import copy
import json


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def inverse_collisions(request):
    inverse = {}
    for name, meta in request["relations"].items():
        other = meta.get("inverse_of")
        if other:
            inverse[name] = other
            inverse.setdefault(other, name)
    base = {(e["src"], e["relation"], e["dst"]) for e in request["edges"]}
    choices = defaultdict(set)
    for src, relation, dst in base:
        if relation in inverse:
            key = (dst, inverse[relation], src)
            if key not in base:
                choices[key].add("inverse:" + relation)
    return {key: values for key, values in choices.items() if len(values) > 1}


def edge_groups(rows, choices, candidate):
    asserted, derived = [], []
    entered_derived = False
    for row in rows:
        if not isinstance(row, dict) or type(row.get("derived")) is not bool:
            raise ValueError("edge derived flag is not Boolean")
        if row["derived"]:
            entered_derived = True
            row = copy.deepcopy(row)
            key = (row.get("src"), row.get("relation"), row.get("dst"))
            allowed = choices.get(key, set())
            via = row.get("via")
            if isinstance(via, list) and len(via) == 1 and via[0] in allowed:
                if candidate and via[0] != min(allowed):
                    raise ValueError("inverse collision did not choose the smallest source relation")
                row["via"] = [min(allowed)]
            derived.append(encoded(row))
        else:
            if entered_derived:
                raise ValueError("asserted edge appears after derived edge")
            asserted.append(row)
    return asserted, sorted(Counter(derived).items())


def path_check(expected, actual, edges, src, dst):
    if not isinstance(actual, list) or any(type(node) is not int for node in actual):
        raise ValueError("path must contain integer IDs")
    if len(actual) != len(expected) or not actual or actual[0] != src or actual[-1] != dst:
        raise ValueError("path endpoints or oracle minimum length differ")
    pairs = {frozenset((e["src"], e["dst"])) for e in edges}
    if any(frozenset((a, b)) not in pairs for a, b in zip(actual, actual[1:])):
        raise ValueError("path hop is absent from oracle graph")


def compare(request, expected, actual, exact):
    """Preserve every field outside the explicitly permitted graph freedoms."""
    if set(expected) != {"value"} or not isinstance(actual, dict) or set(actual) != {"value"}:
        return exact(expected, actual)
    left, right = copy.deepcopy(expected), copy.deepcopy(actual)
    try:
        op = request["op"]
        if op == "shortest_path":
            if left["value"] is None:
                return exact(left, right)
            edges = [{"src": e["src_id"], "dst": e["dst_id"]} for e in request["edges"]]
            path_check(left["value"], right["value"], edges, request["src_id"], request["dst_id"])
            left["value"] = right["value"] = "<validated minimum path>"
        else:
            choices = inverse_collisions(request)
            if op == "derive_edges":
                left["value"] = edge_groups(left["value"], choices, False)
                right["value"] = edge_groups(right["value"], choices, True)
            else:
                for value, candidate in ((left["value"], False), (right["value"], True)):
                    nodes = value["nodes"]
                    if not isinstance(nodes, list) or any(type(n) is not int for n in nodes) \
                            or len(nodes) != len(set(nodes)):
                        raise ValueError("nodes must be unique integer IDs")
                    value["nodes"] = sorted(nodes)
                    value["edges"] = edge_groups(value["edges"], choices, candidate)
    except (KeyError, TypeError, ValueError) as error:
        return [f"graph response refused: {error}"]
    return exact(left, right)
