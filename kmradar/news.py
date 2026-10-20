"""Новости о компаниях территории из RSS/Atom и публичных Telegram-каналов.

Разбор фидов перенесён из uuagent2 (src/uaagent/news/fetcher.py) и переписан
на стандартную библиотеку: urllib вместо httpx, html.parser вместо lxml.
Источники — файл со ссылками, по одной в строке: RSS/Atom-фид, t.me/<канал>
или @канал.

Новость становится событием, только если в ней найдено название компании
территории. Правила совпадения строгие: лишняя карточка «мимо» стоит дороже
пропущенной новости.
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from .models import Company, Event

USER_AGENT = "Mozilla/5.0 (compatible; kmradar/0.1; +news)"
TIMEOUT = 20
FRESH_DAYS = 14
_ATOM_NS = "{http://www.w3.org/2005/Atom}"


# --- разбор фидов (из uuagent2) ---------------------------------------------

def _parse_date(text: str | None) -> datetime | None:
    if not text:
        return None
    text = text.strip()
    try:  # RFC 822 (RSS pubDate)
        dt = parsedate_to_datetime(text)
        if dt is not None:
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        pass
    try:  # ISO 8601 (Atom)
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


def _text(el: ET.Element | None) -> str:
    return "" if el is None else (el.text or "").strip()


def parse_feed(content: bytes) -> list[dict[str, Any]]:
    """RSS 2.0 или Atom 1.0 → [{link, title, summary, published}]."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return []
    tag = root.tag.split("}")[-1].lower()
    out: list[dict[str, Any]] = []
    if tag == "rss":
        channel = root.find("channel")
        for item in (channel.findall("item") if channel is not None else []):
            link = _text(item.find("link"))
            if link:
                out.append({"link": link, "title": _text(item.find("title")),
                            "summary": _text(item.find("description")),
                            "published": _parse_date(_text(item.find("pubDate")))})
    elif tag == "feed":
        for entry in root.findall(f"{_ATOM_NS}entry"):
            link_el = entry.find(f"{_ATOM_NS}link")
            link = (link_el.attrib.get("href") or _text(link_el)) if link_el is not None else ""
            if not link:
                continue
            published = (_text(entry.find(f"{_ATOM_NS}published"))
                         or _text(entry.find(f"{_ATOM_NS}updated")))
            out.append({"link": link, "title": _text(entry.find(f"{_ATOM_NS}title")),
                        "summary": (_text(entry.find(f"{_ATOM_NS}summary"))
                                    or _text(entry.find(f"{_ATOM_NS}content"))),
                        "published": _parse_date(published)})
    return out


_TG_RE = re.compile(r"(?:t\.me/(?:s/)?|^@)([A-Za-z0-9_]{3,})")


def telegram_channel(url: str) -> str | None:
    m = _TG_RE.search((url or "").strip())
    return m.group(1) if m else None


class _TelegramParser(HTMLParser):
    """Публичное веб-превью канала t.me/s/<канал>: сообщения, текст, дата."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.messages: list[dict[str, Any]] = []
        self._cur: dict[str, Any] | None = None
        self._text_depth = 0      # >0 — внутри блока текста сообщения
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "div" and a.get("data-post") and "tgme_widget_message" in cls:
            self._flush()
            self._cur = {"post": a["data-post"], "published": None}
        elif self._cur is not None and tag == "div" and "tgme_widget_message_text" in cls \
                and not self._text_depth:
            self._text_depth = 1
        elif self._text_depth and tag == "div":
            self._text_depth += 1
        elif self._text_depth and tag == "br":
            self._parts.append("\n")
        elif self._cur is not None and tag == "time" and a.get("datetime"):
            self._cur["published"] = _parse_date(a["datetime"])

    def handle_endtag(self, tag: str) -> None:
        if self._text_depth and tag == "div":
            self._text_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._text_depth:
            self._parts.append(data)

    def _flush(self) -> None:
        if self._cur is not None:
            text = re.sub(r"[ \t]+", " ", "".join(self._parts)).strip()
            if text:
                self.messages.append({
                    "link": f"https://t.me/{self._cur['post']}",
                    "title": text.split("\n", 1)[0][:200],
                    "summary": text[:2000],
                    "published": self._cur["published"],
                })
        self._cur, self._parts, self._text_depth = None, [], 0

    def close(self) -> None:
        super().close()
        self._flush()


def parse_telegram(content: bytes) -> list[dict[str, Any]]:
    p = _TelegramParser()
    p.feed(content.decode("utf-8", "replace"))
    p.close()
    return p.messages


# --- загрузка с ETag ----------------------------------------------------------

@dataclass
class FetchResult:
    url: str
    status: str = "ok"          # ok | not_modified | empty | http_<code> | error
    entries: list[dict[str, Any]] = field(default_factory=list)


def fetch_source(url: str, cache: dict[str, dict], opener: Callable | None = None) -> FetchResult:
    res = FetchResult(url)
    tg = telegram_channel(url)
    fetch_url = f"https://t.me/s/{tg}" if tg else url
    headers = {"User-Agent": USER_AGENT}
    saved = cache.get(url, {})
    if not tg:  # ETag/If-Modified — только для фидов
        if saved.get("etag"):
            headers["If-None-Match"] = saved["etag"]
        if saved.get("last_modified"):
            headers["If-Modified-Since"] = saved["last_modified"]
    try:
        req = urllib.request.Request(fetch_url, headers=headers)
        with (opener or urllib.request.urlopen)(req, timeout=TIMEOUT) as resp:
            body = resp.read()
            etag, modified = resp.headers.get("ETag"), resp.headers.get("Last-Modified")
    except urllib.error.HTTPError as exc:
        res.status = "not_modified" if exc.code == 304 else f"http_{exc.code}"
        return res
    except Exception:  # noqa: BLE001 — сеть, таймаут, DNS
        res.status = "error"
        return res
    res.entries = parse_telegram(body) if tg else parse_feed(body)
    if not res.entries:
        res.status = "empty"
        return res
    if not tg:
        cache[url] = {"etag": etag, "last_modified": modified}
    return res


# --- поиск компаний в тексте --------------------------------------------------

LEGAL_FORMS = r"(?:ООО|ОАО|ЗАО|ПАО|НАО|АО|ИП|ГУП|МУП|ФГУП|АНО|НКО|ТОО|ООО\s+ПКФ|ООО\s+ПК)"
QUOTES = "«»\"“”„'"
MIN_CORE = 5


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().replace("ё", "е")).strip()


def core_name(name: str) -> str:
    """'ООО «Северный Стройресурс»' → 'северный стройресурс'."""
    s = re.sub(rf"^\s*{LEGAL_FORMS}\b\.?", "", name or "", flags=re.I)
    s = s.strip().strip(QUOTES).strip()
    s = re.sub(f"[{QUOTES}]", "", s)
    return _norm(s)


@dataclass
class Matcher:
    """Название компании засчитывается, если:
    - в тексте есть её ИНН, или
    - название стоит в кавычках («Теплострой»), или
    - название из двух и более слов встречается целиком.
    Короткие названия (меньше 5 букв) — только по ИНН.
    """
    companies: dict[str, Company]

    def __post_init__(self) -> None:
        self.cores = {inn: core_name(c.name) for inn, c in self.companies.items()}

    def find(self, text: str) -> list[str]:
        raw = text or ""
        low = _norm(re.sub(f"[{QUOTES}]", '"', raw))
        found = []
        for inn, core in self.cores.items():
            if inn in raw:
                found.append(inn)
                continue
            if len(core.replace(" ", "")) < MIN_CORE:
                continue
            if f'"{core}"' in low:
                found.append(inn)
            elif " " in core and re.search(rf"(?<![\w-]){re.escape(core)}(?![\w-])", low):
                found.append(inn)
        return found


# --- источник для pipeline ---------------------------------------------------

def read_feeds(path: str | Path) -> list[str]:
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return list(dict.fromkeys(out))


class NewsSource:
    """manual_events(day) → события news_mention по компаниям территории.

    companies — функция, отдающая компании из базы радара: к моменту вызова
    pipeline уже сохранил снимки дня, так что новые компании тоже учтены.
    """

    def __init__(self, feeds: list[str], companies: Callable[[], dict[str, Company]],
                 cache_file: str | Path, log: Callable[[str], None] = print,
                 opener: Callable | None = None, workers: int = 5):
        self.feeds = feeds
        self.companies = companies
        self.cache_file = Path(cache_file)
        self.log = log
        self.opener = opener
        self.workers = workers
        self.results: list[FetchResult] = []

    def _load_cache(self) -> dict:
        try:
            return json.loads(self.cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def fetch(self) -> list[FetchResult]:
        cache = self._load_cache()
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            self.results = list(pool.map(lambda u: fetch_source(u, cache, self.opener), self.feeds))
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        bad = [r for r in self.results if r.status not in ("ok", "not_modified")]
        self.log(f"новости: источников {len(self.results)}, с ошибками {len(bad)}"
                 + ("".join(f"; {urlsplit(r.url).netloc or r.url} [{r.status}]" for r in bad)))
        return self.results

    def snapshots(self, day: date) -> list[dict[str, Any]]:
        return []

    def tender_wins(self, day: date) -> list[dict[str, Any]]:
        return []

    def manual_events(self, day: date) -> list[Event]:
        matcher = Matcher(self.companies())
        if not matcher.cores or not self.feeds:
            return []
        fresh_from = day - timedelta(days=FRESH_DAYS)
        events, seen = [], set()
        for res in self.fetch():
            source = telegram_channel(res.url) and f"Telegram @{telegram_channel(res.url)}" \
                or (urlsplit(res.url).netloc.removeprefix("www.") or res.url)
            for e in res.entries:
                published = e.get("published")
                when = published.date() if isinstance(published, datetime) else day
                if when < fresh_from or when > day:
                    continue
                text = f"{e.get('title', '')}\n{e.get('summary', '')}"
                for inn in matcher.find(text):
                    key = (inn, hashlib.sha1(e["link"].encode()).hexdigest())
                    if key in seen:
                        continue
                    seen.add(key)
                    title = re.sub(r"\s+", " ", e.get("title") or "").strip()[:200]
                    events.append(Event(inn, "news_mention", when,
                                        {"title": title, "source": source}, "Новости", e["link"]))
        if events:
            self.log(f"новости: найдено упоминаний компаний {len(events)}")
        return events
