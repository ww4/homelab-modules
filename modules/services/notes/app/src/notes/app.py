"""HTTP API + the single-page front end.

Unauthenticated by design, exactly like the SilverBullet vhost it sits beside:
the perimeter is the nginx source-gate (Tailscale/LAN) plus the tailnet ACL,
and adding a login here would reintroduce the friction that made the notes
app go unused in the first place (decided 2026-07-09).

There are no accounts and there will not be. This app has no per-note
permissions to express, so access is all-or-nothing — and a reverse proxy
does all-or-nothing better than we could, before a request reaches Python.
What it does support is BELIEVING a proxy that has already authenticated
someone (``trust_proxy_auth``): that name then becomes the identity that
archives and pins notes, so authentication and attribution are one thing
instead of two stacked on each other.

Note ids travel in the request BODY rather than the URL path. An id is a
space-relative path like ``Keep/2017-03-04 Bring from the cabin.md`` — spaces,
unicode and slashes in a path segment make URL-encoded ids a source of
mismatched-decoding bugs, and nothing here needs REST-shaped URLs.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import paths, store, users
from .config import Settings

log = logging.getLogger("notes")

# Who is using this browser. Not authentication -- there is nothing here to
# protect, and the owner explicitly did not want accounts. It replaces Google
# Keep's "add collaborator", which is annoying to live with for years: the point
# is attribution between two people who already trust each other, so a cookie
# the browser sets for itself is the right weight. An unknown device stays
# unlabelled, which reads as the owner and preserves the 545 notes predating this.
WHO_COOKIE = "notes_who"
WHO_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def normalize_who(raw: str | None) -> str | None:
    """Any sign-in string reduced to a candidate user id. A SUGGESTION only."""
    return users.make_username(raw)


def raw_identity(request: Request, cfg: Settings) -> str | None:
    """The sign-in string as it arrived, before any account is matched.

    Either what a trusted proxy says, or what this browser has previously
    said it is. Not an account on its own: ``users.People.resolve`` decides
    whether anybody claims it.
    """
    return (proxy_raw(request, cfg) or (request.cookies.get(WHO_COOKIE) or "").strip()) or None


def proxy_raw(request: Request, cfg: Settings) -> str | None:
    """The sign-in an upstream proxy says this is — only when told to believe it.

    ⚠️ This header is trustworthy ONLY because nothing can reach the port
    except the proxy. If the app is directly reachable, anyone can send it.
    ``trust_proxy_auth`` is therefore opt-in, and ``main()`` refuses to bind a
    public address without either it or an explicit override.
    """
    if not cfg.trust_proxy_auth:
        return None
    return (request.headers.get(cfg.auth_user_header) or "").strip() or None


def proxy_name(request: Request, cfg: Settings) -> str | None:
    """A human-readable name the proxy offers, if it sends one."""
    if not cfg.trust_proxy_auth:
        return None
    return (request.headers.get(cfg.auth_name_header) or "").strip() or None


def who(request: Request, cfg: Settings, ppl: users.People | None = None) -> str | None:
    """The user id this request belongs to, or None when nobody claims it.

    The proxy's word outranks the cookie: a cookie is a claim the browser
    makes about itself, which is the right weight when there is nothing to
    protect, but once something has actually authenticated the person that
    answer wins.
    """
    raw = raw_identity(request, cfg)
    if ppl is not None:
        u = ppl.resolve(raw)
        return u.username if u else None
    return normalize_who(raw)


def viewer_for(request: Request, cfg: Settings, me: str | None,
               ppl: users.People | None = None) -> str:
    """Whose feed the listing is built for.

    ``?me=`` exists so one browser can preview another person's wall, and so
    the service worker's cache entry is keyed per person rather than shared.
    It is a QUERY PARAMETER, so it must never outrank an authenticated
    sign-in — otherwise proxy auth would be bypassable with a URL.
    """
    if proxy_raw(request, cfg):
        return who(request, cfg, ppl) or store.HUMAN
    if me and WHO_RE.match(me):
        return me
    return who(request, cfg, ppl) or store.HUMAN


STATIC = Path(__file__).parent / "static"


class NoteRef(BaseModel):
    id: str = Field(min_length=1, max_length=1024)


class NoteWrite(NoteRef):
    title: str | None = Field(default=None, max_length=500)
    body: str | None = Field(default=None, max_length=1_000_000)
    pinned: bool | None = None
    archived: bool | None = None
    shared: bool | None = None
    order: int | None = Field(default=None, ge=-1, le=100000)
    category: str | None = Field(default=None, max_length=60)
    author: str | None = Field(default=None, max_length=40)


class NoteNew(BaseModel):
    title: str = Field(default="", max_length=500)
    body: str = Field(default="", max_length=1_000_000)


class UserNew(BaseModel):
    username: str | None = Field(default=None, max_length=32)
    name: str = Field(default="", max_length=60)        # display name, optional
    emails: list[str] | None = Field(default=None, max_length=10)
    claim_sign_in: bool = True


class UserWrite(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    name: str | None = Field(default=None, max_length=60)
    add_emails: list[str] | None = Field(default=None, max_length=10)
    remove_emails: list[str] | None = Field(default=None, max_length=10)


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or Settings()
    space = Path(cfg.space_dir)
    # One module-level default rather than threading it through every call:
    # it is read in a dozen places and never changes after start-up.
    store.HUMAN = users.make_username(cfg.default_author) or store.HUMAN
    app = FastAPI(title="Notes", docs_url=None, redoc_url=None)

    @app.exception_handler(paths.PathError)
    async def _path_error(_request, exc: paths.PathError):
        # Deliberately terse: the client never needs to learn which of the
        # three scope conditions it tripped. Without this an out-of-scope id
        # is a 500 with a traceback in the journal — the boundary still holds,
        # but it says so by crashing, which is neither quiet nor useful.
        log.warning("rejected out-of-scope path: %s", exc)
        return JSONResponse({"error": "no such note"}, status_code=404)

    def roster() -> users.People:
        return users.load(space, paths.PEOPLE_FILE)

    def save_roster(ppl: users.People) -> None:
        users.save(space, paths.PEOPLE_FILE, ppl)

    def seed(ppl: users.People) -> bool:
        """Create accounts for people the notes already say exist.

        A space has authorship in it long before it has a user list, so the
        first time anyone looks the list should describe reality rather than
        being empty. Returns whether anything was added.
        """
        added = False
        for name in [store.HUMAN] + list(store.authors_of(store.listing(space))):
            if users.USERNAME_RE.match(name) and ppl.get(name) is None:
                try:
                    ppl.add(name)
                    added = True
                except ValueError:
                    pass
        return added

    @app.get("/api/notes")
    async def list_notes(
        request: Request,
        q: str | None = None,
        scope: str = "all",
        shared: int = 1,
        me: str | None = None,
    ):
        """The wall: summaries only, and the search runs here.

        Sending whole notes was 546 KB for 586 cards. Previews cut that to a
        fraction, but they also mean the client CANNOT do the matching -- a
        hit inside a long note would silently not be found -- so q is handled
        server-side against the full text.
        """
        # ONE walk of the space, not three. This called listing(), census()
        # and authors() separately, and census/authors each walked the whole
        # space AGAIN — 1412 notes read three times per request, which is most
        # of why a fresh load sat blank (decided 2026-11-09).
        #
        # The query param keeps the CACHE key honest: the service worker stores
        # this response, and two people on one box must not share an entry. The
        # cookie stays authoritative for writes, which are never cached.
        ppl = roster()
        viewer = viewer_for(request, cfg, me, ppl)
        notes = store.listing(space, q)
        # The census has to describe the WHOLE space, not the search result, or
        # a category vanishes from the chips the moment you type its name.
        everything = notes if not q else store.listing(space)
        return {
            "notes": [n.summary() for n in notes],
            "cats": store.census_of(
                everything,
                scope=scope if scope in ("mine", "shared", "all") else "all",
                me=viewer,
                show_shared=bool(shared),
            ),
            "authors": store.authors_of(everything),
            # Labels only. The client renders these; what a note RECORDS is
            # always the person slug.
            # Labels only. What a note RECORDS is always the user id.
            "people": labels(ppl, {viewer} | {n.author for n in notes if n.author}),
        }

    @app.get("/api/note")
    async def get_note(id: str):
        """One note, with its real body. The editor calls this on open."""
        if not paths.resolve(space, id).exists():
            raise HTTPException(404, "no such note")
        return store.read(space, id).public()

    @app.post("/api/notes")
    async def new_note(req: NoteNew, request: Request):
        if not req.title.strip() and not req.body.strip():
            raise HTTPException(400, "an empty note is not saved")
        # Attribution comes from the DEVICE, not from the client asking nicely:
        # the cookie is read here so a note is stamped the same way whichever
        # code path created it.
        # No invented "Untitled": a Keep note may have only a body, and so
        # may one made here.
        return store.create(space, req.title, req.body,
                            author=who(request, cfg, roster())).summary()

    @app.put("/api/notes")
    async def update_note(req: NoteWrite, request: Request):
        if not paths.resolve(space, req.id).exists():
            raise HTTPException(404, "no such note")
        return store.write(
            space, req.id,
            title=req.title, body=req.body,
            pinned=req.pinned, archived=req.archived, shared=req.shared, order=req.order,
            category=req.category, author=req.author,
            actor=who(request, cfg, roster()) or store.HUMAN,
        ).summary()

    @app.get("/api/trash")
    async def list_trash():
        """What is recoverable. Paired with /api/notes/restore."""
        return {"notes": [n.summary() for n in store.trash_listing(space)]}

    @app.post("/api/notes/trash")
    async def trash_note(req: NoteRef):
        return {"trashed": store.trash(space, req.id)}

    @app.post("/api/notes/restore")
    async def restore_note(req: NoteRef):
        return {"restored": store.restore(space, req.id)}

    def labels(ppl: users.People, ids: set[str]) -> dict:
        """id -> what to show for it, for every account AND everyone cited."""
        out = {u.username: u.label for u in ppl.users}
        for i in ids:
            if i and i not in out:
                out[i] = i          # someone the notes name but the list does not
        return out

    @app.get("/api/whoami")
    async def whoami(request: Request):
        """Which account this browser is, and the list to pick from if none.

        ⚠️ Deliberately NOT part of /api/notes. That response is cached by the
        service worker, and identity in a cached body is what made the app
        forget who you were on every reload — two people on one device would
        also share the entry. This endpoint is never cached.
        """
        ppl = roster()
        # Two reasons to write on a read, both once-only: an empty list that
        # the notes can describe, and an old-format table just converted in
        # memory. Leaving the latter unwritten means the file on disk never
        # matches what the app and the README say it looks like.
        if (not ppl.users and seed(ppl)) or ppl.migrated:
            try:
                save_roster(ppl)
            except OSError as e:
                log.warning("could not write the user list: %s", e)
        raw = raw_identity(request, cfg)
        u = ppl.resolve(raw)
        return {
            "sign_in": raw or "",
            "me": u.username if u else None,
            # Nobody claims this sign-in. The front end asks, once.
            "unknown": bool(raw) and u is None,
            "fixed": cfg.trust_proxy_auth,   # the sign-in is the proxy's, not ours
            "users": [x.as_dict() for x in sorted(ppl.users, key=lambda x: x.username)],
            "suggest_name": proxy_name(request, cfg) or "",
            # Only suggest when there is something to suggest FROM. Filling
            # the box with "user" would hand someone a username they did not
            # choose, for the one field here that can never be changed.
            "suggest_username": (ppl.suggest_username(proxy_name(request, cfg), raw)
                                 if (proxy_name(request, cfg) or raw) else ""),
            "default_author": store.HUMAN,
            "file": paths.PEOPLE_FILE,
        }

    @app.get("/api/users")
    async def list_users(request: Request):
        ppl = roster()
        return {
            "users": [u.as_dict() for u in sorted(ppl.users, key=lambda u: u.username)],
            "me": who(request, cfg, ppl),
            "file": paths.PEOPLE_FILE,
            "fixed": cfg.trust_proxy_auth,
        }

    @app.post("/api/users")
    async def create_user(request: Request, req: UserNew):
        """Make an account. The id is chosen ONCE and never changes.

        Taking an id that exists is refused rather than merged: two people
        called Mary is ordinary, and quietly folding one into the other would
        file their notes under someone else.
        """
        ppl = roster()
        uid = (req.username or "").strip().lower() or ppl.suggest_username(req.name)
        try:
            u = ppl.add(uid, req.name)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
        # Whoever is asking is presumably this person; attach the sign-in if
        # it is an email, so they are recognised next time without a step.
        if req.claim_sign_in:
            ppl.attach_email(u, raw_identity(request, cfg))
        for e in req.emails or []:
            ppl.attach_email(u, e)
        save_roster(ppl)
        return {"ok": True, "user": u.as_dict()}

    @app.put("/api/users")
    async def update_user(req: UserWrite):
        """Rename, or add and remove emails. The id is not editable."""
        ppl = roster()
        u = ppl.get(req.username)
        if u is None:
            raise HTTPException(status_code=404, detail="no such user")
        if req.name is not None:
            u.name = users.clean_name(req.name)
        for e in req.add_emails or []:
            ppl.attach_email(u, e)
        for e in req.remove_emails or []:
            ppl.detach_email(u, e)
        save_roster(ppl)
        return {"ok": True, "user": u.as_dict()}

    @app.get("/healthz")
    async def healthz():
        ok = space.is_dir()
        return JSONResponse({"ok": ok, "space": str(space)}, status_code=200 if ok else 503)

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html", media_type="text/html")

    @app.get("/sw.js")
    async def service_worker():
        """The worker must be served from the ROOT to control the whole origin.

        Service-Worker-Allowed is set explicitly rather than relied upon: a
        worker served from a subpath can only control that subpath, and the
        failure mode is a worker that registers fine and then never intercepts
        anything. no-store because a cached worker is how a stale app survives
        a deploy -- this origin already has one dead PWA on it (SilverBullet
        served notes.example.com until 2026-11-09).
        """
        return FileResponse(
            STATIC / "sw.js",
            media_type="application/javascript",
            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-store"},
        )

    @app.get("/service_worker.js")
    async def silverbullet_worker_killer():
        """Evict SilverBullet's worker, which still owns this origin.

        See static/service_worker.js. This path is not ours; it is the one the
        previous occupant registered, and the only thing a poisoned browser
        still fetches from the network.
        """
        return FileResponse(
            STATIC / "service_worker.js",
            media_type="application/javascript",
            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-store"},
        )

    @app.get("/manifest.webmanifest")
    async def manifest():
        return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/icon.svg")
    async def icon_svg():
        return FileResponse(STATIC / "icon.svg", media_type="image/svg+xml")

    @app.get("/icon-192.png")
    async def icon_192():
        return FileResponse(STATIC / "icon-192.png", media_type="image/png")

    @app.get("/icon-512.png")
    async def icon_512():
        return FileResponse(STATIC / "icon-512.png", media_type="image/png")

    return app
