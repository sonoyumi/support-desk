"""Background work: delivering operator replies to Telegram and the SLA / auto-close rules.

Delivery is separate from the web request on purpose: the operator presses "Send", the reply is saved
in a millisecond, and this loop delivers it — with retries if Telegram is unreachable, and with an
honest "failed" (visible in the panel, retry button) if the customer blocked the bot.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

from . import texts
from .bot import notify_operators, rating_keyboard
from .config import Settings
from .store import Delivery, Store

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 8
BASE_DELAY = 5.0
MAX_DELAY = 600.0


def rating_request(ticket_id: int, lang: str) -> str:
    return texts.t(lang, "rating_request", ticket=ticket_id)


def backoff(attempt: int, rnd: Callable[[], float] = random.random) -> float:
    """5 s, 10 s, 20 s … ±20 % jitter; the ceiling is applied LAST, so jitter never exceeds it."""
    delay = BASE_DELAY * 2 ** (attempt - 1)
    return min(delay * (0.8 + 0.4 * rnd()), MAX_DELAY)


class Sender:
    def __init__(self, store: Store, bot: Bot, settings: Settings, clock: Callable[[], float] = time.time,
                 rnd: Callable[[], float] = random.random) -> None:
        self.store, self.bot, self.settings, self.clock, self.rnd = store, bot, settings, clock, rnd

    async def deliver(self, d: Delivery) -> str:
        """Send one queued message; returns the new delivery state."""
        try:
            if d.kind == "rating":
                sent = await self.bot.send_message(d.chat_id, texts.escape(d.text),
                                                   reply_markup=rating_keyboard(d.ticket_id))
            else:
                sent = await self.bot.send_message(d.chat_id, d.text, parse_mode=None)
        except TelegramRetryAfter as exc:  # Telegram asked us to slow down: obey exactly
            await self.store.mark_retry(d.message_id, self.clock() + exc.retry_after, f"retry after {exc.retry_after}s")
            return "pending"
        except TelegramForbiddenError:  # the customer blocked the bot: retrying cannot help
            await self.store.mark_failed(d.message_id, "клиент заблокировал бота")
            return "failed"
        except TelegramBadRequest as exc:  # chat not found, message too long …: permanent
            await self.store.mark_failed(d.message_id, f"Telegram: {exc.message}")
            return "failed"
        except (TimeoutError, TelegramNetworkError, TelegramServerError, OSError) as exc:
            if d.attempts >= MAX_ATTEMPTS:
                await self.store.mark_failed(d.message_id, f"не доставлено после {d.attempts} попыток: "
                                                           f"{type(exc).__name__}")
                return "failed"
            await self.store.mark_retry(d.message_id, self.clock() + backoff(d.attempts, self.rnd),
                                        type(exc).__name__)
            return "pending"
        await self.store.mark_sent(d.message_id, sent.message_id)
        return "sent"

    async def tick(self) -> int:
        batch = await self.store.claim_deliveries(self.clock())
        for d in batch:
            await self.deliver(d)
        return len(batch)

    async def rules(self) -> None:
        """SLA announcements and auto-close (runs once a minute)."""
        now = self.clock()
        for row in await self.store.overdue_to_notify(now):
            await notify_operators(self.bot, self.settings, texts.OPS_SLA.format(
                ticket=row["id"], sla=self.settings.sla_first_reply_minutes, subject=row["subject"],
                url=f"{self.settings.panel_url}/tickets/{row['id']}"))
        await self.store.auto_close(now, self.settings.auto_close_hours, rating_request)

    async def run(self, stop: asyncio.Event, poll: float = 1.0) -> None:
        last_rules = 0.0
        while not stop.is_set():
            try:
                sent = await self.tick()
                if self.clock() - last_rules >= 60:
                    last_rules = self.clock()
                    await self.rules()
            except Exception:  # noqa: BLE001 — a bug in one round must not stop delivery forever
                log.exception("sender round failed")
                sent = 0
            if not sent:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=poll)
                except TimeoutError:
                    pass
