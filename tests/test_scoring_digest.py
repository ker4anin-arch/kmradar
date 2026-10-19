from datetime import date, timedelta

import pytest

from kmradar.digest import build_cards, build_digest
from kmradar.models import Event, KMProfile
from kmradar.scoring import adjust_personal, freshness, score_event, size_factor

TODAY = date(2026, 10, 14)
KM = KMProfile("km", "КМ", ["69"], ["42"], products=["guarantee"])


def ev(inn, type_, days_ago=0, **facts):
    return Event(inn, type_, TODAY - timedelta(days=days_ago), facts, "src", id=hash((inn, type_)))


def test_freshness_is_linear_to_window_end():
    assert freshness(ev("1", "director_change"), TODAY) == 1.0
    assert freshness(ev("1", "director_change", 15), TODAY) == pytest.approx(0.5)
    assert freshness(ev("1", "director_change", 30), TODAY) == 0.0
    assert freshness(ev("1", "director_change", 40), TODAY) == 0.0


def test_size_factor_cap_and_floor():
    assert size_factor(ev("1", "tender_win", amount=500), 100) == 2.0
    assert size_factor(ev("1", "tender_win", amount=150), 100) == 1.5
    assert size_factor(ev("1", "tender_win", amount=1), 100) == 0.5
    assert size_factor(ev("1", "director_change"), 100) == 1.0


def test_score_formula():
    parts = score_event(ev("1", "tender_win", amount=100), KM, TODAY, 100)
    assert (parts.w, parts.f, parts.s, parts.p) == (1.0, 1.0, 1.0, 1.0)
    parts = score_event(ev("1", "director_change"), KM, TODAY, 100, {"director_change": 1.2})
    assert parts.total == pytest.approx(0.6 * 1.0 * 1.0 * 0.5 * 1.2)


def test_adjust_personal_clamped():
    assert adjust_personal(1.0, "useful") == 1.1
    assert adjust_personal(1.0, "miss") == 0.9
    assert adjust_personal(1.0, "knew") == 1.0
    assert adjust_personal(0.5, "miss") == 0.5
    assert adjust_personal(1.5, "useful") == 1.5


def test_events_of_one_company_merge_urgent_first():
    cards = build_cards(KM, [ev("1", "director_change"), ev("1", "tender_win", amount=100)],
                        TODAY, 100)
    assert len(cards) == 1
    assert [e.type for e in cards[0].events] == ["tender_win", "director_change"]
    assert cards[0].urgent and cards[0].deadline == TODAY + timedelta(days=7)


def test_stop_signal_suppresses_company_and_goes_to_cautions():
    stop = ev("1", "stop", reason="банкротство")
    d = build_digest(KM, [ev("1", "tender_win", amount=100), ev("2", "tender_win", amount=100)],
                     TODAY, median_contract=100, stop_events=[stop])
    assert [c.inn for c in d.cards] == ["2"]
    assert list(d.cautions) == ["1"]


def test_company_cooldown():
    d = build_digest(KM, [ev("1", "tender_win", amount=100)], TODAY, median_contract=100,
                     recent_card_inns={"1"})
    assert d.cards == []


def test_limits_7_regular_plus_3_urgent():
    events = [ev(str(i), "tender_win", amount=100) for i in range(5)]
    events += [ev(f"r{i}", "leasing") for i in range(10)]
    d = build_digest(KM, events, TODAY, median_contract=100)
    assert len(d.urgent) == 3
    assert len(d.regular) == 7
    assert d.strong_count == 15


def test_threshold_filters_weak_cards():
    # смена учредителей без совпадения продукта: 0.5 × 0.5 = 0.25; через 20 дней — ниже порога
    d = build_digest(KM, [ev("1", "founder_change", 20)], TODAY, median_contract=100)
    assert d.cards == [] and d.strong_count == 0


def test_skip_when_weak_two_days_but_keep_urgent():
    events = [ev("1", "tender_win", amount=100), ev("2", "leasing")]
    d = build_digest(KM, events, TODAY, median_contract=100, yesterday_strong=1)
    assert d.skipped
    assert [c.inn for c in d.cards] == ["1"]
    # вчера было достаточно — сегодня шлём всё
    d = build_digest(KM, events, TODAY, median_contract=100, yesterday_strong=5)
    assert not d.skipped and len(d.cards) == 2
    # вчера не считали (первый день) — тоже шлём
    d = build_digest(KM, events, TODAY, median_contract=100, yesterday_strong=None)
    assert not d.skipped


def test_profile_matching():
    from kmradar.models import Company
    km = KMProfile("k", "К", ["69"], ["42", "43.2"], revenue_min=100, revenue_max=1000)
    assert km.matches(Company("1", "А", "69", "42.11", revenue=500))
    assert km.matches(Company("1", "А", "69", "23.63", ["43.21"], revenue=500))
    assert not km.matches(Company("1", "А", "77", "42.11", revenue=500))
    assert not km.matches(Company("1", "А", "69", "46.21", revenue=500))
    assert not km.matches(Company("1", "А", "69", "42.11", revenue=5000))
    assert km.matches(Company("1", "А", "69", "42.11", revenue=None))
