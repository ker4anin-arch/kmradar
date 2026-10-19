"""Командная строка: python -m kmradar <команда>."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

from .delivery import ConsoleDelivery, FileDelivery, TelegramDelivery
from .feedback import record_feedback, record_outcome
from .metrics import report
from .models import KMProfile
from .pipeline import run_day
from .sources import FixtureSource
from .storage import Storage

DEMO_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "demo"


def load_kms(db: Storage, path: Path) -> list[KMProfile]:
    kms = [KMProfile(**k) for k in json.loads(path.read_text(encoding="utf-8"))]
    for km in kms:
        db.save_km(km)
    return kms


def cmd_demo(args: argparse.Namespace) -> None:
    db = Storage(":memory:")
    load_kms(db, DEMO_DIR / "kms.json")
    source = FixtureSource(DEMO_DIR / "days")
    delivery = ConsoleDelivery()
    for day in source.days():
        print(f"\n######## {day.isoformat()} ########")
        res = run_day(db, source, day, delivery)
        print(f"новых событий: {res.new_events}")
        for km_id, d in res.digests.items():
            if d.skipped and not d.cards:
                print(f"{km_id}: дайджест не отправлен — меньше 3 сильных поводов два дня подряд")
    # имитация ответов КМ, чтобы показать метрики
    days = source.days()
    for card in db.cards():
        value = {1: "contacted", 2: "knew", 4: "contacted", 5: "useful"}.get(card["id"], "miss")
        record_feedback(db, card["id"], value)
    record_outcome(db, 4, "meeting")
    print(f"\n######## Ответы КМ после {days[-1].isoformat()} (имитация) ########")
    for card in db.cards():
        print(f"карточка {card['id']}: {db.card_row(card['id'])['feedback']}")
    print("\n######## Метрики пилота ########")
    print(report(db))


def _delivery(args: argparse.Namespace):
    if args.telegram:
        return TelegramDelivery(os.environ["TELEGRAM_TOKEN"])
    if args.out:
        return FileDelivery(args.out)
    return ConsoleDelivery()


def cmd_add_km(args: argparse.Namespace) -> None:
    db = Storage(args.db)
    kms = load_kms(db, Path(args.file))
    print(f"Профилей КМ загружено: {len(kms)}")


def cmd_run(args: argparse.Namespace) -> None:
    db = Storage(args.db)
    day = date.fromisoformat(args.date) if args.date else date.today()
    res = run_day(db, FixtureSource(args.source), day, _delivery(args))
    sent = {k: len(d.cards) for k, d in res.digests.items()}
    print(f"{day}: новых событий {res.new_events}, карточек по КМ: {sent}")


def cmd_feedback(args: argparse.Namespace) -> None:
    res = record_feedback(Storage(args.db), args.card, args.value)
    print(res.message)
    if not res.ok:
        sys.exit(1)


def cmd_outcome(args: argparse.Namespace) -> None:
    res = record_outcome(Storage(args.db), args.card, args.value)
    print(res.message)
    if not res.ok:
        sys.exit(1)


def cmd_metrics(args: argparse.Namespace) -> None:
    print(report(Storage(args.db), args.km))


def cmd_bot(args: argparse.Namespace) -> None:  # pragma: no cover
    from .bot import poll
    poll(Storage(args.db), TelegramDelivery(os.environ["TELEGRAM_TOKEN"]))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="kmradar", description="Радар поводов для КМ (MVP)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("demo", help="прогнать демо-данные и показать дайджесты").set_defaults(fn=cmd_demo)

    s = sub.add_parser("add-km", help="загрузить профили КМ из JSON")
    s.add_argument("--db", default="radar.db")
    s.add_argument("file")
    s.set_defaults(fn=cmd_add_km)

    s = sub.add_parser("run", help="ночной прогон за день")
    s.add_argument("--db", default="radar.db")
    s.add_argument("--source", required=True, help="каталог с файлами YYYY-MM-DD.json")
    s.add_argument("--date", help="день (по умолчанию сегодня)")
    s.add_argument("--out", help="каталог для дайджестов в Markdown")
    s.add_argument("--telegram", action="store_true", help="слать в Telegram (TELEGRAM_TOKEN)")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("feedback", help="отметить карточку: useful | miss | knew | contacted")
    s.add_argument("--db", default="radar.db")
    s.add_argument("card", type=int)
    s.add_argument("value")
    s.set_defaults(fn=cmd_feedback)

    s = sub.add_parser("outcome", help="итог: meeting | application | refusal | nothing")
    s.add_argument("--db", default="radar.db")
    s.add_argument("card", type=int)
    s.add_argument("value")
    s.set_defaults(fn=cmd_outcome)

    s = sub.add_parser("metrics", help="метрики пилота")
    s.add_argument("--db", default="radar.db")
    s.add_argument("--km")
    s.set_defaults(fn=cmd_metrics)

    s = sub.add_parser("bot", help="принимать нажатия кнопок в Telegram")
    s.add_argument("--db", default="radar.db")
    s.set_defaults(fn=cmd_bot)

    args = p.parse_args(argv)
    args.fn(args)
