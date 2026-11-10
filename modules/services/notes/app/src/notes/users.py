"""User accounts.

Deliberately the shape people already expect from every other system, after
an earlier design grew three overlapping notions of "who someone is" and
became hard to reason about:

    username  unique, chosen once, and the ONLY thing a note records
            (``archived_by: mary``). It does not change, because changing it
            would orphan every note already filed under it.
    name    a label. Free text, not unique — two people called Mary is an
            ordinary situation, and it is the ids that keep them apart.
    emails  optional. How an authenticated sign-in is recognised as this
            account. Nothing is verified here; the proxy did that.

There is no password and no session. Authentication is somebody else's job
(see config.trust_proxy_auth); this module only answers "which account is
this request", and lets people say who they are when nothing else can.

Stored as a markdown table in the space, because this project has no database
and a user list is not a reason to grow one. It is meant to be read and
edited by hand as readily as through the app.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

HEADERS = ("Username", "Display name", "Emails")

USERNAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_USERNAME_BAD = re.compile(r"[^a-z0-9_-]+")
EMAIL_RE = re.compile(r"^[^@\s,|]+@[^@\s,|]+\.[^@\s,|]+$")

MAX_USERS = 500


def make_username(raw: str | None) -> str | None:
    """A candidate id from free text. ``None`` when nothing usable remains.

    Only ever a SUGGESTION: whether it can be used depends on whether it is
    already taken, which is ``People.available``.
    """
    s = (raw or "").strip().lower().split("@", 1)[0]
    s = _USERNAME_BAD.sub("-", s).strip("-")[:32]
    return s if USERNAME_RE.match(s) else None


def clean_email(raw: str | None) -> str | None:
    e = (raw or "").strip().lower()
    return e if EMAIL_RE.match(e) else None


def clean_name(raw: str | None) -> str:
    return " ".join((raw or "").replace("|", "/").split())[:60]


@dataclass
class User:
    username: str
    name: str = ""
    emails: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        """What to show. Never blank — the id is a serviceable name."""
        return self.name.strip() or self.username

    def as_dict(self) -> dict:
        return {"username": self.username, "name": self.name, "label": self.label,
                "emails": list(self.emails)}


@dataclass
class People:
    users: list[User] = field(default_factory=list)
    prologue: str = ""
    epilogue: str = ""
    # True when this was read from the old identity/person table and converted
    # in memory. The caller persists it once, so the file on disk converges on
    # the documented shape instead of being re-converted on every load.
    migrated: bool = False

    # ------------------------------------------------------------- lookup
    def get(self, username: str | None) -> User | None:
        wanted = (username or "").strip().lower()
        for u in self.users:
            if u.username == wanted:
                return u
        return None

    def by_email(self, email: str | None) -> User | None:
        e = clean_email(email)
        if not e:
            return None
        for u in self.users:
            if e in u.emails:
                return u
        return None

    def resolve(self, sign_in: str | None) -> User | None:
        """The account a sign-in belongs to, or None when nobody claims it.

        Two ways in, both exact: the id itself, or one of the account's
        emails. Nothing is guessed — an unrecognised sign-in is a question
        for the person using it, not something to infer from the spelling.
        """
        raw = (sign_in or "").strip().lower()
        if not raw:
            return None
        return self.get(raw) or self.by_email(raw)

    def available(self, username: str | None) -> bool:
        """Is this id both usable and free?"""
        candidate = (username or "").strip().lower()
        return bool(USERNAME_RE.match(candidate)) and self.get(candidate) is None

    def suggest_username(self, name: str | None, sign_in: str | None = None) -> str:
        """A free id for a new account: from the name, then the sign-in.

        Collisions get a numeric suffix rather than being rejected, because
        "Mary" being taken is an ordinary thing and the person should not have
        to invent spellings to get past it.
        """
        base = make_username(name) or make_username(sign_in) or "user"
        if self.available(base):
            return base
        for n in range(2, 100):
            candidate = "%s%d" % (base[:30], n)
            if self.available(candidate):
                return candidate
        return base

    # ------------------------------------------------------------- change
    def add(self, username: str, name: str = "", emails: list[str] | None = None) -> User:
        uid = (username or "").strip().lower()
        if not USERNAME_RE.match(uid):
            raise ValueError("a user id is a short lowercase name: letters, digits, - and _")
        if self.get(uid) is not None:
            raise ValueError("that id is already taken")
        if len(self.users) >= MAX_USERS:
            raise ValueError("too many accounts")
        u = User(username=uid, name=clean_name(name))
        for e in emails or []:
            self.attach_email(u, e)
        self.users.append(u)
        return u

    def attach_email(self, user: User, email: str | None) -> bool:
        """Attach an address to this account, taking it off any other.

        One address cannot name two people — that is the whole reason it can
        serve as a sign-in, so moving it is the only sane reading of adding
        it somewhere new.
        """
        e = clean_email(email)
        if not e:
            return False
        for other in self.users:
            if other is not user and e in other.emails:
                other.emails.remove(e)
        if e not in user.emails:
            user.emails.append(e)
        return True

    def detach_email(self, user: User, email: str | None) -> bool:
        e = clean_email(email)
        if e and e in user.emails:
            user.emails.remove(e)
            return True
        return False


# ------------------------------------------------------------------ the file

_ROW = re.compile(r"^\s*\|(.*)\|\s*$")
_SEP = re.compile(r"^[\s|:-]+$")

DEFAULT_PROLOGUE = """# People

Everyone who uses this space.

**ID** is what a note records — `archived_by: mary`. It is chosen once and
does not change, because it is written into every note that person has
touched. **Name** is only a label; change it whenever you like, and two
people may share one. **Emails** let a sign-in that has been authenticated
elsewhere be recognised as this account.

There are no passwords here. Edit this table by hand, or from Users in the
side menu."""


def parse(text: str) -> People:
    """Read the first ID-headed table, keeping the prose around it."""
    lines = text.splitlines()
    start = None
    for n, line in enumerate(lines):
        m = _ROW.match(line)
        if m and [c.strip() for c in m.group(1).split("|")][:1] == [HEADERS[0]]:
            start = n
            break
    if start is None:
        return People(prologue=text.rstrip("\n"))

    users: list[User] = []
    seen: set[str] = set()
    n = start + 1
    end = n
    while n < len(lines):
        line = lines[n]
        m = _ROW.match(line)
        if not m:
            break
        if not _SEP.match(line):
            cells = [c.strip() for c in m.group(1).split("|")] + ["", "", ""]
            uid = (cells[0] or "").strip().lower()
            # A duplicate id in a hand-edited file is the one thing that
            # breaks the model, so the first wins and the rest are dropped.
            if USERNAME_RE.match(uid) and uid not in seen:
                seen.add(uid)
                u = User(username=uid, name=clean_name(cells[1]))
                for part in cells[2].split(","):
                    e = clean_email(part)
                    if e and e not in u.emails:
                        u.emails.append(e)
                users.append(u)
        n += 1
        end = n
    return People(users=users,
                  prologue="\n".join(lines[:start]).rstrip("\n"),
                  epilogue="\n".join(lines[end:]).strip("\n"))


# The shape this replaced: one row per sign-in identity, with a separate
# "person" column pointing at whoever owned it. Deployments exist with that
# file already written, so read it once and convert rather than losing who
# everybody is.
LEGACY_HEADER = "identity"


def migrate_legacy(text: str) -> People | None:
    """Convert an identity/person table into accounts, or None if not one."""
    lines = text.splitlines()
    start = None
    for n, line in enumerate(lines):
        m = _ROW.match(line)
        if m and [c.strip().lower() for c in m.group(1).split("|")][:1] == [LEGACY_HEADER]:
            start = n
            break
    if start is None:
        return None

    out = People(prologue="")
    names: dict[str, str] = {}
    mails: dict[str, list[str]] = {}
    order: list[str] = []
    n = start + 1
    while n < len(lines):
        line = lines[n]
        m = _ROW.match(line)
        if not m:
            break
        if not _SEP.match(line):
            cells = [c.strip() for c in m.group(1).split("|")] + ["", "", "", ""]
            identity, person, display = cells[0], cells[1].lower(), cells[2]
            # An unclaimed identity named nobody; there is no account to make.
            if USERNAME_RE.match(person):
                if person not in order:
                    order.append(person)
                    mails[person] = []
                # The fullest display name wins, as it did before.
                if len(clean_name(display)) > len(names.get(person, "")):
                    names[person] = clean_name(display)
                e = clean_email(identity)
                if e and e not in mails[person]:
                    mails[person].append(e)
        n += 1
    if not order:
        return None
    for uid in order:
        out.add(uid, names.get(uid, ""), mails.get(uid, []))
    return out


def render(p: People) -> str:
    head = p.prologue.strip("\n") or DEFAULT_PROLOGUE
    out = [head, "", "| " + " | ".join(HEADERS) + " |",
           "| " + " | ".join("---" for _ in HEADERS) + " |"]
    for u in sorted(p.users, key=lambda u: u.username):
        out.append("| " + " | ".join([u.username, u.name, ", ".join(u.emails)]) + " |")
    if p.epilogue.strip():
        out += ["", p.epilogue.strip("\n")]
    return "\n".join(out) + "\n"


# -------------------------------------------------------- the file, on disk

_CACHE: tuple[int, int, People] | None = None


def load(space: Path, rel: str) -> People:
    """Read the table, or an empty one. Never raises."""
    global _CACHE
    p = space / rel
    try:
        st = p.stat()
    except OSError:
        return People()
    if _CACHE and _CACHE[0] == st.st_mtime_ns and _CACHE[1] == st.st_size:
        return _CACHE[2]
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return People()
    parsed = parse(text)
    if not parsed.users:
        # Nothing in the new shape. It may be the old identity/person table,
        # in which case convert rather than silently forgetting everyone.
        legacy = migrate_legacy(text)
        if legacy is not None:
            legacy.migrated = True
            parsed = legacy
    _CACHE = (st.st_mtime_ns, st.st_size, parsed)
    return parsed


def save(space: Path, rel: str, p: People) -> None:
    global _CACHE
    target = space / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(render(p), encoding="utf-8")
    tmp.replace(target)
    _CACHE = None
