"""Метрики пилота с порогами «идём дальше» / «стоп». Считаются агрегированно по КМ."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .storage import Storage


@dataclass
class Metric:
    name: str
    value: float | None
    go: str
    stop: str
    verdict: str   # go | stop | gray | n/a

    def line(self) -> str:
        if self.value is None:
            shown = "—"
        elif self.name.startswith("Встречи"):
            shown = f"{self.value:.2f}"
        else:
            shown = f"{self.value:.0%}"
        mark = {"go": "✅", "stop": "⛔", "gray": "➖", "n/a": "·"}[self.verdict]
        return f"{mark} {self.name}: {shown}  (идём дальше: {self.go}; стоп: {self.stop})"


def _share(part: int, total: int) -> float | None:
    return part / total if total else None


def _verdict(value: float | None, go, stop) -> str:
    if value is None:
        return "n/a"
    if go(value):
        return "go"
    if stop(value):
        return "stop"
    return "gray"


def pilot_metrics(db: Storage, km_id: str | None = None) -> list[Metric]:
    cards = db.cards(km_id)
    total = len(cards)
    fb = [c["feedback"] for c in cards]
    answered = sum(1 for v in fb if v)
    # «связался» — от всех карточек; «уже знал» и «мимо» — от карточек с ответом
    contacted = _share(fb.count("contacted"), total)
    knew = _share(fb.count("knew"), answered)
    miss = _share(fb.count("miss"), answered)

    digests = [d for d in db.digests(km_id) if d["cards"] > 0]
    opened = _share(sum(1 for d in digests if d["opened_at"]), len(digests))

    wins = sum(1 for c in cards if c["outcome"] in ("meeting", "application"))
    kms = {c["km_id"] for c in cards} or ({km_id} if km_id else set())
    days = sorted({date.fromisoformat(c["created"]) for c in cards})
    months = max(1.0, ((days[-1] - days[0]).days + 1) / 30) if days else None
    per_km_month = wins / len(kms) / months if (kms and months) else None

    return [
        Metric("Доля «связался»", contacted, "≥ 30%", "< 10%",
               _verdict(contacted, lambda v: v >= 0.30, lambda v: v < 0.10)),
        Metric("Доля «уже знал»", knew, "< 50%", "> 70%",
               _verdict(knew, lambda v: v < 0.50, lambda v: v > 0.70)),
        Metric("Доля «мимо»", miss, "< 30%", "> 50%",
               _verdict(miss, lambda v: v < 0.30, lambda v: v > 0.50)),
        Metric("Открытие дайджеста", opened, "≥ 60% дней", "< 30%",
               _verdict(opened, lambda v: v >= 0.60, lambda v: v < 0.30)),
        Metric("Встречи / заявки на КМ в месяц", per_km_month, "≥ 1", "0 за пилот",
               _verdict(per_km_month, lambda v: v >= 1, lambda v: wins == 0)),
    ]


def report(db: Storage, km_id: str | None = None) -> str:
    cards = db.cards(km_id)
    lines = [f"Карточек: {len(cards)}, с ответом: {sum(1 for c in cards if c['feedback'])}"]
    lines += [m.line() for m in pilot_metrics(db, km_id)]
    lines.append("· Точность фактов: ручная проверка 50 карточек; любая выдуманная цифра — стоп.")
    return "\n".join(lines)
