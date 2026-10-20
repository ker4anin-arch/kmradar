"""Веб-версия дайджеста: личная ссылка КМ, карточки, кнопки, вопрос об итоге.

Только стандартная библиотека. Сервер слушает 127.0.0.1, HTTPS даёт Caddy
(или nginx) перед ним. Вход — по личной ссылке /k/<токен>; в базе хранится
только хеш токена. Кнопки — обычные HTML-формы, JavaScript не нужен.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, quote, urlsplit

from .cards import ddmm
from .config import FEEDBACK_VALUES, FOLLOWUP_DAYS, OUTCOME_VALUES, SIGNALS
from .feedback import record_feedback, record_outcome
from .models import KMProfile
from .storage import Storage

MAX_BODY = 10_000
TOKEN_RE = re.compile(r"^/k/([A-Za-z0-9_-]{16,64})(/[a-z]+)?$")
ADMIN_RE = re.compile(r"^/a/([A-Za-z0-9_-]{16,64})(/event)?$")
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                               "base-uri 'none'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",  # токен в адресе не должен утечь по ссылкам на источники
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex, nofollow",
}


@dataclass
class Response:
    status: int
    body: str = ""
    headers: dict[str, str] = field(default_factory=dict)


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def parse_card(text: str) -> dict:
    """Текст карточки (формат cards.render) → части для HTML."""
    blocks = text.split("\n\n")
    head = blocks[0].split("\n")
    header = head[0]
    company = head[1] if len(head) > 1 else ""
    sections = []
    for b in blocks[1:-1]:
        title, _, body = b.partition("\n")
        sections.append((title, body))
    source = blocks[-1].split("\n")[0] if len(blocks) > 1 else ""
    kind = "urgent" if header.startswith("🔴") else "month" if header.startswith("🟡") else "calm"
    name, _, meta = company.partition(" · ")
    return {"badge": re.sub(r"^\S+\s", "", header), "kind": kind, "name": name, "meta": meta,
            "sections": sections, "source": source}


def linkify(text: str) -> str:
    out = esc(text)
    return re.sub(r"(https?://[^\s;<]+)",
                  r'<a href="\1" target="_blank" rel="noopener noreferrer">\1</a>', out)


CSS = """
:root{--bg:#f3f5f8;--surface:#fff;--ink:#142033;--muted:#5a6577;--line:#dde2ea;--accent:#1f4fd1;
--accent-soft:#e6ecfb;--urgent:#c8322b;--urgent-soft:#fbe9e8;--amber:#a86a00;--amber-soft:#fbf0d9;
--good:#1d7a46;--good-soft:#e3f3e9}
@media (prefers-color-scheme:dark){:root{--bg:#0e141e;--surface:#172131;--ink:#e6ebf3;--muted:#97a3b6;
--line:#2a374b;--accent:#7c9cff;--accent-soft:#1f2d4d;--urgent:#ff7a70;--urgent-soft:#3a1f22;
--amber:#f0b44c;--amber-soft:#392c14;--good:#5cc98b;--good-soft:#15321f;color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:720px;margin:0 auto;padding:24px max(16px,env(safe-area-inset-right)) 48px max(16px,env(safe-area-inset-left));display:grid;gap:14px}
h1{font-size:22px;margin:0;line-height:1.25}
h2{font-size:16px;margin:8px 0 0}
p{margin:0}
.muted{color:var(--muted);font-size:14px}
.days{display:flex;flex-wrap:wrap;gap:6px}
.days a{padding:6px 10px;border:1px solid var(--line);border-radius:8px;color:var(--muted);text-decoration:none;background:var(--surface);font-size:14px}
.days a[aria-current]{background:var(--accent-soft);color:var(--accent);border-color:var(--accent);font-weight:600}
.box{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:16px;display:grid;gap:10px;min-width:0}
.caution{background:var(--amber-soft);border-left:3px solid var(--amber);border-radius:10px;padding:10px 12px;font-size:14px}
.ask{background:var(--accent-soft);border-color:var(--accent)}
.head{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;align-items:baseline}
.badge{font-weight:600;font-size:13px;padding:4px 8px;border-radius:7px}
.urgent{background:var(--urgent-soft);color:var(--urgent)}.month{background:var(--amber-soft);color:var(--amber)}
.calm{background:var(--good-soft);color:var(--good)}
.score{font:600 12.5px ui-monospace,Menlo,Consolas,monospace;color:var(--muted)}
.name{font-weight:600;font-size:16px}.meta{color:var(--muted);font-size:13px;overflow-wrap:anywhere}
.sec h3{margin:0;font-size:11.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
.sec p{white-space:pre-line}.phrase p{font-style:italic}
.src{font-size:12.5px;color:var(--muted);overflow-wrap:anywhere}
.src a{color:var(--accent)}
.btns{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px}
@media (max-width:460px){.btns{grid-template-columns:repeat(2,minmax(0,1fr))}}
.btns form{display:contents}
button{font-weight:500;font-size:14px;line-height:1.2;font-family:inherit;padding:10px 6px;border-radius:9px;border:1px solid var(--line);background:var(--bg);color:var(--ink);cursor:pointer}
button:hover{border-color:var(--accent)}
button[aria-pressed=true]{background:var(--accent);border-color:var(--accent);color:var(--surface)}
button:focus-visible,a:focus-visible,textarea:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.row{display:flex;flex-wrap:wrap;gap:6px}
textarea{width:100%;min-height:70px;font:inherit;padding:8px;border-radius:9px;border:1px solid var(--line);background:var(--bg);color:var(--ink)}
.hist{margin:0;padding-left:18px;font-size:14px;display:grid;gap:4px}
.hist a{color:var(--accent)}
button.small{padding:6px 10px;font-size:13px}
h2 .muted{font-weight:400;font-size:14px}
.flash{background:var(--good-soft);color:var(--good);border-radius:10px;padding:8px 12px;font-size:14px}
"""


def page(title: str, body: str) -> str:
    return (f'<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta name="robots" content="noindex,nofollow"><title>{esc(title)}</title>'
            f'<style>{CSS}</style></head><body><main>{body}</main></body></html>')


class App:
    def __init__(self, db_path: str, today: Callable[[], date] = date.today,
                 now: Callable[[], datetime] = datetime.now,
                 refresher: Callable[[str, list[str]], None] | None = None,
                 lookup: Callable[[str], dict | None] | None = None):
        self.refresher = refresher   # фоновая загрузка «Моих компаний» из Checko
        self.lookup = lookup         # карточка компании из Checko для админ-формы
        self.db_path = db_path
        self.today = today
        self.now = now

    # --- маршруты -----------------------------------------------------------
    def handle(self, method: str, raw_path: str, body: bytes = b"") -> Response:
        url = urlsplit(raw_path)
        if url.path == "/healthz":
            return Response(200, "ok", {"Content-Type": "text/plain; charset=utf-8"})
        if url.path == "/":
            return Response(200, page("Радар поводов", "<h1>Радар поводов работает</h1>"
                                      "<p class='muted'>Дайджест открывается по личной ссылке вида "
                                      "/k/…, её выдаёт администратор радара.</p>"),
                            {"Content-Type": "text/html; charset=utf-8"})
        if url.path == "/demo":
            # статичный интерактивный пример на выдуманных компаниях; свой CSP — нужен inline-скрипт и шрифты
            from pathlib import Path
            demo = (Path(__file__).parent / "static" / "demo.html").read_text(encoding="utf-8")
            return Response(200, demo, {
                "Content-Type": "text/html; charset=utf-8",
                "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; "
                                           "style-src 'unsafe-inline' https://fonts.googleapis.com; "
                                           "font-src https://fonts.gstatic.com; base-uri 'none'; frame-ancestors 'none'"})
        a = ADMIN_RE.match(url.path)
        if a:
            return self.admin(method, a.group(1), bool(a.group(2)), url.query, body)
        m = TOKEN_RE.match(url.path)
        if not m:
            return self.not_found()
        token, action = m.group(1), (m.group(2) or "")
        db = Storage(self.db_path)
        try:
            km = db.km_by_token(token)
            if km is None:
                return self.not_found()
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            if method == "GET" and action == "":
                return self.digest(db, km, token, query)
            if method == "POST" and action in ("/fb", "/out", "/reason", "/watch", "/unwatch"):
                form = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}
                return self.post(db, km, token, action, form)
            return Response(405, "Метод не поддерживается", {"Content-Type": "text/plain; charset=utf-8"})
        finally:
            db.close()

    def admin(self, method: str, token: str, is_post: bool, query: str, body: bytes) -> Response:
        from .admin import ADMIN_CSS, add_event, page_body
        db = Storage(self.db_path)
        try:
            if not db.is_admin_token(token):
                return self.not_found()
            if method == "POST" and is_post:
                form = parse_qs(body.decode("utf-8", "replace"))
                flat = {k: v[0] for k, v in form.items()}
                msg = add_event(db, flat, form.get("km", []), self.today(), self.now(), self.lookup)
                return Response(303, "", {"Location": f"/a/{token}?msg={quote(msg)}"})
            if method == "GET" and not is_post:
                msg = parse_qs(query).get("msg", [""])[0][:500]
                body_html = f"<style>{ADMIN_CSS}</style>" + page_body(db, token, msg, self.today())
                return Response(200, page("Радар · добавить повод", body_html),
                                {"Content-Type": "text/html; charset=utf-8"})
            return Response(405, "Метод не поддерживается", {"Content-Type": "text/plain; charset=utf-8"})
        finally:
            db.close()

    def not_found(self) -> Response:
        return Response(404, page("Не найдено", "<h1>Ссылка не работает</h1>"
                                  "<p class='muted'>Попросите новую личную ссылку у администратора радара.</p>"),
                        {"Content-Type": "text/html; charset=utf-8"})

    def redirect(self, token: str, day: str | None, anchor: str = "", **params: str) -> Response:
        q = [f"day={quote(day)}"] if day else []
        q += [f"{k}={quote(v)}" for k, v in params.items() if v]
        loc = f"/k/{token}" + ("?" + "&".join(q) if q else "") + (f"#{anchor}" if anchor else "")
        return Response(303, "", {"Location": loc})

    def post(self, db: Storage, km: KMProfile, token: str, action: str, form: dict) -> Response:
        day = form.get("day") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", form.get("day", "")) else None
        if action == "/watch":
            from .watch import WATCH_LIMIT, parse_inns
            inns, bad = parse_inns(form.get("inns", "")[:2000])
            added = db.add_watch(km.km_id, inns, WATCH_LIMIT, self.now())
            if added and self.refresher:
                self.refresher(km.km_id, added)
            skipped = len(inns) - len(added)
            return self.redirect(token, day, "my", msg="watch", added=str(len(added)),
                                 bad=str(len(bad)), full=str(skipped) if skipped else "")
        if action == "/unwatch":
            inn = form.get("inn", "")
            if re.fullmatch(r"\d{10}", inn):
                db.remove_watch(km.km_id, inn)
            return self.redirect(token, day, "my")
        if action == "/reason":
            type_, text = form.get("type", ""), form.get("text", "").strip()[:1000]
            if type_ in SIGNALS and text:
                db.save_reason(km.km_id, type_, text, self.now())
            return self.redirect(token, day, msg="reason")
        try:
            card_id = int(form.get("card", ""))
        except ValueError:
            return self.redirect(token, day)
        row = db.card_row(card_id)
        if row is None or row["km_id"] != km.km_id:
            return self.not_found()
        value = form.get("value", "")
        if action == "/fb":
            if row["feedback"] == value:  # повторное нажатие той же кнопки ничего не меняет
                return self.redirect(token, day, f"c{card_id}")
            res = record_feedback(db, card_id, value, self.now())
            return self.redirect(token, day, f"c{card_id}", ask=res.ask_reason_for or "")
        record_outcome(db, card_id, value, self.now())
        return self.redirect(token, day, msg="outcome")

    # --- страница дайджеста -------------------------------------------------
    def digest(self, db: Storage, km: KMProfile, token: str, query: dict) -> Response:
        today = self.today()
        days = db.digest_days(km.km_id, today)
        try:
            day = date.fromisoformat(query["day"]) if "day" in query else (days[0] if days else today)
        except ValueError:
            day = days[0] if days else today
        if day in days:
            db.mark_opened(km.km_id, day, self.now())

        parts = [f"<h1>Радар · {ddmm(day)}</h1><p class='muted'>{esc(km.name)}</p>"]
        if days:
            links = "".join(
                f'<a href="/k/{token}?day={d.isoformat()}"{" aria-current=page" if d == day else ""}>{ddmm(d)}</a>'
                for d in days)
            parts.append(f'<nav class="days" aria-label="Дни">{links}</nav>')
        if query.get("msg") == "outcome":
            parts.append('<p class="flash">Итог записан. Спасибо!</p>')
        if query.get("msg") == "reason":
            parts.append('<p class="flash">Причина записана, учтём в настройке радара.</p>')
        if query.get("msg") == "watch":
            note = f"Добавлено компаний: {esc(query.get('added', '0'))}."
            if query.get("added", "0") != "0":
                note += " Данные подтягиваются — обновите страницу через минуту."
            if query.get("bad", "0") != "0":
                note += f" Не похоже на ИНН юрлица: {esc(query['bad'])}."
            if query.get("full"):
                note += " Часть не добавлена: уже в списке или достигнут лимит."
            parts.append(f'<p class="flash">{note}</p>')

        ask = query.get("ask")
        if ask in SIGNALS:
            parts.append(
                f'<form class="box ask" method="post" action="/k/{token}/reason">'
                f'<p>Третий раз «мимо» по типу «{esc(SIGNALS[ask].title)}». Что не так: размер компаний, '
                f'отрасль, сам повод? Название компании писать не нужно.</p>'
                f'<input type="hidden" name="type" value="{esc(ask)}">'
                f'<input type="hidden" name="day" value="{day.isoformat()}">'
                f'<label for="reason" class="muted">Причина</label>'
                f'<textarea id="reason" name="text" maxlength="1000" required></textarea>'
                f'<div class="row"><button type="submit">Отправить</button></div></form>')

        pending = db.pending_outcomes(km.km_id, today, FOLLOWUP_DAYS)
        if pending:
            parts.append("<h2>Чем закончилось?</h2>")
            for r in pending:
                name = parse_card(r["text"])["name"]
                buttons = "".join(
                    f'<button type="submit" name="value" value="{k}">{esc(v)}</button>'
                    for k, v in OUTCOME_VALUES.items())
                parts.append(
                    f'<form class="box" method="post" action="/k/{token}/out">'
                    f'<p>Вы связались с компанией {esc(name)} '
                    f'({ddmm(date.fromisoformat(r["created"]))}).</p>'
                    f'<input type="hidden" name="card" value="{r["id"]}">'
                    f'<input type="hidden" name="day" value="{day.isoformat()}">'
                    f'<div class="row">{buttons}</div></form>')

        for text in db.cautions(km.km_id, day):
            parts.append(f'<p class="caution">{esc(text)}</p>')

        cards = db.cards_for_day(km.km_id, day)
        if not cards:
            parts.append('<p class="box muted">На этот день сильных поводов нет.</p>')
        for r in cards:
            parts.append(self.card_html(r, token, day))
        parts.append(self.watch_html(db, km, token, day))
        parts.append('<p class="muted">Клиент это или нет — проверьте у себя в CRM. '
                     'Радар этого не знает и не должен знать.</p>')
        return Response(200, page(f"Радар · {ddmm(day)}", "".join(parts)),
                        {"Content-Type": "text/html; charset=utf-8"})

    def watch_html(self, db: Storage, km: KMProfile, token: str, day: date) -> str:
        from .cards import describe
        from .watch import HISTORY_DAYS, WATCH_LIMIT
        watched = db.watch_list(km.km_id)
        companies = db.companies()
        since = self.today() - timedelta(days=HISTORY_DAYS)
        out = [f'<h2 id="my">Мои компании <span class="muted">{len(watched)} из {WATCH_LIMIT}</span></h2>',
               f'<form class="box" method="post" action="/k/{token}/watch">'
               f'<p class="caution"><b>Только компании, которые ещё не клиенты банка.</b> '
               f'ИНН клиента сюда не вводите: это раскрывает факт обслуживания в банке.</p>'
               f'<label for="inns" class="muted">ИНН компаний — через пробел, запятую или с новой строки</label>'
               f'<textarea id="inns" name="inns" maxlength="2000" placeholder="например: 1424008710"></textarea>'
               f'<input type="hidden" name="day" value="{day.isoformat()}">'
               f'<div class="row"><button type="submit">Следить</button></div></form>']
        for inn in watched:
            c = companies.get(inn)
            events = db.company_events(inn, since) if c else []
            title = esc(c.name) if c else f"ИНН {inn}"
            meta = (f"ИНН {inn} · регион {esc(c.region)} · ОКВЭД {esc(c.okved)}" if c
                    else "данные загружаются — обновите страницу через минуту (или придут утром, если исчерпан лимит Checko)")
            profile = self.profile_line(db, inn) if c else ""
            if profile:
                meta += f"<br>{profile}"
            lines = "".join(f"<li>{linkify(describe(e))}</li>" for e in events[:8]) if events else (
                "<li class='muted'>За полгода событий нет.</li>" if c else "")
            out.append(
                f'<div class="box"><div class="head"><div><div class="name">{title}</div>'
                f'<div class="meta">{meta}</div></div>'
                f'<form method="post" action="/k/{token}/unwatch"><input type="hidden" name="inn" value="{inn}">'
                f'<input type="hidden" name="day" value="{day.isoformat()}">'
                f'<button type="submit" class="small">Убрать</button></form></div>'
                + (f'<ul class="hist">{lines}</ul>' if lines else "") + '</div>')
        return "".join(out)

    def profile_line(self, db: Storage, inn: str) -> str:
        """Короткая справка из последнего снимка: чтобы пустая история не выглядела поломкой."""
        from .cards import money
        from .diff import MSP_TITLE
        s = db.last_snapshot(inn, self.today() + timedelta(days=1))
        if not s:
            return ""
        bits = []
        rev = s.get("revenue") or {}
        if rev:
            year = max(rev, key=int)
            bits.append(f"выручка {year}: {money(rev[year])}")
        d = s.get("director") or {}
        if d.get("name"):
            since = d.get("since")
            since_txt = f" с {since[8:10]}.{since[5:7]}.{since[:4]}" if since and len(since) >= 10 else ""
            bits.append(f"руководитель {esc(d['name'])}{since_txt}")
        if s.get("msp_category") and s["msp_category"] != "none":
            bits.append(f"МСП: {MSP_TITLE.get(s['msp_category'], s['msp_category'])}")
        names = {"company": "ЕГРЮЛ", "fedresurs": "Федресурс", "finances": "отчётность",
                 "enforcements": "ФССП", "bankruptcy-messages": "ЕФРСБ"}
        checked = [names[x] for x in s.get("_sources", []) if x in names]
        if checked:
            bits.append("проверено: " + ", ".join(checked) + ", госконтракты")
        return " · ".join(bits)

    def card_html(self, r, token: str, day: date) -> str:
        c = parse_card(r["text"])
        secs = "".join(
            f'<div class="sec{" phrase" if title == "Первая фраза" else ""}"><h3>{esc(title)}</h3>'
            f'<p>{esc(body)}</p></div>' for title, body in c["sections"])
        buttons = "".join(
            f'<button type="submit" name="value" value="{k}" aria-pressed="{str(r["feedback"] == k).lower()}">'
            f'{esc(v)}</button>' for k, v in FEEDBACK_VALUES.items())
        return (f'<article class="box" id="c{r["id"]}">'
                f'<div class="head"><span class="badge {c["kind"]}">{esc(c["badge"])}</span>'
                f'<span class="score">score {r["score"]:.2f}</span></div>'
                f'<div><div class="name">{esc(c["name"])}</div><div class="meta">{esc(c["meta"])}</div></div>'
                f'{secs}<div class="src">{linkify(c["source"])}</div>'
                f'<form class="btns" method="post" action="/k/{token}/fb">'
                f'<input type="hidden" name="card" value="{r["id"]}">'
                f'<input type="hidden" name="day" value="{day.isoformat()}">{buttons}</form></article>')


def make_handler(app: App) -> type[BaseHTTPRequestHandler]:  # pragma: no cover — тонкая обёртка
    class Handler(BaseHTTPRequestHandler):
        server_version = "kmradar"
        sys_version = ""

        def _send(self, resp: Response) -> None:
            data = resp.body.encode("utf-8")
            self.send_response(resp.status)
            for k, v in {**SECURITY_HEADERS, **resp.headers}.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            self._send(app.handle("GET", self.path))

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._send(Response(413, "Слишком большой запрос"))
                return
            self._send(app.handle("POST", self.path, self.rfile.read(length)))

        def log_message(self, fmt: str, *args) -> None:
            # токен КМ в журнал не пишем
            msg = fmt % args
            print(re.sub(r"/([ka])/[A-Za-z0-9_-]+", r"/\1/***", msg), flush=True)

    return Handler


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8080) -> None:  # pragma: no cover
    import os
    from pathlib import Path
    Storage(db_path).close()  # создать схему до первого запроса
    refresher = None
    if os.environ.get("CHECKO_API_KEY"):
        from .checko import CheckoClient, CheckoSource
        from .watch import HISTORY_DAYS, Refresher
        cache = Path(db_path).resolve().parent / "checko-cache"
        refresher = Refresher(db_path, lambda: CheckoSource(
            CheckoClient(os.environ["CHECKO_API_KEY"]), [], cache, history_days=HISTORY_DAYS))
    lookup = None
    if os.environ.get("CHECKO_API_KEY"):
        from .checko import CheckoClient, CheckoError, map_company

        def lookup(inn: str) -> dict | None:
            try:
                resp = CheckoClient(os.environ["CHECKO_API_KEY"]).get("company", inn=inn)
            except CheckoError:
                return None
            return map_company(resp) if (resp.get("data") or {}).get("ИНН") else None
    server = ThreadingHTTPServer((host, port), make_handler(App(db_path, refresher=refresher, lookup=lookup)))
    print(f"kmradar web: http://{host}:{port}", flush=True)
    server.serve_forever()
