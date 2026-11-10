"""User accounts: ids, names, emails, and the table they live in."""

from __future__ import annotations

import pytest

from notes import users
from notes.users import People, User


TABLE = """# People

Prose that must survive a rewrite.

| Username | Display name | Emails |
| --- | --- | --- |
| ana | Ana Parker | ana@example.com, a.parker@work.example |
| sam | Sam Reed | sam@example.com |
| maryj | Mary | maryj@example.com |
| marys | Mary | marys@example.com |

A trailing note.
"""


# ------------------------------------------------------------------- ids

@pytest.mark.parametrize("raw,want", [
    ("Ana", "ana"),
    ("Ana Parker", "ana-parker"),
    ("ana@example.com", "ana"),
    ("  ANA  ", "ana"),
    ("github:aparker", "github-aparker"),
    ("1234", None),            # must start with a letter
    ("", None),
    (None, None),
    ("@example.com", None),
    ("x" * 40, "x" * 32),
])
def test_make_username_is_only_a_suggestion(raw, want):
    assert users.make_username(raw) == want


def test_two_people_called_mary_get_different_ids():
    """The situation that broke the previous model."""
    p = People()
    first = p.suggest_username("Mary")
    p.add(first, "Mary")
    second = p.suggest_username("Mary")
    assert first == "mary" and second == "mary2"
    p.add(second, "Mary")
    assert [u.username for u in p.users] == ["mary", "mary2"]
    assert [u.label for u in p.users] == ["Mary", "Mary"], "a NAME may repeat"


def test_an_id_cannot_be_taken_twice():
    p = people_from(TABLE)
    with pytest.raises(ValueError):
        p.add("ana", "Someone Else")


def test_an_unusable_id_is_refused():
    p = People()
    for bad in ("", "1ana", "Ana Parker", "ana!", "-ana", None):
        with pytest.raises(ValueError):
            p.add(bad, "x")


def test_available_reports_both_usable_and_free():
    p = people_from(TABLE)
    assert p.available("newcomer") is True
    assert p.available("ana") is False, "taken"
    assert p.available("1bad") is False, "unusable"


def people_from(text):
    return users.parse(text)


# -------------------------------------------------------------- resolution

def test_a_sign_in_matching_an_id_is_that_account():
    p = people_from(TABLE)
    assert p.resolve("ana").username == "ana"
    assert p.resolve("ANA").username == "ana"


def test_a_sign_in_matching_an_email_is_that_account():
    p = people_from(TABLE)
    assert p.resolve("a.parker@work.example").username == "ana"
    assert p.resolve("A.Parker@Work.Example").username == "ana"


def test_an_unknown_sign_in_belongs_to_nobody():
    """It is a question for the person, not something to guess from spelling."""
    p = people_from(TABLE)
    assert p.resolve("ana@gmail.com") is None
    assert p.resolve("github:aparker") is None
    assert p.resolve("") is None
    assert p.resolve(None) is None


def test_resolution_never_invents_an_account():
    assert People().resolve("anyone@example.com") is None


# ------------------------------------------------------------------ emails

def test_an_email_belongs_to_one_account_at_a_time():
    """It is a sign-in; two accounts claiming it would be ambiguous."""
    p = people_from(TABLE)
    sam = p.get("sam")
    p.attach_email(sam, "a.parker@work.example")
    assert p.resolve("a.parker@work.example").username == "sam"
    assert "a.parker@work.example" not in p.get("ana").emails


def test_rubbish_is_not_an_email():
    p = People()
    u = p.add("ana", "Ana")
    for bad in ("", "ana", "ana@", "@example.com", "a b@c.com", None):
        assert p.attach_email(u, bad) is False
    assert u.emails == []


def test_detach():
    p = people_from(TABLE)
    ana = p.get("ana")
    assert p.detach_email(ana, "ana@example.com") is True
    assert p.detach_email(ana, "ana@example.com") is False
    assert p.resolve("ana@example.com") is None
    assert p.resolve("ana").username == "ana", "the id still works"


# ------------------------------------------------------------------- label

def test_the_label_falls_back_to_the_id_not_to_blank():
    p = People()
    u = p.add("ana", "")
    assert u.label == "ana"
    u.name = "Ana Parker"
    assert u.label == "Ana Parker"


# -------------------------------------------------------------- the file

def test_round_trip_keeps_the_prose_and_is_a_fixed_point():
    p = people_from(TABLE)
    out = users.render(p)
    assert "Prose that must survive a rewrite." in out
    assert "A trailing note." in out
    again = users.parse(out)
    assert [(u.username, u.name, u.emails) for u in again.users] == \
           [(u.username, u.name, u.emails) for u in sorted(p.users, key=lambda u: u.username)]
    assert users.render(again) == out


def test_a_duplicate_id_in_a_hand_edited_file_does_not_split_a_person():
    p = users.parse("""| Username | Display name | Emails |
|---|---|---|
| ana | Ana | ana@example.com |
| ana | Ana Again | other@example.com |
""")
    assert len([u for u in p.users if u.username == "ana"]) == 1
    assert p.get("ana").name == "Ana", "the first wins"


def test_a_ragged_hand_edited_table_still_reads():
    p = users.parse("""| Username | Display name | Emails |
|---|---|---|
|ana|Ana|
| sam | Sam | sam@example.com |
|  |  |  |
| bad id here | X | x@example.com |
| extra | Extra | e@example.com | and | more |
""")
    assert sorted(u.username for u in p.users) == ["ana", "extra", "sam"]


def test_a_pipe_in_a_name_cannot_split_a_cell():
    p = People()
    p.add("ana", "Ana | Parker")
    assert users.parse(users.render(p)).get("ana").name == "Ana / Parker"


def test_an_empty_file_gets_the_explanatory_default():
    out = users.render(People())
    assert "Everyone who uses this space" in out
    assert "| Username | Display name | Emails |" in out


# ---------------------------------------------------- migrating the old shape

LEGACY = """# People

| Identity | Person | Display name | First seen |
| --- | --- | --- | --- |
| ana@example.com | ana | Ana Parker | 2026-11-09 |
| github:aparker | ana |  | 2026-11-10 |
| sam | sam | Sam | 2026-11-09 |
| oidc:9f21c |  |  | 2026-11-10 |
"""


def test_the_old_identity_table_becomes_accounts():
    p = users.migrate_legacy(LEGACY)
    assert sorted(u.username for u in p.users) == ["ana", "sam"]
    assert p.get("ana").name == "Ana Parker"
    assert p.get("ana").emails == ["ana@example.com"], "only emails carry over"
    assert p.resolve("ana@example.com").username == "ana"
    assert p.resolve("sam").username == "sam"


def test_an_unclaimed_legacy_identity_makes_no_account():
    p = users.migrate_legacy(LEGACY)
    assert p.get("oidc-9f21c") is None
    assert p.resolve("oidc:9f21c") is None


def test_a_new_table_is_not_mistaken_for_an_old_one():
    assert users.migrate_legacy(TABLE) is None


def test_a_two_person_table_with_nothing_but_names_migrates():
    """The common shape: two people, no emails, no display names set."""
    p = users.migrate_legacy("""| Identity | Person | Display name | First seen |
| --- | --- | --- | --- |
| ana | ana |  | 2026-11-10 |
| dana | dana |  | 2026-11-10 |
""")
    assert [u.username for u in p.users] == ["ana", "dana"], "file order is kept"
    assert [u.label for u in p.users] == ["ana", "dana"], "the username serves as a name"


def test_a_migrated_table_is_flagged_so_it_can_be_persisted():
    """Otherwise the file on disk never converges on the documented shape."""
    p = users.migrate_legacy(LEGACY)
    assert p is not None
    p.migrated = True          # load() sets this; render/parse must not care
    again = users.parse(users.render(p))
    assert again.migrated is False, "a file already in the new shape is not a migration"
    assert sorted(u.username for u in again.users) == ["ana", "sam"]
