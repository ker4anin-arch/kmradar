"""Источник данных Checko (api.checko.ru/v2): ЕГРЮЛ, МСП, Федресурс, ЕФРСБ, ФССП,
отчётность и госконтракты.

Поля разобраны по реальным ответам API (октябрь 2026). Ответы кешируются на
диске: дорогие и редко меняющиеся данные (отчётность, долги) запрашиваются
реже, а при сбое запроса берётся последний удачный ответ. Если по компании нет
ни свежего, ни сохранённого ответа карточки ЕГРЮЛ, её снимок за день
пропускается — неполный снимок дал бы ложные события.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from .models import Event

BASE_URL = "https://api.checko.ru/v2"

import os

# Тарифы: как часто обновлять каждый вид данных (дней) и какие законы о закупках смотреть.
# Порядок ключей — приоритет: при нехватке лимита первыми идут самые важные данные.
PLANS = {
    # бесплатный ключ: 100 запросов в день, ~1,5 запроса на компанию → ~60 компаний
    "free": {"cadence": {"fedresurs": 1, "company": 3, "contracts": 7, "enforcements": 30,
                         "bankruptcy-messages": 30, "finances": 90},
             "laws": ("44",), "daily_limit": 100},
    "paid": {"cadence": {"company": 1, "fedresurs": 1, "contracts": 1, "enforcements": 7,
                         "bankruptcy-messages": 7, "finances": 30},
             "laws": ("44", "223"), "daily_limit": 0},
}
RESERVE = 3   # запросов в запасе на ручные проверки


def plan_settings(name: str | None = None) -> dict:
    name = (name or os.environ.get("CHECKO_PLAN") or "free").lower()
    plan = dict(PLANS.get(name, PLANS["free"]))
    env_limit = os.environ.get("CHECKO_DAILY_LIMIT")
    if env_limit and env_limit.isdigit():
        plan["daily_limit"] = int(env_limit)
    return plan


CADENCE = PLANS["paid"]["cadence"]
CONTRACT_LAWS = PLANS["paid"]["laws"]
FRESH_DAYS = 30          # свежие сообщения Федресурса и контракты, которые держим в снимке

MSP = {"МИКРО": "micro", "МАЛ": "small", "СРЕДН": "medium"}
LEASE_RE = re.compile(r"^FinancialLeaseContract\d*$")
PLEDGE_RE = re.compile(r"^CreationRightOfPledge\d*$")


class CheckoError(Exception):
    pass


class CheckoClient:
    def __init__(self, key: str, fetch: Callable[[str], dict] | None = None, base: str = BASE_URL):
        if not key:
            raise CheckoError("Не задан CHECKO_API_KEY")
        self.key = key
        self.base = base
        self.fetch = fetch or self._fetch
        self.requests = 0
        self.used_today: int | None = None   # Checko сообщает это в meta каждого ответа

    def _fetch(self, url: str) -> dict:  # pragma: no cover — сеть
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.loads(resp.read())

    def get(self, endpoint: str, **params: Any) -> dict:
        url = f"{self.base}/{endpoint}?" + urllib.parse.urlencode({"key": self.key, **params})
        self.requests += 1
        try:
            data = self.fetch(url)
        except Exception as exc:  # noqa: BLE001
            raise CheckoError(str(exc).replace(self.key, "***")) from None
        meta = data.get("meta") or {}
        if isinstance(meta.get("today_request_count"), int):
            self.used_today = meta["today_request_count"]
        if meta.get("status") not in (None, "ok"):
            raise CheckoError(str(meta.get("message") or meta.get("status")).replace(self.key, "***"))
        return data


def _name(x: Any) -> str:
    if isinstance(x, dict):
        for k in ("ФИО", "НаимСокр", "НаимПолн", "Наим", "ИНН"):
            if x.get(k):
                return str(x[k])
        return ""
    return str(x or "")


def _records(resp: dict) -> list[dict]:
    data = resp.get("data") or {}
    return list(data.get("Записи") or []) if isinstance(data, dict) else []


def map_company(company: dict, *, fedresurs: dict | None = None, finances: dict | None = None,
                enforcements: dict | None = None, bankruptcy: dict | None = None,
                today: date | None = None, fresh_days: int = FRESH_DAYS) -> dict[str, Any]:
    """Ответы Checko → нормализованный снимок (формат в diff.py)."""
    c = company.get("data") or {}
    today = today or date.today()
    fresh_from = (today - timedelta(days=fresh_days)).isoformat()
    ogrn = str(c.get("ОГРН") or "")

    director: dict[str, Any] = {}
    heads = c.get("Руковод") or []
    if heads:
        director = {"name": _name(heads[0]), "position": (heads[0].get("НаимДолжн") or "").capitalize(),
                    "since": heads[0].get("ДатаЗаписи")}
    elif c.get("УпрОрг"):
        director = {"name": _name(c["УпрОрг"]), "position": "Управляющая организация"}

    founders = []
    for group in (c.get("Учред") or {}).values():
        for f in group or []:
            share = (f.get("Доля") or {}).get("Процент") if isinstance(f, dict) else None
            founders.append({"name": _name(f), "share": share,
                             "since": f.get("ДатаЗаписи") if isinstance(f, dict) else None})

    subdiv = c.get("Подразд") or {}
    branches = []
    for kind in ("Филиал", "Представ"):
        for b in (subdiv.get(kind) or []) if isinstance(subdiv, dict) else []:
            kpp = str(b.get("КПП") or "")
            if len(kpp) >= 2:  # первые цифры КПП — код региона налоговой
                branches.append({"region": kpp[:2], "address": b.get("Адрес")})

    msp_cat = str((c.get("РМСП") or {}).get("Кат") or "").upper()
    msp = next((v for k, v in MSP.items() if k in msp_cat), "none")

    status_name = str((c.get("Статус") or {}).get("Наим") or "").lower()
    status = ("bankrupt" if "банкрот" in status_name else
              "liquidating" if any(w in status_name for w in ("ликвид", "не действ", "прекращ"))
              else "active")

    state: dict[str, Any] = {
        "inn": str(c.get("ИНН") or ""),
        "ogrn": ogrn,
        "name": c.get("НаимСокр") or c.get("НаимПолн") or "",
        "region": str((c.get("Регион") or {}).get("Код") or ""),
        "okved_main": (c.get("ОКВЭД") or {}).get("Код", ""),
        "okved_extra": sorted({o.get("Код") for o in c.get("ОКВЭДДоп") or [] if o.get("Код")}),
        "director": director,
        "founders": founders,
        "capital": (c.get("УстКап") or {}).get("Сумма"),
        "branches": branches,
        "msp_category": msp,
        "status": status,
        "revenue": {}, "receivables": {},
        "leasing": [], "pledges": [], "bankruptcy_messages": [], "risk_messages": [],
        "enforcement_total": 0, "efrsb_count": 0,
    }

    for year, lines in ((finances or {}).get("data") or {}).items():
        if isinstance(lines, dict):
            if lines.get("2110") is not None:
                state["revenue"][str(year)] = lines["2110"]
            if lines.get("1230") is not None:
                state["receivables"][str(year)] = lines["1230"]

    for r in _records(fedresurs or {}):
        when = str(r.get("Дата") or "")
        if when < fresh_from:
            continue
        kind, title = str(r.get("Тип") or ""), str(r.get("ТипНаим") or "")
        published_by_self = str(r.get("Публикатор") or "") == ogrn
        item = {"id": r.get("GUID"), "date": when, "url": r.get("URL")}
        if LEASE_RE.match(kind) and not published_by_self:      # компания — лизингополучатель
            state["leasing"].append(item)
        elif PLEDGE_RE.match(kind) and not published_by_self:   # компания — залогодатель
            state["pledges"].append(item)
        elif kind == "DebtorIntentionGoToCourt" and published_by_self:
            state["bankruptcy_messages"].append({**item, "kind": title.lower()})
        elif kind == "CreditorIntentionGoToCourt" and not published_by_self:
            state["risk_messages"].append({**item, "kind": title.lower()})
        elif "ликвидац" in title.lower() and published_by_self:
            state["bankruptcy_messages"].append({**item, "kind": title.lower()})

    enf = (enforcements or {}).get("data") or {}
    if isinstance(enf, dict):
        state["enforcement_total"] = enf.get("ОстЗадолж") or 0
    efrsb = (bankruptcy or {}).get("data") or {}
    if isinstance(efrsb, dict):
        state["efrsb_count"] = efrsb.get("ЗапВсего") or 0
    return state


def map_contracts(inn: str, resp: dict, law: str, today: date,
                  fresh_days: int = FRESH_DAYS) -> list[dict[str, Any]]:
    fresh_from = (today - timedelta(days=fresh_days)).isoformat()
    out = []
    for r in _records(resp):
        when = str(r.get("Дата") or "")
        if when < fresh_from:
            continue
        objects = r.get("Объекты") or []
        out.append({
            "inn": inn, "date": when, "amount": r.get("Цена"),
            "subject": _name(objects[0]) if objects else None,
            "customer": _name(r.get("Заказ")) or None,
            "number": r.get("РегНомер"), "law": law, "url": r.get("СтрЕИС"),
            "stage": "contract",
        })
    return out


class CheckoSource:
    """Источник для pipeline: территория — список ИНН (файл, по одному в строке)."""

    def __init__(self, client: CheckoClient, territory: list[str], cache_dir: str | Path,
                 log: Callable[[str], None] = print, plan: dict | None = None,
                 history_days: int = 0):
        self.plan = plan or plan_settings()
        # >0 — режим истории: в снимок попадают сообщения и контракты за history_days дней
        self.fresh_days = max(FRESH_DAYS, history_days)
        self.client = client
        self.territory = territory
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.log = log
        self.stopped = False   # исчерпан лимит или API недоступен — дальше только кеш
        self._wins: list[dict[str, Any]] = []

    @staticmethod
    def read_territory(path: str | Path) -> list[str]:
        inns = []
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if re.fullmatch(r"\d{10}", line):
                inns.append(line)
        return list(dict.fromkeys(inns))

    def _cached(self, inn: str, endpoint: str, day: date, **params: Any) -> dict | None:
        key = endpoint + "".join(f"_{v}" for v in params.values())
        path = self.cache / inn / f"{key}.json"
        saved = None
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            age = (day - date.fromisoformat(saved["fetched"])).days
            if 0 <= age < self.plan["cadence"].get(endpoint, 1):
                return saved["data"]
        limit = self.plan["daily_limit"]
        if limit and not self.stopped:
            used = self.client.used_today if self.client.used_today is not None else self.client.requests
            if used >= limit - RESERVE:
                self.stopped = True
                self.log(f"checko: дневной лимит {limit} почти исчерпан — дальше данные из кеша")
        if not self.stopped:
            try:
                data = self.client.get(endpoint, inn=inn, **params)
                path.parent.mkdir(exist_ok=True)
                path.write_text(json.dumps({"fetched": day.isoformat(), "data": data},
                                           ensure_ascii=False), encoding="utf-8")
                return data
            except CheckoError as exc:
                self.log(f"checko {endpoint} {inn}: {exc}")
                if "limit" in str(exc).lower() or "лимит" in str(exc).lower():
                    self.stopped = True
        return saved["data"] if saved else None

    def snapshot(self, inn: str, day: date) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        company = self._cached(inn, "company", day)
        if not company or not (company.get("data") or {}).get("ИНН"):
            return None, []
        # порядок — приоритет при нехватке лимита
        parts = {"fedresurs": self._cached(inn, "fedresurs", day, sort="-date")}
        contracts = {law: self._cached(inn, "contracts", day, law=law, role="supplier", sort="-date")
                     for law in self.plan["laws"]}
        for ep in ("finances", "enforcements", "bankruptcy-messages"):
            parts[ep] = self._cached(inn, ep, day)
        state = map_company(
            company, fedresurs=parts["fedresurs"], finances=parts["finances"],
            enforcements=parts["enforcements"], bankruptcy=parts["bankruptcy-messages"], today=day,
            fresh_days=self.fresh_days,
        )
        # какие данные реально есть: diff не сравнивает то, чего не было во вчерашнем снимке
        state["_sources"] = ["company"] + [ep for ep, resp in parts.items() if resp]
        wins = []
        for law, resp in contracts.items():
            wins += map_contracts(inn, resp or {}, law, day, self.fresh_days)
        return state, wins

    # --- протокол Source ----------------------------------------------------
    def snapshots(self, day: date) -> list[dict[str, Any]]:
        states, self._wins = [], []
        # сначала уже известные компании (их события важнее), потом новые
        known = [i for i in self.territory if (self.cache / i / "company.json").exists()]
        order = known + [i for i in self.territory if i not in known]
        for inn in order:
            state, wins = self.snapshot(inn, day)
            if state:
                states.append(state)
                self._wins += wins
            else:
                self.log(f"checko: нет данных по {inn}, снимок пропущен")
        self.log(f"checko: компаний {len(states)}/{len(self.territory)}, запросов {self.client.requests}"
                 + (f", за сутки по данным Checko {self.client.used_today}" if self.client.used_today else ""))
        return states

    def tender_wins(self, day: date) -> list[dict[str, Any]]:
        return self._wins

    def manual_events(self, day: date) -> list[Event]:
        return []


def daily_requests(n_companies: int, plan: dict | None = None) -> dict[str, float]:
    """Сколько запросов в среднем уходит в день на территорию из n компаний."""
    plan = plan or plan_settings()
    per_company = sum((len(plan["laws"]) if ep == "contracts" else 1) / days
                      for ep, days in plan["cadence"].items())
    first = 1 + len(plan["laws"]) + 4
    limit = plan["daily_limit"]
    return {"per_company": round(per_company, 2), "total": round(per_company * n_companies),
            "first_day": first * n_companies, "limit": limit,
            "max_companies": int((limit - RESERVE) / per_company) if limit else None}


SEED_FRESH_DAYS = 14   # договор моложе — карточка гарантированно пройдёт порог


def seed_from_leasing(client: CheckoClient, lessor_inn: str, limit: int, cache_dir: str | Path,
                      day: date | None = None, max_pages: int = 3,
                      log: Callable[[str], None] = lambda m: None) -> list[dict[str, str]]:
    """Компании со свежими (до 14 дней) договорами лизинга у лизинговой компании —
    быстрый способ набрать территорию, где события есть уже в первый день.
    Стоит до max_pages запросов на ленту + 1 запрос на каждую компанию; карточки
    компаний сохраняются в кеш и повторно не запрашиваются."""
    day = day or date.today()
    fresh_from = (day - timedelta(days=SEED_FRESH_DAYS)).isoformat()
    ogrns: list[str] = []
    for page in range(1, max_pages + 1):
        resp = client.get("fedresurs", inn=lessor_inn, sort="-date", page=page)
        own = str((resp.get("company") or {}).get("ОГРН", ""))
        recs = _records(resp)
        for r in recs:
            if not LEASE_RE.match(str(r.get("Тип") or "")) or str(r.get("Дата") or "") < fresh_from:
                continue
            for p in map(str, r.get("Участники") or []):
                if len(p) == 13 and p != own and p not in ogrns:
                    ogrns.append(p)
        oldest = min((str(r.get("Дата") or "") for r in recs), default="")
        leases = [str(r.get("Дата") or "") for r in recs if LEASE_RE.match(str(r.get("Тип") or ""))]
        name = (resp.get("company") or {}).get("НаимСокр") or lessor_inn
        log(f"{name}, стр. {page}: записей {len(recs)} ({min(recs, key=lambda r: r.get('Дата') or '').get('Дата') if recs else '—'}"
            f" … {max(recs, key=lambda r: r.get('Дата') or '').get('Дата') if recs else '—'}), "
            f"договоров лизинга {len(leases)}, самый свежий {max(leases, default='—')}, "
            f"кандидатов не старше {SEED_FRESH_DAYS} дн.: {len(ogrns)}")
        if len(ogrns) >= limit * 2 or not recs or oldest < fresh_from:
            break
    out = []
    for ogrn in ogrns:
        if len(out) >= limit:
            break
        company = client.get("company", ogrn=ogrn)
        c = company.get("data") or {}
        inn = str(c.get("ИНН") or "")
        okved = str((c.get("ОКВЭД") or {}).get("Код") or "")
        if len(inn) != 10 or okved.startswith("64"):   # банки и прочие финорганизации пропускаем
            continue
        path = Path(cache_dir) / inn / "company.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"fetched": day.isoformat(), "data": company}, ensure_ascii=False),
                        encoding="utf-8")
        out.append({"inn": inn, "name": c.get("НаимСокр") or "", "region": str((c.get("Регион") or {}).get("Код") or ""),
                    "okved": okved})
    return out
