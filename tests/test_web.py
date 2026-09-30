from conftest import NOW

from support_desk import security


async def new_ticket(store, text="Заказ не пришёл", tg=42, name="Anna Ferrari"):
    r = await store.add_incoming(tg_id=tg, name=name, username="annaf", text=text, attachment=None,
                                 tg_message_id=1, now=NOW, sla_minutes=30)
    return r.ticket_id


async def test_pages_need_login(panel):
    r = await panel.get("/tickets/5")
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/tickets/5"
    assert (await panel.get("/api/counts")).status_code == 401


async def test_wrong_password_then_rate_limit(panel, operator_id):
    for _ in range(5):
        r = await panel.post("/login", data={"login": "giulia", "password": "nope"})
        assert r.status_code == 401 and "Неверный логин или пароль" in r.text
    r = await panel.post("/login", data={"login": "giulia", "password": "correct horse battery"})
    assert r.status_code == 429  # even the right password waits after 5 failures


async def test_login_sets_httponly_cookie_and_blocks_open_redirect(panel, operator_id):
    r = await panel.post("/login", data={"login": "GIULIA", "password": "correct horse battery",
                                         "next": "//evil.example/x"})
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie


async def test_list_shows_ticket_and_escapes_customer_text(logged_in, store):
    tid = await new_ticket(store, "<script>alert(1)</script> помогите", name="<b>Hacker</b>")
    r = await logged_in.get("/tickets")
    assert r.status_code == 200 and f"#{tid}" in r.text
    assert "<script>alert(1)" not in r.text and "&lt;script&gt;alert(1)" in r.text
    assert "&lt;b&gt;Hacker&lt;/b&gt;" in r.text
    page = await logged_in.get(f"/tickets/{tid}")
    assert "<script>alert(1)" not in page.text


async def test_security_headers(logged_in):
    r = await logged_in.get("/tickets")
    assert r.headers["x-frame-options"] == "DENY"
    assert "script-src 'self'" in r.headers["content-security-policy"]


async def test_forms_without_csrf_are_rejected(logged_in, store):
    tid = await new_ticket(store)
    r = await logged_in.post(f"/tickets/{tid}/reply", data={"text": "hi", "csrf": "forged"})
    assert r.status_code == 403
    assert [m["kind"] for m in await store.messages(tid)] == ["in"]


async def test_reply_is_queued_and_ticket_waits_for_customer(logged_in, store, operator_id):
    tid = await new_ticket(store)
    r = await logged_in.post(f"/tickets/{tid}/reply", data={"text": "  Уже проверяем!  ", "csrf": logged_in.csrf})
    assert r.status_code == 303 and r.headers["location"] == f"/tickets/{tid}"
    out = (await store.messages(tid))[-1]
    assert (out["kind"], out["text"], out["delivery"], out["operator_id"]) == ("out", "Уже проверяем!", "pending",
                                                                               operator_id)
    t = await store.get_ticket(tid)
    assert t["status"] == "waiting" and t["assignee_id"] == operator_id


async def test_empty_and_too_long_replies_are_not_sent(logged_in, store, settings):
    tid = await new_ticket(store)
    r = await logged_in.post(f"/tickets/{tid}/reply", data={"text": "   ", "csrf": logged_in.csrf})
    assert "err=" in r.headers["location"]
    await logged_in.post(f"/tickets/{tid}/reply", data={"text": "x" * (settings.max_message_chars + 1),
                                                         "csrf": logged_in.csrf})
    assert [m["kind"] for m in await store.messages(tid)] == ["in"]


async def test_note_is_never_delivered(logged_in, store):
    tid = await new_ticket(store)
    await logged_in.post(f"/tickets/{tid}/note", data={"text": "Проверить Stripe", "csrf": logged_in.csrf})
    note = (await store.messages(tid))[-1]
    assert note["kind"] == "note" and note["delivery"] is None
    assert await store.claim_deliveries(NOW) == []
    assert (await store.get_ticket(tid))["status"] == "new"  # a note is not an answer


async def test_assign_to_me_and_unknown_operator(logged_in, store, operator_id):
    tid = await new_ticket(store)
    await logged_in.post(f"/tickets/{tid}/assign", data={"operator_id": "me", "csrf": logged_in.csrf})
    assert (await store.get_ticket(tid))["assignee_id"] == operator_id
    r = await logged_in.post(f"/tickets/{tid}/assign", data={"operator_id": "999", "csrf": logged_in.csrf})
    assert "err=" in r.headers["location"]
    assert (await store.get_ticket(tid))["assignee_id"] == operator_id


async def test_reply_and_close_queues_a_rating_request(logged_in, store):
    tid = await new_ticket(store)
    await logged_in.post(f"/tickets/{tid}/reply", data={"text": "Готово", "close": "1", "csrf": logged_in.csrf})
    kinds = [(m["kind"], m["delivery"]) for m in await store.messages(tid)]
    assert kinds == [("in", None), ("out", "pending"), ("rating", "pending")]
    assert (await store.get_ticket(tid))["closed_by"] == "operator"
    r = await logged_in.post(f"/tickets/{tid}/reply", data={"text": "ещё", "csrf": logged_in.csrf})
    assert "err=" in r.headers["location"]  # closed: nothing sent


async def test_rating_request_is_in_the_customer_language(logged_in, store):
    r = await store.add_incoming(tg_id=77, name="Marta Russo", username=None, text="vetro rotto", attachment=None,
                                 tg_message_id=1, now=NOW, sla_minutes=30, lang="it")
    await logged_in.post(f"/tickets/{r.ticket_id}/close", data={"csrf": logged_in.csrf, "ask_rating": "1"})
    rating = (await store.messages(r.ticket_id))[-1]
    assert rating["kind"] == "rating" and rating["text"].startswith(f"Richiesta n. {r.ticket_id} chiusa")


async def test_close_without_rating_and_reopen(logged_in, store):
    tid = await new_ticket(store)
    await logged_in.post(f"/tickets/{tid}/close", data={"csrf": logged_in.csrf})
    assert [m["kind"] for m in await store.messages(tid)] == ["in"]
    await logged_in.post(f"/tickets/{tid}/reopen", data={"csrf": logged_in.csrf})
    t = await store.get_ticket(tid)
    assert t["status"] == "open" and t["sla_due_at"] == NOW + 1800


async def test_ticket_page_fills_canned_placeholders(logged_in, store):
    tid = await new_ticket(store, name="Anna Ferrari")
    await store.add_canned("Привет", "Здравствуйте, {name}! Я {operator}, обращение №{ticket}. {unknown}")
    r = await logged_in.get(f"/tickets/{tid}")
    assert f"Здравствуйте, Anna! Я Giulia Rossi, обращение №{tid}. {{unknown}}" in r.text


async def test_api_messages_after_id(logged_in, store):
    tid = await new_ticket(store)
    await store.add_incoming(tg_id=42, name="Anna", username=None, text="второе", attachment="photo:abc",
                             tg_message_id=2, now=NOW + 5, sla_minutes=30)
    data = (await logged_in.get(f"/api/tickets/{tid}/messages?after=1")).json()["messages"]
    assert [(m["text"], m["attachment"]) for m in data] == [("второе", "photo:abc")]
    assert (await logged_in.get("/api/tickets/999/messages")).status_code == 404


async def test_stats_and_list_filters_render(logged_in, store):
    await new_ticket(store)
    for path in ("/stats", "/stats?days=7", "/tickets?f=overdue", "/tickets?f=bogus", "/tickets?q=заказ", "/canned"):
        assert (await logged_in.get(path)).status_code == 200, path


async def test_only_admins_edit_canned(panel, store, clock):
    op = await store.add_operator("marco", "Marco", security.hash_password("another long pass"), NOW)
    cookie = security.make_session("s" * 40, op, clock())
    panel.cookies.set("sd_session", cookie)
    r = await panel.post("/canned", data={"title": "x", "body": "y", "csrf": security.csrf_token("s" * 40, cookie)})
    assert r.status_code == 403 and await store.canned() == []


async def test_logout_needs_csrf(logged_in):
    assert (await logged_in.post("/logout", data={"csrf": "bad"})).status_code == 403
    r = await logged_in.post("/logout", data={"csrf": logged_in.csrf})
    assert r.status_code == 303 and r.headers["location"] == "/login"
