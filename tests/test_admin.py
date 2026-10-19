from datetime import date, datetime
from urllib.parse import parse_qs, urlencode, urlsplit

from kmradar.admin import build_event, parse_amount
from kmradar.models import KMProfile
from kmradar.storage import Storage
from kmradar.web import App

DAY = date(2026, 10, 20)
TENDER = {"inn": "1424008710", "kind": "tender_win", "date": "2026-10-19", "amount": "186 млн",
          "subject": "Ремонт котельной", "customer": "МКУ «Служба ЖКХ»", "number": "0116300000126000001",
          "law": "44", "url": "https://zakupki.gov.ru/epz/order/notice/ea20/view/common-info.html?regNumber=1"}


def test_parse_amount():
    assert parse_amount("186 млн") == 186_000_000
    assert parse_amount("1,2 млрд") == 1_200_000_000
    assert parse_amount("186 000 000 ₽") == 186_000_000
    assert parse_amount("25 тыс") == 25_000
    assert parse_amount("") is None and parse_amount("много") is None


def test_build_event_validation():
    ev, err = build_event(TENDER, DAY)
    assert not err and ev.type == "tender_win" and ev.facts["amount"] == 186_000_000
    assert ev.facts["manual"] is True and ev.source == "ЕИС"
    assert "ИНН" in build_event({**TENDER, "inn": "1234567890"}, DAY)[1]
    assert "ссылку" in build_event({**TENDER, "url": ""}, DAY)[1]
    assert "сумму" in build_event({**TENDER, "amount": ""}, DAY)[1]
    assert "будущем" in build_event({**TENDER, "date": "2026-10-25"}, DAY)[1]
    ev, err = build_event({"inn": "1424008710", "kind": "director_change", "new": "Петров П. П.",
                           "url": "https://egrul.nalog.ru/"}, DAY)
    assert not err and ev.facts["new"] == "Петров П. П." and "old" not in ev.facts


def site(tmp_path, lookup=None):
    path = str(tmp_path / "r.db")
    db = Storage(path)
    db.save_km(KMProfile("tver", "КМ Тверь", ["69"], ["42"], products=["guarantee"]))
    db.save_km(KMProfile("yakut", "КМ Якутия", ["14"], ["35"], products=["guarantee"]))
    admin = db.new_link(Storage.ADMIN)
    km_token = db.new_link("yakut")
    db.close()
    app = App(path, today=lambda: DAY, now=lambda: datetime(2026, 10, 20, 9), lookup=lookup)
    return app, path, admin, km_token


def post(app, url, form):
    return app.handle("POST", url, urlencode(form, doseq=True).encode())


def msg_of(resp):
    return parse_qs(urlsplit(resp.headers["Location"]).query)["msg"][0]


def test_admin_link_required(tmp_path):
    app, path, admin, km_token = site(tmp_path)
    assert app.handle("GET", f"/a/{admin}").status == 200
    assert app.handle("GET", f"/a/{km_token}").status == 404            # ссылка КМ — не админка
    assert app.handle("GET", f"/k/{admin}").status == 404               # и наоборот
    assert post(app, f"/a/{'x' * 32}/event", TENDER).status == 404


def test_add_tender_goes_to_matching_km_with_checko_lookup(tmp_path):
    from test_checko import COMPANY
    from kmradar.checko import map_company
    calls = []

    def lookup(inn):
        calls.append(inn)
        return map_company(COMPANY)

    app, path, admin, km_token = site(tmp_path, lookup)
    r = post(app, f"/a/{admin}/event", TENDER)
    assert r.status == 303 and "Карточка у КМ: yakut" in msg_of(r)
    assert calls == ["1424008710"]
    page = app.handle("GET", f"/k/{km_token}").body
    assert "Срочно" in page and "контракт 186 млн ₽" in page and "Ремонт котельной" in page
    assert "Ремонт котельной" in app.handle("GET", r.headers["Location"]).body   # в «последних добавленных»


def test_add_without_checko_needs_name_and_checkbox_forces_km(tmp_path):
    app, path, admin, _ = site(tmp_path)
    r = post(app, f"/a/{admin}/event", TENDER)
    assert "Впишите название компании" in msg_of(r)
    # компания из Якутии, но аналитик явно отправляет её КМ Тверь
    r = post(app, f"/a/{admin}/event", {**TENDER, "company_name": "ООО «Теплострой»",
                                         "company_region": "14", "company_okved": "35.30.14", "km": ["tver"]})
    assert "Карточка у КМ: tver" in msg_of(r)
    db = Storage(path)
    assert len(db.cards_for_day("tver", DAY)) == 1 and db.cards_for_day("yakut", DAY)


def test_single_manual_card_is_not_skipped_as_weak_day(tmp_path):
    app, path, admin, _ = site(tmp_path)
    db = Storage(path)
    db.save_digest("tver", date(2026, 10, 19), 0, False, 0)          # вчера было пусто
    db.close()
    lease = {"inn": "1424008710", "kind": "leasing", "date": "2026-10-20", "subject": "Экскаватор",
             "url": "https://fedresurs.ru/x", "company_name": "ООО «Теплострой»", "company_region": "69",
             "company_okved": "42.11"}
    r = post(app, f"/a/{admin}/event", lease)
    assert "Карточка у КМ: tver" in msg_of(r)
