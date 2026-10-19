"""Ночной пайплайн: снимки → разница → события → скоринг → карточки → дайджест к 08:30."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from .cards import render, render_caution
from .config import LIMITS, SIGNALS
from .delivery import Delivery, Outgoing, intro_text
from .diff import diff_snapshots, tender_events
from .digest import Digest, build_digest
from .feedback import due_followups
from .models import Company
from .scoring import territory_median
from .sources import Source
from .storage import Storage
from .writer import TemplateWriter, TextWriter

MAX_WINDOW = max(s.window_days for s in SIGNALS.values())
MEDIAN_LOOKBACK_DAYS = 90


@dataclass
class DayResult:
    day: date
    new_events: int
    digests: dict[str, Digest] = field(default_factory=dict)


def collect(db: Storage, source: Source, day: date) -> int:
    """Шаги 1–3: снять состояние, сравнить со вчерашним, записать события."""
    events = []
    for state in source.snapshots(day):
        db.save_company(Company.from_state(state))
        events += diff_snapshots(db.last_snapshot(state["inn"], day), state, day)
        db.save_snapshot(state["inn"], day, state)
    events += tender_events(source.tender_wins(day), day)
    events += source.manual_events(day)
    return db.save_events(events, day)


def make_digests(db: Storage, day: date, delivery: Delivery | None = None,
                 writer: TextWriter | None = None) -> dict[str, Digest]:
    """Шаги 4–7: скоринг, лимиты, карточки, доставка по каждому КМ."""
    writer = writer or TemplateWriter()
    companies = db.companies()
    stops_all = db.events_since(day - timedelta(days=LIMITS.stop_memory_days), stop=True)
    result = {}
    for km in db.kms():
        territory = {inn for inn, c in companies.items() if km.matches(c)}
        # события, которые этот КМ ещё не видел в карточках
        unseen = db.events_since(day - timedelta(days=MAX_WINDOW), stop=False,
                                 not_in_cards_of=km.km_id)
        candidates = [e for e in unseen if e.inn in territory and e.event_date <= day]
        stops = [e for e in stops_all if e.inn in territory and e.event_date <= day]
        median = territory_median(
            db.tender_amounts(day - timedelta(days=MEDIAN_LOOKBACK_DAYS), territory))
        digest = build_digest(
            km, candidates, day,
            median_contract=median,
            personal=db.personal_weights(km.km_id),
            stop_events=stops,
            recent_card_inns=db.recent_card_inns(
                km.km_id, day - timedelta(days=LIMITS.company_cooldown_days - 1)),
            yesterday_strong=db.strong_count(km.km_id, day - timedelta(days=1)),
        )
        for card in digest.cards:
            card.text = render(card, companies[card.inn], km, writer)
            db.save_card(card)
        db.save_digest(km.km_id, day, digest.strong_count, digest.skipped, len(digest.cards))
        if delivery:
            cautions = [render_caution(companies[inn], evs) for inn, evs in digest.cautions.items()]
            if digest.cards or cautions:
                delivery.send_digest(Outgoing(km, day, intro_text(day, len(digest.cards),
                                                                  digest.skipped),
                                              digest.cards, cautions))
        result[km.km_id] = digest
    return result


def send_followups(db: Storage, day: date, delivery: Delivery) -> int:
    """Шаг 8: через 7 дней после «связался» спросить про итог."""
    companies = db.companies()
    n = 0
    for f in due_followups(db, day):
        km = db.km(f["km_id"])
        if km:
            company = companies.get(f["inn"])
            delivery.send_followup(km, f["card_id"], company.name if company else f["inn"])
            db.mark_followup_sent(f["card_id"])
            n += 1
    return n


def run_day(db: Storage, source: Source, day: date, delivery: Delivery | None = None,
            writer: TextWriter | None = None) -> DayResult:
    new = collect(db, source, day)
    digests = make_digests(db, day, delivery, writer)
    if delivery:
        send_followups(db, day, delivery)
    return DayResult(day, new, digests)
