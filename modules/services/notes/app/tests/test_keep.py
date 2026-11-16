"""Importing a Keep Takeout: what counts as already here, whose a note is,
and that a hostile zip can do no harm. All content is invented."""

from __future__ import annotations

import io
import json
import zipfile

import pytest

from notes import keep, store, users

DAY = 1_700_000_000_000_000          # 2023-11-14, in Keep's microseconds


def note(n: int, **kw) -> dict:
    d = {"createdTimestampUsec": DAY + n * 86_400_000_000,
         "userEditedTimestampUsec": DAY + n * 86_400_000_000,
         "title": f"Note {n}", "textContent": f"body of note {n}",
         "isArchived": False, "isPinned": False, "isTrashed": False}
    d.update(kw)
    return d


def takeout(*notes: dict, extra: dict[str, bytes] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i, d in enumerate(notes):
            z.writestr(f"Takeout/Keep/{i}.json", json.dumps(d))
            z.writestr(f"Takeout/Keep/{i}.html", "<html/>")
        for k, v in (extra or {}).items():
            z.writestr(k, v)
    return buf.getvalue()


@pytest.fixture
def space(tmp_path):
    (tmp_path / "Keep").mkdir()
    (tmp_path / "Trash" / "Keep").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def people():
    p = users.People()
    p.add("sam", "Sam", ["sam@example.com"])
    p.add("ana", "Ana", ["ana@example.com"])
    return p


def run(space, people, data, importer="ana", archive_all=False):
    pl = keep.plan(space, importer, [("t.zip", keep.read_zip(data))])
    return pl, keep.apply(space, pl, people, archive_all)


# ------------------------------------------------------------------ reading

def test_reads_prose_and_checklists():
    notes = keep.read_zip(takeout(
        note(1),
        note(2, textContent=None, listContent=[
            {"text": "flour", "isChecked": True}, {"text": "eggs", "isChecked": False}])))
    by = {n.title: n for n in notes}
    assert by["Note 1"].body == "body of note 1"
    assert by["Note 2"].body == "- [x] flour\n- [ ] eggs"


def test_a_zip_with_no_notes_is_not_an_error():
    """Takeout splits into parts; one may hold only the HTML index."""
    assert keep.read_zip(takeout(extra={"Takeout/archive_browser.html": b"<html/>"})) == []


def test_not_a_zip_is_refused():
    with pytest.raises(keep.ImportError_):
        keep.read_zip(b"this is not a zip")


def test_json_outside_a_keep_folder_is_ignored():
    data = takeout(extra={"Takeout/Drive/x.json": json.dumps(note(9)).encode()})
    assert keep.read_zip(data) == []


def test_an_oversized_member_is_skipped_not_loaded():
    big = json.dumps(note(1, textContent="x" * (keep.MAX_MEMBER + 10))).encode()
    assert keep.read_zip(takeout(extra={"Takeout/Keep/big.json": big})) == []


# ------------------------------------------------------- already here or not

def test_a_fresh_import_writes_everything(space, people):
    pl, written = run(space, people, takeout(note(1), note(2)))
    assert len(written) == 2
    text = (space / written[0]).read_text()
    assert "keep_id:" in text and "tags: keep-import" in text


def test_importing_the_same_export_twice_duplicates_nothing(space, people):
    data = takeout(note(1), note(2), note(3))
    run(space, people, data)
    pl, written = run(space, people, data)
    assert written == [] and pl.have_id == 3
    assert len(list((space / "Keep").glob("*.md"))) == 3


def test_a_deleted_note_does_not_come_back(space, people):
    """Someone deliberately put it in the trash; an import must not undo that."""
    _, written = run(space, people, takeout(note(1)))
    (space / written[0]).rename(space / "Trash" / written[0])
    pl, again = run(space, people, takeout(note(1)))
    assert again == [] and pl.have_trashed == 1


def test_a_note_imported_without_an_id_is_matched_by_body(space, people):
    (space / "Keep" / "old.md").write_text("# A different title\n\nbody of note 4\n")
    pl, written = run(space, people, takeout(note(4)))
    assert written == [] and pl.have_body == 1


def test_trashed_and_empty_notes_are_skipped(space, people):
    pl, written = run(space, people, takeout(
        note(1, isTrashed=True), note(2, title="", textContent="")))
    assert written == [] and pl.trashed_in_keep == 1 and pl.empty == 1


def test_the_same_note_in_two_uploaded_files_counts_once(space, people):
    a = keep.read_zip(takeout(note(1)))
    b = keep.read_zip(takeout(note(1)))
    pl = keep.plan(space, "ana", [("a.zip", a), ("b.zip", b)])
    assert len(pl.new) == 1


LATER = DAY + 40 * 86_400_000_000


def conflicted(space, people):
    """Import a note, edit it here, then bring an export where Keep edited it too."""
    _, w = run(space, people, takeout(note(1)))
    store.write(space, w[0], body="edited here")
    pl = keep.plan(space, "ana", [("t.zip", keep.read_zip(takeout(
        note(1, title="Note 1 renamed", textContent="edited in keep", userEditedTimestampUsec=LATER))))])
    assert len(pl.conflicts) == 1
    return pl, w[0]


def test_a_conflict_shows_both_sides(space, people):
    pl, rel = conflicted(space, people)
    c = pl.summary()["conflicts"][0]
    assert c["here"]["text"] == "edited here" and c["keep"]["text"] == "edited in keep"
    assert c["keep"]["edited"] > c["here"]["edited"]
    assert keep.apply(space, pl, people, False) == [], "a conflict is never a new note"


def test_keep_this_one_changes_nothing_but_is_not_asked_again(space, people):
    pl, rel = conflicted(space, people)
    keep.resolve(space, pl, people, {}, "here")
    assert store.read(space, rel).body == "edited here"
    again = keep.plan(space, "ana", [("t.zip", keep.read_zip(takeout(
        note(1, textContent="edited in keep", userEditedTimestampUsec=LATER))))])
    assert again.conflicts == [], "an answered conflict must stay answered"


def test_use_keeps_version_replaces_text_but_not_ownership(space, people):
    pl, rel = conflicted(space, people)
    store.write(space, rel, archived=True, actor="sam")
    keep.resolve(space, pl, people, {pl.conflicts[0].note.keep_id: "keep"})
    n = store.read(space, rel)
    assert n.body == "edited in keep" and n.title == "Note 1 renamed"
    assert n.archived_by == ["sam"], "someone's archive state is not Keep's to change"
    assert "keep_id:" in (space / rel).read_text()


def test_keep_both_adds_a_marked_copy(space, people):
    pl, rel = conflicted(space, people)
    keep.resolve(space, pl, people, {}, "both")
    assert store.read(space, rel).body == "edited here"
    copies = [p for p in (space / "Keep").glob("*from Keep*.md")]
    assert len(copies) == 1
    text = copies[0].read_text()
    assert "keep_copy_of:" in text and "\nkeep_id:" not in text
    assert "edited in keep" in text
    # and the next import neither re-raises it nor imports the copy again
    again = keep.plan(space, "ana", [("t.zip", keep.read_zip(takeout(
        note(1, textContent="edited in keep", userEditedTimestampUsec=LATER))))])
    assert again.conflicts == [] and again.new == []


def test_a_newer_date_with_the_same_text_is_not_a_conflict(space, people):
    """Keep moves the edit date without changing the text; nothing to choose."""
    run(space, people, takeout(note(1)))
    pl = keep.plan(space, "ana", [("t.zip", keep.read_zip(takeout(note(1, userEditedTimestampUsec=LATER))))])
    assert pl.conflicts == [] and len(pl.same_text) == 1
    keep.resolve(space, pl, people, {})
    again = keep.plan(space, "ana", [("t.zip", keep.read_zip(takeout(note(1, userEditedTimestampUsec=LATER))))])
    assert again.same_text == [], "settled once, not on every import"


def test_an_unknown_choice_falls_back_to_keeping_this_one(space, people):
    pl, rel = conflicted(space, people)
    keep.resolve(space, pl, people, {pl.conflicts[0].note.keep_id: "delete-everything"})
    assert store.read(space, rel).body == "edited here"


def test_a_stale_plan_cannot_duplicate(space, people):
    """Plan, then someone else imports the same note, then the first applies."""
    pl = keep.plan(space, "ana", [("t.zip", keep.read_zip(takeout(note(1))))])
    run(space, people, takeout(note(1)))
    assert keep.apply(space, pl, people, False) == []


# --------------------------------------------------------------- whose it is

def test_the_importer_is_the_author(space, people):
    _, w = run(space, people, takeout(note(1)), importer="ana")
    assert store.read(space, w[0]).author == "ana"


def test_a_flagged_owner_with_an_account_is_the_author(space, people):
    shared = note(1, sharees=[{"email": "sam@example.com", "isOwner": True},
                              {"email": "ana@example.com"}])
    _, w = run(space, people, takeout(shared), importer="ana")
    n = store.read(space, w[0])
    assert n.author == "sam" and n.shared is True


def test_an_owner_with_no_account_falls_back_to_the_importer(space, people):
    shared = note(1, sharees=[{"email": "stranger@example.com", "isOwner": True}])
    _, w = run(space, people, takeout(shared), importer="ana")
    assert store.read(space, w[0]).author == "ana"


def test_archive_and_pin_are_the_importers_alone(space, people):
    _, w = run(space, people, takeout(note(1, isArchived=True), note(2, isPinned=True)))
    by = {store.read(space, r).title: store.read(space, r) for r in w}
    assert by["Note 1"].archived_by == ["ana"]
    assert by["Note 2"].pinned_by == ["ana"]


def test_archive_all_files_a_bulk_history_out_of_the_way(space, people):
    _, w = run(space, people, takeout(note(1, isPinned=True), note(2)), archive_all=True)
    for r in w:
        n = store.read(space, r)
        assert n.archived_by == ["ana"] and n.pinned_by == []


def test_a_keep_label_becomes_the_category(space, people):
    _, w = run(space, people, takeout(note(1, labels=[{"name": "Garden Beds"}])))
    assert store.read(space, w[0]).cat == "garden-beds"


def test_an_untitled_note_gets_no_invented_heading(space, people):
    _, w = run(space, people, takeout(note(1, title="")))
    assert "\n# " not in (space / w[0]).read_text()
    assert store.read(space, w[0]).title == ""


def test_a_hostile_title_cannot_escape_the_keep_folder(space, people):
    _, w = run(space, people, takeout(note(1, title="../../../etc/passwd")))
    assert w[0].startswith("Keep/") and "/" not in w[0][len("Keep/"):]
    assert (space / w[0]).resolve().parent == (space / "Keep").resolve()
