"""support-desk run | web | add-operator | passwd | canned-defaults | stats | check | demo"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys
import time

from . import security, texts
from .config import ConfigError, Settings, load_settings
from .store import Conflict, Store

log = logging.getLogger("support_desk")

# Replies go to customers, so the ready-made ones are in Italian; edit them on the «Шаблоны» page.
DEFAULT_CANNED = {
    "Saluto": "Buongiorno {name}, sono {operator} dell'assistenza. Mi occupo della sua richiesta n. {ticket}.",
    "Servono dettagli": "{name}, può indicarmi il numero d'ordine e inviare una foto o uno screenshot del problema?",
    "Passato al reparto": "Ho inoltrato la richiesta al reparto competente: le rispondiamo oggi entro le 18:00.",
    "Risolto": "Sono contento che sia tutto a posto! Per qualsiasi domanda scriva pure qui.",
}


async def _serve(settings: Settings, *, with_bot: bool) -> None:
    import uvicorn

    from .web import create_app

    store = await Store(settings.database_path).open()
    server = uvicorn.Server(uvicorn.Config(create_app(store, settings), host=settings.host, port=settings.port,
                                           log_level="warning"))
    tasks = [asyncio.create_task(server.serve(), name="web")]
    stop = asyncio.Event()
    bot = None
    if with_bot:
        from aiogram import Bot, Dispatcher
        from aiogram.client.default import DefaultBotProperties
        from aiogram.enums import ParseMode

        from .bot import make_router
        from .sender import Sender

        bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        dp = Dispatcher()
        dp.include_router(make_router(store, settings))
        tasks.append(asyncio.create_task(dp.start_polling(bot, handle_signals=False), name="bot"))
        tasks.append(asyncio.create_task(Sender(store, bot, settings).run(stop), name="sender"))
    print(f"Панель: http://{settings.host}:{settings.port}" + ("  · бот и доставка запущены" if with_bot else ""))
    try:
        # uvicorn handles Ctrl+C / SIGTERM; when the web server stops, everything stops.
        await tasks[0]
    finally:
        stop.set()
        for t in tasks[1:]:
            t.cancel()
        await asyncio.gather(*tasks[1:], return_exceptions=True)
        if bot is not None:
            await bot.session.close()
        await store.close()


async def _with_store(settings: Settings, fn):
    store = await Store(settings.database_path).open()
    try:
        return await fn(store)
    finally:
        await store.close()


def _password(args) -> str:
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("Пароль: ")
    if first != getpass.getpass("Ещё раз: "):
        raise SystemExit("Пароли не совпадают.")
    return first


def _check_password(password: str) -> None:
    if len(password) < 10:
        raise SystemExit("Пароль короче 10 символов — возьмите длиннее.")


async def _demo(store: Store, settings: Settings) -> str:
    """Fictional tickets for showing the panel to a client (never use on a real database)."""
    now = time.time()
    ops = {}
    for login, name in (("giulia", "Giulia Rossi"), ("marco", "Marco Bianchi")):
        try:
            ops[login] = await store.add_operator(login, name, security.hash_password("demo-password-123"), now - 86400,
                                                  admin=login == "giulia")
        except Conflict:
            ops[login] = (await store.operator_by_login(login))["id"]
    for title, body in DEFAULT_CANNED.items():
        await store.add_canned(title, body)
    script = [
        (910001, "Anna Ferrari", "annaf", "it",
         "Buongiorno! L'ordine 4821 non è ancora arrivato, il tracking è fermo da lunedì.",
         -95, [("out", "giulia", -80, "Buongiorno Anna! Ho controllato: il pacco è nel magazzino di Verona, "
                                      "il corriere lo consegna domani entro le 14:00."),
               ("in", None, -30, "Grazie! Si può consegnare dopo le 18:00?")]),
        (910002, "Luca Moretti", None, "it",
         "Non riesco ad accedere all'area clienti: dice «password errata» anche dopo il cambio.", -52, []),
        (910003, "Olena Shevchenko", "olena_s", "uk",
         "Добрий день! Хочу повернути куртку, не підійшов розмір. Як це оформити?", -18, []),
        (910004, "Paolo Greco", "pgreco", "it", "Ho pagato con la carta e mi hanno addebitato due volte 😟",
         -8, [("note", "marco", -6, "Controllare in Stripe il pagamento del 30/09: sembra un doppio addebito.")]),
        (910005, "Sara Conti", "saraconti", "it", "Fate consegne a Bolzano?",
         -300, [("out", "marco", -290, "Sì, a Bolzano consegniamo in 1 giorno, gratis sopra i 50 €."),
                ("in", None, -285, "Perfetto, grazie!")]),
    ]
    for tg, name, user, lang, first, minutes, rest in script:
        inc = await store.add_incoming(tg_id=tg, name=name, username=user, text=first, attachment=None,
                                       tg_message_id=None, now=now + minutes * 60,
                                       sla_minutes=settings.sla_first_reply_minutes, lang=lang)
        for kind, op, m, text in rest:
            at = now + m * 60
            if kind == "out":
                await store.add_reply(inc.ticket_id, ops[op], text, at)
            elif kind == "note":
                await store.add_note(inc.ticket_id, ops[op], text, at)
            else:
                await store.add_incoming(tg_id=tg, name=name, username=user, text=text, attachment=None,
                                         tg_message_id=None, now=at, sla_minutes=settings.sla_first_reply_minutes,
                                         lang=lang)
        if tg == 910005:
            await store.close_ticket(inc.ticket_id, now - 280 * 60, by="operator")
            await store.set_rating(inc.ticket_id, tg, 5)
    return "Демо-данные добавлены. Вход: giulia / demo-password-123 (или marco)."


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="support-desk", description="Поддержка клиентов: бот + панель операторов")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="панель, бот и доставка ответов в одном процессе")
    sub.add_parser("web", help="только панель (без бота): для настройки и показа")
    p = sub.add_parser("add-operator", help="добавить оператора")
    p.add_argument("login")
    p.add_argument("--name", required=True)
    p.add_argument("--admin", action="store_true", help="может менять шаблоны ответов")
    p.add_argument("--password-stdin", action="store_true", help="прочитать пароль из stdin (для скриптов)")
    p = sub.add_parser("passwd", help="сменить пароль оператора")
    p.add_argument("login")
    p.add_argument("--password-stdin", action="store_true")
    sub.add_parser("canned-defaults", help="добавить 4 стандартных шаблона ответа")
    p = sub.add_parser("stats", help="статистика в терминале")
    p.add_argument("--days", type=int, default=30)
    sub.add_parser("check", help="проверить настройки и базу")
    sub.add_parser("demo", help="вымышленные обращения для показа (не на рабочей базе!)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        settings = load_settings(need_bot=args.cmd == "run")
    except ConfigError as exc:
        raise SystemExit(str(exc)) from None

    if args.cmd in ("run", "web"):
        asyncio.run(_serve(settings, with_bot=args.cmd == "run"))
    elif args.cmd == "add-operator":
        password = _password(args)
        _check_password(password)

        async def add(store: Store):
            try:
                await store.add_operator(args.login, args.name, security.hash_password(password), time.time(),
                                         admin=args.admin)
            except Conflict:
                raise SystemExit(f"Логин {args.login} уже занят.") from None
        asyncio.run(_with_store(settings, add))
        print(f"Оператор {args.login} добавлен{' (администратор)' if args.admin else ''}.")
    elif args.cmd == "passwd":
        password = _password(args)
        _check_password(password)
        ok = asyncio.run(_with_store(settings, lambda s: s.set_password(args.login, security.hash_password(password))))
        print("Пароль изменён." if ok else f"Нет оператора {args.login}.")
    elif args.cmd == "canned-defaults":
        async def add_all(store: Store):
            for title, body in DEFAULT_CANNED.items():
                await store.add_canned(title, body)
        asyncio.run(_with_store(settings, add_all))
        print(f"Шаблонов добавлено: {len(DEFAULT_CANNED)}.")
    elif args.cmd == "stats":
        s = asyncio.run(_with_store(settings, lambda st: st.stats(time.time(), settings.sla_first_reply_minutes,
                                                                    args.days)))
        by = s["by_status"]
        print(f"За {s['days']} дн.: новых обращений {s['created']}, с ответом {s['answered']}")
        print(f"Сейчас: {' · '.join(f'{texts.STATUS_LABELS[k].lower()} {v}' for k, v in by.items())}")
        avg = f"{s['avg_first_reply_min']} мин" if s["avg_first_reply_min"] is not None else "—"
        sla = f"{s['sla_percent']}%" if s["sla_percent"] is not None else "—"
        print(f"Первый ответ в среднем: {avg} · в пределах SLA ({settings.sla_first_reply_minutes} мин): {sla}")
        csat = f"{s['csat_percent']}% довольны, средняя {s['csat_avg']}" if s["csat_avg"] else "оценок нет"
        print(f"Оценки: {csat}")
        for o in s["per_operator"]:
            print(f"  {o['name']:<20} ответов {o['replies']:>4} · обращений {o['tickets']:>3}")
        if s["failed_deliveries"]:
            print(f"⚠️  Не доставлено ответов: {s['failed_deliveries']} (клиент заблокировал бота?)")
    elif args.cmd == "check":
        async def check(store: Store):
            return len(await store.operators())
        n = asyncio.run(_with_store(settings, check))
        print("Настройки в порядке.")
        print(f"База: {settings.database_path} · операторов: {n}")
        print(f"Панель: {settings.panel_url} · SLA первого ответа: {settings.sla_first_reply_minutes} мин · "
              f"автозакрытие: {settings.auto_close_hours} ч")
        print(f"Чат операторов: {settings.operators_chat_id or 'не задан (без уведомлений)'}")
        if n == 0:
            print("⚠️  Операторов нет: support-desk add-operator LOGIN --name \"Имя\" --admin")
    elif args.cmd == "demo":
        print(asyncio.run(_with_store(settings, lambda s: _demo(s, settings))))


if __name__ == "__main__":
    main()
