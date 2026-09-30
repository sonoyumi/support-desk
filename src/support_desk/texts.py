"""Everything the bot says to customers and operators, plus small pure helpers."""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

ROME = ZoneInfo("Europe/Rome")

LANGS = ("it", "en", "uk", "ru")

# What the bot says to customers, in the customer's Telegram language (fallback: English).
CUSTOMER = {
    "greeting": {
        "it": "Buongiorno! Questa è l'assistenza di «{company}».\n\nDescriva il problema con uno o più messaggi, "
              "anche con foto o file. Un operatore risponderà qui.\n\n/new — chiudere la richiesta e aprirne una nuova",
        "en": "Hello! This is «{company}» support.\n\nDescribe your question in one or more messages, photos and "
              "files are welcome. An operator will reply right here.\n\n/new — close the request and start a new one",
        "uk": "Вітаємо! Це підтримка «{company}».\n\nОпишіть питання одним або кількома повідомленнями — можна з "
              "фото чи файлом. Оператор відповість тут.\n\n/new — закрити звернення й почати нове",
        "ru": "Здравствуйте! Это поддержка «{company}».\n\nОпишите вопрос одним или несколькими сообщениями — можно "
              "с фото или файлом. Оператор ответит здесь же.\n\n/new — закрыть текущее обращение и начать новое",
    },
    "ticket_created": {
        "it": "Richiesta n. {ticket} ricevuta. Un operatore risponderà qui, di solito entro {sla} min.",
        "en": "Request #{ticket} received. An operator will reply here, usually within {sla} min.",
        "uk": "Звернення №{ticket} прийнято. Оператор відповість тут, зазвичай протягом {sla} хв.",
        "ru": "Обращение №{ticket} принято. Оператор ответит здесь же, обычно в течение {sla} мин.",
    },
    "closed_by_customer": {
        "it": "Richiesta n. {ticket} chiusa. Il prossimo messaggio aprirà una nuova richiesta.",
        "en": "Request #{ticket} closed. Your next message starts a new one.",
        "uk": "Звернення №{ticket} закрито. Наступне повідомлення почне нове.",
        "ru": "Обращение №{ticket} закрыто. Следующее сообщение начнёт новое.",
    },
    "nothing_to_close": {
        "it": "Non ci sono richieste aperte: scriva pure la sua domanda.",
        "en": "No open requests — just write your question.",
        "uk": "Відкритих звернень немає — просто напишіть питання.",
        "ru": "Открытых обращений нет — просто напишите вопрос.",
    },
    "too_long": {
        "it": "Messaggio troppo lungo ({length} caratteri). Lo divida in parti da {limit}.",
        "en": "The message is too long ({length} characters). Please split it into parts of {limit}.",
        "uk": "Повідомлення задовге ({length} симв.). Розділіть його на частини по {limit}.",
        "ru": "Сообщение слишком длинное ({length} симв.). Разделите его на части по {limit}.",
    },
    "unsupported": {
        "it": "Questo tipo di messaggio non è supportato. Scriva un testo o invii una foto o un file.",
        "en": "This kind of message is not supported. Please send text, a photo or a file.",
        "uk": "Цей тип повідомлення не підтримується. Напишіть текстом або надішліть фото чи файл.",
        "ru": "Этот тип сообщения не поддерживается. Напишите текстом или пришлите фото/файл.",
    },
    "rating_request": {
        "it": "Richiesta n. {ticket} chiusa. Come valuta l'aiuto dell'operatore?",
        "en": "Request #{ticket} is closed. How would you rate the help you got?",
        "uk": "Звернення №{ticket} закрито. Оцініть, будь ласка, допомогу оператора:",
        "ru": "Обращение №{ticket} закрыто. Оцените, пожалуйста, помощь оператора:",
    },
    "rating_thanks": {"it": "Grazie per la valutazione!", "en": "Thank you for your rating!",
                      "uk": "Дякуємо за оцінку!", "ru": "Спасибо за оценку!"},
    "rating_already": {"it": "Valutazione già registrata.", "en": "Your rating is already saved.",
                       "uk": "Оцінку вже враховано.", "ru": "Оценка уже учтена."},
}


def lang_of(code: str | None) -> str:
    """Telegram language_code ("it", "en-US", "uk") -> one of LANGS; unknown -> English."""
    short = (code or "")[:2].lower()
    return short if short in LANGS else "en"


def t(lang: str, key: str, **values) -> str:
    variants = CUSTOMER[key]
    return variants.get(lang, variants["en"]).format(**values)


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
