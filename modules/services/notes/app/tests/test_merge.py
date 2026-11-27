"""Two people saving the same note must not erase each other."""

from __future__ import annotations

from notes.merge import merge3, reconcile

BASE = "- [ ] apples\n- [ ] bread\n- [ ] coffee"


def test_untouched_file_takes_the_save_as_sent():
    body, merged, clean = reconcile(BASE, BASE + "\n- [ ] eggs", BASE)
    assert (body, merged, clean) == (BASE + "\n- [ ] eggs", False, True)


def test_no_base_is_the_old_behaviour():
    body, merged, _ = reconcile(None, "mine", "theirs")
    assert (body, merged) == ("mine", False)


def test_both_add_to_the_end_keeps_both():
    # The grocery-list case: two phones, each adding an item.
    mine = BASE + "\n- [ ] eggs"
    theirs = BASE + "\n- [ ] milk"
    body, merged, _ = reconcile(BASE, mine, theirs)
    assert merged
    assert "- [ ] eggs" in body and "- [ ] milk" in body
    assert body.count("- [ ] apples") == 1


def test_edits_in_different_places_merge_cleanly():
    mine = BASE.replace("apples", "green apples")
    theirs = BASE + "\n- [ ] milk"
    body, clean = merge3(BASE, mine, theirs)
    assert clean
    assert body == "- [ ] green apples\n- [ ] bread\n- [ ] coffee\n- [ ] milk"


def test_a_tick_and_an_addition_both_survive():
    mine = BASE.replace("[ ] bread", "[x] bread")
    theirs = "- [ ] apples\n- [ ] bread\n- [ ] butter\n- [ ] coffee"
    body, clean = merge3(BASE, mine, theirs)
    assert clean
    assert body == "- [ ] apples\n- [x] bread\n- [ ] butter\n- [ ] coffee"


def test_a_deletion_on_one_side_holds():
    mine = "- [ ] apples\n- [ ] coffee"            # bread removed
    theirs = BASE + "\n- [ ] milk"
    body, clean = merge3(BASE, mine, theirs)
    assert clean
    assert "bread" not in body and "milk" in body


def test_same_line_changed_both_ways_keeps_both_versions():
    mine = BASE.replace("coffee", "coffee (decaf)")
    theirs = BASE.replace("coffee", "coffee x2")
    body, clean = merge3(BASE, mine, theirs)
    assert not clean
    assert "coffee x2" in body and "coffee (decaf)" in body


def test_whitespace_alone_is_not_a_conflict():
    # The store and the editor can disagree on trailing blanks; that is not
    # somebody else's edit.
    body, merged, _ = reconcile(BASE + "\n", BASE + "\n- [ ] eggs", BASE + "  \n\n")
    assert not merged and body.endswith("eggs")


def test_identical_change_on_both_sides_is_not_doubled():
    mine = theirs = BASE + "\n- [ ] eggs"
    body, merged, _ = reconcile(BASE, mine, theirs)
    assert body.count("eggs") == 1


def test_empty_base_new_note_two_writers():
    body, clean = merge3("", "a\nb", "c")
    assert set(body.split("\n")) == {"a", "b", "c"}
