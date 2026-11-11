"""Path scoping — the security boundary of this service.

Notes is unauthenticated: whatever reverse proxy sits in front of it is the
perimeter. It takes a note id straight from the client, and that id is a
space-relative path, so every request is a path-traversal attempt waiting to
happen. Everything that turns client input into a filesystem path goes through
``resolve`` here, and ``tests/test_paths.py`` is the corpus that keeps it
honest.

Three independent conditions, all required:
  1. the resolved path stays inside the space (defeats ``..`` and symlinks),
  2. it sits under one of the roots this app owns (so a traversal that lands
     on Journal/ or System/ is still refused — the agent's pages are not
     Notes's to rewrite),
  3. it ends in ``.md``.

Resolution is done with ``Path.resolve()`` on the *joined* path, so a symlink
inside the space pointing out of it is caught by (1) rather than trusted.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

# Directories whose markdown files are cards. Everything else in the space —
# Journal/ (the daybook), Areas/ (newsdesk), Projects/, System/, Library/ — is
# machine-written and document-shaped; it is deliberately not reachable here.
CARD_DIRS: tuple[str, ...] = ("Keep", "Notes", "Inbox")

# Individual pages outside those directories that should still be cards.
CARD_FILES: tuple[str, ...] = ("Grocery List.md",)

# Subtrees inside a card dir that are not cards. Inbox/Log is the verbatim
# capture log the daybook appends to — a growing record, not a note. Each entry
# excludes the directory, its contents, AND the sibling index page
# ("Inbox/Log.md"), which is a real file in the space.
EXCLUDE_PREFIXES: tuple[str, ...] = ("Inbox/Log", "Notes/People")

# Who-is-who lives in the space like everything else (see people.py). It sits
# inside a card dir so the existing scoping already permits writing it, and in
# EXCLUDE_PREFIXES above so it never appears as a card.
PEOPLE_FILE = "Notes/People.md"

# Deleted notes are moved here rather than unlinked, so the UI's undo is real
# and a mistake is recoverable from the space itself.
TRASH_DIR = "Trash"


class PathError(ValueError):
    """Raised when a client-supplied id does not name a note this app owns."""


def _norm(rel: str) -> PurePosixPath:
    if not rel or "\x00" in rel:
        raise PathError("empty or null-bearing path")
    p = PurePosixPath(rel)
    if p.is_absolute():
        raise PathError("absolute paths are not note ids")
    if any(part == ".." for part in p.parts):
        raise PathError("parent traversal is not allowed")
    return p


def in_scope(rel: str, *, allow_trash: bool = False) -> bool:
    """True when ``rel`` names a path this app is allowed to touch."""
    try:
        p = _norm(rel)
    except PathError:
        return False
    posix = p.as_posix()
    if p.suffix != ".md":
        return False
    for bad in EXCLUDE_PREFIXES:
        if posix == bad or posix == bad + ".md" or posix.startswith(bad + "/"):
            return False
    if posix in CARD_FILES:
        return True
    head = p.parts[0] if p.parts else ""
    if head in CARD_DIRS:
        return True
    if allow_trash and head == TRASH_DIR:
        return True
    return False


def resolve(space: Path, rel: str, *, allow_trash: bool = False) -> Path:
    """Map a client-supplied note id onto a real path inside ``space``.

    Raises ``PathError`` unless all three scope conditions hold.
    """
    p = _norm(rel)
    if not in_scope(p.as_posix(), allow_trash=allow_trash):
        raise PathError(f"out of scope: {rel}")

    space_real = space.resolve()
    target = (space_real / p).resolve()
    # strict=False resolution above means a not-yet-created file is fine; what
    # matters is where the path LANDS, symlinks included.
    if target != space_real and space_real not in target.parents:
        raise PathError(f"escapes the space: {rel}")
    return target


def relative(space: Path, path: Path) -> str:
    """The note id for a path already known to be inside the space."""
    return path.resolve().relative_to(space.resolve()).as_posix()
