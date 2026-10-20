"""Telegram-бот: приём нажатий на кнопки карточек (long polling, только stdlib).

Настройка профиля КМ в MVP — через kms.json (`kmradar init-km`); диалог
настройки в боте — следующий шаг.
"""
from __future__ import annotations

import time
from datetime import datetime

from .config import SIGNALS
from .delivery import TelegramDelivery
from .feedback import record_feedback, record_outcome
from .storage import Storage


def handle_callback(db: Storage, data: str, chat_id: str | int,
                    at: datetime | None = None) -> tuple[str, str | None]:
    """Разобрать callback_data. Возвращает (ответ на кнопку, доп. сообщение КМ или None).

    Нажимать кнопки карточки может только тот КМ, которому она отправлена."""
    try:
        kind, card_id, value = data.split(":", 2)
        cid = int(card_id)
    except ValueError:
        return "Не понял кнопку", None
    km = db.km_by_chat(str(chat_id))
    row = db.card_row(cid)
    if km is None or row is None or row["km_id"] != km.km_id:
        return "Карточка не найдена", None
    if kind == "fb":
        res = record_feedback(db, cid, value, at)
        extra = None
        if res.ask_reason_for:
            extra = (f"Третий раз «мимо» по типу «{SIGNALS[res.ask_reason_for].title}». "
                     "Что не так: размер, отрасль, сам повод? Ответьте одним сообщением.")
        return res.message, extra
    if kind == "out":
        return record_outcome(db, cid, value, at).message, None
    return "Не понял кнопку", None


def poll(db: Storage, tg: TelegramDelivery, interval: float = 1.0) -> None:  # pragma: no cover
    offset = 0
    while True:
        updates = tg.call("getUpdates", {"offset": offset, "timeout": 25}).get("result", [])
        for upd in updates:
            offset = upd["update_id"] + 1
            cb = upd.get("callback_query")
            if not cb:
                continue
            answer, extra = handle_callback(db, cb.get("data", ""), cb["from"]["id"])
            tg.call("answerCallbackQuery", {"callback_query_id": cb["id"], "text": answer})
            if extra:
                tg.call("sendMessage", {"chat_id": cb["from"]["id"], "text": extra})
        time.sleep(interval)
