"""Everything the bot says to customers and operators, plus small pure helpers."""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

ROME = ZoneInfo("Europe/Rome")

GREETING = ("Здравствуйте! Это поддержка «{company}».\n\n"
            "Опишите вопрос одним или несколькими сообщениями — можно с фото или файлом. "
            "Оператор ответит здесь же.\n\n/new — закрыть текущее обращение и начать новое")
TICKET_CREATED = "Обращение №{ticket} принято. Оператор ответит здесь же, обычно в течение {sla} мин."
TICKET_CLOSED_BY_CUSTOMER = "Обращение №{ticket} закрыто. Следующее сообщение начнёт новое."
NOTHING_TO_CLOSE = "Открытых обращений нет — просто напишите вопрос."
TOO_LONG = "Сообщение слишком длинное ({length} симв.). Разделите его на части по {limit}."
UNSUPPORTED = "Этот тип сообщения не поддерживается. Напишите текстом или пришлите фото/файл."
RATING_REQUEST = "Обращение №{ticket} закрыто. Оцените, пожалуйста, помощь оператора:"
RATING_THANKS = "Спасибо за оценку!"
RATING_ALREADY = "Оценка уже учтена."

OPS_NEW_TICKET = "🆕 Обращение №{ticket} от {customer}\n«{subject}»\n{url}"
OPS_SLA = "⏰ SLA нарушен: №{ticket} ждёт ответа дольше {sla} мин.\n«{subject}»\n{url}"

STATUS_LABELS = {"new": "Новое", "open": "Ждёт ответа", "waiting": "Ждём клиента", "closed": "Закрыто"}
FILTER_LABELS = {"needs_reply": "Нужен ответ", "mine": "Мои", "unassigned": "Без оператора",
                 "overdue": "Просрочено", "waiting": "Ждём клиента", "closed": "Закрытые", "all": "Все"}
CLOSED_BY = {"operator": "оператором", "customer": "клиентом", "auto": "автоматически"}

_PLACEHOLDER = re.compile(r"\{(name|ticket|operator)\}")


def fill_template(body: str, *, name: str, ticket: int, operator: str) -> str:
    """Canned reply placeholders {name} {ticket} {operator}; any other braces stay as they are."""
    values = {"name": name, "ticket": str(ticket), "operator": operator}
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], body)


def first_name(full: str) -> str:
    return full.split()[0] if full.split() else full


def local_time(ts: float | None, fmt: str = "%d.%m %H:%M") -> str:
    if ts is None:
        return "—"
    return datetime.fromtimestamp(ts, UTC).astimezone(ROME).strftime(fmt)


def duration(seconds: float) -> str:
    """Human duration: 45 с, 12 мин, 3 ч 05 мин, 2 дн 4 ч."""
    seconds = int(abs(seconds))
    if seconds < 60:
        return f"{seconds} с"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} ч {minutes:02d} мин"
    days, hours = divmod(hours, 24)
    return f"{days} дн {hours} ч"


def sla_state(sla_due_at: float | None, now: float) -> tuple[str, str]:
    """(css class, label) for the SLA badge of a ticket that needs a reply."""
    if sla_due_at is None:
        return "", ""
    left = sla_due_at - now
    if left < 0:
        return "late", f"просрочено на {duration(left)}"
    if left < 600:
        return "soon", f"осталось {duration(left)}"
    return "ok", f"осталось {duration(left)}"


def escape(text: str) -> str:
    """For Telegram HTML parse mode."""
    return html.escape(text, quote=False)
