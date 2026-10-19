"""Модели данных. В системе только открытые данные о компаниях и фильтр КМ.

Никаких признаков «клиент банка» здесь нет и быть не должно.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass
class KMProfile:
    """Фильтр территории КМ. Списков компаний и портфеля здесь нет."""
    km_id: str
    name: str
    regions: list[str]                 # коды регионов, например "69"
    okved: list[str]                   # префиксы ОКВЭД, например "42" или "42.11"
    revenue_min: int = 0
    revenue_max: int | None = None
    products: list[str] = field(default_factory=list)
    chat_id: str | None = None         # канал доставки (Telegram chat id / e-mail)

    def matches(self, company: "Company") -> bool:
        if company.region not in self.regions:
            return False
        codes = [company.okved, *company.okved_extra]
        if self.okved and not any(c and c.startswith(p) for c in codes for p in self.okved):
            return False
        if company.revenue is not None:
            if company.revenue < self.revenue_min:
                return False
            if self.revenue_max is not None and company.revenue > self.revenue_max:
                return False
        return True


@dataclass
class Company:
    inn: str
    name: str
    region: str
    okved: str
    okved_extra: list[str] = field(default_factory=list)
    revenue: int | None = None
    revenue_year: int | None = None

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> "Company":
        revenue = state.get("revenue") or {}
        year = max((int(y) for y in revenue), default=None)
        return cls(
            inn=state["inn"],
            name=state.get("name", state["inn"]),
            region=str(state.get("region", "")),
            okved=state.get("okved_main", ""),
            okved_extra=list(state.get("okved_extra", [])),
            revenue=revenue.get(str(year)) if year else None,
            revenue_year=year,
        )


@dataclass
class Event:
    inn: str
    type: str
    event_date: date
    facts: dict[str, Any]
    source: str
    url: str | None = None
    id: int | None = None


@dataclass
class Card:
    km_id: str
    inn: str
    events: list[Event]
    score: float
    urgent: bool
    deadline: date
    created: date
    text: str = ""
    id: int | None = None
    feedback: str | None = None
    outcome: str | None = None
