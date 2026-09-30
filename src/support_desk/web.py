"""Operators' web panel (FastAPI + Jinja2, no JavaScript framework).

Every page needs a signed session cookie; every form carries a CSRF token. Templates escape all
customer text automatically, so a message like ``<script>`` is shown as text, never executed.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import security, texts
from .config import Settings
from .store import FILTERS, Conflict, NotFound, Store

HERE = Path(__file__).parent
COOKIE = "sd_session"
LOGIN_WINDOW, LOGIN_MAX_FAILS = 300, 5


class LoginRequired(Exception):
    pass


class Forbidden(Exception):
    pass


class LoginLimiter:
    """At most 5 failed logins per IP in 5 minutes (slows down password guessing)."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self.clock = clock
        self.fails: dict[str, deque[float]] = defaultdict(deque)

    def blocked(self, ip: str) -> bool:
        q = self.fails[ip]
        while q and q[0] < self.clock() - LOGIN_WINDOW:
            q.popleft()
        return len(q) >= LOGIN_MAX_FAILS

    def fail(self, ip: str) -> None:
        self.fails[ip].append(self.clock())

    def reset(self, ip: str) -> None:
        self.fails.pop(ip, None)


def create_app(store: Store, settings: Settings, clock: Callable[[], float] = time.time) -> FastAPI:
    app = FastAPI(title="Support desk", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(STATUS=texts.STATUS_LABELS, FILTERS=texts.FILTER_LABELS,
                                 CLOSED_BY=texts.CLOSED_BY, company=settings.company_name)
    templates.env.filters.update(t=texts.local_time, dur=texts.duration)
    limiter = LoginLimiter(clock)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; form-action 'self'")
        return response

    @app.exception_handler(LoginRequired)
    async def to_login(request: Request, exc: LoginRequired):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "login required"}, status_code=401)
        return RedirectResponse("/login?next=" + quote(request.url.path), status_code=303)

    @app.exception_handler(Forbidden)
    async def forbidden(request: Request, exc: Forbidden):
        return HTMLResponse("Действие отклонено: обновите страницу и повторите.", status_code=403)

    @app.exception_handler(NotFound)
    async def not_found(request: Request, exc: NotFound):
        return HTMLResponse("Обращение не найдено.", status_code=404)

    async def operator(request: Request):
        op_id = security.read_session(settings.secret_key, request.cookies.get(COOKIE), clock())
        op = await store.operator(op_id) if op_id is not None else None
        if op is None:
            raise LoginRequired
        return op

    def check_csrf(request: Request, token: str) -> None:
        if not security.check_csrf(settings.secret_key, request.cookies.get(COOKIE), token):
            raise Forbidden

    def page(request: Request, name: str, op, **ctx) -> HTMLResponse:
        cookie = request.cookies.get(COOKIE, "")
        return templates.TemplateResponse(request, name, {
            "me": op, "csrf": security.csrf_token(settings.secret_key, cookie), "now": clock(), **ctx})

    async def rating(ticket_id: int) -> str:
        """Rating request in the customer's language."""
        return texts.t((await store.get_ticket(ticket_id))["lang"], "rating_request", ticket=ticket_id)

    def back(ticket_id: int, err: str | None = None) -> RedirectResponse:
        url = f"/tickets/{ticket_id}" + (f"?err={quote(err)}" if err else "")
        return RedirectResponse(url, status_code=303)

    # ------------------------------------------------------------------ login
    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request, next: str = "/tickets", err: str = ""):
        return templates.TemplateResponse(request, "login.html", {"next": next, "err": err, "me": None})

    @app.post("/login")
    async def login(request: Request, login: str = Form(...), password: str = Form(...), next: str = Form("/")):
        ip = request.client.host if request.client else "?"
        if limiter.blocked(ip):
            return templates.TemplateResponse(request, "login.html", {
                "next": next, "me": None, "err": "Слишком много попыток. Подождите 5 минут."}, status_code=429)
        op = await store.operator_by_login(login.strip())
        if op is None or not security.verify_password(password, op["password_hash"]):
            limiter.fail(ip)
            return templates.TemplateResponse(request, "login.html", {
                "next": next, "me": None, "err": "Неверный логин или пароль."}, status_code=401)
        limiter.reset(ip)
        target = next if next.startswith("/") and not next.startswith("//") else "/"
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(COOKIE, security.make_session(settings.secret_key, op["id"], clock()),
                            max_age=security.SESSION_TTL, httponly=True, samesite="lax",
                            secure=settings.cookie_secure)
        return response

    @app.post("/logout")
    async def logout(request: Request, csrf: str = Form("")):
        check_csrf(request, csrf)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(COOKIE)
        return response

    # ------------------------------------------------------------------ tickets
    @app.get("/")
    async def home():
        return RedirectResponse("/tickets", status_code=303)

    @app.get("/tickets", response_class=HTMLResponse)
    async def tickets(request: Request, f: str = "needs_reply", q: str = ""):
        op = await operator(request)
        f = f if f in FILTERS else "needs_reply"
        rows = await store.list_tickets(f, operator_id=op["id"], now=clock(), query=q)
        return page(request, "tickets.html", op, rows=rows, f=f, q=q,
                    counts=await store.counts(op["id"], clock()), sla=texts.sla_state)

    @app.get("/tickets/{ticket_id}", response_class=HTMLResponse)
    async def ticket(request: Request, ticket_id: int, err: str = ""):
        op = await operator(request)
        t = await store.get_ticket(ticket_id)
        msgs = await store.messages(ticket_id)
        canned = [{"title": c["title"], "body": texts.fill_template(
            c["body"], name=texts.first_name(t["customer"]), ticket=t["id"], operator=op["name"])}
            for c in await store.canned()]
        return page(request, "ticket.html", op, t=t, msgs=msgs, canned=canned, err=err,
                    operators=await store.operators(), sla=texts.sla_state,
                    last_id=msgs[-1]["id"] if msgs else 0, limit=settings.max_message_chars)

    @app.post("/tickets/{ticket_id}/reply")
    async def reply(request: Request, ticket_id: int, text: str = Form(""), csrf: str = Form(""),
                    close: str = Form("")):
        op = await operator(request)
        check_csrf(request, csrf)
        text = text.strip()
        if not text:
            return back(ticket_id, "Пустой ответ не отправлен.")
        if len(text) > settings.max_message_chars:
            return back(ticket_id, f"Ответ длиннее {settings.max_message_chars} символов.")
        try:
            await store.add_reply(ticket_id, op["id"], text, clock())
            if close:
                await store.close_ticket(ticket_id, clock(), by="operator", rating_text=await rating(ticket_id))
        except Conflict:
            return back(ticket_id, "Обращение закрыто — ответ не отправлен.")
        return back(ticket_id)

    @app.post("/tickets/{ticket_id}/note")
    async def note(request: Request, ticket_id: int, text: str = Form(""), csrf: str = Form("")):
        op = await operator(request)
        check_csrf(request, csrf)
        if text.strip():
            await store.add_note(ticket_id, op["id"], text.strip()[: settings.max_message_chars], clock())
        return back(ticket_id)

    @app.post("/tickets/{ticket_id}/assign")
    async def assign(request: Request, ticket_id: int, operator_id: str = Form(""), csrf: str = Form("")):
        op = await operator(request)
        check_csrf(request, csrf)
        target = op["id"] if operator_id == "me" else (int(operator_id) if operator_id.isdigit() else None)
        if target is not None and await store.operator(target) is None:
            return back(ticket_id, "Нет такого оператора.")
        await store.assign(ticket_id, target, clock())
        return back(ticket_id)

    @app.post("/tickets/{ticket_id}/close")
    async def close(request: Request, ticket_id: int, csrf: str = Form(""), ask_rating: str = Form("")):
        await operator(request)
        check_csrf(request, csrf)
        try:
            await store.close_ticket(ticket_id, clock(), by="operator",
                                    rating_text=await rating(ticket_id) if ask_rating else None)
        except Conflict:
            return back(ticket_id, "Обращение уже закрыто.")
        return back(ticket_id)

    @app.post("/tickets/{ticket_id}/reopen")
    async def reopen(request: Request, ticket_id: int, csrf: str = Form("")):
        await operator(request)
        check_csrf(request, csrf)
        try:
            await store.reopen(ticket_id, clock(), settings.sla_first_reply_minutes)
        except Conflict as exc:
            msg = "У клиента уже есть открытое обращение." if "active" in str(exc) else "Обращение не закрыто."
            return back(ticket_id, msg)
        return back(ticket_id)

    @app.post("/messages/{message_id}/retry")
    async def retry(request: Request, message_id: int, ticket_id: int = Form(...), csrf: str = Form("")):
        await operator(request)
        check_csrf(request, csrf)
        await store.retry_failed(message_id, clock())
        return back(ticket_id)

    # ------------------------------------------------------------------ JSON for the auto-refresh script
    @app.get("/api/tickets/{ticket_id}/messages")
    async def api_messages(request: Request, ticket_id: int, after: int = 0):
        await operator(request)
        await store.get_ticket(ticket_id)
        rows = await store.messages(ticket_id, after)
        return {"messages": [{"id": m["id"], "kind": m["kind"], "text": m["text"], "operator": m["operator"],
                              "attachment": m["attachment"], "delivery": m["delivery"],
                              "time": texts.local_time(m["created_at"], "%H:%M")} for m in rows]}

    @app.get("/api/counts")
    async def api_counts(request: Request):
        op = await operator(request)
        return await store.counts(op["id"], clock())

    # ------------------------------------------------------------------ stats, canned replies
    @app.get("/stats", response_class=HTMLResponse)
    async def stats(request: Request, days: int = 30):
        op = await operator(request)
        days = days if days in (1, 7, 30, 90) else 30
        return page(request, "stats.html", op, s=await store.stats(clock(), settings.sla_first_reply_minutes, days),
                    sla_minutes=settings.sla_first_reply_minutes)

    @app.get("/canned", response_class=HTMLResponse)
    async def canned(request: Request):
        op = await operator(request)
        return page(request, "canned.html", op, items=await store.canned())

    @app.post("/canned")
    async def canned_add(request: Request, title: str = Form(""), body: str = Form(""), csrf: str = Form("")):
        op = await operator(request)
        check_csrf(request, csrf)
        if not op["is_admin"]:
            raise Forbidden
        if title.strip() and body.strip():
            await store.add_canned(title.strip()[:60], body.strip()[: settings.max_message_chars])
        return RedirectResponse("/canned", status_code=303)

    @app.get("/health")
    async def health():
        return {"ok": True}

    return app
