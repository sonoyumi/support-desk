"""Fixtures: temporary database, controllable clock, a fake Telegram (records every Bot API call),
settings and an HTTP client for the panel."""

from __future__ import annotations

import datetime as dt

import httpx
import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, Update, User

from support_desk import security
from support_desk.bot import make_router
from support_desk.config import Settings
from support_desk.store import Store
from support_desk.web import COOKIE, create_app

NOW = 1_790_000_000.0  # 2026-09-21, a fixed moment
TOKEN = "123456:AAtest_token_not_real_000000000000"
SECRET = "s" * 40


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeTelegram(BaseSession):
    """Instead of api.telegram.org: remembers calls; ``fail_with`` raises the next exceptions in order."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod] = []
        self.fail_with: list[Exception] = []
        self.next_id = 500

    async def close(self) -> None:
        return None

    async def stream_content(self, *a, **kw):  # pragma: no cover
        raise NotImplementedError

    async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109 — aiogram BaseSession signature
        self.calls.append(method)
        if self.fail_with:
            raise self.fail_with.pop(0)
        if isinstance(method, SendMessage | EditMessageText):
            self.next_id += 1
            return Message(message_id=self.next_id, date=dt.datetime.fromtimestamp(NOW, dt.UTC),
                           chat=Chat(id=method.chat_id or 1, type="private"), text=method.text)
        if isinstance(method, AnswerCallbackQuery):
            return True
        return True

    def sent(self) -> list[SendMessage]:
        return [c for c in self.calls if isinstance(c, SendMessage)]


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(bot_token=TOKEN, secret_key=SECRET, operators_chat_id=-100200, database_path=tmp_path / "t.db",
                    panel_url="https://support.example", sla_first_reply_minutes=30, auto_close_hours=48,
                    max_message_chars=500)


@pytest.fixture
async def store(tmp_path):
    s = await Store(tmp_path / "t.db").open()
    yield s
    await s.close()


@pytest.fixture
def tg() -> FakeTelegram:
    return FakeTelegram()


@pytest.fixture
def bot(tg) -> Bot:
    return Bot(TOKEN, session=tg, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


class Customer:
    """Sends updates through the REAL aiogram Dispatcher with our router."""

    def __init__(self, dp: Dispatcher, bot: Bot, user_id: int = 42, name: str = "Anna", chat_type: str = "private"):
        self.dp, self.bot = dp, bot
        self.user = User(id=user_id, is_bot=False, first_name=name, username=name.lower())
        self.chat = Chat(id=user_id if chat_type == "private" else -5000, type=chat_type)
        self.update_id = 0
        self.msg_id = 0

    def _ids(self) -> tuple[int, int]:
        self.update_id += 1
        self.msg_id += 1
        return self.update_id, self.msg_id

    async def say(self, text: str | None = None, *, photo: bool = False, caption: str | None = None):
        uid, mid = self._ids()
        kwargs = {"text": text}
        if photo:
            kwargs = {"photo": [PhotoSize(file_id="small", file_unique_id="s", width=90, height=90),
                                PhotoSize(file_id="big-file-id", file_unique_id="b", width=900, height=900)],
                      "caption": caption}
        msg = Message(message_id=mid, date=dt.datetime.fromtimestamp(NOW, dt.UTC), chat=self.chat,
                      from_user=self.user, **kwargs)
        await self.dp.feed_update(self.bot, Update(update_id=uid, message=msg))

    async def press(self, data: str, text: str = "Обращение закрыто"):
        uid, mid = self._ids()
        msg = Message(message_id=mid, date=dt.datetime.fromtimestamp(NOW, dt.UTC), chat=self.chat, text=text)
        cq = CallbackQuery(id=str(uid), from_user=self.user, chat_instance="x", message=msg, data=data)
        await self.dp.feed_update(self.bot, Update(update_id=uid, callback_query=cq))


@pytest.fixture
def customer(store, settings, clock, bot):
    dp = Dispatcher()
    dp.include_router(make_router(store, settings, clock))
    return Customer(dp, bot)


@pytest.fixture
async def operator_id(store) -> int:
    return await store.add_operator("giulia", "Giulia Rossi", security.hash_password("correct horse battery"),
                                    NOW, admin=True)


@pytest.fixture
async def panel(store, settings, clock):
    """Anonymous HTTP client for the panel."""
    app = create_app(store, settings, clock)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.fixture
async def logged_in(panel, operator_id, clock):
    cookie = security.make_session(SECRET, operator_id, clock())
    panel.cookies.set(COOKIE, cookie)
    panel.csrf = security.csrf_token(SECRET, cookie)
    return panel
