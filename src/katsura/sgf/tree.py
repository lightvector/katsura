"""The SGF game-tree data model, parser, and serializer.

Design goals
------------
* **Round-trip fidelity.** Unknown properties and the exact order of
  properties within a node and of variations within a branch are preserved, so
  loading and re-saving an SGF only changes what the user actually edited.
* **Tolerance.** The parser accepts lower-case letters inside property
  identifiers (ignoring them, per the spec), tolerates stray whitespace, and
  does not reject unknown or malformed-but-recoverable input.
* **Correct escaping.** Inside a value only ``\\`` and ``]`` are special. On
  read we undo backslash escapes (an escaped newline is a soft line break and
  is removed). On write we re-escape ``\\`` and ``]``. Because point/number/etc.
  values never contain those characters, uniform escaping is always safe.

A node stores its properties as an ordered mapping ``ident -> list[str]`` of
*decoded* values. The tree is a normal parent/children tree: a linear sequence
of moves is a chain of single-child nodes, and a branch point is a node with
more than one child.
"""

from __future__ import annotations

from collections.abc import Iterator


class SgfParseError(ValueError):
    """Raised when the input is malformed beyond what the tolerant parser can recover."""


class SgfNode:
    """A single node in an SGF game tree.

    Properties are kept in insertion order in :attr:`props`, a dict mapping an
    upper-case identifier to a list of decoded string values. Use the helper
    methods rather than mutating :attr:`props` directly where possible.
    """

    __slots__ = ("props", "children", "parent", "remembered_child")

    def __init__(self, parent: SgfNode | None = None):
        self.props: dict[str, list[str]] = {}
        self.children: list[SgfNode] = []
        self.parent: SgfNode | None = parent
        # Navigation state, not SGF content (never serialized): the child the
        # user last continued into from here, so pressing "forward" returns to
        # the variation they were exploring instead of always taking the first.
        # Held as a node, not an index, so it survives the variation being
        # reordered — and it dies with the node, unlike a map keyed by id().
        self.remembered_child: SgfNode | None = None

    # -- property access ---------------------------------------------------

    def get(self, ident: str) -> list[str]:
        """Return the list of values for ``ident`` (empty list if absent)."""
        return self.props.get(ident, [])

    def get_one(self, ident: str) -> str | None:
        """Return the first value for ``ident``, or ``None`` if absent/empty."""
        vals = self.props.get(ident)
        if not vals:
            return None
        return vals[0]

    def has(self, ident: str) -> bool:
        return ident in self.props

    def set(self, ident: str, values: list[str]) -> None:
        """Set ``ident`` to ``values`` (replacing any existing values).

        Setting to an empty list removes the property entirely, *except* that a
        single empty-string value (``["" ]``) is preserved because some
        properties are meaningfully present-but-empty (e.g. ``VW[]``, ``C[]``).
        """
        if not values:
            self.props.pop(ident, None)
        else:
            self.props[ident] = list(values)

    def set_one(self, ident: str, value: str) -> None:
        self.props[ident] = [value]

    def add_value(self, ident: str, value: str) -> None:
        self.props.setdefault(ident, []).append(value)

    def remove(self, ident: str) -> None:
        self.props.pop(ident, None)

    # -- tree structure ----------------------------------------------------

    def add_child(self, child: SgfNode | None = None, index: int | None = None) -> SgfNode:
        """Create (or attach) a child node and return it."""
        if child is None:
            child = SgfNode(parent=self)
        else:
            child.parent = self
        if index is None:
            self.children.append(child)
        else:
            self.children.insert(index, child)
        return child

    def detach(self) -> None:
        """Remove this node (and its subtree) from its parent."""
        if self.parent is not None:
            self.parent.children.remove(self)
            self.parent = None

    def clone(self, parent: SgfNode | None = None) -> SgfNode:
        """Return a deep copy of this node and its whole subtree.

        Iterative (like :meth:`walk`): a long game record easily exceeds the
        recursion limit, and clone is reachable from the UI (subtree copy,
        cross-tab paste).
        """
        def shallow(src: SgfNode, par: SgfNode | None) -> SgfNode:
            node = SgfNode(par)
            node.props = {ident: list(values) for ident, values in src.props.items()}
            return node

        root = shallow(self, parent)
        stack = [(self, root)]
        while stack:
            src, dst = stack.pop()
            for child in src.children:
                copy = shallow(child, dst)
                dst.children.append(copy)
                stack.append((child, copy))
        return root

    def walk(self) -> Iterator[SgfNode]:
        """Yield this node and all descendants in depth-first order."""
        stack = [self]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(reversed(node.children))

    def main_line(self) -> Iterator[SgfNode]:
        """Yield this node then repeatedly its first child (the main variation)."""
        node: SgfNode | None = self
        while node is not None:
            yield node
            node = node.children[0] if node.children else None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        keys = ",".join(self.props)
        return f"<SgfNode props=[{keys}] children={len(self.children)}>"


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _decode_value(raw: str) -> str:
    """Undo SGF backslash escaping for the raw text between ``[`` and ``]``.

    ``\\`` followed by a newline (in any of the CR/LF combinations) is a soft
    line break and is removed. ``\\`` followed by any other character yields
    that character literally. Bare CR/LF and CRLF are normalised to ``\\n``.
    """
    out: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == "\\":
            i += 1
            if i >= n:
                break
            nxt = raw[i]
            if nxt in "\r\n":
                # Soft line break: consume the (possibly two-char) line ending.
                i += 1
                if i < n and raw[i] in "\r\n" and raw[i] != nxt:
                    i += 1
                continue
            out.append(nxt)
            i += 1
        elif ch == "\r":
            # Normalise CR and CRLF to a single LF.
            out.append("\n")
            i += 1
            if i < n and raw[i] == "\n":
                i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


class _Parser:
    def __init__(self, text: str):
        self.s = text
        self.i = 0
        self.n = len(text)

    def error(self, msg: str) -> SgfParseError:
        return SgfParseError(f"{msg} at offset {self.i}")

    def skip_ws(self) -> None:
        s, n = self.s, self.n
        while self.i < n and s[self.i].isspace():
            self.i += 1

    def parse_collection(self) -> list[SgfNode]:
        roots: list[SgfNode] = []
        self.skip_ws()
        while self.i < self.n:
            if self.s[self.i] == "(":
                roots.append(self.parse_gametree(parent=None))
                self.skip_ws()
            else:
                # Tolerate junk between/around game trees.
                self.i += 1
                self.skip_ws()
        if not roots:
            raise SgfParseError("no game tree found in input")
        return roots

    def parse_gametree(self, parent: SgfNode | None) -> SgfNode:
        """Parse one (arbitrarily nested) game tree, returning its first node.

        Iterative — variation nesting can track path depth in a dense tree, so
        an explicit frame stack replaces recursion. Each frame is a mutable
        ``[attach_to, first, prev]`` for one open ``(``: the node the tree
        attaches to, its first node, and the last node of its sequence (which
        both further nodes and child variations hang off).
        """
        assert self.s[self.i] == "("
        result: SgfNode | None = None
        frames: list[list] = []
        while True:
            self.skip_ws()
            ch = self.s[self.i] if self.i < self.n else None
            if ch == "(":
                self.i += 1  # consume '('
                if frames and frames[-1][2] is None:
                    # A variation before any node in the enclosing tree (e.g.
                    # "((;B))"): fabricate the enclosing tree's empty first
                    # node now so the variation has something to attach to.
                    node = SgfNode(parent=frames[-1][0])
                    frames[-1][1] = frames[-1][2] = node
                attach_to = frames[-1][2] if frames else parent
                frames.append([attach_to, None, None])
                continue
            frame = frames[-1]
            if ch == ";":
                # A node in the current tree's sequence. (This also tolerates a
                # node *after* a variation list — "(;A(;B);C)", produced by some
                # buggy exporters — by attaching it to the sequence as usual.)
                node = self.parse_node(frame[2] if frame[2] is not None else frame[0])
                if frame[1] is None:
                    frame[1] = node
                if frame[2] is not None:
                    frame[2].add_child(node)
                frame[2] = node
                continue
            # Close the current tree: ')' — or end of input (tolerated).
            if ch == ")":
                self.i += 1  # consume ')'
            elif ch is not None:
                raise self.error(f"expected ')' but found {ch!r}")
            first = frame[1]
            if first is None:
                # A game tree with no nodes (e.g. "()"). Represent as an empty
                # node so structure is preserved without crashing.
                first = SgfNode(parent=frame[0])
            frames.pop()
            if frames:
                frames[-1][2].add_child(first)
            else:
                result = first
                break
        return result

    def parse_node(self, parent: SgfNode | None) -> SgfNode:
        assert self.s[self.i] == ";"
        self.i += 1  # consume ';'
        node = SgfNode(parent=parent)
        self.skip_ws()
        while self.i < self.n:
            ch = self.s[self.i]
            if ch.isalpha():
                self.parse_property(node)
                self.skip_ws()
            else:
                break
        return node

    def parse_property(self, node: SgfNode) -> None:
        # Read the identifier: any run of letters; lower-case letters are
        # ignored (kept only as upper-case) per the spec.
        s, n = self.s, self.n
        ident_chars: list[str] = []
        while self.i < n and s[self.i].isalpha():
            c = s[self.i]
            if c.isupper():
                ident_chars.append(c)
            self.i += 1
        ident = "".join(ident_chars)
        self.skip_ws()
        values: list[str] = []
        while self.i < n and s[self.i] == "[":
            values.append(self.parse_value())
            self.skip_ws()
        if not ident:
            # A property with no upper-case letters in its identifier (e.g. all
            # lower-case junk). Drop the values but don't crash.
            return
        if not values:
            # Malformed: SGF requires at least one value. Drop the property
            # rather than fail the whole file — this parser's contract is
            # tolerance, and a stray `;W)` used to make the file unopenable.
            # Dropping (rather than recording `[""]`) matters for B/W, where an
            # empty value would silently invent a *pass*.
            return
        node.props.setdefault(ident, []).extend(values)

    def parse_value(self) -> str:
        assert self.s[self.i] == "["
        self.i += 1  # consume '['
        s, n = self.s, self.n
        start = self.i
        while self.i < n:
            ch = s[self.i]
            if ch == "\\":
                self.i += 2  # skip the escaped char (handled in _decode_value)
            elif ch == "]":
                raw = s[start:self.i]
                self.i += 1  # consume ']'
                return _decode_value(raw)
            else:
                self.i += 1
        raise self.error("unterminated property value (missing ']')")


def parse_collection(text: str) -> list[SgfNode]:
    """Parse SGF text into a list of root :class:`SgfNode` (one per game tree)."""
    # Strip a UTF-8 BOM if present.
    if text and text[0] == "﻿":
        text = text[1:]
    return _Parser(text).parse_collection()


# --------------------------------------------------------------------------
# Serialization
# --------------------------------------------------------------------------


def _encode_value(value: str) -> str:
    """Apply SGF escaping: backslash and close-bracket are escaped."""
    return value.replace("\\", "\\\\").replace("]", "\\]")


def _serialize_node(node: SgfNode) -> str:
    parts = [";"]
    for ident, values in node.props.items():
        parts.append(ident)
        if values:
            for v in values:
                parts.append("[")
                parts.append(_encode_value(v))
                parts.append("]")
        else:
            # Should not normally happen, but emit an empty value for safety.
            parts.append("[]")
    return "".join(parts)


def _serialize_tree(node: SgfNode, out: list[str]) -> None:
    # Iterative: branch nesting can track path depth in a dense tree, and this
    # runs on every edit (undo snapshots), so it must not hit the recursion
    # limit. A ``None`` on the stack closes the currently open game tree.
    stack: list[SgfNode | None] = [node]
    while stack:
        item = stack.pop()
        if item is None:
            out.append(")")
            continue
        out.append("(")
        stack.append(None)
        cur = item
        while True:
            out.append(_serialize_node(cur))
            nchildren = len(cur.children)
            if nchildren == 1:
                cur = cur.children[0]
            else:
                if nchildren > 1:
                    stack.extend(reversed(cur.children))
                break


def serialize_collection(roots: list[SgfNode], newline: str = "\n") -> str:
    """Serialize root nodes back into SGF text (one game tree per root)."""
    out: list[str] = []
    for root in roots:
        _serialize_tree(root, out)
        out.append(newline)
    return "".join(out)
