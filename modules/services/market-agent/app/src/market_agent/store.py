"""SQLite: every listing a brief has seen, its evaluation, price history, runs."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .models import Candidate

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    brief       TEXT NOT NULL,
    id          TEXT NOT NULL,
    first_seen  INTEGER NOT NULL,
    last_seen   INTEGER NOT NULL,
    status      TEXT NOT NULL,          -- candidate | excluded | gone
    title       TEXT,
    price       REAL,
    candidate   TEXT NOT NULL,          -- Candidate JSON
    eval_key    TEXT,                   -- what the evaluation was computed from
    PRIMARY KEY (brief, id)
);
CREATE TABLE IF NOT EXISTS price_history (
    brief TEXT NOT NULL, id TEXT NOT NULL, ts INTEGER NOT NULL, price REAL
);
CREATE TABLE IF NOT EXISTS runs (
    brief TEXT NOT NULL, ts INTEGER NOT NULL, summary TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS favorites (
    brief TEXT NOT NULL, id TEXT NOT NULL, ts INTEGER NOT NULL, PRIMARY KEY (brief, id)
);
-- Listings that were ever worth a look: tracked until sold or gone (a bookmark).
CREATE TABLE IF NOT EXISTS kept (
    brief TEXT NOT NULL, id TEXT NOT NULL, ts INTEGER NOT NULL, PRIMARY KEY (brief, id)
);
-- "Not interested": hidden for good, never re-read or re-judged.
CREATE TABLE IF NOT EXISTS dismissed (
    brief TEXT NOT NULL, id TEXT NOT NULL, ts INTEGER NOT NULL, PRIMARY KEY (brief, id)
);
CREATE TABLE IF NOT EXISTS notified (
    brief TEXT NOT NULL, id TEXT NOT NULL, ts INTEGER NOT NULL, PRIMARY KEY (brief, id)
);
CREATE TABLE IF NOT EXISTS cache (
    key TEXT PRIMARY KEY, ts INTEGER NOT NULL, value TEXT NOT NULL
);
-- Every Facebook request: where the rate limit sits is read from this.
--   job: one watch job (or probe session); seq: order within it
--   via: direct | proxy; kind: search | search_page | photos | info
--   outcome: ok | rate_limit | refusal | error
--   interval: the pace's base interval then; gap: actual seconds since the
--   previous request (jitter and pauses included); latency_ms; HTTP status
CREATE TABLE IF NOT EXISTS fb_requests (
    ts REAL NOT NULL, job TEXT NOT NULL, seq INTEGER, brief TEXT NOT NULL, via TEXT NOT NULL,
    kind TEXT NOT NULL, outcome TEXT NOT NULL, interval REAL, gap REAL, latency_ms INTEGER,
    status INTEGER, detail TEXT
);
CREATE INDEX IF NOT EXISTS fb_requests_ts ON fb_requests (ts);
"""


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # -- cache -------------------------------------------------------------
    def cache_get(self, key: str, max_age: int | None) -> Any:
        row = self.db.execute("SELECT ts, value FROM cache WHERE key = ?", (key,)).fetchone()
        if not row or (max_age is not None and time.time() - row["ts"] > max_age):
            return None
        return json.loads(row["value"])

    def cache_put(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO cache (key, ts, value) VALUES (?, ?, ?)",
            (key, int(time.time()), json.dumps(value)),
        )
        self.db.commit()

    # -- listings ------------------------------------------------------------
    def get(self, brief: str, listing_id: str) -> tuple[Candidate, str | None, float | None] | None:
        row = self.db.execute(
            "SELECT candidate, eval_key, price FROM listings WHERE brief = ? AND id = ?", (brief, listing_id)
        ).fetchone()
        if not row:
            return None
        return Candidate.model_validate_json(row["candidate"]), row["eval_key"], row["price"]

    def save(self, brief: str, c: Candidate, eval_key: str | None) -> bool:
        """Returns True when the listing is new to this brief."""
        now = int(time.time())
        price = (c.detail.price if c.detail and c.detail.price is not None else c.listing.price)
        prev = self.db.execute(
            "SELECT price FROM listings WHERE brief = ? AND id = ?", (brief, c.listing.id)
        ).fetchone()
        status = "excluded" if c.excluded else "candidate"
        self.db.execute(
            """INSERT INTO listings (brief, id, first_seen, last_seen, status, title, price, candidate, eval_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (brief, id) DO UPDATE SET last_seen = excluded.last_seen, status = excluded.status,
                 title = excluded.title, price = excluded.price, candidate = excluded.candidate,
                 eval_key = excluded.eval_key""",
            (brief, c.listing.id, now, now, status, c.listing.title, price, c.model_dump_json(), eval_key),
        )
        if prev is None or prev["price"] != price:
            self.db.execute("INSERT INTO price_history VALUES (?, ?, ?, ?)", (brief, c.listing.id, now, price))
        self.db.commit()
        return prev is None

    def price_history(self, brief: str, listing_id: str) -> list[tuple[int, float | None]]:
        rows = self.db.execute(
            "SELECT ts, price FROM price_history WHERE brief = ? AND id = ? ORDER BY ts", (brief, listing_id)
        ).fetchall()
        return [(r["ts"], r["price"]) for r in rows]

    def mark_gone(self, brief: str, listing_id: str) -> None:
        self.db.execute("UPDATE listings SET status = 'gone' WHERE brief = ? AND id = ?", (brief, listing_id))
        self.db.commit()

    def recent(self, brief: str, since: float) -> list[Candidate]:
        """Listings still live that some run saw after `since`."""
        rows = self.db.execute(
            "SELECT candidate FROM listings WHERE brief = ? AND status != 'gone' AND last_seen >= ?",
            (brief, int(since)),
        )
        return [Candidate.model_validate_json(r["candidate"]) for r in rows]

    def all(self, brief: str, include_gone: bool = False) -> list[Candidate]:
        q = "SELECT candidate FROM listings WHERE brief = ?" + ("" if include_gone else " AND status != 'gone'")
        return [Candidate.model_validate_json(r["candidate"]) for r in self.db.execute(q, (brief,))]

    def gone_ids(self, brief: str) -> set[str]:
        return {r["id"] for r in self.db.execute("SELECT id FROM listings WHERE brief = ? AND status = 'gone'", (brief,))}

    def last_seen(self, brief: str) -> dict[str, int]:
        return {r["id"]: r["last_seen"] for r in self.db.execute("SELECT id, last_seen FROM listings WHERE brief = ?", (brief,))}

    # -- kept (ever worth a look) and dismissed ---------------------------------------
    def keep(self, brief: str, listing_id: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO kept VALUES (?, ?, ?)", (brief, listing_id, int(time.time())))
        self.db.commit()

    def kept(self, brief: str) -> set[str]:
        return {r["id"] for r in self.db.execute("SELECT id FROM kept WHERE brief = ?", (brief,))}

    def set_dismissed(self, brief: str, listing_id: str, dismissed: bool) -> None:
        if dismissed:
            self.db.execute("INSERT OR IGNORE INTO dismissed VALUES (?, ?, ?)", (brief, listing_id, int(time.time())))
        else:
            self.db.execute("DELETE FROM dismissed WHERE brief = ? AND id = ?", (brief, listing_id))
        self.db.commit()

    def dismissed(self, brief: str) -> set[str]:
        return {r["id"] for r in self.db.execute("SELECT id FROM dismissed WHERE brief = ?", (brief,))}

    # -- favorites ----------------------------------------------------------------
    def favorites(self, brief: str) -> set[str]:
        return {r["id"] for r in self.db.execute("SELECT id FROM favorites WHERE brief = ?", (brief,))}

    def all_favorites(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for r in self.db.execute("SELECT brief, id FROM favorites ORDER BY ts"):
            out.setdefault(r["brief"], []).append(r["id"])
        return out

    def set_favorite(self, brief: str, listing_id: str, starred: bool) -> None:
        if starred:
            self.db.execute("INSERT OR IGNORE INTO favorites VALUES (?, ?, ?)", (brief, listing_id, int(time.time())))
        else:
            self.db.execute("DELETE FROM favorites WHERE brief = ? AND id = ?", (brief, listing_id))
        self.db.commit()

    def get_many(self, brief: str, ids: set[str]) -> list[Candidate]:
        out = []
        for i in ids:
            got = self.get(brief, i)
            if got:
                out.append(got[0])
        return out

    def first_seen(self, brief: str) -> dict[str, int]:
        return {r["id"]: r["first_seen"] for r in self.db.execute(
            "SELECT id, first_seen FROM listings WHERE brief = ?", (brief,))}

    # -- notifications --------------------------------------------------------
    def unnotified(self, brief: str, ids: list[str]) -> list[str]:
        seen = {r["id"] for r in self.db.execute("SELECT id FROM notified WHERE brief = ?", (brief,))}
        return [i for i in ids if i not in seen]

    def mark_notified(self, brief: str, ids: list[str]) -> None:
        now = int(time.time())
        self.db.executemany("INSERT OR IGNORE INTO notified VALUES (?, ?, ?)", [(brief, i, now) for i in ids])
        self.db.commit()

    # -- Facebook requests --------------------------------------------------------
    def log_request(self, brief: str, via: str, rec: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO fb_requests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), rec.get("job") or "", rec.get("seq"), brief, via, rec.get("kind") or "",
             rec.get("outcome") or "", rec.get("interval"), rec.get("gap"), rec.get("latency_ms"),
             rec.get("status"), rec.get("detail") or ""))
        self.db.commit()

    def last_request_ts(self) -> float | None:
        row = self.db.execute("SELECT max(ts) FROM fb_requests").fetchone()
        return row[0] if row else None

    # -- runs -------------------------------------------------------------------
    def record_run(self, brief: str, summary: dict[str, Any]) -> None:
        self.db.execute("INSERT INTO runs VALUES (?, ?, ?)", (brief, int(time.time()), json.dumps(summary)))
        self.db.commit()

    def last_run(self, brief: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT ts, summary FROM runs WHERE brief = ? ORDER BY ts DESC, rowid DESC LIMIT 1", (brief,)
        ).fetchone()
        return {"ts": row["ts"], **json.loads(row["summary"])} if row else None
