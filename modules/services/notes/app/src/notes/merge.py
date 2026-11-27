"""Three-way merge of a note body, by line.

WHY. Every save sends the whole body, and the file is simply replaced. Two
phones on one shared grocery list therefore raced: each saved the list as IT
last saw it, and whichever save landed second wiped what the other had added.
Nothing errored and nothing went to the trash, so nothing was left to find.

So a save now says what it was BASED on. If the file still says that, the save
is written as sent. If someone else wrote in between, the two edits are merged
against that common base: each side's changes are kept, and the only hard case
(both sides changed the same lines) keeps BOTH versions rather than choosing.
A duplicated line is a nuisance; a vanished line is the bug this exists for.
"""

from __future__ import annotations

from difflib import SequenceMatcher


def _norm(text: str) -> list[str]:
    """Lines, ignoring what the editor and the store disagree about.

    The editor rebuilds a body from its rows and the store reads it back off
    disk; trailing spaces and blank lines at either end can differ between
    those without anyone having changed anything. Treating that as an edit
    would turn every save into a "conflict" with itself.
    """
    return [line.rstrip() for line in text.strip("\n").split("\n")] if text.strip() else []


def _sync_regions(base: list[str], a: list[str], b: list[str]):
    """Stretches of base that BOTH sides left untouched, plus an end marker.

    Each is (base_start, base_end, a_start, a_end, b_start, b_end). Between two
    consecutive ones lies a region at least one side changed.
    """
    am = SequenceMatcher(None, base, a, autojunk=False).get_matching_blocks()
    bm = SequenceMatcher(None, base, b, autojunk=False).get_matching_blocks()
    out = []
    ia = ib = 0
    while ia < len(am) and ib < len(bm):
        abase, amatch, alen = am[ia]
        bbase, bmatch, blen = bm[ib]
        lo, hi = max(abase, bbase), min(abase + alen, bbase + blen)
        if lo < hi:
            out.append((lo, hi, amatch + lo - abase, amatch + hi - abase,
                        bmatch + lo - bbase, bmatch + hi - bbase))
        if abase + alen < bbase + blen:
            ia += 1
        else:
            ib += 1
    out.append((len(base), len(base), len(a), len(a), len(b), len(b)))
    return out


def _slots(sub: list[str], seq: list[str]) -> list[int] | None:
    """Where each line of `sub` sits in `seq`, if `seq` only ADDED lines to it."""
    pos, j = [], 0
    for line in sub:
        while j < len(seq) and seq[j] != line:
            j += 1
        if j == len(seq):
            return None
        pos.append(j)
        j += 1
    return pos


def _overlay(b_seg: list[str], ins: list[str], edit: list[str]) -> list[str] | None:
    """One side only inserted lines; the other changed lines one-for-one.

    The checklist case: ticking "bread" while the other phone adds "butter"
    under it. Line-level diff3 calls that a conflict because the two edits
    touch; it is not one — put the tick on the inserted-into list.
    """
    if len(edit) != len(b_seg):
        return None
    slots = _slots(b_seg, ins)
    if slots is None:
        return None
    out = list(ins)
    for i, k in enumerate(slots):
        out[k] = edit[i]
    return out


def merge3(base: str, mine: str, theirs: str) -> tuple[str, bool]:
    """(merged body, clean). Not clean means both sides changed the same lines.

    An unclean region keeps THEIR lines (already on disk, maybe already seen by
    the other person) followed by any of MINE they do not already contain.
    """
    B, A, T = _norm(base), _norm(mine), _norm(theirs)
    out: list[str] = []
    clean = True
    iz = ia = it = 0
    for zs, ze, as_, ae, ts, te in _sync_regions(B, A, T):
        b_seg, a_seg, t_seg = B[iz:zs], A[ia:as_], T[it:ts]
        if a_seg or t_seg or b_seg:
            if a_seg == t_seg or a_seg == b_seg:
                out += t_seg            # same change on both sides, or only theirs
            elif t_seg == b_seg:
                out += a_seg            # only mine changed it
            else:
                both = _overlay(b_seg, t_seg, a_seg) or _overlay(b_seg, a_seg, t_seg)
                if both is not None:
                    out += both
                else:
                    clean = False
                    out += t_seg + [x for x in a_seg if x not in t_seg]
        out += B[zs:ze]
        iz, ia, it = ze, ae, te
    return "\n".join(out), clean


def reconcile(base: str | None, mine: str, current: str) -> tuple[str, bool, bool]:
    """What to write for a save of `mine` that was based on `base`.

    Returns (body, merged, clean). With no base (an old client), or when the
    file still matches the base, the save is written exactly as sent.
    """
    if base is None or _norm(current) == _norm(base) or _norm(current) == _norm(mine):
        return mine, False, True
    body, clean = merge3(base, mine, current)
    return body, True, clean
