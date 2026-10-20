import copy
from datetime import date

from kmradar.diff import diff_snapshots, tender_events

TODAY = date(2026, 10, 14)


def base():
    return {
        "inn": "1", "name": "ООО «Тест»", "region": "69", "okved_main": "42.11",
        "okved_extra": [], "director": {"name": "А", "position": "Директор"},
        "founders": [{"name": "А", "share": 100}], "capital": 10_000, "branches": [],
        "msp_category": "small", "revenue": {"2023": 100, "2024": 120}, "receivables": {},
        "leasing": [], "pledges": [], "status": "active", "bankruptcy_messages": [],
        "enforcement_total": 0,
    }


def changed(**kw):
    s = copy.deepcopy(base())
    s.update(kw)
    return s


def types(events):
    return [e.type for e in events]


def test_first_snapshot_is_baseline():
    assert diff_snapshots(None, base(), TODAY) == []


def test_no_change_no_events():
    assert diff_snapshots(base(), base(), TODAY) == []


def test_director_change():
    ev = diff_snapshots(base(), changed(director={"name": "Б", "position": "Директор"}), TODAY)
    assert types(ev) == ["director_change"]
    assert ev[0].facts == {"old": "А", "new": "Б", "position": "Директор"}
    assert ev[0].event_date == TODAY


def test_founders_and_capital():
    ev = diff_snapshots(base(), changed(founders=[{"name": "Б"}], capital=50_000), TODAY)
    assert types(ev) == ["founder_change"]
    assert ev[0].facts == {"added": ["Б"], "removed": ["А"], "capital_old": 10_000,
                           "capital_new": 50_000}


def test_expansion():
    ev = diff_snapshots(base(), changed(okved_extra=["43.12"], branches=[{"region": "77"}]), TODAY)
    assert types(ev) == ["expansion"]
    assert ev[0].facts == {"new_okved": ["43.12"], "new_branch_regions": ["77"]}


def test_msp_upgrade_only_upwards_and_not_from_none():
    assert types(diff_snapshots(base(), changed(msp_category="medium"), TODAY)) == ["msp_upgrade"]
    assert diff_snapshots(base(), changed(msp_category="micro"), TODAY) == []
    assert diff_snapshots(changed(msp_category="none"), base(), TODAY) == []


def test_revenue_growth_threshold():
    ev = diff_snapshots(base(), changed(revenue={"2023": 100, "2024": 120, "2025": 200}), TODAY)
    assert types(ev) == ["revenue_growth"]
    assert ev[0].facts["growth_pct"] == 67
    # +25% — ниже порога 30%
    assert diff_snapshots(base(), changed(revenue={"2023": 100, "2024": 120, "2025": 150}),
                          TODAY) == []


def test_leasing_and_pledge_use_own_date():
    item = {"id": "L1", "date": "2026-10-10", "subject": "Тягач", "url": "u"}
    ev = diff_snapshots(base(), changed(leasing=[item], pledges=[dict(item, id="P1")]), TODAY)
    assert types(ev) == ["leasing", "leasing"]
    assert ev[0].event_date == date(2026, 10, 10)
    assert {e.facts["kind"] for e in ev} == {"лизинг", "залог"}
    # повторно тот же договор не даёт события
    assert diff_snapshots(changed(leasing=[item]), changed(leasing=[item]), TODAY) == []


def test_stop_signals():
    ev = diff_snapshots(base(), changed(status="liquidating",
                                        bankruptcy_messages=[{"id": "B"}],
                                        enforcement_total=10_000_000), TODAY)
    assert types(ev) == ["stop", "stop", "stop"]


def test_tender_events_filter_by_amount():
    wins = [{"inn": "1", "date": "2026-10-14", "amount": 186_000_000, "number": "N"},
            {"inn": "2", "date": "2026-10-14", "amount": 1_000_000}]
    ev = tender_events(wins, TODAY)
    assert [e.inn for e in ev] == ["1"]
    assert ev[0].facts == {"amount": 186_000_000, "number": "N"}
