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

# Как часто обновлять каждый вид данных, дней
CADENCE = {
    "company": 1,
    "fedresurs": 1,
    "contracts": 1,
    "enforcements": 7,
    "bankruptcy-messages": 7,
    "finances": 30,
}
CONTRACT_LAWS = ("44", "223")
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
                today: date | None = None) -> dict[str, Any]:
    """Ответы Checko → нормализованный снимок (формат в diff.py)."""
    c = company.get("data") or {}
    today = today or date.today()
    fresh_from = (today - timedelta(days=FRESH_DAYS)).isoformat()
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
        "leasing": [], "pledges": [], "bankruptcy_messages": [],
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
            state["bankruptcy_messages"].append({**item, "kind": title.lower()})
        elif "ликвидац" in title.lower() and published_by_self:
            state["bankruptcy_messages"].append({**item, "kind": title.lower()})

    enf = (enforcements or {}).get("data") or {}
    if isinstance(enf, dict):
        state["enforcement_total"] = enf.get("ОстЗадолж") or 0
    efrsb = (bankruptcy or {}).get("data") or {}
    if isinstance(efrsb, dict):
        state["efrsb_count"] = efrsb.get("ЗапВсего") or 0
    return state


def map_contracts(inn: str, resp: dict, law: str, today: date) -> list[dict[str, Any]]:
    fresh_from = (today - timedelta(days=FRESH_DAYS)).isoformat()
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
                 log: Callable[[str], None] = print):
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
            if 0 <= age < CADENCE.get(endpoint, 1):
                return saved["data"]
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
        state = map_company(
            company,
            fedresurs=self._cached(inn, "fedresurs", day, sort="-date"),
            finances=self._cached(inn, "finances", day),
            enforcements=self._cached(inn, "enforcements", day),
            bankruptcy=self._cached(inn, "bankruptcy-messages", day),
            today=day,
        )
        wins = []
        for law in CONTRACT_LAWS:
            resp = self._cached(inn, "contracts", day, law=law, role="supplier", sort="-date")
            wins += map_contracts(inn, resp or {}, law, day)
        return state, wins

    # --- протокол Source ----------------------------------------------------
    def snapshots(self, day: date) -> list[dict[str, Any]]:
        states, self._wins = [], []
        for inn in self.territory:
            state, wins = self.snapshot(inn, day)
            if state:
                states.append(state)
                self._wins += wins
            else:
                self.log(f"checko: нет данных по {inn}, снимок пропущен")
        self.log(f"checko: компаний {len(states)}/{len(self.territory)}, "
                 f"запросов {self.client.requests}")
        return states

    def tender_wins(self, day: date) -> list[dict[str, Any]]:
        return self._wins

    def manual_events(self, day: date) -> list[Event]:
        return []


def daily_requests(n_companies: int) -> dict[str, float]:
    """Сколько запросов в среднем уходит в день на территорию из n компаний."""
    per_company = sum((len(CONTRACT_LAWS) if ep == "contracts" else 1) / days
                      for ep, days in CADENCE.items())
    return {"per_company": round(per_company, 2), "total": round(per_company * n_companies)}
