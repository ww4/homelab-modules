"""Reading and writing the card subset of a folder of markdown notes.

Two rules shape everything here:

**Don't reformat the owner's files.** The space is edited by at least three other
writers (SilverBullet itself, the daybook, interactive agent sessions), and
539 of the Keep-import pages carry hand-written frontmatter. So frontmatter is
edited LINE BY LINE — the keys this app owns (``pinned``, ``archived``) are
replaced or appended in place and every other line is passed through verbatim.
A yaml.safe_load/safe_dump round trip would be shorter and would quietly
restyle quoting, key order and dates across 500+ files on first write.

**Deletion moves, it doesn't unlink.** ``trash`` relocates a note under
``Trash/`` so the UI's undo restores a real file, and a mis-tap is recoverable
without reaching for the space's git log.
"""

from __future__ import annotations

import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass, asdict
from datetime import date, datetime
from pathlib import Path

from . import paths

FM_DELIM = "---"
H1 = re.compile(r"^#\s+(.*\S)\s*$")
LEADING_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}[ _-]+")
TRUE = {"true", "yes", "1", "on"}


# Authorship is recorded ONLY on the agent's own notes. Unlabelled means
# the owner's, which is the asymmetry that matters: those 553 notes keep their files
# byte-identical, anything created in this app is theirs by construction, and the
# agent is the one party that has to declare itself. A text heuristic was tried
# first and misfiled "Queue replies — server side" as theirs, which is exactly the
# kind of quiet wrongness a default filter must not have.
AGENT = "claude"
# The author a note is attributed to when its frontmatter says nothing. Every
# note written before authorship existed is unlabelled, so this is not a
# cosmetic default: changing it re-attributes all of them. Set it once, at
# deploy time, via NOTES_DEFAULT_AUTHOR, and then leave it alone.
HUMAN = "owner"

# Enough to overflow a card at the widest column; the wall clips at ~184px,
# which is about twelve lines. 700 was generous guesswork and, at 1412 notes,
# the difference between a 773 KB listing and a 500 KB one.
PREVIEW_CHARS = 500


@dataclass
class Note:
    id: str
    title: str
    body: str
    cat: str
    date: str
    pinned: bool            # pinned by anyone (coarse; the viewer asks pinned_by)
    pinned_by: list[str]
    pin_order: dict[str, int]
    archived: bool          # legacy/global: archived for everyone
    archived_by: list[str]  # and per person, which is what a shared note needs
    shared: bool
    filed: bool
    author: str
    had_h1: bool

    def public(self) -> dict:
        d = asdict(self)
        d.pop("had_h1")
        return d

    def summary(self) -> dict:
        """What the wall needs: enough body to fill a card, and nothing more.

        A card clips at a fixed height, so shipping whole notes just to render
        previews cost 546 KB for 586 notes -- 422 KB of it body text the wall
        never shows, including one 49 KB page. The editor fetches the real body
        when a note is actually opened.
        """
        d = self.public()
        body = d.pop("body")
        d["preview"] = body[:PREVIEW_CHARS]
        d["lines"] = sum(1 for line in body.split("\n") if line.strip())
        d["truncated"] = len(body) > PREVIEW_CHARS
        return d


def _split_frontmatter(text: str) -> tuple[list[str], str]:
    """Return (frontmatter lines without delimiters, remaining body)."""
    if not text.startswith(FM_DELIM):
        return [], text
    lines = text.split("\n")
    if lines[0].strip() != FM_DELIM:
        return [], text
    for i in range(1, len(lines)):
        if lines[i].strip() == FM_DELIM:
            return lines[1:i], "\n".join(lines[i + 1:])
    return [], text  # unterminated: treat the whole thing as body


def _fm_get(fm: list[str], key: str) -> str | None:
    pat = re.compile(rf"^{re.escape(key)}\s*:\s*(.*)$", re.IGNORECASE)
    for line in fm:
        m = pat.match(line)
        if m:
            return m.group(1).strip().strip("'\"")
    return None


def _fm_set(fm: list[str], key: str, value: str | None) -> list[str]:
    """Replace, append, or (value=None) drop a single key, leaving the rest."""
    pat = re.compile(rf"^{re.escape(key)}\s*:", re.IGNORECASE)
    out = [line for line in fm if not pat.match(line)]
    if value is not None:
        out.append(f"{key}: {value}")
    return out


# A Keep note's title is OPTIONAL, and the other person mostly leaves it off. Those notes
# still need a unique FILENAME, so the import uses "<date> <keep_id>" — a
# uniqueness device, not a title. Deriving a title from it would put a
# timestamp at the top of 347 of those cards.
KEEP_ID_STEM = re.compile(r"^\d{10,}$")


def _title_from_name(rel: str) -> str:
    stem = Path(rel).stem
    rest = LEADING_DATE.sub("", stem).strip()
    if KEEP_ID_STEM.match(rest):
        return ""
    return rest or stem


def _int_or_none(v: str | None) -> int | None:
    try:
        return int((v or "").strip())
    except ValueError:
        return None


def _actor(actor: str | None) -> str:
    return re.sub(r"[^a-z0-9_-]+", "", (actor or HUMAN).strip().lower())[:32] or HUMAN


def _people(v: str | None) -> list[str]:
    return [w for w in re.split(r"[,\s]+", (v or "")) if w]


def _order_map(v: str | None) -> dict[str, int]:
    """"sam=10, ana=30" — a manual pin position PER PERSON.

    One key rather than order_sam/order_ana, so adding a third person does
    not add a third frontmatter key to every note.
    """
    out: dict[str, int] = {}
    for part in re.split(r"[,\s]+", (v or "")):
        if "=" not in part:
            continue
        who, _, num = part.partition("=")
        try:
            out[who.strip().lower()] = int(num)
        except ValueError:
            continue
    return out


def _pinned_any(fm: list[str]) -> bool:
    if (_fm_get(fm, "pinned") or "").lower() in TRUE:
        return True
    return bool(_people(_fm_get(fm, "pinned_by")))


def _archived_any(fm: list[str]) -> bool:
    """Archived by anyone — the legacy global flag, or any named person.

    Per-VIEWER archiving is the client's question (is my name on it?); this is
    the coarse "has anyone put this away" used for counting and for the
    pre-identity notes that carry a bare archived: true.
    """
    if (_fm_get(fm, "archived") or "").lower() in TRUE:
        return True
    return bool([w for w in re.split(r"[,\s]+", (_fm_get(fm, "archived_by") or "")) if w])


def _category(rel: str, fm: list[str]) -> str:
    explicit = _fm_get(fm, "category")
    if explicit:
        return explicit
    head = Path(rel).parts[0] if len(Path(rel).parts) > 1 else ""
    return head.lower() if head else "list"


def read(space: Path, rel: str, *, allow_trash: bool = False) -> Note:
    path = paths.resolve(space, rel, allow_trash=allow_trash)
    raw = path.read_text(encoding="utf-8", errors="replace")
    fm, body = _split_frontmatter(raw)

    had_h1 = False
    title = _title_from_name(rel)
    stripped = body.lstrip("\n")
    first, _, rest = stripped.partition("\n")
    m = H1.match(first)
    if m:
        had_h1 = True
        title = m.group(1)
        body = rest

    when = _fm_get(fm, "date")
    if not when:
        when = date.fromtimestamp(path.stat().st_mtime).isoformat()

    return Note(
        id=rel,
        title=title,
        body=body.strip("\n"),
        cat=_category(rel, fm),
        # Whether the category was CHOSEN or just inherited from the folder.
        # Without this "uncategorised" is invisible: an unfiled Keep note
        # reads as category "keep", indistinguishable from one filed there.
        filed=bool(_fm_get(fm, "category")),
        date=when[:10],
        pinned=_pinned_any(fm),
        pinned_by=_people(_fm_get(fm, "pinned_by")),
        pin_order=_order_map(_fm_get(fm, "pin_order")),
        archived=_archived_any(fm),
        archived_by=[w for w in re.split(r"[,\s]+", (_fm_get(fm, "archived_by") or "")) if w],
        shared=(_fm_get(fm, "shared") or "").lower() in TRUE,
        author=(_fm_get(fm, "author") or HUMAN).strip().lower(),
        had_h1=had_h1,
    )


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file in the same directory, then rename.

    Same-directory is deliberate: the space is one filesystem, so os.replace is
    atomic, and a reader (SilverBullet's indexer, the agent, a git autosave)
    never sees a half-written page.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".notes-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o7777)
        else:
            os.chmod(tmp, 0o660)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def write(
    space: Path,
    rel: str,
    *,
    title: str | None = None,
    body: str | None = None,
    pinned: bool | None = None,
    archived: bool | None = None,
    shared: bool | None = None,
    order: int | None = None,
    actor: str | None = None,
    category: str | None = None,
    author: str | None = None,
) -> Note:
    path = paths.resolve(space, rel)
    raw = path.read_text(encoding="utf-8", errors="replace")
    fm, rest = _split_frontmatter(raw)
    current = read(space, rel)

    new_title = current.title if title is None else title.strip()
    # An untitled note stays untitled: no heading is invented for it.
    had_h1 = bool(new_title) and (current.had_h1 or new_title != _title_from_name(rel))

    if pinned is not None:
        # A PIN IS PERSONAL, for the same reason archiving is: it means "keep
        # this at the top FOR ME", which is not a claim about anyone else. On a
        # shared list, one person pinning it used to pin it for the other too.
        act = _actor(actor)
        now = list(current.pinned_by)
        if current.pinned and not now:
            now = sorted({current.author, HUMAN})      # an old global pin
        if pinned and act not in now:
            now.append(act)
        if not pinned and act in now:
            now.remove(act)
        fm = _fm_set(fm, "pinned", None)
        fm = _fm_set(fm, "pinned_by", ", ".join(sorted(set(now))) if now else None)
        if not pinned:
            # Unpinning drops this person's manual position with it; it means
            # nothing once the note is off their pinned row.
            om = dict(current.pin_order)
            om.pop(act, None)
            fm = _fm_set(fm, "pin_order",
                         ", ".join(f"{k}={v}" for k, v in sorted(om.items())) or None)
    if archived is not None:
        # ARCHIVE IS PERSONAL. The question was what happens when you archive a
        # SHARED note, and the honest answer was "it archives for the other person too" —
        # the flag lived on the one file they both read. Archive means "I am
        # done with this", which is not a claim about anyone else, so it
        # records WHO, and a viewer sees it archived only if they are named.
        #
        # (Trash stays global, and has to: it MOVES the file. A shared note
        # either of them deletes is gone for both. Said plainly, not faked.)
        act = _actor(actor)
        now = list(current.archived_by)
        if current.archived and not now:
            # An old GLOBAL flag meant archived, full stop. Keep that true for
            # everyone else while this person changes only their own view.
            now = sorted({current.author, HUMAN})
        if archived and act not in now:
            now.append(act)
        if not archived and act in now:
            now.remove(act)
        fm = _fm_set(fm, "archived", None)
        fm = _fm_set(fm, "archived_by", ", ".join(sorted(set(now))) if now else None)
    if order is not None:
        # Manual position, ONLY for pinned notes and ONLY for this person —
        # otherwise the owner arranging their pinned row would rearrange the other person's.
        act = _actor(actor)
        om = dict(current.pin_order)
        if order >= 0:
            om[act] = order
        else:
            om.pop(act, None)
        fm = _fm_set(fm, "order", None)        # retire the global key
        fm = _fm_set(fm, "pin_order",
                     ", ".join(f"{k}={v}" for k, v in sorted(om.items())) or None)
    if shared is not None:
        # "Shared" is a PLACE a note is put, like archived -- not a computed
        # union of everyone's notes. The ask was "I want that to be a shared space
        # we can intentionally add things to". Ownership is untouched by it:
        # a shared note still belongs to whoever wrote it.
        fm = _fm_set(fm, "shared", "true" if shared else None)
    if author is not None:
        # Reassigning WHOSE note this is. Deliberately separate from writing
        # the body: an edit never changes authorship -- the other person adding a line to
        # a shared list does not take it over -- so the only way ownership
        # moves is somebody saying so.
        who = re.sub(r"[^a-z0-9_-]+", "", author.strip().lower())[:32]
        fm = _fm_set(fm, "author", who if who and who != HUMAN else None)
    if category is not None:
        # Empty string clears it, which falls the note back to the directory
        # default rather than writing an empty key.
        clean = re.sub(r"[^A-Za-z0-9 _-]+", "", category).strip().lower()[:40]
        fm = _fm_set(fm, "category", clean.replace(" ", "-") or None)

    out: list[str] = []
    if fm:
        out.append(FM_DELIM)
        out.extend(fm)
        out.append(FM_DELIM)
        out.append("")
    if had_h1:
        out.append(f"# {new_title}")
        out.append("")
    new_body = current.body if body is None else body
    out.append(new_body.strip("\n"))
    _atomic_write(path, "\n".join(out).rstrip("\n") + "\n")
    return read(space, rel)


def _slug(title: str) -> str:
    s = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9 _-]+", "", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s[:60] or "note"


def create(space: Path, title: str, body: str, author: str | None = None) -> Note:
    stamp = datetime.now().strftime("%Y-%m-%d")
    # An untitled note needs a unique filename but must not acquire a title
    # from it, so it uses the same all-digits stem the Keep import uses —
    # which _title_from_name reads back as "no title" rather than as "note".
    base = (f"Inbox/{stamp} {_slug(title)}" if title.strip()
            else f"Inbox/{stamp} {int(datetime.now().timestamp() * 1_000_000)}")
    rel = f"{base}.md"
    n = 2
    while paths.resolve(space, rel).exists():
        rel = f"{base} ({n}).md"
        n += 1
    path = paths.resolve(space, rel)
    out: list[str] = []
    # Only write the key when the device said who it is. No key reads as the owner,
    # which is what every note predating device identity is.
    if author and author != HUMAN:
        out += ["---", f"author: {author}", "---", ""]
    # A title is optional here too — no "Untitled" is invented for a note that
    # is just a thought and a body.
    if title.strip():
        out += [f"# {title.strip()}", ""]
    out += [body.strip("\n")]
    _atomic_write(path, "\n".join(out).rstrip("\n") + "\n")
    return read(space, rel)


def trash(space: Path, rel: str) -> str:
    """Move a note under Trash/, preserving its path. Returns the new id."""
    src = paths.resolve(space, rel)
    dest_rel = f"{paths.TRASH_DIR}/{rel}"
    dest = paths.resolve(space, dest_rel, allow_trash=True)
    n = 2
    while dest.exists():
        dest_rel = f"{paths.TRASH_DIR}/{Path(rel).with_suffix('')} ({n}).md"
        dest = paths.resolve(space, dest_rel, allow_trash=True)
        n += 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dest)
    return dest_rel


def restore(space: Path, trash_rel: str) -> str:
    """Move a trashed note back where it came from. Returns the restored id."""
    src = paths.resolve(space, trash_rel, allow_trash=True)
    parts = Path(trash_rel).parts
    if not parts or parts[0] != paths.TRASH_DIR:
        raise paths.PathError("not a trashed note")
    dest_rel = Path(*parts[1:]).as_posix()
    dest = paths.resolve(space, dest_rel)
    if dest.exists():
        raise paths.PathError("a note already lives at that path")
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dest)
    return dest_rel


# Parsed notes, keyed by path and invalidated by (mtime_ns, size). FOUR parties
# write this space -- this service, SilverBullet, the daybook and interactive
# agent sessions -- so the key is the file's own identity rather than a
# timestamp of our own. A change by any of them invalidates the entry exactly
# as a change by this service does.
_CACHE: dict[str, tuple[int, int, Note]] = {}


def _cached(space: Path, rel: str, st: os.stat_result) -> Note:
    hit = _CACHE.get(rel)
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    note = read(space, rel)
    _CACHE[rel] = (st.st_mtime_ns, st.st_size, note)
    return note


def _walk(space: Path):
    for d in paths.CARD_DIRS:
        root = space / d
        if not root.is_dir():
            continue
        for p in root.rglob("*.md"):
            rel = paths.relative(space, p)
            if paths.in_scope(rel):
                yield rel, p
    for f in paths.CARD_FILES:
        p = space / f
        if p.is_file():
            yield f, p


def _word(hay: str, term: str) -> bool:
    return re.search(r"\b" + re.escape(term) + r"\b", hay) is not None


# A term long enough that matching the FRONT of a category name is meaningful.
# "hand" -> handsaw is a real intent; "saw" -> sawdust-anything is not, and a
# 3-letter prefix would drag whole categories in by accident.
CAT_PREFIX_MIN = 4


def split_query(query: str | None) -> tuple[list[str], list[str], list[str]]:
    """Split a raw query into (cat: filters, by: filters, ordinary terms).

    ``cat:handsaw blade`` means "inside the handsaw category, anything about
    trucks", and ``by:ana`` narrows to what the other person wrote -- the prefixed parts
    are filters, the rest is the usual ranked OR.
    """
    cats: list[str] = []
    whos: list[str] = []
    terms: list[str] = []
    for tok in (query or "").lower().split():
        if tok.startswith("cat:") and len(tok) > 4:
            cats.append(tok[4:].strip())
        elif tok.startswith("by:") and len(tok) > 3:
            whos.append(tok[3:].strip())
        elif tok:
            terms.append(tok)
    return cats, whos, terms


def _cat_terms(note: Note, needles: list[str]) -> set[str]:
    """Which query terms this note's CATEGORY answers.

    The ask was "if the category name is a high grade match for the search term you
    should pull in the whole category." So a term that names a category counts
    as satisfied for every note in it, even one whose text never says the word
    -- searching handsaw should return the handsaw notes, not just the ones
    that happen to spell it out.
    """
    cat = (note.cat or "").lower()
    if not cat:
        return set()
    parts = set(cat.split("-"))
    out = set()
    for t in needles:
        if t == cat or t in parts or (len(t) >= CAT_PREFIX_MIN and cat.startswith(t)):
            out.add(t)
    return out


def _score(note: Note, needles: list[str]) -> tuple[int, int, int]:
    """How well a note answers the query. Bigger is better.

    (terms matched anywhere, terms matched as whole words, terms in the title)

    Substring matching is kept -- it is what Keep does, and dropping it would
    lose real hits -- but on this corpus "gas" also matches gasket, gasoline
    and gasbuddy, and "wood" matches plywood, firewood and Woodstock. Measured
    on the live space, "wood gas" is 90 notes by substring and 41 by whole
    word. Rather than choose, rank: whole-word hits rise to the top and the
    in-word ones become a tail you never scroll to.
    """
    title = note.title.lower()
    hay = title + "\n" + note.body.lower()
    cats = _cat_terms(note, needles)
    # A category match is high-grade by definition, so it counts as a
    # whole-word hit rather than a weak substring one.
    anywhere = sum(1 for t in needles if t in hay or t in cats)
    whole = sum(1 for t in needles if _word(hay, t) or t in cats)
    in_title = sum(1 for t in needles if t in title)
    return anywhere, whole, in_title


def authors_of(notes: list[Note]) -> dict[str, int]:
    out: dict[str, int] = {}
    for n in notes:
        out[n.author] = out.get(n.author, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def census_of(
    notes: list[Note],
    scope: str = "all",
    me: str = HUMAN,
    show_shared: bool = True,
) -> dict[str, int]:
    out: dict[str, int] = {}
    for n in notes:
        if in_feed(n, scope, me, show_shared):
            out[n.cat] = out.get(n.cat, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def authors(space: Path) -> dict[str, int]:
    """Who has written in this space, and how much.

    Drives the by: suggestions and the Mine/Ours/Claude control, so it has to
    be the real set rather than a hardcoded list of people.
    """
    out: dict[str, int] = {}
    for n in listing(space):
        out[n.author] = out.get(n.author, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def trash_listing(space: Path) -> list[Note]:
    """Everything sitting in Trash/, newest first.

    Delete has always MOVED a note here rather than unlinking it, which is why
    a note deleted by accident on 2026-11-09 was recoverable at all. What was
    missing was any way for the owner to do that without an agent and a
    shell — so the trash is a view now.
    """
    out: list[Note] = []
    root = space / paths.TRASH_DIR
    if not root.is_dir():
        return out
    for p in sorted(root.rglob("*.md")):
        rel = paths.relative(space, p)
        if paths.in_scope(rel, allow_trash=True):
            try:
                out.append(read(space, rel, allow_trash=True))
            except OSError:
                continue
    out.sort(key=lambda n: (n.date, n.id), reverse=True)
    return out


def in_feed(n: Note, scope: str, me: str, show_shared: bool) -> bool:
    """Is this note in the given feed? One definition, used by chips and wall.

    "mine"   your own notes, plus the shared space if you keep it in your feed
    "shared" the shared space itself
    "all"    literally everything, agent notes included
    """
    if scope == "all":
        return True
    if scope == "shared":
        return n.shared
    return n.author == me or (show_shared and n.shared)


def census(
    space: Path,
    scope: str = "all",
    me: str = HUMAN,
    show_shared: bool = True,
) -> dict[str, int]:
    """Categories and counts FOR THE CURRENT FEED.

    Scoped rather than global because a count that includes notes you are not
    looking at is a lie: with the wall set to "mine", a chip reading 83 that
    yields 4 notes is worse than no chip at all (decided 2026-11-09).

    Deliberately NOT narrowed by the search query — a category has to stay
    suggestible while you are typing its name.
    """
    out: dict[str, int] = {}
    for n in listing(space):
        if in_feed(n, scope, me, show_shared):
            out[n.cat] = out.get(n.cat, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def listing(space: Path, query: str | None = None) -> list[Note]:
    """All cards, newest first; with a query, only those that match.

    Search runs HERE, over the whole body, because the wall is only sent
    previews. Matching client-side against a truncated preview would miss hits
    inside long notes and present as "no results" -- a silent wrong answer
    rather than a slow one.
    """
    # ANY term, substring, ranked -- which is what Keep does, and what the owner
    # expects from it. Two earlier attempts were both wrong:
    #   literal phrase : "wood gas" found nothing, because no note contains
    #                    that exact run -- they say "handsaw", or have "hand"
    #                    on one line and "gas" on another;
    #   AND of terms   : still missed notes that say only "wood", or only
    #                    "gas", which Keep returns.
    # Keep's own behaviour is visible in a screenshot of it searching
    # "food lion": a note with neither word in full came back because "lion"
    # appears inside "million". So: substring, OR, and let the ranking decide
    # what rises -- a note matching both terms outranks one matching either.
    cat_filters, who_filters, needles = split_query(query)
    out: list[Note] = []
    seen: set[str] = set()
    for rel, p in _walk(space):
        if rel in seen:
            continue
        seen.add(rel)
        try:
            st = p.stat()
        except OSError:
            continue
        note = _cached(space, rel, st)
        if cat_filters:
            # cat:none is "never filed", not a category called none.
            wanted = (note.cat or "").lower() in cat_filters
            if "none" in cat_filters and not note.filed:
                wanted = True
            if not wanted:
                continue
        if who_filters and (note.author or HUMAN).lower() not in who_filters:
            continue
        if needles and not _score(note, needles)[0]:
            continue
        out.append(note)
    for gone in set(_CACHE) - seen:
        _CACHE.pop(gone, None)
    if needles:  # cat:-only queries keep plain newest-first order
        # Most terms matched first, title hits ahead of body-only hits, then
        # newest. Without this, OR puts a note mentioning "gas" once above the
        # note actually called "Wood gas truck front end parts".
        out.sort(key=lambda n: (_score(n, needles), n.date, n.id), reverse=True)
    else:
        out.sort(key=lambda n: (n.date, n.id), reverse=True)
    return out
