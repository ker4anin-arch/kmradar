"""Доставка дайджеста КМ. Единственная точка контакта с банком — сам КМ.

Каналы: консоль (демо), файлы Markdown (письмо / веб-страница), Telegram-бот.
Если внешний мессенджер запрещён ИБ — используется файловый/почтовый канал.
"""
from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

from .cards import ddmm
from .config import FEEDBACK_VALUES, OUTCOME_VALUES
from .models import Card, KMProfile


@dataclass
class Outgoing:
    km: KMProfile
    day: date
    intro: str
    cards: list[Card]
    cautions: list[str]


def intro_text(day: date, n: int, skipped: bool) -> str:
    if n == 0:
        return f"Радар · {ddmm(day)} · сегодня сильных поводов нет."
    tail = " (только срочные: остальные поводы слабые)" if skipped else ""
    return f"Радар · {ddmm(day)} · поводов: {n}{tail}"


def as_markdown(out: Outgoing) -> str:
    parts = [f"# {out.intro}", ""]
    parts += out.cautions + ([""] if out.cautions else [])
    for card in out.cards:
        parts += [f"## Карточка {card.id} · score {card.score:.2f}", "```", card.text, "```", ""]
    return "\n".join(parts)


class Delivery(Protocol):
    def send_digest(self, out: Outgoing) -> None: ...
    def send_followup(self, km: KMProfile, card_id: int, company_name: str) -> None: ...
    def send_text(self, km: KMProfile, text: str) -> None: ...


class ConsoleDelivery:
    def send_digest(self, out: Outgoing) -> None:
        print(f"\n===== {out.km.name} =====")
        print(as_markdown(out))

    def send_followup(self, km: KMProfile, card_id: int, company_name: str) -> None:
        print(f"[{km.name}] Карточка {card_id} ({company_name}): чем закончилось? "
              + " / ".join(OUTCOME_VALUES.values()))

    def send_text(self, km: KMProfile, text: str) -> None:
        print(f"[{km.name}] {text}")


class FileDelivery:
    """out/<km_id>/<дата>.md — для рассылки письмом или страницы с авторизацией."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, km: KMProfile, name: str) -> Path:
        p = self.root / km.km_id
        p.mkdir(parents=True, exist_ok=True)
        return p / name

    def send_digest(self, out: Outgoing) -> None:
        self._path(out.km, f"{out.day.isoformat()}.md").write_text(as_markdown(out), encoding="utf-8")

    def send_followup(self, km: KMProfile, card_id: int, company_name: str) -> None:
        with self._path(km, "followups.md").open("a", encoding="utf-8") as f:
            f.write(f"- Карточка {card_id} ({company_name}): чем закончилось?\n")

    def send_text(self, km: KMProfile, text: str) -> None:
        with self._path(km, "messages.md").open("a", encoding="utf-8") as f:
            f.write(text + "\n")


class TelegramDelivery:
    """Bot API через urllib, без сторонних библиотек. Кнопки шлют callback
    вида fb:<card_id>:<value> и out:<card_id>:<value>, их разбирает bot.py."""

    def __init__(self, token: str, api: str = "https://api.telegram.org"):
        self.base = f"{api}/bot{token}"

    def call(self, method: str, payload: dict) -> dict:
        req = urllib.request.Request(
            f"{self.base}/{method}", data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    def _send(self, chat_id: str, text: str, buttons: list[tuple[str, str]] | None = None) -> None:
        payload: dict = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [
                [{"text": label, "callback_data": data} for label, data in buttons[:2]],
                [{"text": label, "callback_data": data} for label, data in buttons[2:]],
            ]}
        self.call("sendMessage", payload)

    def send_digest(self, out: Outgoing) -> None:
        if not out.km.chat_id:
            return
        self._send(out.km.chat_id, "\n".join([out.intro, *out.cautions]))
        for card in out.cards:
            text = card.text.rsplit("\n", 1)[0]  # кнопки вместо строки [Полезно] [Мимо] …
            self._send(out.km.chat_id, text,
                       [(label, f"fb:{card.id}:{key}") for key, label in FEEDBACK_VALUES.items()])

    def send_followup(self, km: KMProfile, card_id: int, company_name: str) -> None:
        if km.chat_id:
            self._send(km.chat_id, f"По карточке «{company_name}» вы связались. Чем закончилось?",
                       [(label, f"out:{card_id}:{key}") for key, label in OUTCOME_VALUES.items()])

    def send_text(self, km: KMProfile, text: str) -> None:
        if km.chat_id:
            self._send(km.chat_id, text)
