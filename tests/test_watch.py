import copy
from datetime import date, datetime

from kmradar.checko import PLANS, CheckoClient, CheckoSource
from kmradar.models import KMProfile
from kmradar.storage import Storage
from kmradar.watch import Refresher, parse_inns, valid_inn
from kmradar.web import App

from test_checko import COMPANY, FakeAPI, fed, rec

DAY = date(2026, 10, 20)


def test_inn_checksum():
    assert valid_inn("7707083893") and valid_inn("1424008710")
    assert not valid_inn("7707083894") and not valid_inn("123") and not valid_inn("770708389312")
    ok, bad = parse_inns("7707083893, 1424008710\n7707083893; 1234567890 abc 42")
    assert ok == ["7707083893", "1424008710"] and bad == ["1234567890", "42"]


def make_site(tmp_path, api):
    path = str(tmp_path / "r.db")
    db = Storage(path)
    # профиль КМ — Тверь, стройка; компания из Якутии в него не попадает, но её можно добавить вручную
    db.save_km(KMProfile("km1", "КМ 1", ["69"], ["42"], products=["contract_loan", "leasing"]))
    db.save_km(KMProfile("km2", "КМ 2", ["69"], ["42"]))
    tokens = {k: db.new_link(k) for k in ("km1", "km2")}
    db.close()
    refresher = Refresher(path, lambda: CheckoSource(CheckoClient("secret", api), [], tmp_path / "cache",
                                                     log=lambda m: None, plan=PLANS["paid"],
                                                     history_days=180),
                          today=lambda: DAY, background=False, log=lambda m: None)
    app = App(path, today=lambda: DAY, now=lambda: datetime(2026, 10, 20, 9, 0), refresher=refresher)
    return app, path, tokens


def post(app, url, **form):
    from urllib.parse import urlencode
    return app.handle("POST", url, urlencode(form).encode())


def test_add_company_shows_history_and_fresh_card(tmp_path):
    api = FakeAPI()
    c = copy.deepcopy(COMPANY)
    c["data"]["Руковод"][0]["ДатаЗаписи"] = "2026-07-01"        # 3,5 месяца назад — только история
    api.responses["company"] = c
    api.responses["contracts"] = {"data": {"Записи": [
        {"РегНомер": "N1", "Дата": "2026-10-15", "Цена": 25000000, "Заказ": {"НаимСокр": "МКУ"},
         "Объекты": [{"Наим": "Ремонт котельной"}], "СтрЕИС": "https://zakupki.gov.ru/x"},
        {"РегНомер": "N0", "Дата": "2026-06-01", "Цена": 15000000, "Заказ": {"НаимСокр": "МКУ"},
         "Объекты": [{"Наим": "Поставка угля"}]}]}, "meta": {"status": "ok"}}
    app, path, tokens = make_site(tmp_path, api)
    t = tokens["km1"]

    r = post(app, f"/k/{t}/watch", inns="1424008710, 1234567890", day="2026-10-20")
    assert r.status == 303 and "added=1" in r.headers["Location"] and "bad=1" in r.headers["Location"]
    page = app.handle("GET", f"/k/{t}").body
    assert "Только компании, которые ещё не клиенты банка" in page
    assert 'ООО &quot;ТЕПЛОСТРОЙ&quot;' in page
    # история за полгода: директор 01.07 и старый контракт 01.06
    assert "Новый руководитель (Генеральный директор): Яковлев Игнат Романович" in page
    assert "Поставка угля" in page
    # свежий контракт — карточка дайджеста, хотя компания не по профилю КМ
    db = Storage(path)
    cards = db.cards_for_day("km1", DAY)
    assert len(cards) == 1 and "Ремонт котельной" in cards[0]["text"]
    # другой КМ этих компаний не видит
    assert db.watch_list("km2") == [] and db.cards_for_day("km2", DAY) == []
    assert 'ТЕПЛОСТРОЙ' not in app.handle("GET", f"/k/{tokens['km2']}").body


def test_unwatch_and_limit(tmp_path):
    app, path, tokens = make_site(tmp_path, FakeAPI())
    t = tokens["km1"]
    post(app, f"/k/{t}/watch", inns="1424008710")
    post(app, f"/k/{t}/unwatch", inn="1424008710")
    assert Storage(path).watch_list("km1") == []
    db = Storage(path)
    many = [str(1000000000 + i) for i in range(30)]
    assert len(db.add_watch("km1", many, 20, datetime(2026, 10, 20))) == 20


def test_watch_without_checko_key_still_saves(tmp_path):
    path = str(tmp_path / "r.db")
    db = Storage(path)
    db.save_km(KMProfile("km1", "КМ 1", ["69"], []))
    t = db.new_link("km1")
    app = App(path, today=lambda: DAY)        # без refresher: данные придут утром
    post(app, f"/k/{t}/watch", inns="1424008710")
    page = app.handle("GET", f"/k/{t}").body
    assert "ИНН 1424008710" in page and "данные загружаются" in page


def test_nightly_run_includes_watched(tmp_path, monkeypatch, capsys):
    from kmradar import cli
    path = str(tmp_path / "r.db")
    db = Storage(path)
    db.save_km(KMProfile("km1", "КМ 1", ["69"], []))
    db.add_watch("km1", ["1424008710"], 20, datetime(2026, 10, 20))
    seen = {}

    class FakeSource:
        territory = []
        def snapshots(self, day):
            seen["territory"] = list(self.territory); return []
        def tender_wins(self, day): return []
        def manual_events(self, day): return []

    monkeypatch.setattr(cli, "_checko", lambda args: FakeSource())
    cli.main(["run", "--db", path, "--territory", str(tmp_path / "nofile.txt"), "--date", "2026-10-20"])
    assert seen["territory"] == ["1424008710"]
