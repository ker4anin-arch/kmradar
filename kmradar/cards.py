"""Карточка повода: что произошло, зачем звонить, что сказать.

Блок «Что произошло» собирается шаблоном из фактов. Пустое поле — нет строки.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from .config import FEEDBACK_VALUES, SIGNALS
from .models import Card, Company, Event, KMProfile
from .writer import TemplateWriter, TextWriter


def money(value: int | float | None) -> str:
    if value is None:
        return ""
    v = float(value)
    for limit, unit in ((1e9, "млрд"), (1e6, "млн"), (1e3, "тыс.")):
        if abs(v) >= limit:
            num = v / limit
            text = f"{num:.1f}".rstrip("0").rstrip(".").replace(".", ",")
            return f"{text} {unit} ₽"
    return f"{int(v)} ₽"


def ddmm(d: date) -> str:
    return d.strftime("%d.%m")


def ddmmyyyy(d: date) -> str:
    return d.strftime("%d.%m.%Y")


def describe(event: Event) -> str:
    """Одна строка фактов по событию. Только данные, без интерпретации."""
    f: dict[str, Any] = event.facts
    when = ddmmyyyy(event.event_date)
    t = event.type
    if t == "tender_win":
        parts = ["Победа в закупке"]
        if f.get("subject"):
            parts.append(f"«{f['subject']}»")
        line = " ".join(parts)
        if f.get("amount"):
            line += f", контракт {money(f['amount'])}"
        if f.get("customer"):
            line += f", заказчик — {f['customer']}"
        details = [f"протокол от {when}"]
        if f.get("law"):
            details.append(f"{f['law']}-ФЗ")
        if f.get("number"):
            details.append(f"ЕИС № {f['number']}")
        return f"{line} ({', '.join(details)})."
    if t == "contract_signed":
        line = "Заключён госконтракт"
        if f.get("subject"):
            line += f" «{f['subject']}»"
        if f.get("amount"):
            line += f", цена {money(f['amount'])}"
        if f.get("customer"):
            line += f", заказчик — {f['customer']}"
        details = [f"дата {when}"]
        if f.get("law"):
            details.append(f"{f['law']}-ФЗ")
        if f.get("number"):
            details.append(f"реестровый № {f['number']}")
        return f"{line} ({', '.join(details)})."
    if t == "leasing":
        line = f"Новый {'договор лизинга' if f.get('kind', 'лизинг') == 'лизинг' else 'залог имущества'} от {when}"
        if f.get("subject"):
            line += f": {f['subject']}"
        if f.get("counterparty"):
            line += f" ({f['counterparty']})"
        return line + "."
    if t == "director_change":
        if not f.get("old"):
            return (f"Новый руководитель ({f.get('position', 'руководитель')}): "
                    f"{f['new']}, запись в ЕГРЮЛ от {when}.")
        return (f"Сменился руководитель ({f.get('position', 'руководитель')}): "
                f"{f['old']} → {f['new']}, запись от {when}.")
    if t == "founder_change":
        bits = []
        if f.get("added"):
            bits.append("новые учредители: " + ", ".join(f["added"]))
        if f.get("removed"):
            bits.append("вышли: " + ", ".join(f["removed"]))
        if f.get("capital_new"):
            bits.append(f"уставный капитал {money(f['capital_old'])} → {money(f['capital_new'])}")
        return f"Изменения в собственности ({when}): " + "; ".join(bits) + "."
    if t == "expansion":
        bits = []
        if f.get("new_okved"):
            bits.append("новые ОКВЭД " + ", ".join(f["new_okved"]))
        if f.get("new_branch_regions"):
            bits.append("филиал в регионе " + ", ".join(f["new_branch_regions"]))
        if f.get("region_new"):
            bits.append(f"смена региона {f['region_old']} → {f['region_new']}")
        return f"Расширение ({when}): " + "; ".join(bits) + "."
    if t == "msp_upgrade":
        return f"В реестре МСП категория сменилась: {f['old']} → {f['new']} ({when})."
    if t == "revenue_growth":
        line = (f"Выручка {f['year']}: {money(f['revenue'])} против {money(f['revenue_prev'])} "
                f"годом ранее (+{f['growth_pct']}%)")
        if f.get("receivables"):
            line += f"; дебиторка {money(f['receivables_prev'])} → {money(f['receivables'])}"
        return line + "."
    if t == "news_mention":
        return f"Новость ({f.get('source', 'СМИ')}, {when}): «{f.get('title', '')}»."
    if t == "risk":
        return f"{f.get('reason', 'намерение кредитора о банкротстве').capitalize()} ({when})."
    if t == "stop" and f.get("status") == "current":
        return f"Статус в ЕГРЮЛ: {f.get('reason', 'ликвидация')} (на {when})."
    if t == "stop" and f.get("status") == "changed":
        return f"Статус в ЕГРЮЛ сменился: {f.get('reason', 'ликвидация')} (замечено {when})."
    if t == "stop":
        line = f"{f.get('reason', 'стоп-сигнал').capitalize()} ({when})"
        if f.get("amount"):
            line += f", сумма {money(f['amount'])}"
        return line + "."
    return f"{SIGNALS[t].title} ({when})."


def facts_block(events: list[Event]) -> str:
    return "\n".join(describe(e) for e in events)


def sources_block(events: list[Event]) -> str:
    seen, lines = set(), []
    for e in events:
        key = (e.source, e.url)
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"{e.source}: {e.url}" if e.url else e.source)
    return "Источник: " + "; ".join(lines)


def header(card: Card) -> str:
    if card.urgent:
        return f"🔴 Срочно · окно до {ddmm(card.deadline)}"
    days = (card.deadline - card.created).days
    if days <= 31:
        return f"🟡 В течение месяца · до {ddmm(card.deadline)}"
    return f"🟢 Без спешки · до {ddmm(card.deadline)}"


def company_line(c: Company) -> str:
    parts = [c.name, f"ИНН {c.inn}"]
    if c.region:
        parts.append(f"регион {c.region}")
    if c.okved:
        parts.append(f"ОКВЭД {c.okved}")
    if c.revenue:
        parts.append(f"выручка {c.revenue_year}: {money(c.revenue)}")
    return " · ".join(parts)


def render(card: Card, company: Company, km: KMProfile, writer: TextWriter | None = None) -> str:
    writer = writer or TemplateWriter()
    facts = facts_block(card.events)
    texts = writer.write(card.events[0], facts, km)
    buttons = " ".join(f"[{v}]" for v in FEEDBACK_VALUES.values())
    return "\n".join([
        header(card),
        company_line(company),
        "",
        "Что произошло",
        facts,
        "",
        "Зачем звонить",
        texts["why"],
        "",
        "Первая фраза",
        f"«{texts['phrase']}»",
        "",
        "Спросить",
        texts["ask"],
        "",
        sources_block(card.events),
        buttons,
    ])


def render_caution(company: Company, events: list[Event]) -> str:
    return f"⚠️ Осторожно: {company.name} (ИНН {company.inn}) — " + " ".join(
        describe(e) for e in events)
