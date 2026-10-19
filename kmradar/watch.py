"""«Мои компании»: холодные компании, которые КМ добавляет сам.

Только компании, которые ещё НЕ клиенты банка: ввод ИНН клиента во внешний
сервис раскрыл бы факт обслуживания. Радар не спрашивает и не хранит, клиент
компания или нет.

После добавления данные подтягиваются сразу (в фоне) с историей за полгода,
чтобы КМ видел, что происходило с компанией, а не ждал утра.
"""
from __future__ import annotations

import re
import threading
from datetime import date
from typing import Callable

from .diff import bootstrap_events, diff_snapshots, tender_events
from .models import Company
from .pipeline import make_digests
from .storage import Storage

WATCH_LIMIT = 20       # компаний на одного КМ
HISTORY_DAYS = 180     # глубина истории при добавлении

_W = (2, 4, 10, 3, 5, 9, 4, 6, 8)


def valid_inn(inn: str) -> bool:
    """ИНН юрлица: 10 цифр с верной контрольной цифрой."""
    if not re.fullmatch(r"\d{10}", inn):
        return False
    return sum(int(d) * w for d, w in zip(inn, _W)) % 11 % 10 == int(inn[9])


def parse_inns(text: str) -> tuple[list[str], list[str]]:
    """Текст из поля ввода → (верные ИНН, отклонённые строки)."""
    ok, bad = [], []
    for token in re.findall(r"\d+", text or ""):
        if valid_inn(token):
            if token not in ok:
                ok.append(token)
        else:
            bad.append(token)
    return ok, bad


def refresh(db: Storage, source, inns: list[str], day: date, km_id: str | None = None) -> int:
    """Скачать компании и записать события. source — CheckoSource в режиме истории.

    Компания, которой ещё не было в базе, получает события за HISTORY_DAYS дней
    из датированных фактов; уже известная — обычное сравнение со вчерашним снимком.
    """
    events = []
    source.territory = inns
    for state in source.snapshots(day):
        db.save_company(Company.from_state(state))
        if db.has_snapshot(state["inn"]):
            events += diff_snapshots(db.last_snapshot(state["inn"], day), state, day)
        else:
            events += bootstrap_events(state, day, HISTORY_DAYS)
        db.save_snapshot(state["inn"], day, state)
    events += tender_events(source.tender_wins(day), day)
    added = db.save_events(events, day)
    make_digests(db, day, km_ids={km_id} if km_id else None)
    return added


class Refresher:
    """Запускает refresh в фоне, по одной задаче за раз (лимит Checko общий)."""

    def __init__(self, db_path: str, make_source: Callable[[], object],
                 today: Callable[[], date] = date.today, background: bool = True,
                 log: Callable[[str], None] = print):
        self.db_path = db_path
        self.make_source = make_source
        self.today = today
        self.background = background
        self.log = log
        self.lock = threading.Lock()

    def _run(self, km_id: str, inns: list[str]) -> None:
        with self.lock:
            db = Storage(self.db_path)
            try:
                n = refresh(db, self.make_source(), inns, self.today(), km_id)
                self.log(f"мои компании: {km_id} +{len(inns)} компаний, событий {n}")
            except Exception as exc:  # noqa: BLE001 — фоновая задача не должна ронять сайт
                self.log(f"мои компании: ошибка обновления: {exc}")
            finally:
                db.close()

    def __call__(self, km_id: str, inns: list[str]) -> None:
        if not inns:
            return
        if self.background:
            threading.Thread(target=self._run, args=(km_id, inns), daemon=True).start()
        else:
            self._run(km_id, inns)
