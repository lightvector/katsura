"""Tests for SGF parsing, serialization, escaping, and coordinates."""

import pytest

from katsura.sgf import (
    parse_collection,
    serialize_collection,
    SgfParseError,
)
from katsura.sgf.coords import (
    Point,
    sgf_to_point,
    point_to_sgf,
    sgf_to_move,
    parse_point_list,
    point_list_to_sgf,
)


# -- coordinates -----------------------------------------------------------


def test_point_roundtrip_corners():
    assert sgf_to_point("aa") == Point(0, 0)
    assert sgf_to_point("ss") == Point(18, 18)
    assert point_to_sgf(Point(0, 0)) == "aa"
    assert point_to_sgf(Point(18, 18)) == "ss"


def test_point_beyond_19():
    # Column 26 -> 'A', row 0 -> 'a'
    assert point_to_sgf(Point(26, 0)) == "Aa"
    assert sgf_to_point("Aa") == Point(26, 0)
    assert point_to_sgf(Point(51, 51)) == "ZZ"


def test_pass_handling():
    assert sgf_to_move("", 19, 19) is None
    assert sgf_to_move("tt", 19, 19) is None  # legacy pass on <=19
    # On a big board tt is a real point, not a pass.
    assert sgf_to_move("tt", 25, 25) == Point(19, 19)


def test_point_list_compression_roundtrip():
    pts = [Point(0, 0), Point(1, 0), Point(2, 0), Point(0, 1), Point(1, 1), Point(2, 1)]
    encoded = point_list_to_sgf(pts)
    assert encoded == ["aa:cb"]
    assert set(parse_point_list(encoded)) == set(pts)


def test_point_list_mixed():
    pts = [Point(5, 5), Point(0, 0), Point(1, 0)]
    encoded = point_list_to_sgf(pts)
    assert set(parse_point_list(encoded)) == set(pts)


# -- parsing / escaping ----------------------------------------------------


def test_simple_parse():
    roots = parse_collection("(;GM[1]FF[4]SZ[19];B[pd];W[dp])")
    assert len(roots) == 1
    root = roots[0]
    assert root.get_one("GM") == "1"
    assert root.get_one("SZ") == "19"
    b = root.children[0]
    assert b.get_one("B") == "pd"
    w = b.children[0]
    assert w.get_one("W") == "dp"


def test_lowercase_in_ident_ignored():
    roots = parse_collection("(;GaMe[1]SiZe[19])")
    assert roots[0].get_one("GM") == "1"
    assert roots[0].get_one("SZ") == "19"


def test_escaping_decode():
    # A comment containing an escaped ] and an escaped backslash.
    roots = parse_collection("(;C[a\\]b\\\\c])")
    assert roots[0].get_one("C") == "a]b\\c"


def test_soft_line_break():
    # Backslash-newline is a soft break and should vanish.
    roots = parse_collection("(;C[hello \\\nworld])")
    assert roots[0].get_one("C") == "hello world"


def test_hard_newline_preserved():
    roots = parse_collection("(;C[line1\nline2])")
    assert roots[0].get_one("C") == "line1\nline2"


def test_crlf_normalized():
    roots = parse_collection("(;C[a\r\nb])")
    assert roots[0].get_one("C") == "a\nb"


def test_multiple_values():
    roots = parse_collection("(;AB[aa][bb][cc])")
    assert roots[0].get("AB") == ["aa", "bb", "cc"]


def test_escaping_roundtrip():
    original = "(;GM[1]C[brackets: \\] and backslash: \\\\ and colon: :])"
    roots = parse_collection(original)
    out = serialize_collection(roots)
    reparsed = parse_collection(out)
    assert reparsed[0].get_one("C") == roots[0].get_one("C")
    assert "]" in roots[0].get_one("C")


def test_variation_structure_roundtrip():
    src = "(;GM[1];B[pd](;W[dp];B[pp])(;W[pp]))"
    roots = parse_collection(src)
    b = roots[0].children[0]
    assert len(b.children) == 2
    out = serialize_collection(roots).strip()
    # Re-parse and compare structure deeply.
    again = parse_collection(out)
    assert len(again[0].children[0].children) == 2


def test_unknown_props_preserved():
    src = "(;GM[1]KGSDE[aa]ZZ[whatever]FOO[1][2])"
    roots = parse_collection(src)
    assert roots[0].get("KGSDE") == ["aa"]
    assert roots[0].get("FOO") == ["1", "2"]
    out = serialize_collection(roots)
    assert "ZZ[whatever]" in out
    assert "FOO[1][2]" in out


def test_property_order_preserved():
    src = "(;GM[1]FF[4]CA[UTF-8]SZ[19]AP[test:1.0])"
    roots = parse_collection(src)
    out = serialize_collection(roots)
    # Order in output should match input order.
    assert out.index("GM") < out.index("FF") < out.index("CA") < out.index("SZ")


def test_multiple_gametrees():
    roots = parse_collection("(;GM[1]C[one])(;GM[1]C[two])")
    assert len(roots) == 2
    assert roots[0].get_one("C") == "one"
    assert roots[1].get_one("C") == "two"


def test_whitespace_tolerance():
    roots = parse_collection("  ( ; GM [1] ; B [pd] ) ")
    assert roots[0].get_one("GM") == "1"
    assert roots[0].children[0].get_one("B") == "pd"


def test_empty_value_preserved():
    roots = parse_collection("(;GM[1]C[];B[])")
    assert roots[0].get("C") == [""]
    out = serialize_collection(roots)
    assert "C[]" in out
    assert "B[]" in out


def test_no_gametree_errors():
    with pytest.raises(SgfParseError):
        parse_collection("not an sgf at all")


def test_realistic_roundtrip_fidelity():
    """A tricky file (handicap, markup, escapes, unknown/private props,
    variations) must survive a parse -> serialize -> parse cycle unchanged."""
    src = (
        "(;GM[1]FF[4]CA[UTF-8]AP[Some Editor:2.0]SZ[19]HA[2]KM[0.5]"
        "AB[pd][dp]PL[W]RU[Japanese]C[bracket \\] and backslash \\\\ end]"
        "KGSDE[aa]ZQX[private data]"
        ";W[dd]LB[qf:A][nc:B]TR[pd][dp]"
        ";B[qf]CR[dd]C[ko: \\]watch\\]]"
        "(;W[pf];B[pg])(;W[qe]C[variation]))"
    )
    roots = parse_collection(src)
    out = serialize_collection(roots)
    again = parse_collection(out)
    # Unknown/private properties preserved.
    assert again[0].get("KGSDE") == ["aa"]
    assert again[0].get("ZQX") == ["private data"]
    # Escapes preserved through the cycle.
    comments = [n.get_one("C") for n in again[0].walk() if n.get_one("C")]
    assert "ko: ]watch]" in comments
    assert "bracket ] and backslash \\ end" in comments
    # Variation structure preserved.
    bnode = [n for n in again[0].walk() if n.get_one("B") == "qf"][0]
    assert len(bnode.children) == 2
    # A second full cycle is identical (idempotent serialization).
    assert serialize_collection(again) == out


def test_deep_trees_no_recursion_limit():
    """Clone, serialize, and parse must all survive trees whose *branch
    nesting* tracks path depth (dense variation trees, e.g. joseki
    dictionaries) far past the Python recursion limit."""
    from katsura.sgf.tree import SgfNode

    depth = 3000
    root = SgfNode()
    root.set_one("SZ", "19")
    cur = root
    for _ in range(depth):
        cur.add_child()              # a one-node variation at every level...
        cur = cur.add_child()        # ...plus the main continuation
    total = sum(1 for _ in root.walk())
    assert total == 1 + 2 * depth

    c = root.clone()
    assert sum(1 for _ in c.walk()) == total

    out = serialize_collection([root])
    again = parse_collection(out)
    assert sum(1 for _ in again[0].walk()) == total
    assert serialize_collection(again) == out


def test_node_after_variation_tolerated():
    # Some buggy exporters emit a node after a variation list. It attaches to
    # the sequence rather than crashing the load.
    roots = parse_collection("(;GM[1](;B[aa]);B[bb])")
    root = roots[0]
    assert len(root.children) == 2
    moves = sorted(ch.get_one("B") for ch in root.children)
    assert moves == ["aa", "bb"]


def test_variation_before_any_node_tolerated():
    # "((;B[aa]))": an outer tree with no nodes of its own gets a fabricated
    # empty first node the inner variation hangs off.
    roots = parse_collection("((;B[aa]))")
    root = roots[0]
    assert root.props == {}
    assert len(root.children) == 1
    assert root.children[0].get_one("B") == "aa"


def test_valueless_property_is_dropped_not_fatal():
    """A property with no value is malformed, but the parser's contract is
    tolerance: a stray `;W)` used to make the whole file unopenable."""
    roots = parse_collection("(;GM[1]FF[4]SZ[19];B[pd];W)")
    nodes = list(roots[0].main_line())
    assert len(nodes) == 3
    # Dropped, not recorded as W[] — an empty value would invent a pass.
    assert not nodes[2].has("W") and not nodes[2].props
    # A valueless property next to a good one loses only itself.
    node = list(parse_collection("(;GM[1]SZ[19];FOO C[kept])")[0].main_line())[1]
    assert node.get_one("C") == "kept" and not node.has("FOO")
    assert "W" not in serialize_collection(roots).split("SZ[19]")[1]
