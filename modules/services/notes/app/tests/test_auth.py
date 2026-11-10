"""Which account a request belongs to, and the refusal to be exposed.

There are no passwords in this app. These two things are the whole of its
security posture that lives in code — everything else is the deployment's job
— so they get the same corpus treatment as path scoping.
"""

from __future__ import annotations

import pytest
from fastapi import Request

from notes import users
from notes.app import normalize_who, proxy_raw, raw_identity, viewer_for, who
from notes.config import Settings
from notes.__main__ import check_exposure, is_loopback


def req(headers: dict[str, str] | None = None, cookies: dict[str, str] | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if cookies:
        jar = "; ".join(f"{k}={v}" for k, v in cookies.items())
        raw.append((b"cookie", jar.encode()))
    return Request({"type": "http", "method": "GET", "path": "/", "headers": raw})


def roster() -> users.People:
    p = users.People()
    p.add("sam", "Sam Reed", ["sam@example.com"])
    p.add("ana", "Ana Parker", ["ana@example.com", "a.parker@work.example"])
    return p


# -------------------------------------------------------- the sign-in string

def test_the_sign_in_arrives_unchanged():
    """It is matched against accounts verbatim; nothing is guessed from it."""
    cfg = Settings(space_dir="/nonexistent", trust_proxy_auth=True)
    assert raw_identity(req({"Remote-User": "a.parker@work.example"}), cfg) == "a.parker@work.example"
    assert raw_identity(req(cookies={"notes_who": "ana"}), cfg) == "ana"
    assert raw_identity(req(), cfg) is None


def test_header_ignored_unless_trusted():
    cfg = Settings(space_dir="/nonexistent")
    assert cfg.trust_proxy_auth is False, "trusting a header must never be the default"
    assert proxy_raw(req({"Remote-User": "admin"}), cfg) is None
    assert who(req({"Remote-User": "admin"}), cfg, roster()) is None


@pytest.mark.parametrize("header", [
    "Remote-User", "X-Forwarded-User", "X-authentik-username",
    "Cf-Access-Authenticated-User-Email", "Tailscale-User-Login",
])
def test_the_proxies_people_actually_run(header):
    cfg = Settings(space_dir="/nonexistent", trust_proxy_auth=True, auth_user_header=header)
    assert who(req({header: "ana@example.com"}), cfg, roster()) == "ana"


def test_only_the_configured_header_counts():
    cfg = Settings(space_dir="/nonexistent", trust_proxy_auth=True,
                   auth_user_header="X-authentik-username")
    r = req({"X-authentik-username": "ana", "Remote-User": "sam"})
    assert who(r, cfg, roster()) == "ana"


# ------------------------------------------------------- matching an account

def test_a_sign_in_matching_an_id():
    cfg = Settings(space_dir="/nonexistent", trust_proxy_auth=True)
    assert who(req({"Remote-User": "ana"}), cfg, roster()) == "ana"


def test_a_sign_in_matching_an_email():
    cfg = Settings(space_dir="/nonexistent", trust_proxy_auth=True)
    assert who(req({"Remote-User": "a.parker@work.example"}), cfg, roster()) == "ana"


def test_an_unknown_sign_in_is_nobody():
    """Not guessed into an account — the front end asks who they are."""
    cfg = Settings(space_dir="/nonexistent", trust_proxy_auth=True)
    assert who(req({"Remote-User": "ana@gmail.com"}), cfg, roster()) is None
    assert who(req({"Remote-User": "github:aparker"}), cfg, roster()) is None


def test_proxy_beats_the_cookie():
    """A cookie is the browser's claim about itself; the proxy has checked."""
    cfg = Settings(space_dir="/nonexistent", trust_proxy_auth=True)
    r = req({"Remote-User": "ana"}, {"notes_who": "sam"})
    assert who(r, cfg, roster()) == "ana"


def test_the_cookie_works_without_a_proxy():
    cfg = Settings(space_dir="/nonexistent")
    assert who(req(cookies={"notes_who": "ana"}), cfg, roster()) == "ana"
    assert who(req(cookies={"notes_who": "nobody"}), cfg, roster()) is None


# --------------------------------------------- whose feed a listing is built for

def test_me_param_works_without_auth():
    cfg = Settings(space_dir="/nonexistent")
    assert viewer_for(req(cookies={"notes_who": "sam"}), cfg, "ana", roster()) == "ana"


def test_me_param_cannot_override_an_authenticated_sign_in():
    """Otherwise proxy auth is bypassable with a query string."""
    cfg = Settings(space_dir="/nonexistent", trust_proxy_auth=True)
    r = req({"Remote-User": "ana"}, {"notes_who": "ana"})
    for pretend in ("sam", "admin", None):
        assert viewer_for(r, cfg, pretend, roster()) == "ana"


def test_viewer_falls_back_to_the_default_author():
    from notes import store
    cfg = Settings(space_dir="/nonexistent")
    assert viewer_for(req(), cfg, None, roster()) == store.HUMAN


def test_make_id_is_a_suggestion_not_a_lookup():
    assert normalize_who("Ana Parker") == "ana-parker"
    assert normalize_who("ana@example.com") == "ana"
    assert normalize_who("!!!") is None


# ------------------------------------------------------------- exposure guard

@pytest.mark.parametrize("host,loop", [
    ("127.0.0.1", True), ("localhost", True), ("::1", True), ("127.0.0.53", True),
    ("0.0.0.0", False), ("::", False), ("192.168.1.10", False), ("notes.example.com", False),
])
def test_loopback_detection(host, loop):
    assert is_loopback(host) is loop


def test_loopback_needs_no_permission():
    assert check_exposure(Settings(space_dir="/nonexistent")) is None


def test_public_bind_is_refused_by_default():
    problem = check_exposure(Settings(space_dir="/nonexistent", host="0.0.0.0"))
    assert problem is not None
    assert "NO authentication" in problem


def test_public_bind_allowed_when_said_out_loud():
    cfg = Settings(space_dir="/nonexistent", host="0.0.0.0", allow_public_bind=True)
    assert check_exposure(cfg) is None


def test_public_bind_allowed_behind_proxy_auth():
    cfg = Settings(space_dir="/nonexistent", host="0.0.0.0", trust_proxy_auth=True)
    assert check_exposure(cfg) is None


def test_an_unresolvable_host_counts_as_public():
    """Fail closed on anything we cannot prove is loopback."""
    assert check_exposure(Settings(space_dir="/nonexistent", host="notes.example.com")) is not None


# --------------------------------------------- whose feed a listing is built for

def test_me_param_works_without_auth():
    """One browser previewing the other person's wall — the reason ?me= exists."""
    cfg = Settings(space_dir="/nonexistent")
    assert viewer_for(req(cookies={"notes_who": "sam"}), cfg, "ana") == "ana"


def test_me_param_cannot_override_an_authenticated_user():
    """Otherwise proxy auth is bypassable with a query string."""
    cfg = Settings(space_dir="/nonexistent", trust_proxy_auth=True)
    r = req({"Remote-User": "ana"}, {"notes_who": "ana"})
    assert viewer_for(r, cfg, "sam") == "ana"
    assert viewer_for(r, cfg, "admin") == "ana"
    assert viewer_for(r, cfg, None) == "ana"


def test_viewer_falls_back_to_the_default_identity():
    cfg = Settings(space_dir="/nonexistent")
    assert viewer_for(req(), cfg, None) == "sam"


def test_a_junk_me_param_does_not_become_the_viewer():
    cfg = Settings(space_dir="/nonexistent")
    assert viewer_for(req(cookies={"notes_who": "ana"}), cfg, "../../etc") == "ana"


# ------------------------------------------------- the default author is config

def test_the_default_author_comes_from_configuration():
    """⚠️ Not cosmetic: every note predating authorship is attributed to this."""
    from notes import store
    from notes.app import create_app
    before = store.HUMAN
    try:
        create_app(Settings(space_dir="/nonexistent", default_author="Sam"))
        assert store.HUMAN == "sam", "slugged, then applied"
        create_app(Settings(space_dir="/nonexistent", default_author="!!!"))
        assert store.HUMAN == "sam", "an unusable value must not blank it"
    finally:
        store.HUMAN = before
