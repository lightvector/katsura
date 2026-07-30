"""A Document: an open SGF file (a collection of game trees) plus edit state.

A document owns the parsed collection, the active :class:`Game`, the file path,
a dirty flag, and an undo/redo history. Undo snapshots are serialized SGF plus
the path (child indices) to the current node, which makes them simple and
guaranteed-consistent at the cost of a reparse on undo (negligible for normal
files).

**Cursor placement.** A snapshot's ``path`` is where the cursor goes when that
state is restored, and its ``counter_path`` is where the cursor goes when the
*opposite* operation restores the other side of the same edit. So each undo
entry carries the pre-edit position (restored by undo) *and* the post-edit
position (used by redo); the two swap on every undo/redo, which keeps them in
step however deep the stacks get. The point is that redo lands you where the
edit left you — the node it created, the parent a deletion fell back to, the
node whose comment you typed — rather than wherever you had navigated to
afterwards, which may be somewhere completely unrelated.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from ..model.game import Game
from ..sgf.tree import SgfNode, parse_collection, serialize_collection


def _path_indices(game: Game) -> list[int]:
    """The list of child indices from root to the current node."""
    indices: list[int] = []
    node = game.current
    while node.parent is not None:
        indices.append(node.parent.children.index(node))
        node = node.parent
    indices.reverse()
    return indices


def _follow_indices(root: SgfNode, indices: list[int]) -> SgfNode:
    node = root
    for i in indices:
        if 0 <= i < len(node.children):
            node = node.children[i]
        else:
            break
    return node


@dataclass
class _Snapshot:
    sgf: str
    path: list[int]                          # cursor to restore with this state
    active_index: int
    # Cursor for the *other* end of this edit (see the module docstring). None
    # on entries recorded by push_undo(), where the post-edit position is not
    # known yet; undo then falls back to the live cursor.
    counter_path: Optional[list[int]] = None


_untitled_counter = 0


def _next_untitled_name() -> str:
    global _untitled_counter
    _untitled_counter += 1
    return f"Untitled{_untitled_counter}"


@dataclass
class Document:
    """An open file and its editing state."""

    roots: list[SgfNode]
    game: Game
    active_index: int = 0
    path: Optional[str] = None
    dirty: bool = False
    _undo: list[_Snapshot] = field(default_factory=list)
    _redo: list[_Snapshot] = field(default_factory=list)
    untitled_name: Optional[str] = None

    # -- construction ------------------------------------------------------

    @classmethod
    def new(cls, width: int = 19, height: Optional[int] = None) -> "Document":
        game = Game.new(width, height)
        return cls(roots=[game.root], game=game,
                   untitled_name=_next_untitled_name())

    @classmethod
    def open(cls, path: str) -> "Document":
        with open(path, "rb") as fh:
            raw = fh.read()
        text = _decode_bytes(raw)
        roots = parse_collection(text)
        game = Game(roots[0])
        return cls(roots=roots, game=game, path=path)

    @classmethod
    def open_sgfs(cls, path: str) -> tuple[list["Document"], int]:
        """Load a KataGo-style ``.sgfs`` file: one newline-free SGF per line.

        Returns ``(documents, failed)`` where ``failed`` counts non-blank
        lines that did not parse. Any line-ending style, blank lines and
        trailing newlines are tolerated. The documents are deliberately
        pathless (saving acts as Save As — there is no way to save back into
        the ``.sgfs``) and are titled ``<stem>[<i>]`` with ``i`` the 1-based
        index among the file's non-blank lines.
        """
        with open(path, "rb") as fh:
            raw = fh.read()
        text = _decode_bytes(raw)
        stem = os.path.splitext(os.path.basename(path))[0]
        docs: list[Document] = []
        failed = 0
        lines = [ln for ln in text.splitlines() if ln.strip()]
        for i, line in enumerate(lines, 1):
            try:
                roots = parse_collection(line)
            except Exception:
                failed += 1
                continue
            docs.append(cls(roots=roots, game=Game(roots[0]),
                            untitled_name=f"{stem}[{i}]"))
        return docs, failed

    # -- display -----------------------------------------------------------

    @property
    def title(self) -> str:
        if self.path:
            name = os.path.basename(self.path)
        else:
            name = self.untitled_name or "Untitled"
        return ("* " + name) if self.dirty else name

    # -- saving ------------------------------------------------------------

    def to_sgf(self) -> str:
        return serialize_collection(self.roots)

    def save(self, path: Optional[str] = None) -> None:
        target = path or self.path
        if target is None:
            raise ValueError("no path to save to")
        with open(target, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(self.to_sgf())
        self.path = target
        self.dirty = False

    # -- undo / redo -------------------------------------------------------

    def _snapshot(self) -> _Snapshot:
        return _Snapshot(
            sgf=serialize_collection(self.roots),
            path=_path_indices(self.game),
            active_index=self.active_index,
        )

    def begin_edit(self) -> _Snapshot:
        """Capture the pre-edit state for a *speculative* edit.

        Nothing is recorded (and the redo stack is untouched) until the caller
        passes the returned snapshot to :meth:`commit_edit`; if the edit turns
        out to be a no-op, simply drop the snapshot. Snapshots are independent
        tokens, so overlapping sessions (e.g. a shortcut-triggered edit while a
        comment-editing session is open) commit or discard only themselves.
        """
        return self._snapshot()

    def commit_edit(self, snap: _Snapshot) -> None:
        """Record a completed edit: its pre-edit snapshot becomes undoable.

        Called once the edit has been applied, so the cursor is wherever the
        edit itself left it (the new move's node, the parent a delete fell back
        to, …). That position is stored as the snapshot's ``counter_path`` and is
        where redoing this edit will put you.
        """
        snap.counter_path = _path_indices(self.game)
        self._undo.append(snap)
        self._redo.clear()
        # Cap history to keep memory bounded.
        if len(self._undo) > 200:
            self._undo.pop(0)

    def push_undo(self) -> None:
        """Record the current state so the *next* edit can be undone.

        For edits known to happen unconditionally; speculative edits should
        use :meth:`begin_edit` / :meth:`commit_edit` so an abandoned edit
        cannot clear the redo stack. Because this runs *before* the edit, the
        post-edit cursor is unknown and redo falls back to wherever the cursor
        is when the edit is undone — prefer begin_edit/commit_edit when the edit
        moves the cursor.
        """
        snap = self._snapshot()
        self.commit_edit(snap)
        # commit_edit stamps the post-edit cursor, but we are still *pre*-edit
        # here, so drop the stamp rather than record a wrong one.
        snap.counter_path = None

    def mark_dirty(self) -> None:
        self.dirty = True

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def _restore(self, snap: _Snapshot) -> None:
        self.roots = parse_collection(snap.sgf)
        self.active_index = min(snap.active_index, len(self.roots) - 1)
        self.game = Game(self.roots[self.active_index])
        self.game.goto(_follow_indices(self.game.root, snap.path))

    def _counterpart(self, entry: _Snapshot) -> _Snapshot:
        """Snapshot of the *current* state, to be pushed on the opposite stack.

        Its cursor is ``entry.counter_path`` — the other end of this same edit —
        and its own counterpart is ``entry.path``, so the pair keeps swapping as
        the user undoes and redoes.
        """
        return _Snapshot(
            sgf=serialize_collection(self.roots),
            path=(entry.counter_path if entry.counter_path is not None
                  else _path_indices(self.game)),
            active_index=self.active_index,
            counter_path=entry.path,
        )

    def undo(self) -> bool:
        if not self._undo:
            return False
        entry = self._undo.pop()
        self._redo.append(self._counterpart(entry))
        self._restore(entry)
        self.dirty = True
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        entry = self._redo.pop()
        self._undo.append(self._counterpart(entry))
        self._restore(entry)
        self.dirty = True
        return True

    # -- multiple game trees ----------------------------------------------

    def switch_game(self, index: int) -> None:
        if 0 <= index < len(self.roots):
            self.active_index = index
            self.game = Game(self.roots[index])


def _decode_bytes(raw: bytes) -> str:
    """Decode SGF bytes, honouring a leading BOM or a CA[...] charset hint.

    Defaults to UTF-8 (the modern default), falling back to latin-1 (which
    cannot fail) so that even mis-encoded files load rather than crash.
    """
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    # Peek for a CA[...] charset declaration in the ASCII-safe prefix.
    head = raw[:2000].decode("ascii", errors="ignore")
    charset = None
    idx = head.find("CA[")
    if idx != -1:
        end = head.find("]", idx + 3)
        if end != -1:
            charset = head[idx + 3:end].strip()
    for enc in filter(None, [charset, "utf-8"]):
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("latin-1")
