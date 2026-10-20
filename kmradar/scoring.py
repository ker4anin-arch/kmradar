"""score = W_событие × F_свежесть × S_размер × P_продукт × личный вес КМ.

Прозрачные правила: каждый множитель можно показать КМ.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median

from .config import SCORING, SIGNALS, ScoringConfig
from .models import Event, KMProfile


@dataclass
class ScoreParts:
    w: float
    f: float
    s: float
    p: float
    personal: float

    @property
    def total(self) -> float:
        return round(self.w * self.f * self.s * self.p * self.personal, 4)


def freshness(event: Event, today: date) -> float:
    window = SIGNALS[event.type].window_days
    age = (today - event.event_date).days
    if age < 0:
        age = 0
    return max(0.0, 1.0 - age / window)


def deadline(event: Event) -> date:
    return event.event_date + timedelta(days=SIGNALS[event.type].window_days)


def size_factor(event: Event, median_contract: float, cfg: ScoringConfig = SCORING) -> float:
    if event.type not in ("tender_win", "contract_signed"):
        return 1.0
    amount = event.facts.get("amount") or 0
    if median_contract <= 0:
        return 1.0
    return max(cfg.size_floor, min(cfg.size_cap, amount / median_contract))


def product_factor(event: Event, km: KMProfile, cfg: ScoringConfig = SCORING) -> float:
    if set(SIGNALS[event.type].products) & set(km.products):
        return cfg.product_match
    return cfg.product_miss


def score_event(event: Event, km: KMProfile, today: date, median_contract: float,
                personal: dict[str, float] | None = None) -> ScoreParts:
    sig = SIGNALS[event.type]
    return ScoreParts(
        w=sig.weight,
        f=freshness(event, today),
        s=size_factor(event, median_contract),
        p=product_factor(event, km),
        personal=(personal or {}).get(event.type, 1.0),
    )


def territory_median(amounts: list[int], cfg: ScoringConfig = SCORING) -> float:
    return float(median(amounts)) if amounts else float(cfg.default_median_contract)


def adjust_personal(current: float, feedback: str, cfg: ScoringConfig = SCORING) -> float:
    """«Полезно» поднимает личный вес типа события для КМ, «мимо» — опускает."""
    if feedback == "useful":
        current += cfg.feedback_step
    elif feedback == "miss":
        current -= cfg.feedback_step
    return round(min(cfg.feedback_max, max(cfg.feedback_min, current)), 4)
