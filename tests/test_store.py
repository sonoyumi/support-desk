import aiosqlite
import pytest
from conftest import NOW

from support_desk.store import Conflict, build_query, subject_from


async def incoming(store, text="Заказ не пришёл", tg=42, name="Anna Ferrari", at=NOW, **kw):
    return await store.add_incoming(tg_id=tg, name=name, username="annaf", text=text, attachment=kw.get("att"),
                                    tg_message_id=1, now=at, sla_minutes=30)


async def test_first_message_opens_a_ticket_with_sla(store):
    r = await incoming(store, "Здравствуйте!   Заказ №4821\nне пришёл")
    t = await store.get_ticket(r.ticket_id)
    assert r.new_ticket and t["status"] == "new"
    assert t["subject"] == "Здравствуйте! Заказ №4821 не пришёл"  # one line, spaces collapsed
    assert t["sla_due_at"] == NOW + 30 * 60


async def test_next_messages_join_the_active_ticket(store):
    a = await incoming(store, "раз")
    b = await incoming(store, "два", at=NOW + 10)
    assert b.ticket_id == a.ticket_id and not b.new_ticket
    assert [m["text"] for m in await store.messages(a.ticket_id)] == ["раз", "два"]


async def test_reply_moves_to_waiting_and_customer_answer_restarts_sla(store, operator_id):
    r = await incoming(store)
    await store.add_reply(r.ticket_id, operator_id, "Проверяем", NOW + 600)
    t = await store.get_ticket(r.ticket_id)
    assert (t["status"], t["first_reply_at"], t["sla_due_at"], t["assignee_id"]) == (
        "waiting", NOW + 600, None, operator_id)
    await incoming(store, "Спасибо, жду", at=NOW + 3600)
    t = await store.get_ticket(r.ticket_id)
    assert t["status"] == "open" and t["sla_due_at"] == NOW + 3600 + 1800
    await store.add_reply(r.ticket_id, operator_id, "Готово", NOW + 4000)
    assert (await store.get_ticket(r.ticket_id))["first_reply_at"] == NOW + 600  # the FIRST reply is kept


async def test_message_after_closing_starts_a_new_ticket(store, operator_id):
    first = await incoming(store)
    await store.close_ticket(first.ticket_id, NOW + 100)
    second = await incoming(store, "Новый вопрос", at=NOW + 200)
    assert second.new_ticket and second.ticket_id != first.ticket_id


async def test_database_allows_only_one_active_ticket_per_customer(store):
    r = await incoming(store)
    with pytest.raises(aiosqlite.IntegrityError):
        await store.db.execute("INSERT INTO tickets (customer_id, status, subject, created_at, updated_at) "
                               "VALUES (?, 'new', 'x', 0, 0)", (r.customer_id,))


async def test_customer_can_close_with_new(store):
    r = await incoming(store)
    assert await store.close_by_customer(42, NOW + 5) == r.ticket_id
    t = await store.get_ticket(r.ticket_id)
    assert (t["status"], t["closed_by"]) == ("closed", "customer")
    assert await store.close_by_customer(42, NOW + 6) is None


async def test_rating_once_only_by_its_customer_only_when_closed(store):
    r = await incoming(store)
    assert not await store.set_rating(r.ticket_id, 42, 5)  # still open
    await store.close_ticket(r.ticket_id, NOW + 1)
    assert not await store.set_rating(r.ticket_id, 999, 5)  # someone else
    assert not await store.set_rating(r.ticket_id, 42, 9)  # out of range
    assert await store.set_rating(r.ticket_id, 42, 4)
    assert not await store.set_rating(r.ticket_id, 42, 1)  # second vote ignored
    assert (await store.get_ticket(r.ticket_id))["rating"] == 4


async def test_closed_ticket_rejects_replies_and_reopen_respects_active(store, operator_id):
    first = await incoming(store)
    await store.close_ticket(first.ticket_id, NOW + 1)
    with pytest.raises(Conflict):
        await store.add_reply(first.ticket_id, operator_id, "x", NOW + 2)
    with pytest.raises(Conflict):
        await store.close_ticket(first.ticket_id, NOW + 2)
    await incoming(store, "новое", at=NOW + 3)
    with pytest.raises(Conflict, match="active"):
        await store.reopen(first.ticket_id, NOW + 4, 30)


async def test_delivery_claim_lease_and_retry(store, operator_id):
    r = await incoming(store)
    mid = await store.add_reply(r.ticket_id, operator_id, "Ответ", NOW)
    [d] = await store.claim_deliveries(NOW, lease=60)
    assert (d.message_id, d.chat_id, d.attempts) == (mid, 42, 1)
    assert await store.claim_deliveries(NOW + 30) == []  # leased: nobody else takes it
    [again] = await store.claim_deliveries(NOW + 61)  # the process "crashed": lease expired
    assert again.attempts == 2
    await store.mark_retry(mid, NOW + 500, "TelegramNetworkError")
    assert await store.claim_deliveries(NOW + 499) == []
    assert len(await store.claim_deliveries(NOW + 500)) == 1


async def test_failed_delivery_can_be_retried_from_the_panel(store, operator_id):
    r = await incoming(store)
    mid = await store.add_reply(r.ticket_id, operator_id, "Ответ", NOW)
    await store.claim_deliveries(NOW)
    await store.mark_failed(mid, "клиент заблокировал бота")
    assert await store.retry_failed(mid, NOW + 10)
    assert not await store.retry_failed(mid, NOW + 11)  # only failed messages
    [d] = await store.claim_deliveries(NOW + 10)
    assert d.attempts == 1


def test_build_query_neutralises_fts_syntax():
    assert build_query('доставка" OR text:* NEAR(') == '"доставка"* "or"* "text"* "near"*'
    assert build_query("  ,, !! ") is None
    assert subject_from("x" * 100).endswith("…") and len(subject_from("x" * 100)) == 80


async def test_search_by_words_accents_name_and_number(store, operator_id):
    a = await incoming(store, "Il pacco è arrivato rotto", tg=1, name="Paolo Greco")
    b = await incoming(store, "Как оформить возврат куртки?", tg=2, name="Olena Shevchenko")
    await store.add_note(b.ticket_id, operator_id, "проверить склад", NOW)

    async def ids(q):
        return [r["id"] for r in await store.list_tickets("all", now=NOW, query=q)]

    assert await ids("возврат") == [b.ticket_id]
    assert await ids("возвр") == [b.ticket_id]  # prefix
    assert await ids("e arrivato") == [a.ticket_id]  # "è" matches "e"
    assert await ids("склад") == [b.ticket_id]  # notes are searchable for operators
    assert await ids("Greco") == [a.ticket_id]  # customer name
    assert await ids(f"#{a.ticket_id}") == [a.ticket_id]
    assert await ids('" OR 1=1 --') == []  # no crash, no leak


async def test_filters_and_counts(store, operator_id, clock):
    new = await incoming(store, tg=1)
    waiting = await incoming(store, tg=2)
    await store.add_reply(waiting.ticket_id, operator_id, "ok", NOW + 1)
    closed = await incoming(store, tg=3)
    await store.close_ticket(closed.ticket_id, NOW + 1)
    later = NOW + 31 * 60
    counts = await store.counts(operator_id, later)
    assert counts == {"needs_reply": 1, "mine": 1, "unassigned": 1, "overdue": 1, "waiting": 1, "closed": 1,
                      "all": 3}
    assert [r["id"] for r in await store.list_tickets("overdue", now=later)] == [new.ticket_id]
    assert await store.list_tickets("overdue", now=NOW) == []


async def test_sla_announced_once_and_auto_close_queues_rating(store, operator_id):
    late = await incoming(store, tg=1)
    idle = await incoming(store, tg=2)
    await store.add_reply(idle.ticket_id, operator_id, "Ответили", NOW)
    [row] = await store.overdue_to_notify(NOW + 31 * 60)
    assert row["id"] == late.ticket_id
    assert await store.overdue_to_notify(NOW + 40 * 60) == []  # announced only once
    assert await store.auto_close(NOW + 47 * 3600, 48, "№{ticket}?") == []
    assert await store.auto_close(NOW + 49 * 3600, 48, "Обращение №{ticket} закрыто") == [idle.ticket_id]
    rating = [m for m in await store.messages(idle.ticket_id) if m["kind"] == "rating"]
    assert rating[0]["text"] == f"Обращение №{idle.ticket_id} закрыто" and rating[0]["delivery"] == "pending"
    assert (await store.get_ticket(idle.ticket_id))["closed_by"] == "auto"


async def test_stats(store, operator_id):
    for tg, reply_after in ((1, 600), (2, 3600)):
        r = await incoming(store, tg=tg)
        await store.add_reply(r.ticket_id, operator_id, "ok", NOW + reply_after)
        await store.close_ticket(r.ticket_id, NOW + 4000)
        await store.set_rating(r.ticket_id, tg, 5 if tg == 1 else 2)
    await incoming(store, tg=3)
    s = await store.stats(NOW + 5000, sla_minutes=30)
    assert s["created"] == 3 and s["answered"] == 2
    assert s["avg_first_reply_min"] == 35.0  # (10 + 60) / 2
    assert s["sla_percent"] == 50.0  # 10 min within 30, 60 min not
    assert s["csat_avg"] == 3.5 and s["csat_percent"] == 50.0
    assert s["by_status"] == {"new": 1, "open": 0, "waiting": 0, "closed": 2}
    assert s["per_operator"][0]["replies"] == 2
