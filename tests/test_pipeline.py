from datetime import date, datetime
from pathlib import Path

import pytest

from kmradar.bot import handle_callback
from kmradar.cli import DEMO_DIR, load_kms, main
from kmradar.delivery import FileDelivery
from kmradar.feedback import due_followups, record_feedback, record_outcome
from kmradar.metrics import pilot_metrics
from kmradar.pipeline import run_day, send_followups
from kmradar.sources import FixtureSource
from kmradar.storage import Storage


@pytest.fixture
def demo():
    db = Storage(":memory:")
    kms = load_kms(db, DEMO_DIR / "kms.json")
    db.save_km(kms[0].__class__(**{**kms[0].__dict__, "chat_id": "100"}))
    source = FixtureSource(DEMO_DIR / "days")
    results = {d: run_day(db, source, d) for d in source.days()}
    return db, results


def test_demo_flow(demo):
    db, res = demo
    d13 = res[date(2026, 10, 13)].digests["km-tver-1"]
    assert {c.inn for c in d13.cards} == {"0000000002", "0000000003", "0000000004"}
    # банкротство: победа в закупке подавлена, компания в «осторожно»
    assert list(d13.cautions) == ["0000000005"]

    d14 = res[date(2026, 10, 14)].digests["km-tver-1"]
    top = d14.cards[0]
    assert top.inn == "0000000001" and top.urgent
    assert top.text.startswith("🔴 Срочно")
    # МСП и учредители у «Торжокстроя» склеены в одну карточку
    merged = next(c for c in d14.cards if c.inn == "0000000007")
    assert {e.type for e in merged.events} == {"msp_upgrade", "founder_change"}
    # компания не по профилю (ОКВЭД 46) не попадает
    assert all(c.inn != "0000000006" for d in res.values()
               for dg in d.digests.values() for c in dg.cards)

    # 15.10: залог у Стройресурса, но карточка по нему была вчера — неделя тишины
    assert res[date(2026, 10, 15)].digests["km-tver-1"].cards == []
    # узкий профиль второго КМ: меньше 3 сильных два дня подряд — пропуск
    assert res[date(2026, 10, 13)].digests["km-tver-2"].skipped


def test_rerun_same_day_does_not_duplicate(demo):
    db, _ = demo
    before = len(db.cards())
    res = run_day(db, FixtureSource(DEMO_DIR / "days"), date(2026, 10, 14))
    assert res.new_events == 0
    assert len(db.cards()) == before


def test_feedback_followup_and_metrics(demo):
    db, _ = demo
    cards = db.cards("km-tver-1")
    at = datetime(2026, 10, 15, 9, 0)
    assert record_feedback(db, cards[0]["id"], "contacted", at).ok
    assert not record_feedback(db, cards[0]["id"], "bad").ok
    assert db.personal_weights("km-tver-1") == {"leasing": 1.0}

    assert due_followups(db, date(2026, 10, 21)) == []
    due = due_followups(db, date(2026, 10, 22))
    assert [f["card_id"] for f in due] == [cards[0]["id"]]

    class Rec:
        sent = []
        def send_followup(self, km, card_id, name):
            self.sent.append((km.km_id, card_id, name))
    rec = Rec()
    assert send_followups(db, date(2026, 10, 22), rec) == 1
    assert send_followups(db, date(2026, 10, 23), rec) == 0  # второй раз не спрашиваем

    assert record_outcome(db, cards[0]["id"], "meeting").ok
    m = {x.name: x for x in pilot_metrics(db, "km-tver-1")}
    assert m["Доля «связался»"].value == pytest.approx(1 / len(cards))
    assert m["Доля «уже знал»"].value == 0
    assert m["Открытие дайджеста"].value == pytest.approx(0.5)  # открыт 13.10 из 13.10 и 14.10


def test_miss_streak_asks_reason(demo):
    db, _ = demo
    card_ids = [c["id"] for c in db.cards("km-tver-1")]
    # три «мимо» подряд по одному типу события
    results = [record_feedback(db, card_ids[0], "miss") for _ in range(3)]
    assert [r.ask_reason_for for r in results] == [None, None, "leasing"]
    assert db.personal_weights("km-tver-1")["leasing"] == pytest.approx(0.7)


def test_bot_callback_only_for_owner(demo):
    db, _ = demo
    card_id = db.cards("km-tver-1")[0]["id"]
    assert handle_callback(db, f"fb:{card_id}:useful", "999")[0] == "Карточка не найдена"
    assert handle_callback(db, f"fb:{card_id}:useful", "100")[0] == "Записано: Полезно"
    assert handle_callback(db, f"out:{card_id}:meeting", "100")[0] == "Итог записан: Встреча"
    assert handle_callback(db, "garbage", "100")[0] == "Не понял кнопку"


def test_file_delivery_and_cli(tmp_path: Path, capsys):
    dbp = str(tmp_path / "r.db")
    main(["add-km", "--db", dbp, str(DEMO_DIR / "kms.json")])
    src = str(DEMO_DIR / "days")
    for d in ("2026-10-12", "2026-10-13", "2026-10-14"):
        main(["run", "--db", dbp, "--source", src, "--date", d, "--out", str(tmp_path / "out")])
    md = (tmp_path / "out" / "km-tver-1" / "2026-10-14.md").read_text(encoding="utf-8")
    assert "Северный Стройресурс" in md and "🔴 Срочно" in md
    main(["feedback", "--db", dbp, "1", "useful"])
    main(["metrics", "--db", dbp])
    assert "Доля «связался»" in capsys.readouterr().out
    assert isinstance(FileDelivery(tmp_path), FileDelivery)
