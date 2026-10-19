"""Обратная связь в один тап и вопрос об итоге через 7 дней.

«Уже знал» — «событие было мне известно», без привязки к статусу клиента.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from .config import FEEDBACK_VALUES, FOLLOWUP_DAYS, MISS_STREAK_ASK, OUTCOME_VALUES
from .scoring import adjust_personal
from .storage import Storage


@dataclass
class FeedbackResult:
    ok: bool
    message: str
    ask_reason_for: str | None = None   # тип события, по которому спросить причину «мимо»


def record_feedback(db: Storage, card_id: int, value: str,
                    at: datetime | None = None) -> FeedbackResult:
    if value not in FEEDBACK_VALUES:
        return FeedbackResult(False, f"Неизвестный ответ: {value}")
    row = db.card_row(card_id)
    if row is None:
        return FeedbackResult(False, f"Карточка {card_id} не найдена")
    at = at or datetime.now()
    km_id = row["km_id"]
    types = db.card_types(card_id) or []
    primary = types[0] if types else None

    db.set_feedback(card_id, value, at)
    db.mark_opened(km_id, date.fromisoformat(row["created"]), at)
    ask = None
    if primary:
        weights = db.personal_weights(km_id)
        db.set_weight(km_id, primary, adjust_personal(weights.get(primary, 1.0), value))
        db.log_feedback(card_id, km_id, primary, value, at)
        history = db.feedback_history(km_id, primary)
        tail = history[-MISS_STREAK_ASK:]
        if value == "miss" and len(tail) == MISS_STREAK_ASK and all(v == "miss" for v in tail):
            ask = primary
    db.commit()
    return FeedbackResult(True, f"Записано: {FEEDBACK_VALUES[value]}", ask)


def record_outcome(db: Storage, card_id: int, value: str,
                   at: datetime | None = None) -> FeedbackResult:
    if value not in OUTCOME_VALUES:
        return FeedbackResult(False, f"Неизвестный итог: {value}")
    if db.card_row(card_id) is None:
        return FeedbackResult(False, f"Карточка {card_id} не найдена")
    db.set_outcome(card_id, value, at or datetime.now())
    return FeedbackResult(True, f"Итог записан: {OUTCOME_VALUES[value]}")


def due_followups(db: Storage, today: date) -> list[dict]:
    """Карточки «связался», по которым пора спросить: «Чем закончилось?»"""
    return [{"card_id": r["id"], "km_id": r["km_id"], "inn": r["inn"]}
            for r in db.due_followups(today, FOLLOWUP_DAYS)]
