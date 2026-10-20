"""Утренний дайджест КМ: склейка событий, лимиты, стоп-сигналы, порог.

Чистая логика без базы и сети — так её проще проверять тестами.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from .config import LIMITS, SIGNALS, Limits
from .models import Card, Event, KMProfile
from .scoring import deadline, score_event


@dataclass
class Digest:
    km_id: str
    day: date
    urgent: list[Card] = field(default_factory=list)
    regular: list[Card] = field(default_factory=list)
    cautions: dict[str, list[Event]] = field(default_factory=dict)  # ИНН → стоп-события
    strong_count: int = 0
    skipped: bool = False

    @property
    def cards(self) -> list[Card]:
        return self.urgent + self.regular


def build_cards(km: KMProfile, events: list[Event], today: date, median_contract: float,
                personal: dict[str, float] | None = None) -> list[Card]:
    """Одна компания — одна карточка: события склеиваются, главное — первым."""
    by_inn: dict[str, list[tuple[float, Event]]] = defaultdict(list)
    for e in events:
        if SIGNALS[e.type].stop:
            continue
        parts = score_event(e, km, today, median_contract, personal)
        if parts.f <= 0:
            continue
        by_inn[e.inn].append((parts.total, e))
    cards = []
    for inn, scored in by_inn.items():
        # срочное событие всегда главное, дальше — по score
        scored.sort(key=lambda x: (SIGNALS[x[1].type].urgent and x[0] > 0, x[0]), reverse=True)
        primary_score, primary = scored[0]
        urgent = SIGNALS[primary.type].urgent
        cards.append(Card(
            km_id=km.km_id, inn=inn, events=[e for _, e in scored],
            score=max(s for s, _ in scored), urgent=urgent,
            deadline=deadline(primary), created=today,
        ))
    cards.sort(key=lambda c: c.score, reverse=True)
    return cards


def build_digest(km: KMProfile, events: list[Event], today: date, *,
                 median_contract: float,
                 personal: dict[str, float] | None = None,
                 stop_events: list[Event] | None = None,
                 recent_card_inns: set[str] | None = None,
                 yesterday_strong: int | None = None,
                 limits: Limits = LIMITS) -> Digest:
    """events — свежие события по компаниям территории КМ, ещё не попадавшие в карточки.

    stop_events — стоп-сигналы за limits.stop_memory_days.
    recent_card_inns — компании, по которым КМ уже получал карточку за неделю.
    yesterday_strong — сколько сильных карточек было вчера (None — вчера не считали).
    """
    digest = Digest(km.km_id, today)
    stopped: dict[str, list[Event]] = defaultdict(list)
    for e in stop_events or []:
        stopped[e.inn].append(e)
    # «осторожно» показываем только по свежим стоп-сигналам, чтобы не повторять каждый день
    digest.cautions = {inn: evs for inn, evs in stopped.items()
                       if any(e.event_date == today for e in evs)}

    recent = recent_card_inns or set()
    cards = [c for c in build_cards(km, events, today, median_contract, personal)
             if c.inn not in stopped and c.inn not in recent]
    strong = [c for c in cards if c.score >= limits.score_threshold]
    digest.strong_count = len(strong)

    digest.urgent = [c for c in strong if c.urgent][: limits.max_urgent]
    regular = [c for c in strong if not c.urgent]

    weak_today = len(strong) < limits.min_strong_cards
    weak_yesterday = yesterday_strong is not None and yesterday_strong < limits.min_strong_cards
    if weak_today and weak_yesterday:
        # не забиваем дайджест слабыми; срочные (окно в днях) всё равно уходят
        digest.skipped = True
        regular = []
    digest.regular = regular[: limits.max_cards]
    return digest
