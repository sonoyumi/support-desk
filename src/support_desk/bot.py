"""Customer side: the Telegram bot (aiogram 3).

The bot never answers questions itself: it files every customer message into a ticket and confirms
a new ticket with its number. Operators answer from the web panel; replies come back through the
delivery queue (sender.py).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from . import texts
from .config import Settings
from .store import Store

log = logging.getLogger(__name__)


class Rate(CallbackData, prefix="rate"):
    ticket: int
    score: int


def rating_keyboard(ticket_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for score in range(1, 6):
        kb.button(text="⭐" * score, callback_data=Rate(ticket=ticket_id, score=score))
    kb.adjust(1)
    return kb.as_markup()


def describe(message: Message) -> tuple[str, str | None] | None:
    """(text for the ticket, attachment reference) or None for unsupported content."""
    caption = message.caption or ""
    if message.text:
        return message.text, None
    if message.photo:
        return caption or "[фото]", f"photo:{message.photo[-1].file_id}"
    if message.document:
        name = message.document.file_name or "файл"
        return caption or f"[файл: {name}]", f"document:{message.document.file_id}"
    if message.voice:
        return caption or "[голосовое сообщение]", f"voice:{message.voice.file_id}"
    if message.video:
        return caption or "[видео]", f"video:{message.video.file_id}"
    return None


async def notify_operators(bot: Bot, settings: Settings, text: str) -> None:
    """Best effort: a broken operators chat must never break the customer flow."""
    if settings.operators_chat_id is None:
        return
    try:
        await bot.send_message(settings.operators_chat_id, text, parse_mode=None,
                               disable_web_page_preview=True)
    except Exception as exc:  # noqa: BLE001 — log and go on
        log.warning("operators chat notification failed: %s", type(exc).__name__)


def make_router(store: Store, settings: Settings, clock: Callable[[], float] = time.time) -> Router:
    router = Router(name="customers")
    router.message.filter(F.chat.type == ChatType.PRIVATE)

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        await message.answer(texts.GREETING.format(company=texts.escape(settings.company_name)))

    @router.message(Command("new"))
    async def new(message: Message) -> None:
        ticket = await store.close_by_customer(message.from_user.id, clock())
        await message.answer(texts.NOTHING_TO_CLOSE if ticket is None
                             else texts.TICKET_CLOSED_BY_CUSTOMER.format(ticket=ticket))

    @router.message()
    async def incoming(message: Message, bot: Bot) -> None:
        described = describe(message)
        if described is None:
            await message.answer(texts.UNSUPPORTED)
            return
        text, attachment = described
        if len(text) > settings.max_message_chars:
            await message.answer(texts.TOO_LONG.format(length=len(text), limit=settings.max_message_chars))
            return
        user = message.from_user
        result = await store.add_incoming(
            tg_id=user.id, name=user.full_name, username=user.username, text=text, attachment=attachment,
            tg_message_id=message.message_id, now=clock(), sla_minutes=settings.sla_first_reply_minutes)
        if result.new_ticket:
            await message.answer(texts.TICKET_CREATED.format(ticket=result.ticket_id,
                                                             sla=settings.sla_first_reply_minutes))
            await notify_operators(bot, settings, texts.OPS_NEW_TICKET.format(
                ticket=result.ticket_id, customer=user.full_name, subject=text[:120],
                url=f"{settings.panel_url}/tickets/{result.ticket_id}"))

    @router.callback_query(Rate.filter())
    async def rate(callback: CallbackQuery, callback_data: Rate) -> None:
        ok = await store.set_rating(callback_data.ticket, callback.from_user.id, callback_data.score)
        await callback.answer(texts.RATING_THANKS if ok else texts.RATING_ALREADY)
        if ok and isinstance(callback.message, Message):
            await callback.message.edit_text(
                f"{texts.escape(callback.message.text or '')}\n\n{'⭐' * callback_data.score}", reply_markup=None)

    return router
