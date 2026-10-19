import io
import urllib.error
from datetime import date

from kmradar.cards import describe
from kmradar.models import Company
from kmradar.news import Matcher, NewsSource, core_name, parse_feed, parse_telegram, telegram_channel

DAY = date(2026, 10, 20)
COMPANIES = {
    "0000000001": Company("0000000001", "ООО «Северный Стройресурс»", "69", "42.11"),
    "1424008710": Company("1424008710", 'ООО "ТЕПЛОСТРОЙ"', "14", "35.30.14"),
    "0000000003": Company("0000000003", "АО «Рост»", "69", "42.11"),          # короткое — только по ИНН
}

RSS = """<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>Тверь</title>
<item><title>«Северный Стройресурс» отремонтирует трассу</title><link>https://media.example/1</link>
<description>Подрядчик выиграл торги.</description><pubDate>Mon, 19 Oct 2026 10:00:00 +0300</pubDate></item>
<item><title>Компания Теплострой без кавычек</title><link>https://media.example/2</link>
<pubDate>Mon, 19 Oct 2026 11:00:00 +0300</pubDate></item>
<item><title>Рост цен на бетон</title><link>https://media.example/3</link>
<description>ИНН 0000000003 упомянут в документах</description>
<pubDate>Sun, 18 Oct 2026 09:00:00 +0300</pubDate></item>
<item><title>Старое про «Теплострой»</title><link>https://media.example/4</link>
<pubDate>Mon, 01 Sep 2026 09:00:00 +0300</pubDate></item>
</channel></rss>""".encode()

ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Atom-новость</title><link href="https://a.example/x"/><updated>2026-10-19T08:00:00Z</updated>
<summary>Текст</summary></entry></feed>""".encode()

TG = """<html><body>
<div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="tver_biz/101">
  <div class="tgme_widget_message_bubble">
    <div class="tgme_widget_message_text js-message_text">Компания <b>«Теплострой»</b> запустила котельную<br>Подробности ниже</div>
    <a class="tgme_widget_message_date"><time datetime="2026-10-19T09:15:00+00:00">09:15</time></a>
  </div>
</div>
<div class="tgme_widget_message js-widget_message" data-post="tver_biz/102">
  <div class="tgme_widget_message_bubble"><div class="tgme_widget_message_text">Без упоминаний</div>
  <time datetime="2026-10-19T10:00:00+00:00">10:00</time></div>
</div></body></html>""".encode()


def test_parse_rss_atom_telegram():
    rss = parse_feed(RSS)
    assert len(rss) == 4 and rss[0]["link"] == "https://media.example/1"
    assert rss[0]["published"].date() == date(2026, 10, 19)
    atom = parse_feed(ATOM)
    assert atom[0]["link"] == "https://a.example/x" and atom[0]["summary"] == "Текст"
    assert parse_feed(b"<html>not a feed") == []
    tg = parse_telegram(TG)
    assert [m["link"] for m in tg] == ["https://t.me/tver_biz/101", "https://t.me/tver_biz/102"]
    assert tg[0]["title"] == "Компания «Теплострой» запустила котельную"
    assert "Подробности ниже" in tg[0]["summary"]


def test_telegram_channel_names():
    assert telegram_channel("https://t.me/tver_biz") == "tver_biz"
    assert telegram_channel("https://t.me/s/tver_biz") == "tver_biz"
    assert telegram_channel("@tver_biz") == "tver_biz"
    assert telegram_channel("https://example.com/rss@x") is None


def test_matcher_rules():
    assert core_name("ООО «Северный Стройресурс»") == "северный стройресурс"
    assert core_name('ООО "ТЕПЛОСТРОЙ"') == "теплострой"
    m = Matcher(COMPANIES)
    assert m.find("«Северный Стройресурс» выиграл") == ["0000000001"]
    assert m.find("компания северный стройресурс открыла") == ["0000000001"]   # 2 слова — без кавычек
    assert m.find("Новый «Теплострой» в Якутии") == ["1424008710"]
    assert m.find("теплострой без кавычек") == []                             # одно слово без кавычек
    assert m.find("Рост «Рост» рост") == []                                    # короткое название
    assert m.find("ИНН 0000000003") == ["0000000003"]


class FakeResp(io.BytesIO):
    def __init__(self, body, headers=None):
        super().__init__(body)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_news_source_events_and_etag(tmp_path):
    seen_headers = []

    def opener(req, timeout):
        seen_headers.append(dict(req.header_items()))
        if "t.me/s/tver_biz" in req.full_url:
            return FakeResp(TG)
        if req.get_header("If-none-match") == '"v1"':
            raise urllib.error.HTTPError(req.full_url, 304, "Not Modified", {}, None)
        if "broken" in req.full_url:
            raise OSError("timeout")
        return FakeResp(RSS, {"ETag": '"v1"'})

    logs = []
    src = NewsSource(["https://media.example/rss", "https://t.me/tver_biz", "https://broken.example/rss"],
                     lambda: COMPANIES, tmp_path / "cache.json", log=logs.append, opener=opener)
    events = src.manual_events(DAY)
    got = sorted((e.inn, e.url) for e in events)
    assert got == [("0000000001", "https://media.example/1"),
                   ("0000000003", "https://media.example/3"),
                   ("1424008710", "https://t.me/tver_biz/101")]
    tg_event = next(e for e in events if e.url.startswith("https://t.me"))
    assert tg_event.facts["source"] == "Telegram @tver_biz"
    assert describe(tg_event).startswith("Новость (Telegram @tver_biz, 19.10.2026): «Компания")
    assert any("broken.example [error]" in line for line in logs)
    # второй прогон: фид отвечает 304 — новых событий нет, ошибки нет
    events2 = src.manual_events(DAY)
    assert {r.status for r in src.results if "media" in r.url} == {"not_modified"}
    assert [e.url for e in events2] == ["https://t.me/tver_biz/101"]
