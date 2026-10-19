"""Источники данных. Каждый отдаёт данные в нормализованном виде (см. diff.py).

FixtureSource — файлы JSON по дням. Подходит для демо, тестов и для фазы
Wizard of Oz: аналитик кладёт найденные руками события в `events`, а
радар делает из них карточки по тем же правилам.

Адаптеры к реальным источникам (Checko / Контур.Фокус для ЕГРЮЛ, Федресурса,
МСП, ГИР БО; ЕИС с индивидуальным токеном или коммерческий API закупок)
реализуют тот же протокол Source.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from .config import SIGNALS
from .models import Event


class Source(Protocol):
    def snapshots(self, day: date) -> list[dict[str, Any]]:
        """Состояние компаний территории на день (ЕГРЮЛ, МСП, отчётность, Федресурс, ФССП)."""

    def tender_wins(self, day: date) -> list[dict[str, Any]]:
        """Протоколы итогов закупок, опубликованные в этот день."""

    def manual_events(self, day: date) -> list[Event]:
        """События, найденные аналитиком руками (Wizard of Oz)."""


class FixtureSource:
    """Каталог с файлами YYYY-MM-DD.json:

        {"snapshots": [...], "tenders": [...], "events": [
            {"inn": "...", "type": "tender_win", "date": "2026-10-14",
             "facts": {...}, "source": "ЕИС", "url": "..."}
        ]}
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _load(self, day: date) -> dict[str, Any]:
        f = self.path / f"{day.isoformat()}.json"
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}

    def days(self) -> list[date]:
        return sorted(date.fromisoformat(p.stem) for p in self.path.glob("*.json"))

    def snapshots(self, day: date) -> list[dict[str, Any]]:
        return self._load(day).get("snapshots", [])

    def tender_wins(self, day: date) -> list[dict[str, Any]]:
        return self._load(day).get("tenders", [])

    def manual_events(self, day: date) -> list[Event]:
        out = []
        for e in self._load(day).get("events", []):
            if e["type"] not in SIGNALS:
                raise ValueError(f"Неизвестный тип события: {e['type']}")
            out.append(Event(e["inn"], e["type"], date.fromisoformat(e.get("date", day.isoformat())),
                             e.get("facts", {}), e.get("source", SIGNALS[e["type"]].source),
                             e.get("url")))
        return out
