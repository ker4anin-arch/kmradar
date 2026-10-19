"""Хранилище. Для MVP — SQLite из стандартной библиотеки; схема переносится в Postgres
без изменений по смыслу (JSON → JSONB, INTEGER PRIMARY KEY → BIGSERIAL).

Основные таблицы из документа: company, snapshot, event, card.
Служебные: km, card_event, digest, km_weight, feedback_log.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .models import Card, Company, Event, KMProfile

SCHEMA = """
CREATE TABLE IF NOT EXISTS km (
    km_id TEXT PRIMARY KEY,
    profile TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS company (
    inn TEXT PRIMARY KEY,
    name TEXT, region TEXT, okved TEXT, okved_extra TEXT,
    revenue INTEGER, revenue_year INTEGER
);
CREATE TABLE IF NOT EXISTS snapshot (
    inn TEXT NOT NULL,
    day TEXT NOT NULL,
    state TEXT NOT NULL,
    PRIMARY KEY (inn, day)
);
CREATE TABLE IF NOT EXISTS event (
    id INTEGER PRIMARY KEY,
    inn TEXT NOT NULL,
    type TEXT NOT NULL,
    event_date TEXT NOT NULL,
    facts TEXT NOT NULL,
    source TEXT NOT NULL,
    url TEXT,
    detected TEXT NOT NULL,
    UNIQUE (inn, type, event_date, facts)
);
CREATE TABLE IF NOT EXISTS card (
    id INTEGER PRIMARY KEY,
    km_id TEXT NOT NULL,
    inn TEXT NOT NULL,
    created TEXT NOT NULL,
    score REAL NOT NULL,
    urgent INTEGER NOT NULL,
    deadline TEXT NOT NULL,
    text TEXT NOT NULL,
    feedback TEXT, feedback_at TEXT,
    outcome TEXT, outcome_at TEXT,
    followup_sent INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS card_event (
    card_id INTEGER NOT NULL,
    event_id INTEGER NOT NULL,
    PRIMARY KEY (card_id, event_id)
);
CREATE TABLE IF NOT EXISTS digest (
    km_id TEXT NOT NULL,
    day TEXT NOT NULL,
    strong_count INTEGER NOT NULL,
    skipped INTEGER NOT NULL,
    cards INTEGER NOT NULL,
    opened_at TEXT,
    PRIMARY KEY (km_id, day)
);
CREATE TABLE IF NOT EXISTS km_weight (
    km_id TEXT NOT NULL,
    type TEXT NOT NULL,
    value REAL NOT NULL,
    PRIMARY KEY (km_id, type)
);
CREATE TABLE IF NOT EXISTS km_watch (
    km_id TEXT NOT NULL,
    inn TEXT NOT NULL,
    added TEXT NOT NULL,
    PRIMARY KEY (km_id, inn)
);
CREATE TABLE IF NOT EXISTS km_link (
    token_hash TEXT PRIMARY KEY,
    km_id TEXT NOT NULL,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS caution (
    km_id TEXT NOT NULL,
    day TEXT NOT NULL,
    text TEXT NOT NULL,
    PRIMARY KEY (km_id, day, text)
);
CREATE TABLE IF NOT EXISTS miss_reason (
    id INTEGER PRIMARY KEY,
    km_id TEXT NOT NULL,
    type TEXT NOT NULL,
    text TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback_log (
    id INTEGER PRIMARY KEY,
    card_id INTEGER NOT NULL,
    km_id TEXT NOT NULL,
    type TEXT NOT NULL,
    value TEXT NOT NULL,
    at TEXT NOT NULL
);
"""


def _d(value: str) -> date:
    return date.fromisoformat(value)


class Storage:
    def __init__(self, path: str | Path = ":memory:"):
        self.conn = sqlite3.connect(str(path), timeout=15)
        self.conn.row_factory = sqlite3.Row
        if str(path) != ":memory:":
            # веб-сервер и ночной прогон пишут в одну базу одновременно
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # --- личные ссылки КМ (храним только хеш) -------------------------------
    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def new_link(self, km_id: str) -> str:
        """Новая личная ссылка КМ; старая перестаёт работать."""
        token = secrets.token_urlsafe(24)
        self.conn.execute("DELETE FROM km_link WHERE km_id = ?", (km_id,))
        self.conn.execute("INSERT INTO km_link VALUES (?, ?, ?)",
                          (self._hash(token), km_id, datetime.now().isoformat(timespec="seconds")))
        self.conn.commit()
        return token

    def km_by_token(self, token: str) -> KMProfile | None:
        row = self.conn.execute("SELECT km_id FROM km_link WHERE token_hash = ?",
                                (self._hash(token),)).fetchone()
        return self.km(row["km_id"]) if row else None

    # --- «Мои компании»: холодные компании, которые КМ добавил сам ----------
    def add_watch(self, km_id: str, inns: list[str], limit: int, at: datetime) -> list[str]:
        have = set(self.watch_list(km_id))
        added = []
        for inn in inns:
            if inn in have or len(have) >= limit:
                continue
            self.conn.execute("INSERT OR IGNORE INTO km_watch VALUES (?, ?, ?)",
                              (km_id, inn, at.isoformat(timespec="seconds")))
            have.add(inn)
            added.append(inn)
        self.conn.commit()
        return added

    def remove_watch(self, km_id: str, inn: str) -> None:
        self.conn.execute("DELETE FROM km_watch WHERE km_id = ? AND inn = ?", (km_id, inn))
        self.conn.commit()

    def watch_list(self, km_id: str) -> list[str]:
        return [r["inn"] for r in self.conn.execute(
            "SELECT inn FROM km_watch WHERE km_id = ? ORDER BY added", (km_id,))]

    def all_watched(self) -> list[str]:
        return [r["inn"] for r in self.conn.execute("SELECT DISTINCT inn FROM km_watch ORDER BY inn")]

    def company_events(self, inn: str, since: date) -> list[Event]:
        return [self._event(r) for r in self.conn.execute(
            "SELECT * FROM event WHERE inn = ? AND event_date >= ? ORDER BY event_date DESC, id DESC",
            (inn, since.isoformat()))]

    def has_snapshot(self, inn: str) -> bool:
        return self.conn.execute("SELECT 1 FROM snapshot WHERE inn = ? LIMIT 1", (inn,)).fetchone() is not None

    # --- что показать на странице КМ ----------------------------------------
    def save_caution(self, km_id: str, day: date, text: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO caution VALUES (?, ?, ?)",
                          (km_id, day.isoformat(), text))
        self.conn.commit()

    def cautions(self, km_id: str, day: date) -> list[str]:
        return [r["text"] for r in self.conn.execute(
            "SELECT text FROM caution WHERE km_id = ? AND day = ?", (km_id, day.isoformat()))]

    def cards_for_day(self, km_id: str, day: date) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM card WHERE km_id = ? AND created = ? ORDER BY urgent DESC, score DESC",
            (km_id, day.isoformat())))

    def digest_days(self, km_id: str, until: date, limit: int = 10) -> list[date]:
        rows = self.conn.execute(
            "SELECT day FROM digest WHERE km_id = ? AND day <= ? AND cards > 0"
            " UNION SELECT day FROM caution WHERE km_id = ? AND day <= ?"
            " ORDER BY day DESC LIMIT ?",
            (km_id, until.isoformat(), km_id, until.isoformat(), limit))
        return [_d(r["day"]) for r in rows]

    def pending_outcomes(self, km_id: str, today: date, days: int) -> list[sqlite3.Row]:
        cutoff = (today - timedelta(days=days)).isoformat()
        return list(self.conn.execute(
            "SELECT * FROM card WHERE km_id = ? AND feedback = 'contacted' AND outcome IS NULL"
            " AND substr(feedback_at, 1, 10) <= ? ORDER BY id", (km_id, cutoff)))

    def save_reason(self, km_id: str, type_: str, text: str, at: datetime) -> None:
        self.conn.execute("INSERT INTO miss_reason (km_id, type, text, at) VALUES (?, ?, ?, ?)",
                          (km_id, type_, text, at.isoformat(timespec="seconds")))
        self.conn.commit()

    def reasons(self, km_id: str | None = None) -> list[sqlite3.Row]:
        if km_id:
            return list(self.conn.execute("SELECT * FROM miss_reason WHERE km_id = ?", (km_id,)))
        return list(self.conn.execute("SELECT * FROM miss_reason"))

    # --- КМ -----------------------------------------------------------------
    def save_km(self, km: KMProfile) -> None:
        self.conn.execute("INSERT OR REPLACE INTO km VALUES (?, ?)",
                          (km.km_id, json.dumps(asdict(km), ensure_ascii=False)))
        self.conn.commit()

    def kms(self) -> list[KMProfile]:
        return [KMProfile(**json.loads(r["profile"]))
                for r in self.conn.execute("SELECT profile FROM km ORDER BY km_id")]

    def km(self, km_id: str) -> KMProfile | None:
        row = self.conn.execute("SELECT profile FROM km WHERE km_id = ?", (km_id,)).fetchone()
        return KMProfile(**json.loads(row["profile"])) if row else None

    def km_by_chat(self, chat_id: str) -> KMProfile | None:
        return next((k for k in self.kms() if k.chat_id == str(chat_id)), None)

    # --- компании и снимки --------------------------------------------------
    def save_company(self, c: Company) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO company VALUES (?, ?, ?, ?, ?, ?, ?)",
            (c.inn, c.name, c.region, c.okved, json.dumps(c.okved_extra), c.revenue,
             c.revenue_year))

    def companies(self) -> dict[str, Company]:
        out = {}
        for r in self.conn.execute("SELECT * FROM company"):
            out[r["inn"]] = Company(r["inn"], r["name"], r["region"], r["okved"],
                                    json.loads(r["okved_extra"] or "[]"), r["revenue"],
                                    r["revenue_year"])
        return out

    def last_snapshot(self, inn: str, before: date) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT state FROM snapshot WHERE inn = ? AND day < ? ORDER BY day DESC LIMIT 1",
            (inn, before.isoformat())).fetchone()
        return json.loads(row["state"]) if row else None

    def save_snapshot(self, inn: str, day: date, state: dict[str, Any]) -> None:
        self.conn.execute("INSERT OR REPLACE INTO snapshot VALUES (?, ?, ?)",
                          (inn, day.isoformat(), json.dumps(state, ensure_ascii=False)))

    # --- события ------------------------------------------------------------
    def save_events(self, events: Iterable[Event], detected: date) -> int:
        added = 0
        for e in events:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO event (inn, type, event_date, facts, source, url, detected)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (e.inn, e.type, e.event_date.isoformat(),
                 json.dumps(e.facts, ensure_ascii=False, sort_keys=True), e.source, e.url,
                 detected.isoformat()))
            added += cur.rowcount
        self.conn.commit()
        return added

    def _event(self, r: sqlite3.Row) -> Event:
        return Event(r["inn"], r["type"], _d(r["event_date"]), json.loads(r["facts"]),
                     r["source"], r["url"], r["id"])

    def events_since(self, since: date, *, stop: bool | None = None,
                     not_in_cards_of: str | None = None) -> list[Event]:
        sql = "SELECT * FROM event WHERE event_date >= ?"
        args: list[Any] = [since.isoformat()]
        if stop is True:
            sql += " AND type = 'stop'"
        elif stop is False:
            sql += " AND type != 'stop'"
        if not_in_cards_of:
            sql += (" AND id NOT IN (SELECT ce.event_id FROM card_event ce"
                    " JOIN card c ON c.id = ce.card_id WHERE c.km_id = ?)")
            args.append(not_in_cards_of)
        return [self._event(r) for r in self.conn.execute(sql + " ORDER BY id", args)]

    def tender_amounts(self, since: date, inns: set[str]) -> list[int]:
        rows = self.conn.execute(
            "SELECT inn, facts FROM event WHERE type IN ('tender_win', 'contract_signed')"
            " AND event_date >= ?",
            (since.isoformat(),))
        return [json.loads(r["facts"]).get("amount") or 0 for r in rows if r["inn"] in inns]

    # --- карточки -----------------------------------------------------------
    def save_card(self, card: Card) -> int:
        cur = self.conn.execute(
            "INSERT INTO card (km_id, inn, created, score, urgent, deadline, text)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (card.km_id, card.inn, card.created.isoformat(), card.score, int(card.urgent),
             card.deadline.isoformat(), card.text))
        card.id = cur.lastrowid
        for e in card.events:
            if e.id is not None:
                self.conn.execute("INSERT OR IGNORE INTO card_event VALUES (?, ?)", (card.id, e.id))
        self.conn.commit()
        return card.id

    def card_row(self, card_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM card WHERE id = ?", (card_id,)).fetchone()

    def card_types(self, card_id: int) -> list[str]:
        return [r["type"] for r in self.conn.execute(
            "SELECT e.type FROM card_event ce JOIN event e ON e.id = ce.event_id"
            " WHERE ce.card_id = ? ORDER BY e.id", (card_id,))]

    def recent_card_inns(self, km_id: str, since: date) -> set[str]:
        return {r["inn"] for r in self.conn.execute(
            "SELECT inn FROM card WHERE km_id = ? AND created >= ?", (km_id, since.isoformat()))}

    def cards(self, km_id: str | None = None) -> list[sqlite3.Row]:
        if km_id:
            return list(self.conn.execute("SELECT * FROM card WHERE km_id = ? ORDER BY id", (km_id,)))
        return list(self.conn.execute("SELECT * FROM card ORDER BY id"))

    # --- дайджесты ----------------------------------------------------------
    def save_digest(self, km_id: str, day: date, strong: int, skipped: bool, cards: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO digest (km_id, day, strong_count, skipped, cards)"
            " VALUES (?, ?, ?, ?, ?)", (km_id, day.isoformat(), strong, int(skipped), cards))
        self.conn.commit()

    def strong_count(self, km_id: str, day: date) -> int | None:
        row = self.conn.execute("SELECT strong_count FROM digest WHERE km_id = ? AND day = ?",
                                (km_id, day.isoformat())).fetchone()
        return row["strong_count"] if row else None

    def mark_opened(self, km_id: str, day: date, at: datetime) -> None:
        self.conn.execute(
            "UPDATE digest SET opened_at = COALESCE(opened_at, ?) WHERE km_id = ? AND day = ?",
            (at.isoformat(timespec="seconds"), km_id, day.isoformat()))
        self.conn.commit()

    def digests(self, km_id: str | None = None) -> list[sqlite3.Row]:
        sql, args = "SELECT * FROM digest", []
        if km_id:
            sql, args = sql + " WHERE km_id = ?", [km_id]
        return list(self.conn.execute(sql + " ORDER BY day", args))

    # --- личные веса и обратная связь ---------------------------------------
    def personal_weights(self, km_id: str) -> dict[str, float]:
        return {r["type"]: r["value"] for r in self.conn.execute(
            "SELECT type, value FROM km_weight WHERE km_id = ?", (km_id,))}

    def set_weight(self, km_id: str, type_: str, value: float) -> None:
        self.conn.execute("INSERT OR REPLACE INTO km_weight VALUES (?, ?, ?)",
                          (km_id, type_, value))

    def set_feedback(self, card_id: int, value: str, at: datetime) -> None:
        self.conn.execute("UPDATE card SET feedback = ?, feedback_at = ? WHERE id = ?",
                          (value, at.isoformat(timespec="seconds"), card_id))

    def log_feedback(self, card_id: int, km_id: str, type_: str, value: str, at: datetime) -> None:
        self.conn.execute(
            "INSERT INTO feedback_log (card_id, km_id, type, value, at) VALUES (?, ?, ?, ?, ?)",
            (card_id, km_id, type_, value, at.isoformat(timespec="seconds")))

    def feedback_history(self, km_id: str, type_: str) -> list[str]:
        return [r["value"] for r in self.conn.execute(
            "SELECT value FROM feedback_log WHERE km_id = ? AND type = ? ORDER BY id",
            (km_id, type_))]

    def set_outcome(self, card_id: int, value: str, at: datetime) -> None:
        self.conn.execute("UPDATE card SET outcome = ?, outcome_at = ? WHERE id = ?",
                          (value, at.isoformat(timespec="seconds"), card_id))
        self.conn.commit()

    def due_followups(self, today: date, days: int) -> list[sqlite3.Row]:
        cutoff = (today - timedelta(days=days)).isoformat()
        return list(self.conn.execute(
            "SELECT * FROM card WHERE feedback = 'contacted' AND outcome IS NULL"
            " AND followup_sent = 0 AND substr(feedback_at, 1, 10) <= ?", (cutoff,)))

    def mark_followup_sent(self, card_id: int) -> None:
        self.conn.execute("UPDATE card SET followup_sent = 1 WHERE id = ?", (card_id,))
        self.conn.commit()

    def commit(self) -> None:
        self.conn.commit()
