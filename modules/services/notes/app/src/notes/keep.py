"""Importing a Google Keep Takeout.

A Takeout is one or more zips. Keep's part holds one JSON file per note under
``Takeout/Keep/``; other parts may hold only an HTML index, which is why more
than one file is accepted and a file with no notes in it is reported, not
treated as an error.

IDENTITY. Every Keep note carries ``createdTimestampUsec`` — the note's own
creation time, a property of the note rather than of the export. A note
shared between two accounts is ONE object in Keep and appears in BOTH
people's Takeouts with the same value, so it is what dedupe keys on. It is
written into each imported file as ``keep_id``.

What counts as already here: a ``keep_id`` anywhere under Keep/ or Trash/ —
a note someone deliberately deleted must not come back on the next import —
and, for older notes imported without an id, the same body text.

WHOSE NOTE IS IT. Takeout lists the OTHER parties in ``sharees`` and flags the
owner with ``isOwner: true``. A flagged owner whose email belongs to an
account here becomes the author; anything else belongs to whoever is
importing. Archived and pinned are recorded for the IMPORTER only, because
they are per-person here, exactly as they are in Keep.

Nothing is extracted to disk. Members are read from the zip in memory, only
``*.json`` under a ``Keep/`` directory are opened, and each is size-capped,
so a hostile archive can neither write outside the space nor exhaust memory.
"""

from __future__ import annotations

import datetime as _dt
import io
import json
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import paths, store, users

MAX_MEMBER = 2 * 1024 * 1024        # one note's JSON; real ones are a few KB
MAX_MEMBERS = 50_000
MAX_TOTAL = 400 * 1024 * 1024       # uncompressed JSON read across a file


class ImportError_(ValueError):
    """The upload is not something this can import."""


@dataclass
class KeepNote:
    keep_id: str
    source: str
    title: str
    body: str
    created: str
    edited: str | None
    archived: bool
    pinned: bool
    trashed: bool
    owner_email: str | None
    shared: bool
    label: str | None


@dataclass
class Plan:
    importer: str
    new: list[KeepNote] = field(default_factory=list)
    have_id: int = 0
    have_trashed: int = 0
    have_body: int = 0
    # Already here, but edited in Keep after the copy here was taken. Not
    # overwritten: the copy here may have been edited too, and there is no
    # way to merge two edits to a note. Counted so it is not silently lost.
    changed_in_keep: list[str] = field(default_factory=list)
    trashed_in_keep: int = 0
    empty: int = 0
    files: list[dict] = field(default_factory=list)

    def summary(self) -> dict:
        return {
            "importer": self.importer,
            "new": len(self.new),
            "already_here": self.have_id + self.have_body,
            "deleted_here": self.have_trashed,
            "trashed_in_keep": self.trashed_in_keep,
            "empty": self.empty,
            "changed_in_keep": len(self.changed_in_keep),
            "changed_sample": self.changed_in_keep[:15],
            "files": self.files,
            "sample": [n.title or _first_line(n.body) for n in self.new[:25]],
        }


def _first_line(body: str) -> str:
    for line in body.splitlines():
        line = re.sub(r"^- \[[ x]\] ", "", line).strip()
        if line:
            return line[:80]
    return "(empty)"


def _day(usec) -> str | None:
    try:
        return _dt.date.fromtimestamp(int(usec) // 1_000_000).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def body_of(d: dict) -> str:
    """Keep stores a note as either prose or a checklist, never both."""
    if d.get("listContent"):
        out = []
        for item in d["listContent"]:
            mark = "x" if item.get("isChecked") else " "
            out.append(f"- [{mark}] {(item.get('text') or '').strip()}")
        return "\n".join(out)
    return (d.get("textContent") or "").rstrip()


def _note(d: dict, source: str) -> KeepNote | None:
    if not isinstance(d, dict) or "createdTimestampUsec" not in d:
        return None
    created = _day(d["createdTimestampUsec"])
    if not created:
        return None
    sharees = d.get("sharees") or []
    owner = next((s.get("email") for s in sharees
                  if isinstance(s, dict) and s.get("isOwner")), None)
    labels = [l.get("name") for l in (d.get("labels") or [])
              if isinstance(l, dict) and l.get("name")]
    return KeepNote(
        keep_id=str(d["createdTimestampUsec"]),
        source=source,
        title=" ".join((d.get("title") or "").split()),
        body=body_of(d),
        created=created,
        edited=_day(d.get("userEditedTimestampUsec")),
        archived=bool(d.get("isArchived")),
        pinned=bool(d.get("isPinned")),
        trashed=bool(d.get("isTrashed")),
        owner_email=owner,
        shared=bool(sharees),
        label=labels[0] if labels else None,
    )


def read_zip(data: bytes, name: str = "upload.zip") -> list[KeepNote]:
    """Every Keep note in one Takeout zip. Raises on anything not a zip."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ImportError_(f"{name} is not a zip file")
    infos = zf.infolist()
    if len(infos) > MAX_MEMBERS:
        raise ImportError_(f"{name} has too many entries to be a Takeout")
    out: list[KeepNote] = []
    total = 0
    for info in infos:
        p = PurePosixPath(info.filename)
        if info.is_dir() or p.suffix.lower() != ".json" or "Keep" not in p.parts:
            continue
        if info.file_size > MAX_MEMBER:
            continue                                  # not a note; skip, don't die
        total += info.file_size
        if total > MAX_TOTAL:
            raise ImportError_(f"{name} holds more note data than a Takeout should")
        try:
            d = json.loads(zf.read(info).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, zipfile.BadZipFile, RuntimeError):
            continue
        n = _note(d, p.name)
        if n:
            out.append(n)
    return out


# ------------------------------------------------------------- what is here

def _split_fm(text: str) -> tuple[list[str], str]:
    if not text.startswith("---"):
        return [], text
    lines = text.split("\n")
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return lines[1:i], "\n".join(lines[i + 1:])
    return [], text


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().casefold()


def content_key(title: str, body: str) -> tuple[str, str]:
    """Fallback identity for notes imported without a keep_id: the BODY.

    Not the title — earlier imports rewrote titles, but bodies went in
    verbatim. Empty bodies all collide, so those key on the title instead.
    """
    b = _norm(body)[:400]
    return ("b", b) if b else ("t", _norm(title))


def inventory(space: Path) -> tuple[dict[str, str], set[str], set[tuple[str, str]]]:
    """(live keep_id -> last edit date recorded here, keep_ids in the trash,
    body keys of live Keep notes)."""
    live: dict[str, str] = {}
    gone: set[str] = set()
    bodies: set[tuple[str, str]] = set()
    for root, bucket in ((space / "Keep", live), (space / paths.TRASH_DIR, gone)):
        if not root.is_dir():
            continue
        for p in root.rglob("*.md"):
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            fm, body = _split_fm(text)
            kid = next((l.split(":", 1)[1].strip() for l in fm
                        if re.match(r"^keep_id\s*:", l, re.I)), None)
            if kid and bucket is live:
                field_ = lambda k: next((l.split(":", 1)[1].strip() for l in fm
                                         if re.match(rf"^{k}\s*:", l, re.I)), "")
                live[kid] = field_("edited") or field_("date")
            elif kid:
                bucket.add(kid)
            if bucket is live:
                lines = body.split("\n")
                title = next((l[2:].strip() for l in lines if l.startswith("# ")), "")
                rest = "\n".join(l for l in lines if not l.startswith("# "))
                bodies.add(content_key(title, rest))
    return live, gone, bodies


def plan(space: Path, importer: str, batches: list[tuple[str, list[KeepNote]]]) -> Plan:
    """Decide, without writing anything, what an import would do."""
    live, gone, bodies = inventory(space)
    pl = Plan(importer=importer)
    seen: set[str] = set()
    for name, notes in batches:
        pl.files.append({"name": name, "notes": len(notes)})
        for n in notes:
            if n.keep_id in seen:              # the same note in two uploaded files
                continue
            seen.add(n.keep_id)
            if n.trashed:
                pl.trashed_in_keep += 1
            elif n.keep_id in live:
                pl.have_id += 1
                if n.edited and live[n.keep_id] and n.edited > live[n.keep_id]:
                    pl.changed_in_keep.append(n.title or _first_line(n.body))
            elif n.keep_id in gone:
                pl.have_trashed += 1
            elif not n.title and not n.body.strip():
                pl.empty += 1
            elif content_key(n.title, n.body) in bodies:
                pl.have_body += 1
            else:
                pl.new.append(n)
    return pl


# ------------------------------------------------------------------- writing

def _slug(title: str) -> str:
    s = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9 _-]+", "", s).strip()
    return re.sub(r"\s+", " ", s)[:60]


def render(n: KeepNote, importer: str, people: users.People,
           archive_all: bool) -> str:
    owner = importer
    if n.owner_email:
        u = people.by_email(n.owner_email)
        owner = u.username if u else importer
    fm = [f"date: {n.created}", f"source: {n.source}", "tags: keep-import",
          f"keep_id: {n.keep_id}"]
    if n.edited and n.edited != n.created:
        fm.append(f"edited: {n.edited}")
    if n.label:
        cat = re.sub(r"[^a-z0-9]+", "-", n.label.lower()).strip("-")[:40]
        if cat:
            fm.append(f"category: {cat}")
    # A note that was collaborative in Keep belongs in the shared space —
    # sharing is a place here, so it is carried across rather than inferred.
    if n.shared:
        fm.append("shared: true")
    if owner != store.HUMAN:
        fm.append(f"author: {owner}")
    # Per-person, as in Keep: the importer's own archive and pin state only.
    if n.archived or archive_all:
        fm.append(f"archived_by: {importer}")
    if n.pinned and not archive_all:
        fm.append(f"pinned_by: {importer}")
    out = ["---", *fm, "---", ""]
    # A title is optional in Keep. No heading is invented for an untitled
    # note — the filename's id is a uniqueness device, not a title.
    if n.title:
        out += [f"# {n.title}", ""]
    out.append(n.body.strip("\n"))
    return "\n".join(out).rstrip("\n") + "\n"


def apply(space: Path, pl: Plan, people: users.People, archive_all: bool) -> list[str]:
    """Write the planned notes. Re-checks every id, so a stale plan cannot
    duplicate something imported since it was made."""
    live, gone, _ = inventory(space)
    keep = paths.resolve(space, "Keep/x.md").parent
    keep.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for n in pl.new:
        if n.keep_id in live or n.keep_id in gone:
            continue
        stem = f"{n.created} {_slug(n.title) or n.keep_id}"
        rel = f"Keep/{stem}.md"
        k = 2
        while paths.resolve(space, rel).exists():
            rel = f"Keep/{stem} ({k}).md"
            k += 1
        target = paths.resolve(space, rel)       # the boundary, as for every write
        target.write_text(render(n, pl.importer, people, archive_all), encoding="utf-8")
        live[n.keep_id] = n.created
        written.append(rel)
    return written
