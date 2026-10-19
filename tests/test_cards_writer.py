import json
from datetime import date

from kmradar.cards import describe, money, render
from kmradar.models import Card, Company, Event, KMProfile
from kmradar.writer import TEMPLATES, LLMWriter, validate

KM = KMProfile("km", "КМ", ["69"], [], products=["guarantee"])
TENDER = Event("1", "tender_win", date(2026, 10, 14),
               {"amount": 186_000_000, "subject": "Ремонт дороги", "number": "0136"}, "ЕИС", "u")
GOOD = {"why": "Скорее всего, нужна гарантия.", "phrase": "Поздравляю с победой! Обсудим гарантию?",
        "ask": "Как будете финансировать закупки?"}


def test_money():
    assert money(186_000_000) == "186 млн ₽"
    assert money(1_200_000_000) == "1,2 млрд ₽"
    assert money(10_000) == "10 тыс. ₽"
    assert money(500) == "500 ₽"


def test_describe_skips_empty_fields():
    e = Event("1", "tender_win", date(2026, 10, 14), {"amount": 50_000_000}, "ЕИС")
    assert describe(e) == "Победа в закупке, контракт 50 млн ₽ (протокол от 14.10.2026)."


def test_all_templates_pass_rules():
    for kind, texts in TEMPLATES.items():
        assert validate(texts) == [], kind


def test_validate_rules():
    assert validate(GOOD) == []
    assert any("цифры" in p for p in validate(dict(GOOD, why="Скорее всего, нужно 186 млн")))
    assert any("гипотеза" in p for p in validate(dict(GOOD, why="Им нужна гарантия.")))
    assert any("продукт" in p for p in validate(dict(GOOD, phrase="Предлагаем гарантию. Да?")))
    assert any("пусто" in p for p in validate(dict(GOOD, ask="")))


def test_llm_writer_accepts_valid_json():
    w = LLMWriter(lambda prompt: "Ответ:\n" + json.dumps(GOOD, ensure_ascii=False))
    assert w.write(TENDER, "факты", KM) == GOOD
    assert w.rejected == []


def test_llm_writer_falls_back_on_invented_numbers_and_errors():
    bad = dict(GOOD, why="Скорее всего, нужно 9 млн на гарантию")
    w = LLMWriter(lambda prompt: json.dumps(bad, ensure_ascii=False))
    assert w.write(TENDER, "факты", KM) == TEMPLATES["tender_win"]
    w2 = LLMWriter(lambda prompt: "не JSON")
    assert w2.write(TENDER, "факты", KM) == TEMPLATES["tender_win"]
    def boom(prompt):
        raise TimeoutError("timeout")
    w3 = LLMWriter(boom)
    assert w3.write(TENDER, "факты", KM) == TEMPLATES["tender_win"]
    assert len(w.rejected) == len(w2.rejected) == len(w3.rejected) == 1


def test_llm_prompt_has_no_facts_to_rewrite_except_given():
    seen = {}
    def capture(prompt):
        seen["p"] = prompt
        return json.dumps(GOOD, ensure_ascii=False)
    LLMWriter(capture).write(TENDER, "ФАКТЫ-БЛОК", KM)
    assert "ФАКТЫ-БЛОК" in seen["p"] and "Банковская гарантия" in seen["p"]


def test_render_card():
    company = Company("1", "ООО «Тест»", "69", "42.11", revenue=1_200_000_000, revenue_year=2024)
    card = Card("km", "1", [TENDER], 1.0, True, date(2026, 10, 21), date(2026, 10, 14))
    text = render(card, company, KM)
    assert text.startswith("🔴 Срочно · окно до 21.10")
    assert "выручка 2024: 1,2 млрд ₽" in text
    assert "контракт 186 млн ₽" in text and "ЕИС № 0136" in text
    assert "Источник: ЕИС: u" in text
    assert text.endswith("[Полезно] [Мимо] [Уже знал] [Связался]")
