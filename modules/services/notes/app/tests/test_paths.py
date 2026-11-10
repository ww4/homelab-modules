"""The path-scoping corpus. This is the security boundary; treat it as such.

Notes is unauthenticated, so a client-supplied note id is the only thing
standing between a request and the filesystem. Each case below is a way that
boundary has been broken in real services.
"""

from pathlib import Path

import pytest

from notes import paths


# --- things that must be reachable -----------------------------------------
IN_SCOPE = [
    "Keep/2017-03-04 Bring from the cabin.md",
    "Keep/2013-05-14 Argos list.md",
    "Notes/paint list — girls room.md",
    "Notes/bl-corp v2 af_heart/bl-corp v2 af_heart.md",
    "Inbox/2026-10-27-k-family-farms.md",
    "Grocery List.md",
]

# --- things that must not be -----------------------------------------------
OUT_OF_SCOPE = [
    # traversal, plain and encoded-after-decode
    "../../etc/passwd",
    "../flakes/secrets.yaml",
    "Keep/../../etc/shadow.md",
    "Keep/../Journal/Day/2026-11-08.md",
    "Keep/subdir/../../../root/.ssh/id_ed25519.md",
    "./../../var/lib/tailscale/tailscaled.state.md",
    # absolute paths
    "/etc/passwd",
    "/var/lib/silverbullet/Keep/x.md",
    # the agent's own pages — in the space, still not ours to rewrite
    "Journal/Day/2026-11-08.md",
    "Areas/Newsdesk/2026-11-07-longread.md",
    "System/Agent Queue.md",
    "Library/mrmugame/Silversearch/silversearch.plug.js",
    "Projects/Side Business.md",
    "CONFIG.md",
    "CONVENTIONS.md",
    # the capture log is a record, not a card
    "Inbox/Log.md",
    "Inbox/Log/2026-11.md",
    # non-markdown, including the space's own index
    "Keep/notes.db",
    "Keep/index.html",
    ".silverbullet.db",
    "Keep/x.md.swp",
    # degenerate
    "",
    "..",
    ".",
    "Keep/",
    "Keep",
    "\x00Keep/x.md",
    "Keep/\x00x.md",
]


@pytest.mark.parametrize("rel", IN_SCOPE)
def test_in_scope(rel):
    assert paths.in_scope(rel), rel


@pytest.mark.parametrize("rel", OUT_OF_SCOPE)
def test_out_of_scope(rel):
    assert not paths.in_scope(rel), rel


@pytest.mark.parametrize("rel", OUT_OF_SCOPE)
def test_resolve_refuses(tmp_path, rel):
    with pytest.raises(paths.PathError):
        paths.resolve(tmp_path, rel)


def test_resolve_maps_into_space(tmp_path):
    got = paths.resolve(tmp_path, "Keep/a.md")
    assert got == (tmp_path / "Keep" / "a.md").resolve()


def test_symlink_out_of_space_is_refused(tmp_path):
    """A symlink inside the space pointing out of it must not be followed."""
    space = tmp_path / "space"
    (space / "Keep").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("no")
    (space / "Keep" / "link.md").symlink_to(outside / "secret.md")
    with pytest.raises(paths.PathError):
        paths.resolve(space, "Keep/link.md")


def test_symlinked_directory_out_of_space_is_refused(tmp_path):
    space = tmp_path / "space"
    (space).mkdir()
    outside = tmp_path / "outside"
    (outside / "deep").mkdir(parents=True)
    (space / "Keep").symlink_to(outside, target_is_directory=True)
    with pytest.raises(paths.PathError):
        paths.resolve(space, "Keep/deep/x.md")


def test_trash_only_with_the_flag(tmp_path):
    assert not paths.in_scope("Trash/Keep/a.md")
    assert paths.in_scope("Trash/Keep/a.md", allow_trash=True)
    with pytest.raises(paths.PathError):
        paths.resolve(tmp_path, "Trash/Keep/a.md")
    assert paths.resolve(tmp_path, "Trash/Keep/a.md", allow_trash=True)


def test_relative_round_trip(tmp_path):
    p = paths.resolve(tmp_path, "Notes/x.md")
    assert paths.relative(tmp_path, p) == "Notes/x.md"


# --- the handler that turns a refusal into an answer -------------------------

def test_an_out_of_scope_id_answers_404_rather_than_crashing():
    """The boundary must REFUSE, not crash.

    A block edit once dropped the PathError handler, and every rejected id
    became a 500 with a traceback in the journal. The scoping still worked —
    it just stopped being able to say so. This asserts the handler is
    registered AND what it answers, which is the pair that regressed.
    """
    import asyncio

    from notes import paths
    from notes.app import create_app
    from notes.config import Settings

    app = create_app(Settings(space_dir="/nonexistent"))
    handler = app.exception_handlers.get(paths.PathError)
    assert handler is not None, "an unhandled PathError is a 500"

    reply = asyncio.run(handler(None, paths.PathError("out of scope: Journal/secret.md")))
    assert reply.status_code == 404
    assert b"no such note" in reply.body
    # The reply must not echo back which path was asked for, or what rule it
    # broke: a refusal that explains itself is a map of what exists.
    assert b"Journal" not in reply.body
