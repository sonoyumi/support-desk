"""SQLite storage: customers, tickets, messages (+ delivery queue), operators, canned replies, search, stats.

Ticket statuses:
    new      — customer wrote, nobody answered yet          ┐ "needs reply": SLA clock is running
    open     — customer answered again after our reply      ┘
    waiting  — we answered, waiting for the customer
    closed   — done (by an operator, by the customer or automatically)

A customer has at most ONE active (not closed) ticket: a partial UNIQUE index guarantees it even if two
messages arrive at the same moment. A message after closing starts a new ticket.

Outgoing replies are not sent from the web request: they are stored with ``delivery='pending'`` and the
sender loop delivers them with retries (outbox pattern). A crashed send is returned to the queue when its
lease expires.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

NEEDS_REPLY = ("new", "open")
ACTIVE = ("new", "open", "waiting")
FILTERS = ("needs_reply", "mine", "unassigned", "overdue", "waiting", "closed", "all")

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY,
    tg_id INTEGER NOT NULL UNIQUE,
    name TEXT NOT NULL,
    username TEXT,
    lang TEXT NOT NULL DEFAULT 'en',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS operators (
    id INTEGER PRIMARY KEY,
    login TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    status TEXT NOT NULL CHECK (status IN ('new', 'open', 'waiting', 'closed')),
    subject TEXT NOT NULL,
    assignee_id INTEGER REFERENCES operators(id),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    first_reply_at REAL,
    sla_due_at REAL,
    sla_notified INTEGER NOT NULL DEFAULT 0,
    closed_at REAL,
    closed_by TEXT,
    rating INTEGER CHECK (rating BETWEEN 1 AND 5)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_ticket ON tickets(customer_id) WHERE status != 'closed';
CREATE INDEX IF NOT EXISTS tickets_status ON tickets(status, updated_at);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id),
    kind TEXT NOT NULL CHECK (kind IN ('in', 'out', 'note', 'rating')),
    operator_id INTEGER REFERENCES operators(id),
    text TEXT NOT NULL,
    attachment TEXT,
    created_at REAL NOT NULL,
    tg_message_id INTEGER,
    delivery TEXT CHECK (delivery IN ('pending', 'sending', 'sent', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL,
    lease_until REAL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS messages_ticket ON messages(ticket_id, id);
CREATE INDEX IF NOT EXISTS messages_delivery ON messages(delivery, next_attempt_at);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text, content='messages', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TABLE IF NOT EXISTS canned (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL UNIQUE,
    body TEXT NOT NULL
);
"""

_WORD = re.compile(r"\w+", re.UNICODE)


class NotFound(Exception):
    """Ticket / operator does not exist."""


class Conflict(Exception):
    """The action is not allowed in the current state (e.g. replying to a closed ticket)."""


@dataclass(frozen=True)
class Incoming:
    ticket_id: int
    new_ticket: bool
    customer_id: int


@dataclass(frozen=True)
class Delivery:
    message_id: int
    ticket_id: int
    chat_id: int
    kind: str
    text: str
    attempts: int


def build_query(text: str) -> str | None:
    """User text -> safe FTS5 query: every word quoted, prefix match. No FTS syntax gets through."""
    words = _WORD.findall(text.lower())[:8]
    return " ".join(f'"{w}"*' for w in words) or None


def subject_from(text: str, limit: int = 80) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1].rstrip() + "…"


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ lifecycle
    async def open(self) -> Store:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode = WAL")
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        return self

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Store.open() first"
        return self._db

    @asynccontextmanager
    async def _tx(self) -> AsyncIterator[aiosqlite.Connection]:
        """One connection for the whole process: a lock keeps transactions from interleaving."""
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
            except BaseException:
                await self.db.rollback()
                raise
            else:
                await self.db.commit()

    async def _one(self, sql: str, args: tuple = ()) -> aiosqlite.Row | None:
        async with self.db.execute(sql, args) as cur:
            return await cur.fetchone()

    async def _all(self, sql: str, args: tuple = ()) -> list[aiosqlite.Row]:
        async with self.db.execute(sql, args) as cur:
            return list(await cur.fetchall())

    # ------------------------------------------------------------------ customers → tickets
    async def add_incoming(self, *, tg_id: int, name: str, username: str | None, text: str,
                           attachment: str | None, tg_message_id: int | None, now: float,
                           sla_minutes: int, lang: str = "en") -> Incoming:
        """A customer message: append to the active ticket or open a new one."""
        async with self._tx() as db:
            await db.execute(
                "INSERT INTO customers (tg_id, name, username, lang, created_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(tg_id) DO UPDATE SET name = excluded.name, username = excluded.username, "
                "lang = excluded.lang", (tg_id, name, username, lang, now))
            async with db.execute("SELECT id FROM customers WHERE tg_id = ?", (tg_id,)) as cur:
                customer_id = (await cur.fetchone())["id"]
            async with db.execute("SELECT id, status FROM tickets WHERE customer_id = ? AND status != 'closed'",
                                  (customer_id,)) as cur:
                active = await cur.fetchone()
            due = now + sla_minutes * 60
            if active is None:
                cur = await db.execute(
                    "INSERT INTO tickets (customer_id, status, subject, created_at, updated_at, sla_due_at) "
                    "VALUES (?, 'new', ?, ?, ?, ?)",
                    (customer_id, subject_from(text), now, now, due))
                ticket_id, new_ticket = cur.lastrowid, True
            else:
                ticket_id, new_ticket = active["id"], False
                if active["status"] == "waiting":  # the customer answered us: back to "needs reply"
                    await db.execute("UPDATE tickets SET status = 'open', sla_due_at = ?, sla_notified = 0, "
                                     "updated_at = ? WHERE id = ?", (due, now, ticket_id))
                else:
                    await db.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (now, ticket_id))
            await db.execute(
                "INSERT INTO messages (ticket_id, kind, text, attachment, created_at, tg_message_id) "
                "VALUES (?, 'in', ?, ?, ?, ?)", (ticket_id, text, attachment, now, tg_message_id))
        return Incoming(ticket_id=ticket_id, new_ticket=new_ticket, customer_id=customer_id)

    async def close_by_customer(self, tg_id: int, now: float) -> int | None:
        """/new: the customer closes the current ticket; the next message starts a new one."""
        async with self._tx() as db:
            async with db.execute(
                "SELECT t.id FROM tickets t JOIN customers c ON c.id = t.customer_id "
                "WHERE c.tg_id = ? AND t.status != 'closed'", (tg_id,)) as cur:
                row = await cur.fetchone()
            if row is None:
                return None
            await db.execute("UPDATE tickets SET status = 'closed', closed_at = ?, closed_by = 'customer', "
                             "sla_due_at = NULL, updated_at = ? WHERE id = ?", (now, now, row["id"]))
            return row["id"]

    async def set_rating(self, ticket_id: int, tg_id: int, score: int) -> bool:
        """Rate a closed ticket once; only its own customer can do it."""
        if not 1 <= score <= 5:
            return False
        async with self._tx() as db:
            cur = await db.execute(
                "UPDATE tickets SET rating = ? WHERE id = ? AND status = 'closed' AND rating IS NULL "
                "AND customer_id = (SELECT id FROM customers WHERE tg_id = ?)", (score, ticket_id, tg_id))
            return cur.rowcount == 1

    # ------------------------------------------------------------------ operator actions
    async def _ticket_status(self, db: aiosqlite.Connection, ticket_id: int) -> str:
        async with db.execute("SELECT status FROM tickets WHERE id = ?", (ticket_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            raise NotFound(ticket_id)
        return row["status"]

    async def add_reply(self, ticket_id: int, operator_id: int, text: str, now: float) -> int:
        """Store the reply for delivery and move the ticket to "waiting for the customer"."""
        async with self._tx() as db:
            if await self._ticket_status(db, ticket_id) == "closed":
                raise Conflict("ticket is closed")
            cur = await db.execute(
                "INSERT INTO messages (ticket_id, kind, operator_id, text, created_at, delivery, next_attempt_at) "
                "VALUES (?, 'out', ?, ?, ?, 'pending', ?)", (ticket_id, operator_id, text, now, now))
            await db.execute(
                "UPDATE tickets SET status = 'waiting', sla_due_at = NULL, updated_at = ?, "
                "first_reply_at = COALESCE(first_reply_at, ?), assignee_id = COALESCE(assignee_id, ?) WHERE id = ?",
                (now, now, operator_id, ticket_id))
            return cur.lastrowid

    async def add_note(self, ticket_id: int, operator_id: int, text: str, now: float) -> int:
        """Internal note: visible to operators only, never sent to the customer."""
        async with self._tx() as db:
            await self._ticket_status(db, ticket_id)
            cur = await db.execute("INSERT INTO messages (ticket_id, kind, operator_id, text, created_at) "
                                   "VALUES (?, 'note', ?, ?, ?)", (ticket_id, operator_id, text, now))
            return cur.lastrowid

    async def assign(self, ticket_id: int, operator_id: int | None, now: float) -> None:
        async with self._tx() as db:
            await self._ticket_status(db, ticket_id)
            await db.execute("UPDATE tickets SET assignee_id = ?, updated_at = ? WHERE id = ?",
                             (operator_id, now, ticket_id))

    async def close_ticket(self, ticket_id: int, now: float, *, by: str = "operator",
                           rating_text: str | None = None) -> None:
        """Close and (optionally) queue a rating request for the customer."""
        async with self._tx() as db:
            if await self._ticket_status(db, ticket_id) == "closed":
                raise Conflict("already closed")
            await db.execute("UPDATE tickets SET status = 'closed', closed_at = ?, closed_by = ?, sla_due_at = NULL, "
                             "updated_at = ? WHERE id = ?", (now, by, now, ticket_id))
            if rating_text:
                await db.execute("INSERT INTO messages (ticket_id, kind, text, created_at, delivery, next_attempt_at) "
                                 "VALUES (?, 'rating', ?, ?, 'pending', ?)", (ticket_id, rating_text, now, now))

    async def reopen(self, ticket_id: int, now: float, sla_minutes: int) -> None:
        async with self._tx() as db:
            if await self._ticket_status(db, ticket_id) != "closed":
                raise Conflict("ticket is not closed")
            async with db.execute("SELECT customer_id FROM tickets WHERE id = ?", (ticket_id,)) as cur:
                customer_id = (await cur.fetchone())["customer_id"]
            async with db.execute("SELECT 1 FROM tickets WHERE customer_id = ? AND status != 'closed'",
                                  (customer_id,)) as cur:
                if await cur.fetchone():
                    raise Conflict("the customer already has an active ticket")
            await db.execute("UPDATE tickets SET status = 'open', closed_at = NULL, closed_by = NULL, "
                             "sla_due_at = ?, sla_notified = 0, updated_at = ? WHERE id = ?",
                             (now + sla_minutes * 60, now, ticket_id))

    # ------------------------------------------------------------------ delivery queue (outbox)
    async def claim_deliveries(self, now: float, limit: int = 20, lease: float = 60) -> list[Delivery]:
        async with self._tx() as db:
            async with db.execute(
                "UPDATE messages SET delivery = 'sending', lease_until = ?, attempts = attempts + 1 "
                "WHERE id IN (SELECT id FROM messages WHERE kind IN ('out', 'rating') AND ("
                "  (delivery = 'pending' AND next_attempt_at <= ?) OR (delivery = 'sending' AND lease_until < ?)"
                ") ORDER BY id LIMIT ?) RETURNING id, ticket_id, kind, text, attempts",
                (now + lease, now, now, limit)) as cur:
                claimed = list(await cur.fetchall())
            out = []
            for row in sorted(claimed, key=lambda r: r["id"]):
                async with db.execute("SELECT c.tg_id FROM tickets t JOIN customers c ON c.id = t.customer_id "
                                      "WHERE t.id = ?", (row["ticket_id"],)) as cur:
                    chat = (await cur.fetchone())["tg_id"]
                out.append(Delivery(row["id"], row["ticket_id"], chat, row["kind"], row["text"], row["attempts"]))
            return out

    async def mark_sent(self, message_id: int, tg_message_id: int | None) -> None:
        async with self._tx() as db:
            await db.execute("UPDATE messages SET delivery = 'sent', tg_message_id = ?, lease_until = NULL, "
                             "error = NULL WHERE id = ?", (tg_message_id, message_id))

    async def mark_retry(self, message_id: int, next_at: float, error: str) -> None:
        async with self._tx() as db:
            await db.execute("UPDATE messages SET delivery = 'pending', next_attempt_at = ?, lease_until = NULL, "
                             "error = ? WHERE id = ?", (next_at, error[:300], message_id))

    async def mark_failed(self, message_id: int, error: str) -> None:
        async with self._tx() as db:
            await db.execute("UPDATE messages SET delivery = 'failed', lease_until = NULL, error = ? WHERE id = ?",
                             (error[:300], message_id))

    async def retry_failed(self, message_id: int, now: float) -> bool:
        async with self._tx() as db:
            cur = await db.execute("UPDATE messages SET delivery = 'pending', next_attempt_at = ?, attempts = 0, "
                                   "error = NULL WHERE id = ? AND delivery = 'failed'", (now, message_id))
            return cur.rowcount == 1

    # ------------------------------------------------------------------ background rules
    async def overdue_to_notify(self, now: float) -> list[aiosqlite.Row]:
        """Tickets that crossed the SLA and were not announced yet; marks them announced."""
        async with self._tx() as db:
            async with db.execute(
                "UPDATE tickets SET sla_notified = 1 WHERE status IN ('new', 'open') AND sla_due_at < ? "
                "AND sla_notified = 0 RETURNING id, subject, sla_due_at", (now,)) as cur:
                return sorted(await cur.fetchall(), key=lambda r: r["id"])

    async def auto_close(self, now: float, hours: int,
                         rating_text: Callable[[int, str], str] | None = None) -> list[int]:
        """Close tickets that have waited for the customer longer than ``hours``.

        ``rating_text(ticket_id, customer_lang)`` builds the rating request queued for each closed ticket."""
        async with self._tx() as db:
            async with db.execute(
                "UPDATE tickets SET status = 'closed', closed_at = ?, closed_by = 'auto', updated_at = ? "
                "WHERE status = 'waiting' AND updated_at < ? RETURNING id", (now, now, now - hours * 3600)) as cur:
                ids = sorted(r["id"] for r in await cur.fetchall())
            if rating_text:
                for tid in ids:
                    async with db.execute("SELECT c.lang FROM tickets t JOIN customers c ON c.id = t.customer_id "
                                          "WHERE t.id = ?", (tid,)) as cur:
                        lang = (await cur.fetchone())["lang"]
                    await db.execute("INSERT INTO messages (ticket_id, kind, text, created_at, delivery, "
                                     "next_attempt_at) VALUES (?, 'rating', ?, ?, 'pending', ?)",
                                     (tid, rating_text(tid, lang), now, now))
            return ids

    # ------------------------------------------------------------------ reading for the panel
    def _where(self, flt: str, operator_id: int | None, now: float) -> tuple[str, tuple]:
        return {
            "needs_reply": ("t.status IN ('new', 'open')", ()),
            "mine": ("t.status != 'closed' AND t.assignee_id = ?", (operator_id,)),
            "unassigned": ("t.status != 'closed' AND t.assignee_id IS NULL", ()),
            "overdue": ("t.status IN ('new', 'open') AND t.sla_due_at < ?", (now,)),
            "waiting": ("t.status = 'waiting'", ()),
            "closed": ("t.status = 'closed'", ()),
            "all": ("1 = 1", ()),
        }[flt]

    async def counts(self, operator_id: int | None, now: float) -> dict[str, int]:
        out = {}
        for flt in FILTERS:
            where, args = self._where(flt, operator_id, now)
            row = await self._one(f"SELECT COUNT(*) AS n FROM tickets t WHERE {where}", args)
            out[flt] = row["n"]
        return out

    async def list_tickets(self, flt: str = "needs_reply", *, operator_id: int | None = None, now: float,
                           query: str = "", limit: int = 100) -> list[aiosqlite.Row]:
        if flt not in FILTERS:
            flt = "needs_reply"
        where, args = self._where(flt, operator_id, now)
        extra, extra_args = "", ()
        query = query.strip()
        if query:
            if re.fullmatch(r"#?\d+", query):  # "#12" or "12" — ticket number
                extra, extra_args = " AND t.id = ?", (int(query.lstrip("#")),)
                where, args = "1 = 1", ()  # a number finds the ticket in any list
            elif fts := build_query(query):
                extra = (" AND (t.id IN (SELECT m.ticket_id FROM messages m JOIN messages_fts f ON f.rowid = m.id "
                         "WHERE messages_fts MATCH ?) OR c.name LIKE ? ESCAPE '\\' OR t.subject LIKE ? ESCAPE '\\')")
                like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                extra_args = (fts, like, like)
        order = "t.updated_at DESC" if flt in ("closed", "all") else "COALESCE(t.sla_due_at, t.updated_at) ASC"
        return await self._all(
            "SELECT t.*, c.name AS customer, c.username, o.name AS assignee, "
            "(SELECT COUNT(*) FROM messages m WHERE m.ticket_id = t.id AND m.kind IN ('in', 'out')) AS messages, "
            "(SELECT text FROM messages m WHERE m.ticket_id = t.id AND m.kind IN ('in', 'out') "
            " ORDER BY m.id DESC LIMIT 1) AS last_text "
            "FROM tickets t JOIN customers c ON c.id = t.customer_id LEFT JOIN operators o ON o.id = t.assignee_id "
            f"WHERE {where}{extra} ORDER BY {order} LIMIT ?", (*args, *extra_args, limit))

    async def get_ticket(self, ticket_id: int) -> aiosqlite.Row:
        row = await self._one(
            "SELECT t.*, c.name AS customer, c.username, c.tg_id, c.lang, o.name AS assignee, "
            "(SELECT COUNT(*) FROM tickets t2 WHERE t2.customer_id = t.customer_id) AS customer_tickets "
            "FROM tickets t JOIN customers c ON c.id = t.customer_id LEFT JOIN operators o ON o.id = t.assignee_id "
            "WHERE t.id = ?", (ticket_id,))
        if row is None:
            raise NotFound(ticket_id)
        return row

    async def messages(self, ticket_id: int, after_id: int = 0) -> list[aiosqlite.Row]:
        return await self._all(
            "SELECT m.*, o.name AS operator FROM messages m LEFT JOIN operators o ON o.id = m.operator_id "
            "WHERE m.ticket_id = ? AND m.id > ? ORDER BY m.id", (ticket_id, after_id))

    # ------------------------------------------------------------------ operators and canned replies
    async def add_operator(self, login: str, name: str, password_hash: str, now: float, *, admin: bool = False) -> int:
        async with self._tx() as db:
            try:
                cur = await db.execute("INSERT INTO operators (login, name, password_hash, is_admin, created_at) "
                                       "VALUES (?, ?, ?, ?, ?)", (login, name, password_hash, int(admin), now))
            except aiosqlite.IntegrityError:
                raise Conflict(f"login {login!r} already exists") from None
            return cur.lastrowid

    async def set_password(self, login: str, password_hash: str) -> bool:
        async with self._tx() as db:
            cur = await db.execute("UPDATE operators SET password_hash = ? WHERE login = ?", (password_hash, login))
            return cur.rowcount == 1

    async def operator_by_login(self, login: str) -> aiosqlite.Row | None:
        return await self._one("SELECT * FROM operators WHERE login = ? AND active = 1", (login,))

    async def operator(self, operator_id: int) -> aiosqlite.Row | None:
        return await self._one("SELECT * FROM operators WHERE id = ? AND active = 1", (operator_id,))

    async def operators(self) -> list[aiosqlite.Row]:
        return await self._all("SELECT id, login, name, is_admin FROM operators WHERE active = 1 ORDER BY name")

    async def canned(self) -> list[aiosqlite.Row]:
        return await self._all("SELECT * FROM canned ORDER BY title")

    async def add_canned(self, title: str, body: str) -> int:
        async with self._tx() as db:
            cur = await db.execute("INSERT INTO canned (title, body) VALUES (?, ?) "
                                   "ON CONFLICT(title) DO UPDATE SET body = excluded.body", (title, body))
            return cur.lastrowid

    # ------------------------------------------------------------------ statistics
    async def stats(self, now: float, sla_minutes: int, days: int = 30) -> dict:
        since = now - days * 86400
        by_status = {r["status"]: r["n"] for r in await self._all(
            "SELECT status, COUNT(*) AS n FROM tickets GROUP BY status")}
        first = await self._one(
            "SELECT COUNT(*) AS n, AVG(first_reply_at - created_at) AS avg_s, "
            "SUM(first_reply_at - created_at <= ?) AS in_sla FROM tickets "
            "WHERE first_reply_at IS NOT NULL AND created_at >= ?", (sla_minutes * 60, since))
        ratings = {r["rating"]: r["n"] for r in await self._all(
            "SELECT rating, COUNT(*) AS n FROM tickets WHERE rating IS NOT NULL AND created_at >= ? "
            "GROUP BY rating", (since,))}
        rated = sum(ratings.values())
        per_operator = await self._all(
            "SELECT o.name, COUNT(m.id) AS replies, COUNT(DISTINCT m.ticket_id) AS tickets "
            "FROM operators o LEFT JOIN messages m ON m.operator_id = o.id AND m.kind = 'out' AND m.created_at >= ? "
            "WHERE o.active = 1 GROUP BY o.id ORDER BY replies DESC, o.name", (since,))
        failed = await self._one("SELECT COUNT(*) AS n FROM messages WHERE delivery = 'failed'")
        new_count = await self._one("SELECT COUNT(*) AS n FROM tickets WHERE created_at >= ?", (since,))
        return {
            "days": days,
            "by_status": {s: by_status.get(s, 0) for s in ("new", "open", "waiting", "closed")},
            "created": new_count["n"],
            "answered": first["n"],
            "avg_first_reply_min": round(first["avg_s"] / 60, 1) if first["avg_s"] is not None else None,
            "sla_percent": round(100 * (first["in_sla"] or 0) / first["n"], 1) if first["n"] else None,
            "ratings": {s: ratings.get(s, 0) for s in range(1, 6)},
            "csat_avg": round(sum(s * n for s, n in ratings.items()) / rated, 2) if rated else None,
            "csat_percent": round(100 * (ratings.get(4, 0) + ratings.get(5, 0)) / rated, 1) if rated else None,
            "per_operator": [dict(r) for r in per_operator],
            "failed_deliveries": failed["n"],
        }
