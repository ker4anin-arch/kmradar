from datetime import date, datetime

import pytest

from kmradar.cli import DEMO_DIR, load_kms, main
from kmradar.pipeline import run_day
from kmradar.sources import FixtureSource
from kmradar.storage import Storage
from kmradar.web import App, parse_card


@pytest.fixture
def site(tmp_path):
    path = str(tmp_path / "r.db")
    db = Storage(path)
    load_kms(db, DEMO_DIR / "kms.json")
    source = FixtureSource(DEMO_DIR / "days")
    for d in source.days():
        run_day(db, source, d)
    tokens = {k.km_id: db.new_link(k.km_id) for k in db.kms()}
    db.close()
    clock = {"today": date(2026, 10, 15), "now": datetime(2026, 10, 15, 8, 40)}
    app = App(path, today=lambda: clock["today"], now=lambda: clock["now"])
    return app, path, tokens, clock


def post(app, url, **form):
    body = "&".join(f"{k}={v}" for k, v in form.items()).encode()
    return app.handle("POST", url, body)


def test_unknown_token_and_paths_are_404(site):
    app, *_ = site
    assert app.handle("GET", "/").status == 200 and "по личной ссылке" in app.handle("GET", "/").body
    assert app.handle("GET", "/k").status == 404
    assert app.handle("GET", "/k/" + "x" * 32).status == 404
    assert app.handle("GET", "/healthz").body == "ok"


def test_digest_page_shows_latest_day_with_cards(site):
    app, path, tokens, _ = site
    r = app.handle("GET", f"/k/{tokens['km-tver-1']}")
    assert r.status == 200
    assert "Радар · 14.10" in r.body                 # 15.10 карточек не было
    assert "ООО «Северный Стройресурс»" in r.body and "Срочно" in r.body
    assert 'href="https://zakupki.gov.ru/"' in r.body
    r13 = app.handle("GET", f"/k/{tokens['km-tver-1']}?day=2026-10-13")
    assert "Осторожно" in r13.body and "Верхневолжский монтаж" in r13.body
    # открытие страницы считается открытием дайджеста
    db = Storage(path)
    opened = {d["day"]: d["opened_at"] for d in db.digests("km-tver-1")}
    assert opened["2026-10-14"] and opened["2026-10-13"]


def test_html_is_escaped(site):
    app, path, tokens, _ = site
    db = Storage(path)
    db.conn.execute("UPDATE card SET text = replace(text, 'Северный', '<script>x</script>') WHERE id = 4")
    db.commit()
    body = app.handle("GET", f"/k/{tokens['km-tver-1']}?day=2026-10-14").body
    assert "<script>x</script>" not in body and "&lt;script&gt;" in body


def test_feedback_outcome_flow(site):
    app, path, tokens, clock = site
    t = tokens["km-tver-1"]
    r = post(app, f"/k/{t}/fb", card=4, value="contacted", day="2026-10-14")
    assert r.status == 303 and r.headers["Location"].endswith("#c4")
    assert 'value="contacted" aria-pressed="true"' in app.handle("GET", f"/k/{t}?day=2026-10-14").body
    # через 7 дней появляется вопрос об итоге
    assert "Чем закончилось?" not in app.handle("GET", f"/k/{t}").body
    clock["today"] = date(2026, 10, 22)
    assert "Чем закончилось?" in app.handle("GET", f"/k/{t}").body
    r = post(app, f"/k/{t}/out", card=4, value="meeting", day="2026-10-14")
    assert r.status == 303 and "msg=outcome" in r.headers["Location"]
    assert Storage(path).card_row(4)["outcome"] == "meeting"
    assert "Чем закончилось?" not in app.handle("GET", f"/k/{t}").body


def test_cannot_touch_other_km_cards(site):
    app, path, tokens, _ = site
    r = post(app, f"/k/{tokens['km-tver-2']}/fb", card=4, value="miss")
    assert r.status == 404
    assert Storage(path).card_row(4)["feedback"] is None


def test_same_button_twice_counts_once(site):
    app, path, tokens, _ = site
    t = tokens["km-tver-1"]
    post(app, f"/k/{t}/fb", card=1, value="useful")
    post(app, f"/k/{t}/fb", card=1, value="useful")
    assert Storage(path).personal_weights("km-tver-1")["leasing"] == pytest.approx(1.1)


def test_miss_streak_asks_reason_on_page(site):
    app, path, tokens, _ = site
    t = tokens["km-tver-1"]
    db = Storage(path)
    for _ in range(2):  # две прошлых «мимо» по лизингу
        db.log_feedback(1, "km-tver-1", "leasing", "miss", datetime(2026, 10, 14))
    db.commit()
    r = post(app, f"/k/{t}/fb", card=1, value="miss", day="2026-10-13")
    assert "ask=leasing" in r.headers["Location"]
    page = app.handle("GET", r.headers["Location"].split("#")[0]).body
    assert "Третий раз «мимо»" in page
    post(app, f"/k/{t}/reason", type="leasing", text="слишком+мелкие", day="2026-10-13")
    assert [x["text"] for x in Storage(path).reasons("km-tver-1")] == ["слишком мелкие"]


def test_new_link_revokes_old(site, capsys):
    app, path, tokens, _ = site
    main(["link", "--db", path, "--base-url", "https://radar.example.ru/", "km-tver-1"])
    out = capsys.readouterr().out
    assert "https://radar.example.ru/k/" in out
    assert app.handle("GET", f"/k/{tokens['km-tver-1']}").status == 404
    new = out.strip().split("/k/")[1]
    assert app.handle("GET", f"/k/{new}").status == 200


def test_parse_card_roundtrip():
    text = ("🔴 Срочно · окно до 21.10\nООО «А» · ИНН 1 · регион 69\n\nЧто произошло\nФакт.\n\n"
            "Зачем звонить\nГипотеза.\n\nИсточник: ЕИС: https://x.ru/\n[Полезно]")
    c = parse_card(text)
    assert c["badge"] == "Срочно · окно до 21.10" and c["kind"] == "urgent"
    assert c["name"] == "ООО «А»" and c["meta"] == "ИНН 1 · регион 69"
    assert c["sections"] == [("Что произошло", "Факт."), ("Зачем звонить", "Гипотеза.")]
    assert c["source"] == "Источник: ЕИС: https://x.ru/"


def test_static_demo_page(site):
    app, *_ = site
    r = app.handle("GET", "/demo")
    assert r.status == 200 and "Утро КМ с радаром поводов" in r.body and "const DATA" in r.body
    assert "script-src 'unsafe-inline'" in r.headers["Content-Security-Policy"]
