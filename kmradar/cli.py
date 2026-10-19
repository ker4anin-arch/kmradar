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
from .sources import CompositeSource, FixtureSource
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
    path = Path(args.file)
    if not path.is_file():
        print(f"Файл не найден: {path}. Укажите путь к JSON с профилями КМ "
              f"(пример — fixtures/demo/kms.json).")
        sys.exit(1)
    db = Storage(args.db)
    try:
        kms = load_kms(db, path)
    except (ValueError, TypeError) as exc:
        print(f"Не получилось прочитать {path}: {exc}")
        sys.exit(1)
    print(f"Профилей КМ загружено: {len(kms)}")


def _checko(args: argparse.Namespace):
    from .checko import CheckoClient, CheckoError, CheckoSource
    try:
        client = CheckoClient(os.environ.get("CHECKO_API_KEY", ""))
    except CheckoError as exc:
        print(exc); sys.exit(1)
    territory = CheckoSource.read_territory(args.territory) if getattr(args, "territory", None) else []
    cache = args.cache or str(Path(args.db).resolve().parent / "checko-cache")
    return CheckoSource(client, territory, cache)


def cmd_run(args: argparse.Namespace) -> None:
    db = Storage(args.db)
    day = date.fromisoformat(args.date) if args.date else date.today()
    sources = []
    if args.territory and Path(args.territory).is_file():
        sources.append(_checko(args))
    elif args.territory:
        print(f"Файла территории {args.territory} нет — Checko пропускаю.")
    if args.source:
        sources.append(FixtureSource(args.source))
    if getattr(args, "feeds", None) and Path(args.feeds).is_file():
        from .news import NewsSource, read_feeds
        sources.append(NewsSource(read_feeds(args.feeds), db.companies,
                                  Path(args.db).resolve().parent / "news-cache.json"))
    if not sources:
        print("Укажите --territory (Checko) и/или --source (файлы аналитика)."); sys.exit(1)
    res = run_day(db, CompositeSource(*sources), day, _delivery(args))
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


def cmd_checko_test(args: argparse.Namespace) -> None:
    """Снимок одной компании из Checko — проверить, что данные разбираются верно."""
    src = _checko(args)
    state, wins = src.snapshot(args.inn, date.today())
    if not state:
        print("Нет данных по этому ИНН."); sys.exit(1)
    short = {k: v for k, v in state.items() if k not in ("founders",)}
    short["founders"] = [f["name"] for f in state["founders"]]
    print(json.dumps(short, ensure_ascii=False, indent=1))
    print(f"госконтрактов за {30} дней: {len(wins)}; запросов к Checko: {src.client.requests}")


def cmd_checko_plan(args: argparse.Namespace) -> None:
    from .checko import CheckoSource, daily_requests
    if not Path(args.territory).is_file():
        print(f"Файла {args.territory} нет. Создайте его: ИНН компаний территории, по одному в строке.")
        sys.exit(1)
    n = len(CheckoSource.read_territory(args.territory))
    plan = daily_requests(n)
    name = (os.environ.get("CHECKO_PLAN") or "free").lower()
    print(f"Тариф: {name}" + (f", лимит {plan['limit']} запросов в день" if plan["limit"] else ", без лимита"))
    print(f"Компаний в территории: {n}")
    print(f"В обычный день: около {plan['total']} запросов ({plan['per_company']} на компанию).")
    print(f"Первое заполнение: около {plan['first_day']} запросов"
          + (f" — растянется на {-(-plan['first_day'] // max(1, plan['limit'] - 3))} дн." if plan["limit"] else ""))
    if plan["max_companies"] is not None and n > plan["max_companies"]:
        print(f"Внимание: на этом тарифе держится примерно {plan['max_companies']} компаний. "
              f"Остальные будут обновляться реже — из кеша.")


def cmd_news_test(args: argparse.Namespace) -> None:
    """Проверить источники новостей и показать найденные упоминания компаний из базы."""
    from .news import NewsSource, read_feeds
    if not Path(args.feeds).is_file():
        print(f"Файла {args.feeds} нет. Впишите в него RSS-фиды и Telegram-каналы, по одному в строке.")
        sys.exit(1)
    db = Storage(args.db)
    src = NewsSource(read_feeds(args.feeds), db.companies, Path(args.db).resolve().parent / "news-cache.json")
    events = src.manual_events(date.today())
    for r in src.results:
        print(f"{r.status:12} {len(r.entries):4} записей  {r.url}")
    names = db.companies()
    for e in events:
        print(f"* {names[e.inn].name}: {e.facts['title']} — {e.url}")
    print(f"Компаний в базе: {len(names)}, упоминаний за {14} дней: {len(events)}")


def cmd_checko_seed(args: argparse.Namespace) -> None:
    """Набрать территорию из компаний со свежими договорами лизинга."""
    from .checko import seed_from_leasing
    src = _checko(args)
    found = seed_from_leasing(src.client, args.lessor, args.limit, src.cache)
    existing = set(src.read_territory(args.territory)) if Path(args.territory).is_file() else set()
    new = [f for f in found if f["inn"] not in existing]
    with open(args.territory, "a", encoding="utf-8") as fh:
        for f in new:
            fh.write(f"{f['inn']}  # {f['name']}, регион {f['region']}, ОКВЭД {f['okved']}\n")
    for f in found:
        print(f"{f['inn']}  регион {f['region']:>3}  ОКВЭД {f['okved']:<9} {f['name']}")
    regions = sorted({f["region"] for f in found})
    print(f"Добавлено в {args.territory}: {len(new)}. Регионы: {', '.join(regions)}. "
          f"Запросов к Checko: {src.client.requests}.")


def cmd_metrics(args: argparse.Namespace) -> None:
    print(report(Storage(args.db), args.km))


def cmd_link(args: argparse.Namespace) -> None:
    db = Storage(args.db)
    kms = db.kms() if args.km == "all" else [k for k in [db.km(args.km)] if k]
    if not kms:
        print(f"КМ не найден: {args.km}")
        sys.exit(1)
    base = args.base_url.rstrip("/")
    for km in kms:
        print(f"{km.km_id}\t{km.name}\t{base}/k/{db.new_link(km.km_id)}")
    print("Старые ссылки этих КМ больше не работают.", file=sys.stderr)


def cmd_web(args: argparse.Namespace) -> None:  # pragma: no cover
    from .web import serve
    serve(args.db, args.host, args.port)


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
    s.add_argument("--source", help="каталог с файлами аналитика YYYY-MM-DD.json")
    s.add_argument("--territory", help="файл со списком ИНН территории — данные из Checko")
    s.add_argument("--cache", help="каталог кеша Checko (по умолчанию рядом с базой)")
    s.add_argument("--feeds", help="файл с RSS-фидами и Telegram-каналами, по одному в строке")
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

    s = sub.add_parser("checko-test", help="показать снимок одной компании из Checko")
    s.add_argument("--db", default="radar.db")
    s.add_argument("--cache")
    s.add_argument("inn")
    s.set_defaults(fn=cmd_checko_test)

    s = sub.add_parser("checko-seed", help="набрать территорию из свежих договоров лизинга")
    s.add_argument("--db", default="radar.db")
    s.add_argument("--cache")
    s.add_argument("--lessor", default="7707009586", help="ИНН лизинговой компании (по умолчанию Сбербанк Лизинг)")
    s.add_argument("--limit", type=int, default=10, help="сколько компаний взять (каждая — 1 запрос)")
    s.add_argument("territory", help="файл территории, куда дописать ИНН")
    s.set_defaults(fn=cmd_checko_seed)

    s = sub.add_parser("checko-plan", help="сколько запросов к Checko в день уйдёт на территорию")
    s.add_argument("territory")
    s.set_defaults(fn=cmd_checko_plan)

    s = sub.add_parser("news-test", help="проверить источники новостей")
    s.add_argument("--db", default="radar.db")
    s.add_argument("feeds")
    s.set_defaults(fn=cmd_news_test)

    s = sub.add_parser("metrics", help="метрики пилота")
    s.add_argument("--db", default="radar.db")
    s.add_argument("--km")
    s.set_defaults(fn=cmd_metrics)

    s = sub.add_parser("link", help="выдать КМ новую личную ссылку на веб-дайджест")
    s.add_argument("--db", default="radar.db")
    s.add_argument("--base-url", required=True, help="например https://radar.example.ru")
    s.add_argument("km", help="km_id или all")
    s.set_defaults(fn=cmd_link)

    s = sub.add_parser("web", help="веб-версия дайджеста")
    s.add_argument("--db", default="radar.db")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)
    s.set_defaults(fn=cmd_web)

    s = sub.add_parser("bot", help="принимать нажатия кнопок в Telegram")
    s.add_argument("--db", default="radar.db")
    s.set_defaults(fn=cmd_bot)

    args = p.parse_args(argv)
    args.fn(args)
