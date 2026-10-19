"""Снимок сегодня минус снимок вчера = события.

Формат снимка (нормализованный, одинаковый для любого источника):

    {
      "inn": "...", "name": "...", "region": "69",
      "okved_main": "42.11", "okved_extra": ["43.12"],
      "director": {"name": "...", "position": "Генеральный директор"},
      "founders": [{"name": "...", "share": 50}],
      "capital": 10000,
      "branches": [{"region": "77", "address": "..."}],
      "msp_category": "small",            # micro | small | medium | none
      "revenue": {"2023": 900000000, "2024": 1200000000},
      "receivables": {"2023": ..., "2024": ...},
      "leasing": [{"id": "...", "date": "2026-10-10", "subject": "...", "url": "..."}],
      "pledges": [{"id": "...", "date": "...", "subject": "...", "url": "..."}],
      "status": "active",                 # active | liquidating | bankrupt
      "bankruptcy_messages": [{"id": "...", "date": "...", "kind": "...", "url": "..."}],
      "enforcement_total": 0,
      "efrsb_count": 0                    # число сообщений ЕФРСБ о банкротстве
    }

Факты в событии берутся из данных как есть — LLM их потом не трогает.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from .config import DIFF, DiffConfig
from .models import Event

MSP_RANK = {"none": 0, "micro": 1, "small": 2, "medium": 3}
MSP_TITLE = {"micro": "микро", "small": "малое", "medium": "среднее", "none": "не МСП"}


def _parse_date(value: str | None, default: date | None) -> date | None:
    if not value:
        return default
    try:
        return date.fromisoformat(value)
    except ValueError:
        return default


def diff_snapshots(prev: dict[str, Any] | None, curr: dict[str, Any], today: date,
                   cfg: DiffConfig = DIFF) -> list[Event]:
    """Первый снимок — база: событий из сравнения нет, но поднимаются свежие
    датированные события из самого снимка (см. bootstrap_events)."""
    if prev is None:
        return bootstrap_events(curr, today)
    inn = curr["inn"]
    events: list[Event] = []

    def add(kind: str, facts: dict[str, Any], source: str, url: str | None = None,
            when: date | None = None) -> None:
        events.append(Event(inn, kind, when or today, facts, source, url))

    # 3. Смена руководителя
    old_dir = (prev.get("director") or {}).get("name")
    new_dir = (curr.get("director") or {}).get("name")
    if old_dir and new_dir and old_dir != new_dir:
        add("director_change", {
            "old": old_dir, "new": new_dir,
            "position": (curr.get("director") or {}).get("position", "руководитель"),
        }, "ЕГРЮЛ")

    # 4. Учредители и уставный капитал
    old_f = {f["name"] for f in prev.get("founders", [])}
    new_f = {f["name"] for f in curr.get("founders", [])}
    cap_old, cap_new = prev.get("capital"), curr.get("capital")
    facts: dict[str, Any] = {}
    if new_f - old_f:
        facts["added"] = sorted(new_f - old_f)
    if old_f - new_f:
        facts["removed"] = sorted(old_f - new_f)
    if cap_old is not None and cap_new is not None and cap_new > cap_old:
        facts["capital_old"], facts["capital_new"] = cap_old, cap_new
    if facts:
        add("founder_change", facts, "ЕГРЮЛ")

    # 5. Расширение: новые ОКВЭД, филиалы в новых регионах, смена региона
    facts = {}
    new_okved = sorted(set(curr.get("okved_extra", [])) - set(prev.get("okved_extra", [])))
    if new_okved:
        facts["new_okved"] = new_okved
    old_br = {b.get("region") for b in prev.get("branches", [])}
    new_br = sorted({b.get("region") for b in curr.get("branches", [])} - old_br - {None})
    if new_br:
        facts["new_branch_regions"] = new_br
    if prev.get("region") and curr.get("region") and prev["region"] != curr["region"]:
        facts["region_old"], facts["region_new"] = prev["region"], curr["region"]
    if facts:
        add("expansion", facts, "ЕГРЮЛ")

    # 6. Категория МСП выросла
    old_c, new_c = prev.get("msp_category", "none"), curr.get("msp_category", "none")
    if MSP_RANK.get(new_c, 0) > MSP_RANK.get(old_c, 0) and old_c != "none":
        add("msp_upgrade", {"old": MSP_TITLE[old_c], "new": MSP_TITLE[new_c]}, "Реестр МСП")

    # 7. Появилась новая отчётность с ростом выручки > N%
    def had(source: str) -> bool:
        """Источник был и вчера, и сегодня (для снимков Checko; у остальных — всегда да)."""
        return all(source in s.get("_sources", [source]) for s in (prev, curr))

    rev_old, rev_new = prev.get("revenue", {}), curr.get("revenue", {})
    for year in (sorted(set(rev_new) - set(rev_old)) if rev_old and had("finances") else []):
        last, before = rev_new.get(year), rev_new.get(str(int(year) - 1))
        if last and before and before > 0:
            growth = (last - before) / before * 100
            if growth > cfg.revenue_growth_pct:
                facts = {"year": int(year), "revenue": last, "revenue_prev": before,
                         "growth_pct": round(growth)}
                rec = curr.get("receivables", {})
                if rec.get(year) and rec.get(str(int(year) - 1)):
                    facts["receivables"] = rec[year]
                    facts["receivables_prev"] = rec[str(int(year) - 1)]
                add("revenue_growth", facts, "ГИР БО")

    # 2. Новые договоры лизинга / залоги
    for key, label in (("leasing", "лизинг"), ("pledges", "залог")):
        seen = {x["id"] for x in prev.get(key, [])}
        for item in curr.get(key, []):
            if item["id"] not in seen:
                add("leasing", {"kind": label, "subject": item.get("subject"),
                                "counterparty": item.get("counterparty")},
                    "Федресурс", item.get("url"), _parse_date(item.get("date"), today))

    # 8. Стоп-сигналы
    status_old, status_new = prev.get("status", "active"), curr.get("status", "active")
    if status_new != status_old and status_new in ("liquidating", "bankrupt"):
        add("stop", {"reason": "ликвидация" if status_new == "liquidating" else "банкротство"},
            "ЕГРЮЛ")
    seen = {m["id"] for m in prev.get("bankruptcy_messages", [])}
    for msg in curr.get("bankruptcy_messages", []):
        if msg["id"] not in seen:
            add("stop", {"reason": msg.get("kind", "сообщение о банкротстве")},
                "Федресурс", msg.get("url"), _parse_date(msg.get("date"), today))
    efrsb_old, efrsb_new = prev.get("efrsb_count", 0) or 0, curr.get("efrsb_count", 0) or 0
    if efrsb_new > efrsb_old and had("bankruptcy-messages"):
        add("stop", {"reason": "новые сообщения о банкротстве в ЕФРСБ"}, "ЕФРСБ")
    enf_old, enf_new = prev.get("enforcement_total", 0), curr.get("enforcement_total", 0)
    if enf_new >= cfg.enforcement_stop_amount > enf_old and had("enforcements"):
        add("stop", {"reason": "крупные исполнительные производства",
                     "amount": enf_new}, "ФССП")
    return events


def bootstrap_events(state: dict[str, Any], today: date) -> list[Event]:
    """События из первого снимка компании — только то, у чего в данных есть дата
    и она попадает в окно сигнала. Так радар показывает свежие поводы с первого
    дня, а не через неделю. Снимки без дат (файлы аналитика) событий не дают."""
    from .config import SIGNALS
    inn = state["inn"]
    events: list[Event] = []

    def fresh(value: Any, kind: str) -> date | None:
        d = _parse_date(value, None) if isinstance(value, str) else None
        if d and 0 <= (today - d).days < SIGNALS[kind].window_days:
            return d
        return None

    director = state.get("director") or {}
    when = fresh(director.get("since"), "director_change")
    if when and director.get("name"):
        events.append(Event(inn, "director_change", when,
                            {"new": director["name"],
                             "position": director.get("position") or "руководитель"}, "ЕГРЮЛ"))

    new_founders = [(fresh(f.get("since"), "founder_change"), f["name"])
                    for f in state.get("founders", []) if f.get("name")]
    new_founders = [(d, n) for d, n in new_founders if d]
    if new_founders:
        events.append(Event(inn, "founder_change", max(d for d, _ in new_founders),
                            {"added": sorted(n for _, n in new_founders)}, "ЕГРЮЛ"))

    for key, label in (("leasing", "лизинг"), ("pledges", "залог")):
        for item in state.get(key, []):
            when = fresh(item.get("date"), "leasing")
            if when:
                events.append(Event(inn, "leasing", when, {"kind": label, "subject": item.get("subject"),
                                                           "counterparty": item.get("counterparty")},
                                    "Федресурс", item.get("url")))

    for msg in state.get("bankruptcy_messages", []):
        when = fresh(msg.get("date"), "stop")
        if when:
            events.append(Event(inn, "stop", when, {"reason": msg.get("kind", "сообщение о банкротстве")},
                                "Федресурс", msg.get("url")))
    if state.get("status") in ("liquidating", "bankrupt"):
        events.append(Event(inn, "stop", today, {
            "reason": "ликвидация" if state["status"] == "liquidating" else "банкротство"}, "ЕГРЮЛ"))
    return events


def tender_events(wins: list[dict[str, Any]], today: date, cfg: DiffConfig = DIFF) -> list[Event]:
    """Протоколы итогов ЕИС → «победа в закупке»; заключённые контракты → «заключён госконтракт».

    Формат: {"inn", "date", "amount", "subject", "customer", "number", "law", "url",
             "stage": "protocol" | "contract"}
    """
    events = []
    for w in wins:
        if (w.get("amount") or 0) < cfg.min_contract_amount:
            continue
        facts = {k: w[k] for k in ("amount", "subject", "customer", "number", "law") if w.get(k)}
        kind = "contract_signed" if w.get("stage") == "contract" else "tender_win"
        events.append(Event(w["inn"], kind, _parse_date(w.get("date"), today),
                            facts, "ЕИС", w.get("url")))
    return events
