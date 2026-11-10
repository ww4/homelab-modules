"""Store behaviour, with the real shapes found in the space.

The fixtures below are trimmed copies of actual pages: a Keep-import note with
hand-written frontmatter, a sectioned checklist with no frontmatter and no h1,
and a plain bare-lines note (which is what most of the 556 Keep pages are).
"""

from pathlib import Path

import pytest

from notes import paths, store

KEEP_NOTE = """---
date: 2017-03-04
category: logistics
source: Keep.json
tags: keep-import
---

# Bring from the cabin

*Google Keep, 2017-03-04 · archived*

Exercise mat
Monitor
Spices
"""

GROCERY = """## Walmart
- [ ] Bread

## Kroger

## Frankfort house
*(pantry/storage at dad's — grab on the next visit)*
- [ ] Diapers
"""


@pytest.fixture
def space(tmp_path):
    (tmp_path / "Keep").mkdir()
    (tmp_path / "Notes").mkdir()
    (tmp_path / "Inbox" / "Log").mkdir(parents=True)
    (tmp_path / "Journal").mkdir()
    (tmp_path / "Keep" / "2017-03-04 Bring from the cabin.md").write_text(KEEP_NOTE)
    (tmp_path / "Grocery List.md").write_text(GROCERY)
    (tmp_path / "Inbox" / "Log" / "2026-11.md").write_text("# log\nnot a card\n")
    (tmp_path / "Journal" / "2026-11-08.md").write_text("# day\n")
    return tmp_path


def test_reads_frontmatter_title_and_date(space):
    n = store.read(space, "Keep/2017-03-04 Bring from the cabin.md")
    assert n.title == "Bring from the cabin"
    assert n.date == "2017-03-04"
    assert n.cat == "logistics"
    assert n.pinned is False and n.archived is False
    assert n.body.splitlines()[-3:] == ["Exercise mat", "Monitor", "Spices"]
    assert not n.body.startswith("# ")


def test_title_falls_back_to_filename_without_the_date(space):
    (space / "Keep" / "2015-10-14 website file notes.md").write_text("a\nb\n")
    n = store.read(space, "Keep/2015-10-14 website file notes.md")
    assert n.title == "website file notes"
    assert n.had_h1 is False


def test_no_frontmatter_no_h1_survives_a_write(space):
    """Grocery List has neither; a pin must not grow an h1 or reorder it."""
    before = (space / "Grocery List.md").read_text()
    store.write(space, "Grocery List.md", pinned=True)
    after = (space / "Grocery List.md").read_text()
    assert after.startswith("---\npinned_by: sam\n---\n")
    assert "## Walmart" in after and "## Frankfort house" in after
    assert "# Grocery List" not in after          # no invented heading
    assert before.strip() in after                # body untouched


def test_unknown_frontmatter_keys_are_preserved_verbatim(space):
    rel = "Keep/2017-03-04 Bring from the cabin.md"
    store.write(space, rel, pinned=True)
    text = (space / rel).read_text()
    for line in ("date: 2017-03-04", "category: logistics",
                 "source: Keep.json", "tags: keep-import"):
        assert line in text, line
    assert "pinned_by: sam" in text


def test_unpinning_drops_the_key_rather_than_writing_false(space):
    rel = "Keep/2017-03-04 Bring from the cabin.md"
    store.write(space, rel, pinned=True)
    store.write(space, rel, pinned=False)
    text = (space / rel).read_text()
    assert "pinned" not in text
    assert "category: logistics" in text


def test_body_round_trips_without_drift(space):
    rel = "Keep/2017-03-04 Bring from the cabin.md"
    first = store.read(space, rel)
    store.write(space, rel, body=first.body)
    second = store.read(space, rel)
    assert second.body == first.body
    assert second.title == first.title


def test_checkbox_edits_land_in_the_file(space):
    store.write(space, "Grocery List.md",
                body=GROCERY.replace("- [ ] Bread", "- [x] Bread"))
    assert "- [x] Bread" in (space / "Grocery List.md").read_text()


def test_retitling_a_note_without_an_h1_adds_one(space):
    rel = "Keep/2017-03-04 Bring from the cabin.md"
    store.write(space, rel, title="Bring from KY")
    assert (space / rel).read_text().count("# Bring from KY") == 1


def test_create_lands_in_inbox_and_is_unique(space):
    a = store.create(space, "paint list", "blue pearl")
    b = store.create(space, "paint list", "white")
    assert a.id.startswith("Inbox/") and a.id.endswith(".md")
    assert a.id != b.id
    assert store.read(space, a.id).body == "blue pearl"


def test_create_slug_strips_path_characters(space):
    n = store.create(space, "../../etc/passwd", "x")
    assert paths.in_scope(n.id)
    assert ".." not in n.id


def test_trash_moves_and_restore_returns(space):
    rel = "Keep/2017-03-04 Bring from the cabin.md"
    trashed = store.trash(space, rel)
    assert not (space / rel).exists()
    assert (space / trashed).exists()
    restored = store.restore(space, trashed)
    assert restored == rel
    assert (space / rel).exists()


def test_restore_refuses_to_clobber(space):
    rel = "Keep/2017-03-04 Bring from the cabin.md"
    trashed = store.trash(space, rel)
    (space / rel).write_text("something else\n")
    with pytest.raises(paths.PathError):
        store.restore(space, trashed)


def test_listing_excludes_the_agents_pages(space):
    ids = {n.id for n in store.listing(space)}
    assert "Keep/2017-03-04 Bring from the cabin.md" in ids
    assert "Grocery List.md" in ids
    assert not any(i.startswith("Journal/") for i in ids)
    assert not any(i.startswith("Inbox/Log") for i in ids)


def test_atomic_write_leaves_no_temp_files(space):
    store.write(space, "Grocery List.md", pinned=True)
    assert not list(space.glob(".notes-*"))


# --- authorship -------------------------------------------------------------
# Only the agent's own notes carry the key; unlabelled means the owner's. The
# default wall filter depends on this, so a silent flip either way is a bug
# that hides the default author's notes or buries them again.

def test_unlabelled_notes_are_the_default_author(space):
    assert store.read(space, "Keep/2017-03-04 Bring from the cabin.md").author == "sam"
    assert store.read(space, "Grocery List.md").author == "sam"


def test_author_key_is_read(space):
    rel = "Notes/agent page.md"
    (space / rel).write_text("---\nauthor: claude\n---\n\n# agent page\n\nbody\n")
    assert store.read(space, rel).author == "claude"


def test_author_is_case_and_space_insensitive(space):
    rel = "Notes/odd.md"
    (space / rel).write_text("---\nauthor:  Claude \n---\n\nbody\n")
    assert store.read(space, rel).author == "claude"


def test_author_survives_an_unrelated_write(space):
    """Pinning an agent note must not quietly re-attribute it to the owner."""
    rel = "Notes/agent page.md"
    (space / rel).write_text("---\nauthor: claude\ncategory: homelab\n---\n\nbody\n")
    store.write(space, rel, pinned=True)
    n = store.read(space, rel)
    assert n.author == "claude" and n.pinned is True
    assert "author: claude" in (space / rel).read_text()


def test_notes_created_here_use_the_default_author(space):
    """Anything made in this app is theirs by construction — no key written."""
    n = store.create(space, "deck screws", "3 inch")
    assert n.author == "sam"
    assert "author:" not in (space / n.id).read_text()


# --- previews, search and the cache ------------------------------------------
# The wall is sent summaries, not bodies. Two things must hold: the preview is
# never mistaken for the note, and search still sees the whole text.

def test_summary_carries_a_preview_not_the_body(space):
    n = store.read(space, "Keep/2017-03-04 Bring from the cabin.md").summary()
    assert "body" not in n
    assert n["preview"].startswith("*Google Keep")
    assert n["lines"] == 4 and n["truncated"] is False


def test_long_notes_are_truncated_in_the_summary_only(space):
    rel = "Notes/long.md"
    body = "\n".join("line %d padding padding padding" % i for i in range(400))
    (space / rel).write_text("# long\n\n" + body + "\n")
    s = store.read(space, rel).summary()
    assert s["truncated"] is True
    assert len(s["preview"]) == store.PREVIEW_CHARS
    assert s["lines"] == 400
    # the real note is untouched
    assert len(store.read(space, rel).body) > store.PREVIEW_CHARS


def test_search_finds_text_past_the_preview(space):
    """The whole point of searching server-side rather than in the client."""
    rel = "Notes/long.md"
    body = "\n".join("filler line %d" % i for i in range(400))
    (space / rel).write_text("# long\n\n" + body + "\n\nxyzzy-needle\n")
    assert store.PREVIEW_CHARS < (space / rel).stat().st_size
    hits = [n.id for n in store.listing(space, "xyzzy-needle")]
    assert hits == [rel]


def test_search_matches_titles_and_is_case_insensitive(space):
    hits = [n.id for n in store.listing(space, "BRING FROM the cabin")]
    assert "Keep/2017-03-04 Bring from the cabin.md" in hits


def test_empty_query_lists_everything(space):
    assert len(store.listing(space, "")) == len(store.listing(space))
    assert len(store.listing(space, None)) == len(store.listing(space))


def test_cache_notices_a_write_by_another_party(space):
    """SilverBullet, the daybook and agent sessions write this space too.

    The cache key is the file's own (mtime_ns, size), so an edit made outside
    this service must show up on the next listing — a stale card here would be
    the service quietly disagreeing with the disk.
    """
    rel = "Keep/2017-03-04 Bring from the cabin.md"
    assert store.listing(space, "Spices")
    # somebody else rewrites the file
    (space / rel).write_text(KEEP_NOTE.replace("Spices", "Cardamom"))
    titles = {n.id: n.body for n in store.listing(space)}
    assert "Cardamom" in titles[rel] and "Spices" not in titles[rel]
    assert store.listing(space, "Cardamom")
    assert not store.listing(space, "Spices")


def test_cache_drops_entries_for_deleted_files(space):
    rel = "Keep/2017-03-04 Bring from the cabin.md"
    assert any(n.id == rel for n in store.listing(space))
    store.trash(space, rel)
    assert not any(n.id == rel for n in store.listing(space))


# --- categories --------------------------------------------------------------

def test_category_can_be_set_and_is_slugged(space):
    rel = "Keep/2017-03-04 Bring from the cabin.md"
    n = store.write(space, rel, category="House Project")
    assert n.cat == "house-project"
    assert "category: house-project" in (space / rel).read_text()


def test_setting_a_category_replaces_rather_than_duplicates(space):
    rel = "Keep/2017-03-04 Bring from the cabin.md"
    store.write(space, rel, category="travel")
    store.write(space, rel, category="packing")
    text = (space / rel).read_text()
    assert text.count("category:") == 1 and "category: packing" in text


def test_empty_category_clears_the_key(space):
    rel = "Keep/2017-03-04 Bring from the cabin.md"
    store.write(space, rel, category="")
    assert "category:" not in (space / rel).read_text()
    # falls back to the directory default rather than an empty string
    assert store.read(space, rel).cat == "keep"


def test_category_cannot_smuggle_frontmatter(space):
    """A newline here would inject arbitrary keys into the file."""
    rel = "Keep/2017-03-04 Bring from the cabin.md"
    store.write(space, rel, category="x\nauthor: sam\npinned: true")
    text = (space / rel).read_text()
    assert "author: sam" not in text
    assert text.count("pinned:") == 0
    assert store.read(space, rel).author == "sam"  # unchanged, not injected


def test_census_counts_every_category_unfiltered(space):
    c = store.census(space)
    assert c["logistics"] >= 1 and c["list"] >= 1
    assert sum(c.values()) == len(store.listing(space))
    counts = list(c.values())
    assert counts == sorted(counts, reverse=True)   # busiest first


def test_search_returns_any_term_ranked_like_keep(space):
    """Keep's semantics, which is what the owner expects from this box.

    Searching "wood gas" must return the note that says "woodgas", the one
    that only says "wood", and the one that only says "gas" -- and rank the
    note matching both terms above the ones matching one.
    """
    (space / "Notes/both.md").write_text("# chunker\n\nwood chips feed the gas producer\n")
    (space / "Notes/woodonly.md").write_text("# shed\n\nstack the wood outside\n")
    (space / "Notes/gasonly.md").write_text("# truck\n\ncheck the gas gauge\n")
    (space / "Notes/joined.md").write_text("# rig\n\nwoodgas conversion notes\n")
    ids = [n.id for n in store.listing(space, "wood gas")]
    for rel in ("Notes/both.md", "Notes/woodonly.md", "Notes/gasonly.md", "Notes/joined.md"):
        assert rel in ids, rel
    # "woodgas" contains BOTH terms as substrings, so it ties with the note
    # that spells them separately — both must outrank the single-term notes.
    two = {"Notes/both.md", "Notes/joined.md"}
    one = {"Notes/woodonly.md", "Notes/gasonly.md"}
    assert set(ids[:2]) == two
    assert max(ids.index(i) for i in two) < min(ids.index(i) for i in one)


def test_search_matches_inside_a_word(space):
    """Keep highlighted "lion" inside "million" — substring, not whole word."""
    (space / "Notes/solar.md").write_text("# solar heat storage\n\n20,000 kwh = 68 million BTUs\n")
    assert "Notes/solar.md" in [n.id for n in store.listing(space, "lion")]


def test_title_hits_outrank_body_hits(space):
    (space / "Notes/titled.md").write_text("# gasifier rebuild\n\nnothing else\n")
    (space / "Notes/bodied.md").write_text("# shed\n\nthe gasifier is outside\n")
    ids = [n.id for n in store.listing(space, "gasifier")]
    assert ids.index("Notes/titled.md") < ids.index("Notes/bodied.md")


def test_a_term_matching_nothing_does_not_exclude_others(space):
    (space / "Notes/w.md").write_text("# shed\n\nwood\n")
    assert "Notes/w.md" in [n.id for n in store.listing(space, "wood zzzznope")]


def test_whole_word_hits_rank_above_in_word_hits(space):
    """"gas" matches gasket and gasoline too; those belong below the real ones."""
    (space / "Notes/real.md").write_text("# trailer\n\ncheck the gas level\n")
    (space / "Notes/inword.md").write_text("# trailer\n\nreplace the head gasket\n")
    ids = [n.id for n in store.listing(space, "gas")]
    assert "Notes/inword.md" in ids          # still found
    assert ids.index("Notes/real.md") < ids.index("Notes/inword.md")


# --- searching by category ---------------------------------------------------

def test_category_name_pulls_in_the_whole_category(space):
    """A note in the woodgas category counts as a hit for "woodgas" even if
    its text never says the word — the owner: "if the category name is a high
    grade match for the search term you should pull in the whole category"."""
    rel = "Notes/chunker.md"
    (space / rel).write_text("---\ncategory: woodgas\n---\n\n# chunker\n\nspare tire mount\n")
    ids = [n.id for n in store.listing(space, "woodgas")]
    assert rel in ids


def test_a_four_letter_prefix_of_a_category_counts(space):
    rel = "Notes/chunker.md"
    (space / rel).write_text("---\ncategory: woodgas\n---\n\n# chunker\n\nspare tire mount\n")
    assert rel in [n.id for n in store.listing(space, "wood")]


def test_a_short_prefix_does_not_drag_a_category_in(space):
    """"gas" must not pull every woodgas note; three letters is too loose."""
    rel = "Notes/chunker.md"
    (space / rel).write_text("---\ncategory: woodgas\n---\n\n# chunker\n\nspare tire mount\n")
    assert rel not in [n.id for n in store.listing(space, "gas")]


def test_a_hyphenated_category_matches_either_word(space):
    rel = "Notes/shed.md"
    (space / rel).write_text("---\ncategory: house-project\n---\n\n# shed\n\nnothing relevant\n")
    assert rel in [n.id for n in store.listing(space, "house")]
    assert rel in [n.id for n in store.listing(space, "project")]


def test_cat_prefix_filters_hard(space):
    (space / "Notes/a.md").write_text("---\ncategory: woodgas\n---\n\n# a\n\ntruck parts\n")
    (space / "Notes/b.md").write_text("---\ncategory: travel\n---\n\n# b\n\ntruck parts\n")
    assert [n.id for n in store.listing(space, "cat:woodgas")] == ["Notes/a.md"]
    # the filter composes with ordinary terms
    assert [n.id for n in store.listing(space, "cat:woodgas truck")] == ["Notes/a.md"]
    assert store.listing(space, "cat:woodgas unicorn") == []


def test_split_query_separates_filters_from_terms(space):
    assert store.split_query("cat:woodgas truck parts") == (["woodgas"], [], ["truck", "parts"])
    assert store.split_query("plain words") == ([], [], ["plain", "words"])
    assert store.split_query("cat:") == ([], [], ["cat:"])   # not a filter, just text
    assert store.split_query(None) == ([], [], [])


# --- who wrote it ------------------------------------------------------------
# Attribution between two people who trust each other, not authentication.
# the owner: "There's no secrets here to protect."

def test_create_stamps_a_named_author(space):
    n = store.create(space, "milk", "2%", author="ana")
    assert n.author == "ana"
    assert "author: ana" in (space / n.id).read_text()


def test_create_without_a_device_identity_stays_unlabelled(space):
    """545 notes predate this; unlabelled must keep reading as the owner."""
    n = store.create(space, "milk", "2%")
    assert n.author == "sam"
    assert "author:" not in (space / n.id).read_text()


def test_the_default_author_is_not_written_redundantly(space):
    n = store.create(space, "milk", "2%", author="sam")
    assert n.author == "sam"
    assert "author:" not in (space / n.id).read_text()


def test_by_filter_selects_one_persons_notes(space):
    store.create(space, "theirs", "x", author="ana")
    store.create(space, "mine", "y")
    ana = [n.title for n in store.listing(space, "by:ana")]
    assert ana == ["theirs"]
    mine = [n.title for n in store.listing(space, "by:sam")]
    assert "mine" in mine and "theirs" not in mine


def test_by_composes_with_terms_and_categories(space):
    store.create(space, "theirs bread", "bread", author="ana")
    store.create(space, "theirs paint", "paint", author="ana")
    assert [n.title for n in store.listing(space, "by:ana bread")] == ["theirs bread"]


def test_split_query_separates_all_three(space):
    assert store.split_query("cat:woodgas by:ana truck") == (["woodgas"], ["ana"], ["truck"])
    assert store.split_query("by:") == ([], [], ["by:"])


def test_authors_census_counts_everyone(space):
    store.create(space, "theirs", "x", author="ana")
    a = store.authors(space)
    assert a["ana"] == 1
    assert a["sam"] >= 1
    assert sum(a.values()) == len(store.listing(space))


def test_ownership_can_be_reassigned(space):
    n = store.create(space, "theirs", "x", author="ana")
    assert n.author == "ana"
    moved = store.write(space, n.id, author="sam")
    assert moved.author == "sam"
    assert "author:" not in (space / n.id).read_text()   # sam == unlabelled
    back = store.write(space, n.id, author="ana")
    assert back.author == "ana"
    assert (space / n.id).read_text().count("author:") == 1


def test_editing_a_note_never_changes_its_owner(space):
    """Ana adding a line to the owner's grocery list must not take it over."""
    n = store.create(space, "list", "bread", author="sam")
    store.write(space, n.id, body="bread\nmilk")
    assert store.read(space, n.id).author == "sam"
    m = store.create(space, "theirs", "x", author="ana")
    store.write(space, m.id, body="x\ny", pinned=True)
    assert store.read(space, m.id).author == "ana"


def test_reassigned_author_is_slugged(space):
    n = store.create(space, "x", "y")
    store.write(space, n.id, author="Ana Jo\npinned: true")
    text = (space / n.id).read_text()
    assert "author: anajopinnedtrue" in text or "author: anajo" in text
    assert text.count("pinned:") == 0


# --- the shared space --------------------------------------------------------
# "Ours" is a place a note is put, like archived -- not a computed union of
# everyone's notes. the owner, 2026-11-09: "I don't want 'ours' to be a mashup of
# every human note. I want that to be a shared space we can intentionally add
# things to, like 'archived'."

def test_notes_are_not_shared_by_default(space):
    n = store.create(space, "mine", "x", author="ana")
    assert n.shared is False
    assert "shared:" not in (space / n.id).read_text()


def test_sharing_is_a_flag_that_toggles(space):
    n = store.create(space, "list", "bread")
    on = store.write(space, n.id, shared=True)
    assert on.shared is True
    assert "shared: true" in (space / n.id).read_text()
    off = store.write(space, n.id, shared=False)
    assert off.shared is False
    assert "shared:" not in (space / n.id).read_text()


def test_sharing_does_not_change_who_owns_it(space):
    """A shared note still belongs to whoever wrote it."""
    n = store.create(space, "theirs", "x", author="ana")
    shared = store.write(space, n.id, shared=True)
    assert shared.author == "ana" and shared.shared is True


def test_sharing_is_independent_of_pinned_and_archived(space):
    n = store.create(space, "list", "bread")
    store.write(space, n.id, shared=True, pinned=True)
    got = store.read(space, n.id)
    assert got.shared and got.pinned and not got.archived
    store.write(space, n.id, archived=True)
    got = store.read(space, n.id)
    assert got.shared and got.pinned and got.archived


def test_shared_survives_an_edit(space):
    n = store.create(space, "list", "bread")
    store.write(space, n.id, shared=True)
    store.write(space, n.id, body="bread\nmilk")
    assert store.read(space, n.id).shared is True


def test_summary_carries_shared(space):
    n = store.create(space, "list", "bread")
    store.write(space, n.id, shared=True)
    assert store.read(space, n.id).summary()["shared"] is True


def test_trash_is_listable_and_restorable(space):
    """Deleting moves; the trash view is how that becomes recoverable by hand."""
    rel = "Keep/2017-03-04 Bring from the cabin.md"
    assert store.trash_listing(space) == []
    trashed = store.trash(space, rel)
    listed = store.trash_listing(space)
    assert [n.id for n in listed] == [trashed]
    assert "Spices" in listed[0].body            # content survived the move
    store.restore(space, trashed)
    assert store.trash_listing(space) == []


def test_trash_listing_is_empty_without_the_directory(space):
    assert store.trash_listing(space) == []


def test_trashed_notes_stay_out_of_the_normal_listing(space):
    rel = "Keep/2017-03-04 Bring from the cabin.md"
    store.trash(space, rel)
    assert not any(n.id.startswith("Trash/") for n in store.listing(space))
    assert not store.listing(space, "Spices")


# --- the census follows the feed --------------------------------------------
# A chip counting notes you are not looking at is a lie: with the wall on
# "mine", a chip reading 83 that yields 4 notes is worse than no chip.

def test_census_scoped_to_mine_counts_only_my_notes(space):
    store.create(space, "theirs", "x", author="ana")
    store.write(space, "Keep/2017-03-04 Bring from the cabin.md", category="travel")
    mine = store.census(space, scope="mine", me="sam", show_shared=False)
    theirs = store.census(space, scope="mine", me="ana", show_shared=False)
    assert theirs == {"inbox": 1}
    assert "inbox" not in mine or mine.get("inbox", 0) == 0
    assert mine.get("travel") == 1


def test_mine_includes_the_shared_space_when_asked(space):
    n = store.create(space, "theirs", "x", author="ana")
    store.write(space, n.id, shared=True)
    with_shared = store.census(space, scope="mine", me="sam", show_shared=True)
    without = store.census(space, scope="mine", me="sam", show_shared=False)
    assert with_shared.get("inbox") == 1
    assert without.get("inbox") is None


def test_census_all_counts_everything_including_the_agent(space):
    store.create(space, "theirs", "x", author="ana")
    (space / "Notes/agent.md").write_text("---\nauthor: claude\n---\n\n# a\n\nx\n")
    everything = store.census(space, scope="all")
    assert sum(everything.values()) == len(store.listing(space))


def test_census_shared_counts_only_the_shared_space(space):
    n = store.create(space, "list", "bread")
    store.write(space, n.id, shared=True)
    store.create(space, "private", "x")
    assert sum(store.census(space, scope="shared").values()) == 1


# --- untitled notes ----------------------------------------------------------
# Keep treats a title as optional and many notes have none. Those get
# a "<date> <keep_id>" FILENAME for uniqueness; that must never surface as a
# title (it did, on 347 of her cards).

def test_a_keep_id_filename_yields_no_title(space):
    rel = "Keep/2020-02-14 1581722144437000.md"
    (space / rel).write_text("---\ndate: 2020-02-14\n---\n\nmilk\neggs\n")
    n = store.read(space, rel)
    assert n.title == ""
    assert n.body == "milk\neggs"


def test_editing_an_untitled_note_does_not_invent_a_heading(space):
    rel = "Keep/2020-02-14 1581722144437000.md"
    (space / rel).write_text("---\ndate: 2020-02-14\n---\n\nmilk\n")
    store.write(space, rel, body="milk\neggs")
    text = (space / rel).read_text()
    assert "# " not in text
    assert store.read(space, rel).title == ""


def test_giving_an_untitled_note_a_title_writes_a_heading(space):
    rel = "Keep/2020-02-14 1581722144437000.md"
    (space / rel).write_text("---\ndate: 2020-02-14\n---\n\nmilk\n")
    store.write(space, rel, title="Shopping")
    assert "# Shopping" in (space / rel).read_text()
    assert store.read(space, rel).title == "Shopping"


def test_a_normal_filename_still_yields_a_title(space):
    n = store.read(space, "Keep/2017-03-04 Bring from the cabin.md")
    assert n.title == "Bring from the cabin"


def test_a_new_note_with_no_title_gets_no_heading(space):
    n = store.create(space, "", "just a thought")
    assert n.title == ""
    text = (space / n.id).read_text()
    assert "# " not in text and "Untitled" not in text
    assert store.read(space, n.id).body == "just a thought"


# --- cat:none ----------------------------------------------------------------
# An unfiled note inherits its folder name as a display category, so without a
# "was it actually filed" bit, "uncategorised" cannot be asked for at all.

def test_filed_distinguishes_chosen_from_inherited(space):
    rel = "Keep/2017-03-04 Bring from the cabin.md"
    assert store.read(space, rel).filed is True          # has category: logistics
    (space / "Keep/loose.md").write_text("# loose\n\nnothing\n")
    loose = store.read(space, "Keep/loose.md")
    assert loose.filed is False
    assert loose.cat == "keep"                            # still displays a folder


def test_cat_none_finds_only_unfiled_notes(space):
    (space / "Keep/loose.md").write_text("# loose\n\nnothing\n")
    ids = [n.id for n in store.listing(space, "cat:none")]
    assert "Keep/loose.md" in ids
    assert "Keep/2017-03-04 Bring from the cabin.md" not in ids


def test_cat_none_composes_with_terms(space):
    (space / "Keep/loose.md").write_text("# loose\n\nxyzzy\n")
    (space / "Keep/loose2.md").write_text("# loose2\n\nsomething else\n")
    assert [n.id for n in store.listing(space, "cat:none xyzzy")] == ["Keep/loose.md"]


def test_filing_a_note_moves_it_out_of_cat_none(space):
    (space / "Keep/loose.md").write_text("# loose\n\nnothing\n")
    store.write(space, "Keep/loose.md", category="logistics")
    assert "Keep/loose.md" not in [n.id for n in store.listing(space, "cat:none")]
    assert store.read(space, "Keep/loose.md").filed is True


# --- archive is personal, trash is not ---------------------------------------
# The question was what happens when you archive a SHARED note. It used to archive
# for the other person too, because the flag lived on the one file both read.

def test_archiving_names_who_did_it(space):
    n = store.create(space, "list", "bread")
    store.write(space, n.id, shared=True)
    store.write(space, n.id, archived=True, actor="sam")
    got = store.read(space, n.id)
    assert got.archived_by == ["sam"]
    assert "archived_by: sam" in (space / n.id).read_text()


def test_one_person_archiving_leaves_the_other_alone(space):
    n = store.create(space, "list", "bread")
    store.write(space, n.id, shared=True)
    store.write(space, n.id, archived=True, actor="sam")
    assert store.read(space, n.id).archived_by == ["sam"]   # not ana
    store.write(space, n.id, archived=True, actor="ana")
    assert store.read(space, n.id).archived_by == ["ana", "sam"]
    store.write(space, n.id, archived=False, actor="sam")
    assert store.read(space, n.id).archived_by == ["ana"]    # theirs survives


def test_unarchiving_the_last_person_clears_the_key(space):
    n = store.create(space, "list", "bread")
    store.write(space, n.id, archived=True, actor="sam")
    store.write(space, n.id, archived=False, actor="sam")
    text = (space / n.id).read_text()
    assert "archived" not in text
    assert store.read(space, n.id).archived_by == []


def test_a_legacy_global_flag_is_preserved_for_the_other_party(space):
    """The 764 notes imported from Keep carry a plain `archived: true`."""
    rel = "Notes/old.md"
    (space / rel).write_text("---\narchived: true\nauthor: ana\n---\n\n# old\n\nx\n")
    assert store.read(space, rel).archived is True
    # one person unarchiving it must not un-archive it for the other
    store.write(space, rel, archived=False, actor="sam")
    assert store.read(space, rel).archived_by == ["ana"]


def test_archiving_does_not_disturb_sharing_or_owner(space):
    n = store.create(space, "theirs", "x", author="ana")
    store.write(space, n.id, shared=True)
    store.write(space, n.id, archived=True, actor="sam")
    got = store.read(space, n.id)
    assert got.author == "ana" and got.shared is True and got.archived_by == ["sam"]


def test_trash_is_global_and_that_is_deliberate(space):
    """Delete MOVES the file; there is no per-person version of that."""
    n = store.create(space, "list", "bread")
    store.write(space, n.id, shared=True)
    trashed = store.trash(space, n.id)
    assert not (space / n.id).exists()          # gone for everyone
    assert (space / trashed).exists()           # and recoverable by anyone

# --- pins and their order are personal ---------------------------------------
# the owner asked whether pins are individual "as they should be". They were not:
# a pin was one flag on the one file, so pinning a SHARED note pinned it for
# the other person too — and a manual order would have rearranged both rows.

def test_pinning_names_who_did_it(space):
    n = store.create(space, "list", "bread")
    store.write(space, n.id, shared=True)
    store.write(space, n.id, pinned=True, actor="sam")
    got = store.read(space, n.id)
    assert got.pinned_by == ["sam"] and got.pinned is True
    assert "pinned_by: sam" in (space / n.id).read_text()


def test_one_person_pinning_leaves_the_other_alone(space):
    n = store.create(space, "list", "bread")
    store.write(space, n.id, shared=True)
    store.write(space, n.id, pinned=True, actor="sam")
    store.write(space, n.id, pinned=True, actor="ana")
    assert store.read(space, n.id).pinned_by == ["ana", "sam"]
    store.write(space, n.id, pinned=False, actor="sam")
    assert store.read(space, n.id).pinned_by == ["ana"]


def test_pin_order_is_per_person(space):
    n = store.create(space, "list", "bread")
    store.write(space, n.id, pinned=True, actor="sam")
    store.write(space, n.id, pinned=True, actor="ana")
    store.write(space, n.id, order=10, actor="sam")
    store.write(space, n.id, order=70, actor="ana")
    got = store.read(space, n.id)
    assert got.pin_order == {"sam": 10, "ana": 70}
    # one person rearranging must not move the other
    store.write(space, n.id, order=50, actor="sam")
    assert store.read(space, n.id).pin_order == {"sam": 50, "ana": 70}


def test_unpinning_drops_only_that_persons_position(space):
    n = store.create(space, "list", "bread")
    store.write(space, n.id, pinned=True, actor="sam")
    store.write(space, n.id, pinned=True, actor="ana")
    store.write(space, n.id, order=10, actor="sam")
    store.write(space, n.id, order=70, actor="ana")
    store.write(space, n.id, pinned=False, actor="sam")
    got = store.read(space, n.id)
    assert got.pinned_by == ["ana"] and got.pin_order == {"ana": 70}


def test_a_legacy_global_pin_is_kept_for_the_other_party(space):
    rel = "Notes/old.md"
    (space / rel).write_text("---\npinned: true\nauthor: ana\n---\n\n# old\n\nx\n")
    assert store.read(space, rel).pinned is True
    store.write(space, rel, pinned=False, actor="sam")
    assert store.read(space, rel).pinned_by == ["ana"]


def test_a_junk_pin_order_reads_as_empty(space):
    rel = "Notes/odd.md"
    (space / rel).write_text("---\npin_order: banana, sam=x, ana=20\n---\n\n# odd\n\ny\n")
    assert store.read(space, rel).pin_order == {"ana": 20}
