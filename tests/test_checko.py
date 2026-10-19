"""Ответы Checko здесь повторяют структуру реальных ответов API (октябрь 2026)."""
import copy
import json
from datetime import date
from urllib.parse import parse_qs, urlsplit

import pytest

from kmradar.checko import (CheckoClient, CheckoError, CheckoSource, daily_requests,
                            map_company, map_contracts)
from kmradar.cards import render
from kmradar.diff import diff_snapshots, tender_events
from kmradar.digest import build_cards
from kmradar.models import KMProfile
from kmradar.pipeline import run_day
from kmradar.storage import Storage

DAY = date(2026, 10, 20)
OGRN = "1121419000185"

COMPANY = {"data": {
    "ОГРН": OGRN, "ИНН": "1424008710", "НаимСокр": 'ООО "ТЕПЛОСТРОЙ"',
    "Статус": {"Код": "001", "Наим": "Действует"},
    "Регион": {"Код": 14, "Наим": "Саха (Якутия), республика"},
    "ОКВЭД": {"Код": "35.30.14", "Наим": "Производство пара", "Версия": "2014"},
    "ОКВЭДДоп": [{"Код": "35.30.3", "Наим": "Распределение пара"}],
    "УстКап": {"Тип": "УСТАВНЫЙ КАПИТАЛ", "Сумма": 10000},
    "УпрОрг": {},
    "Руковод": [{"ФИО": "Яковлев Игнат Романович", "ИНН": "142400810530",
                 "НаимДолжн": "ГЕНЕРАЛЬНЫЙ ДИРЕКТОР", "ДатаЗаписи": "2015-02-16"}],
    "Учред": {"ФЛ": [{"ФИО": "Яковлев Владимир Иванович", "ИНН": "540700616161", "Доля": {}}],
              "РосОрг": [], "ИнОрг": [], "ПИФ": [], "РФ": []},
    "Подразд": {},
    "РМСП": {"Кат": "МИКРОПРЕДПРИЯТИЕ", "ДатаВкл": "2016-08-01"},
}, "meta": {"status": "ok", "today_request_count": 1, "balance": 0.0}}

FINANCES = {"data": {"2024": {"1230": 12597000, "2110": 35228000},
                     "2025": {"1230": 14534000, "2110": 29702000}},
            "meta": {"status": "ok"}}


def fed(*records):
    return {"data": {"ЗапВсего": len(records), "Записи": list(records)}, "meta": {"status": "ok"}}


def rec(guid, kind, when, publisher, title="", participants=()):
    return {"GUID": guid, "URL": f"https://fedresurs.ru/sfactmessages/{guid}", "Дата": when,
            "Тип": kind, "ТипНаим": title, "Публикатор": publisher,
            "Участники": list(participants)}


def test_map_company_fields():
    s = map_company(COMPANY, finances=FINANCES, today=DAY)
    assert s["inn"] == "1424008710" and s["region"] == "14"
    assert s["okved_main"] == "35.30.14" and s["okved_extra"] == ["35.30.3"]
    assert s["director"] == {"name": "Яковлев Игнат Романович", "position": "Генеральный директор"}
    assert [f["name"] for f in s["founders"]] == ["Яковлев Владимир Иванович"]
    assert s["capital"] == 10000 and s["msp_category"] == "micro" and s["status"] == "active"
    assert s["revenue"] == {"2024": 35228000, "2025": 29702000}
    assert s["receivables"]["2025"] == 14534000


def test_branch_region_from_kpp_and_liquidation_status():
    c = copy.deepcopy(COMPANY)
    c["data"]["Подразд"] = {"Филиал": [{"КПП": "780443001", "Адрес": "СПб"}], "Представ": []}
    c["data"]["Статус"] = {"Код": "101", "Наим": "Находится в стадии ликвидации"}
    s = map_company(c, today=DAY)
    assert s["branches"] == [{"region": "78", "address": "СПб"}]
    assert s["status"] == "liquidating"


def test_fedresurs_roles():
    f = fed(
        rec("L1", "FinancialLeaseContract", "2026-10-14", "1027700000001"),       # лизингодатель публикует
        rec("P1", "CreationRightOfPledge2", "2026-10-15", "1027700132195"),       # банк-залогодержатель
        rec("L2", "FinancialLeaseContract", "2026-10-15", OGRN),                  # компания сама лизингодатель
        rec("B1", "CreditorIntentionGoToCourt", "2026-10-16", "1027700132195",
            "Намерение кредитора обратиться в суд с заявлением о банкротстве"),
        rec("B2", "CreditorIntentionGoToCourt", "2026-10-16", OGRN, "Намерение кредитора"),  # она кредитор
        rec("B3", "DebtorIntentionGoToCourt", "2026-10-17", "1027700000002", "Намерение должника"),
        rec("OLD", "FinancialLeaseContract", "2026-01-10", "1027700000001"),      # старое
    )
    s = map_company(COMPANY, fedresurs=f, today=DAY)
    assert [x["id"] for x in s["leasing"]] == ["L1"]
    assert [x["id"] for x in s["pledges"]] == ["P1"]
    assert [x["id"] for x in s["bankruptcy_messages"]] == ["B1"]


def test_contracts_mapping_and_event_type():
    resp = {"data": {"Записи": [
        {"РегНомер": "2142400871026000011", "СтрЕИС": "https://zakupki.gov.ru/x", "Дата": "2026-10-13",
         "Цена": 25000000, "Заказ": {"НаимСокр": "МКУ «Служба ЖКХ»"},
         "Объекты": [{"Наим": "Ремонт котельной"}]},
        {"РегНомер": "OLD", "Дата": "2025-01-01", "Цена": 1},
    ]}, "meta": {"status": "ok"}}
    wins = map_contracts("1424008710", resp, "44", DAY)
    assert len(wins) == 1 and wins[0]["customer"] == "МКУ «Служба ЖКХ»"
    ev = tender_events(wins, DAY)
    assert ev[0].type == "contract_signed" and ev[0].facts["subject"] == "Ремонт котельной"


class FakeAPI:
    """Отвечает как Checko; можно менять ответы по ходу и ломать запросы."""

    def __init__(self):
        self.responses = {"company": COMPANY, "finances": FINANCES, "fedresurs": fed(),
                          "enforcements": {"data": {"ОстЗадолж": 0}, "meta": {"status": "ok"}},
                          "bankruptcy-messages": {"data": {"ЗапВсего": 0}, "meta": {"status": "ok"}},
                          "contracts": {"data": {"Записи": []}, "meta": {"status": "ok"}}}
        self.calls = []
        self.fail = set()

    def __call__(self, url):
        u = urlsplit(url)
        ep = u.path.rsplit("/", 1)[1]
        self.calls.append(ep)
        assert parse_qs(u.query)["key"] == ["secret"]
        if ep in self.fail:
            raise OSError(f"HTTP 503 for {url}")
        if ep == "contracts" and parse_qs(u.query).get("law") == ["223"]:
            return {"data": {"Записи": []}, "meta": {"status": "ok"}}
        return copy.deepcopy(self.responses[ep])


def test_client_hides_key_in_errors():
    api = FakeAPI(); api.fail.add("company")
    with pytest.raises(CheckoError) as e:
        CheckoClient("secret", api).get("company", inn="1")
    assert "secret" not in str(e.value) and "***" in str(e.value)
    with pytest.raises(CheckoError):
        CheckoClient("secret", lambda url: {"meta": {"status": "error", "message": "Лимит исчерпан"}}
                     ).get("company", inn="1")


def test_cache_cadence_and_fallback(tmp_path):
    api = FakeAPI()
    src = CheckoSource(CheckoClient("secret", api), ["1424008710"], tmp_path, log=lambda m: None)
    state, _ = src.snapshot("1424008710", DAY)
    assert state and api.calls.count("finances") == 1 and api.calls.count("contracts") == 2
    api.calls.clear()
    src.snapshot("1424008710", date(2026, 10, 21))
    # на следующий день: карточка, Федресурс, контракты — заново; отчётность и долги — из кеша
    assert sorted(api.calls) == ["company", "contracts", "contracts", "fedresurs"]
    # сбой API: берём вчерашний ответ, снимок не пропадает
    api.fail.add("company")
    state, _ = src.snapshot("1424008710", date(2026, 10, 22))
    assert state["name"] == 'ООО "ТЕПЛОСТРОЙ"'


def test_company_without_any_data_is_skipped(tmp_path):
    api = FakeAPI(); api.fail.add("company")
    src = CheckoSource(CheckoClient("secret", api), ["1424008710"], tmp_path, log=lambda m: None)
    assert src.snapshots(DAY) == []


def test_territory_file(tmp_path):
    f = tmp_path / "t.txt"
    f.write_text("# Якутия\n1424008710\n1424008710  # дубль\nмусор\n7707083893\n", encoding="utf-8")
    assert CheckoSource.read_territory(f) == ["1424008710", "7707083893"]


def test_end_to_end_with_pipeline(tmp_path):
    api = FakeAPI()
    db = Storage(str(tmp_path / "r.db"))
    db.save_km(KMProfile("km", "КМ Якутия", ["14"], ["35"], products=["leasing", "contract_loan"]))
    src = CheckoSource(CheckoClient("secret", api), ["1424008710"], tmp_path / "cache", log=lambda m: None)
    assert run_day(db, src, date(2026, 10, 19)).new_events == 0          # базовый снимок

    c = copy.deepcopy(COMPANY)
    c["data"]["Руковод"][0]["ФИО"] = "Петров Пётр Петрович"
    api.responses["company"] = c
    api.responses["fedresurs"] = fed(rec("L1", "FinancialLeaseContract", "2026-10-20", "1027700000001"))
    api.responses["contracts"] = {"data": {"Записи": [
        {"РегНомер": "N1", "Дата": "2026-10-20", "Цена": 25000000, "Заказ": {"НаимСокр": "МКУ"},
         "Объекты": [{"Наим": "Ремонт котельной"}], "СтрЕИС": "https://zakupki.gov.ru/x"}]},
        "meta": {"status": "ok"}}
    res = run_day(db, src, DAY)
    # одна компания в территории: слабый день два раза подряд — дайджест пропущен по правилам
    assert res.digests["km"].skipped and res.new_events == 3
    km = db.km("km")
    card = build_cards(km, db.events_since(date(2026, 10, 1), stop=False), DAY, 25000000)[0]
    card.text = render(card, db.companies()["1424008710"], km)
    assert {e.type for e in card.events} == {"director_change", "leasing", "contract_signed"}
    assert card.events[0].type == "contract_signed"
    assert "Заключён госконтракт «Ремонт котельной», цена 25 млн ₽" in card.text
    assert "Новый договор лизинга от 20.10.2026" in card.text
    # повторный прогон того же дня ничего не дублирует
    assert run_day(db, src, DAY).new_events == 0


def test_daily_request_plan():
    plan = daily_requests(100)
    assert plan["per_company"] == pytest.approx(4.32, abs=0.01)
    assert plan["total"] == 432


def test_real_shapes_round_trip_through_diff():
    prev = map_company(COMPANY, finances={"data": {"2024": {"2110": 20000000}}}, today=DAY)
    curr = map_company(COMPANY, finances={"data": {"2024": {"2110": 20000000}, "2025": {"2110": 30000000}}},
                       today=DAY)
    ev = diff_snapshots(prev, curr, DAY)
    assert [e.type for e in ev] == ["revenue_growth"] and ev[0].facts["growth_pct"] == 50
    json.dumps(curr, ensure_ascii=False)  # снимок сериализуется
