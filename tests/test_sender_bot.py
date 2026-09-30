import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage
from conftest import NOW

from support_desk import texts
from support_desk.bot import Rate
from support_desk.sender import MAX_ATTEMPTS, MAX_DELAY, Sender, backoff

M = SendMessage(chat_id=1, text="x")


async def queued_reply(store, operator_id, text="Ваш заказ в пути"):
    r = await store.add_incoming(tg_id=42, name="Anna", username=None, text="где заказ?", attachment=None,
                                 tg_message_id=1, now=NOW, sla_minutes=30)
    mid = await store.add_reply(r.ticket_id, operator_id, text, NOW)
    return r.ticket_id, mid


async def delivery_of(store, mid):
    return next(m for m in await store.db.execute_fetchall("SELECT * FROM messages") if m["id"] == mid)


def test_backoff_doubles_and_jitter_never_breaks_the_ceiling():
    assert [backoff(n, lambda: 0.5) for n in (1, 2, 3)] == [5.0, 10.0, 20.0]
    assert backoff(20, lambda: 0.999) == MAX_DELAY  # cap applied after jitter
    assert backoff(1, lambda: 0.0) == pytest.approx(4.0) and backoff(1, lambda: 1.0) == pytest.approx(6.0)  # ±20 %


async def test_reply_is_delivered_as_plain_text(store, operator_id, bot, tg, settings, clock):
    _, mid = await queued_reply(store, operator_id, "Цена <b>50 €</b> & доставка")
    assert await Sender(store, bot, settings, clock).tick() == 1
    [call] = tg.sent()
    assert call.chat_id == 42 and call.text == "Цена <b>50 €</b> & доставка" and call.parse_mode is None
    row = await delivery_of(store, mid)
    assert row["delivery"] == "sent" and row["tg_message_id"] == tg.next_id


async def test_retry_after_is_obeyed(store, operator_id, bot, tg, settings, clock):
    _, mid = await queued_reply(store, operator_id)
    tg.fail_with = [TelegramRetryAfter(M, "Flood control", retry_after=17)]
    await Sender(store, bot, settings, clock).tick()
    row = await delivery_of(store, mid)
    assert row["delivery"] == "pending" and row["next_attempt_at"] == NOW + 17


async def test_network_error_backs_off_then_gives_up(store, operator_id, bot, tg, settings, clock):
    _, mid = await queued_reply(store, operator_id)
    sender = Sender(store, bot, settings, clock, rnd=lambda: 0.5)
    tg.fail_with = [TelegramNetworkError(M, "timeout")]
    await sender.tick()
    assert (await delivery_of(store, mid))["next_attempt_at"] == NOW + 5
    for _ in range(MAX_ATTEMPTS):
        clock.advance(MAX_DELAY + 1)
        tg.fail_with = [TelegramNetworkError(M, "timeout")]
        await sender.tick()
    row = await delivery_of(store, mid)
    assert row["delivery"] == "failed" and "попыток" in row["error"]


@pytest.mark.parametrize("exc, reason", [
    (TelegramForbiddenError(M, "bot was blocked by the user"), "заблокировал"),
    (TelegramBadRequest(M, "chat not found"), "chat not found"),
])
async def test_permanent_errors_fail_at_once(store, operator_id, bot, tg, settings, clock, exc, reason):
    _, mid = await queued_reply(store, operator_id)
    tg.fail_with = [exc]
    await Sender(store, bot, settings, clock).tick()
    row = await delivery_of(store, mid)
    assert row["delivery"] == "failed" and reason in row["error"] and row["attempts"] == 1


async def test_rating_request_has_five_star_buttons(store, operator_id, bot, tg, settings, clock):
    tid, _ = await queued_reply(store, operator_id)
    await store.close_ticket(tid, NOW, rating_text=texts.t("ru", "rating_request", ticket=tid))
    await Sender(store, bot, settings, clock).tick()
    rating = tg.sent()[-1]
    buttons = [b for row in rating.reply_markup.inline_keyboard for b in row]
    assert [b.text for b in buttons] == ["⭐" * n for n in range(1, 6)]
    assert Rate.unpack(buttons[4].callback_data) == Rate(ticket=tid, score=5)


async def test_rules_announce_sla_once(store, bot, tg, settings, clock):
    await store.add_incoming(tg_id=42, name="Anna", username=None, text="помогите", attachment=None,
                             tg_message_id=1, now=NOW, sla_minutes=30)
    sender = Sender(store, bot, settings, clock)
    clock.advance(31 * 60)
    await sender.rules()
    await sender.rules()
    alerts = [c for c in tg.sent() if c.chat_id == settings.operators_chat_id]
    assert len(alerts) == 1 and "SLA нарушен" in alerts[0].text
    assert "https://support.example/tickets/1" in alerts[0].text


# ---------------------------------------------------------------------- the bot, through the real Dispatcher
async def test_first_message_confirms_ticket_and_alerts_operators(customer, tg, store, settings):
    await customer.say("Заказ №4821 не пришёл")
    to_customer = [c for c in tg.sent() if c.chat_id == 42]
    to_ops = [c for c in tg.sent() if c.chat_id == settings.operators_chat_id]
    assert "Обращение №1 принято" in to_customer[0].text and "30 мин" in to_customer[0].text
    assert "Заказ №4821" in to_ops[0].text and "https://support.example/tickets/1" in to_ops[0].text
    await customer.say("и ещё: трекинг пустой")
    assert len(tg.sent()) == 2  # no second confirmation, no second alert
    assert len(await store.messages(1)) == 2


async def test_photo_is_filed_with_its_file_id(customer, store):
    await customer.say(photo=True, caption="Вот так пришла коробка")
    [m] = await store.messages(1)
    assert m["text"] == "Вот так пришла коробка" and m["attachment"] == "photo:big-file-id"


async def test_too_long_message_is_refused(customer, tg, store):
    await customer.say("а" * 501)
    assert "слишком длинное" in tg.sent()[0].text
    assert await store.list_tickets("all", now=NOW) == []


async def test_new_command_and_start(customer, tg, store):
    await customer.say("/new")
    assert tg.sent()[-1].text == texts.t("ru", "nothing_to_close")
    await customer.say("вопрос")
    await customer.say("/new")
    assert "№1 закрыто" in tg.sent()[-1].text
    await customer.say("/start")
    assert "Assistenza clienti" in tg.sent()[-1].text


async def test_rating_button(customer, tg, store):
    await customer.say("вопрос")
    await store.close_ticket(1, NOW)
    await customer.press(Rate(ticket=1, score=5).pack())
    await customer.press(Rate(ticket=1, score=1).pack())
    assert (await store.get_ticket(1))["rating"] == 5
    answers = [c.text for c in tg.calls if type(c).__name__ == "AnswerCallbackQuery"]
    assert answers == [texts.t("ru", "rating_thanks"), texts.t("ru", "rating_already")]


@pytest.mark.parametrize("code, expected", [("it", "Richiesta n. 1 ricevuta"), ("en-GB", "Request #1 received"),
                                            ("uk", "Звернення №1 прийнято"), ("de", "Request #1 received")])
async def test_bot_speaks_the_customer_language(store, settings, clock, bot, tg, code, expected):
    from aiogram import Dispatcher
    from conftest import Customer

    from support_desk.bot import make_router
    dp = Dispatcher()
    dp.include_router(make_router(store, settings, clock))
    await Customer(dp, bot, lang=code).say("help")
    assert tg.sent()[0].text.startswith(expected)
    assert (await store.get_ticket(1))["lang"] == ("en" if code in ("en-GB", "de") else code)


async def test_group_chats_are_ignored(store, settings, clock, bot, tg):
    from aiogram import Dispatcher
    from conftest import Customer

    from support_desk.bot import make_router
    dp = Dispatcher()
    dp.include_router(make_router(store, settings, clock))
    group = Customer(dp, bot, chat_type="group")
    await group.say("всем привет")
    assert tg.calls == [] and await store.list_tickets("all", now=NOW) == []
