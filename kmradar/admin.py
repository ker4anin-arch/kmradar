"""Админ-страница аналитика: «Добавить повод» руками (режим Wizard of Oz).

Аналитик находит настоящий повод (например, итоги закупки на zakupki.gov.ru),
вносит его формой — радар делает карточку у подходящих КМ теми же правилами,
что и для автоматических событий. Доступ — по отдельной админ-ссылке /a/<токен>.
"""
from __future__ import annotations

import html
import re
from datetime import date, datetime
from typing import Callable

from .cards import describe
from .config import SIGNALS
from .models import Company, Event
from .pipeline import make_digests
from .storage import Storage
from .watch import valid_inn

KINDS = {
    "tender_win": "Победа в закупке (протокол итогов)",
    "contract_signed": "Заключён госконтракт",
    "director_change": "Смена руководителя",
    "leasing": "Новый договор лизинга",
    "news_mention": "Новость о компании",
    "stop": "Стоп-сигнал: банкротство, ликвидация",
}
DEFAULT_SOURCE = {"tender_win": "ЕИС", "contract_signed": "ЕИС", "director_change": "ЕГРЮЛ",
                  "leasing": "Федресурс", "news_mention": "СМИ", "stop": "Федресурс"}


def esc(v: object) -> str:
    return html.escape(str(v), quote=True)


def parse_amount(text: str) -> int | None:
    """'186 000 000', '186 млн', '1,2 млрд', '186000000 ₽' → рубли."""
    t = (text or "").lower().replace(" ", " ").replace("₽", "").replace("руб", "").strip()
    if not t:
        return None
    m = re.match(r"^([\d\s]+(?:[.,]\d+)?)\s*(млрд|млн|тыс)?", t)
    if not m:
        return None
    num = float(m.group(1).replace(" ", "").replace(",", "."))
    mult = {"млрд": 1e9, "млн": 1e6, "тыс": 1e3}.get(m.group(2) or "", 1)
    return int(round(num * mult))


def build_event(form: dict[str, str], today: date) -> tuple[Event | None, str]:
    """Поля формы → событие. Возвращает (событие, ошибка)."""
    inn = form.get("inn", "").strip()
    kind = form.get("kind", "")
    if not valid_inn(inn):
        return None, "ИНН не похож на ИНН юрлица (10 цифр с верной контрольной цифрой)."
    if kind not in KINDS:
        return None, "Выберите тип повода."
    try:
        when = date.fromisoformat(form.get("date") or today.isoformat())
    except ValueError:
        return None, "Дата в формате ГГГГ-ММ-ДД."
    if when > today:
        return None, "Дата повода не может быть в будущем."
    subject = form.get("subject", "").strip()[:300]
    customer = form.get("customer", "").strip()[:200]
    url = form.get("url", "").strip()[:500]
    if not url:
        return None, "Укажите ссылку на источник: каждый факт в карточке должен вести к источнику."
    if not re.match(r"^https?://", url):
        return None, "Ссылка должна начинаться с http:// или https://"
    facts: dict = {"manual": True}
    if kind in ("tender_win", "contract_signed"):
        amount = parse_amount(form.get("amount", ""))
        if not amount:
            return None, "Укажите сумму контракта, например «186 млн»."
        if not subject:
            return None, "Укажите предмет закупки."
        facts.update({k: v for k, v in {
            "amount": amount, "subject": subject, "customer": customer,
            "number": form.get("number", "").strip()[:40], "law": form.get("law", "")}.items() if v})
    elif kind == "director_change":
        new = form.get("new", "").strip()[:200]
        if not new:
            return None, "Укажите нового руководителя."
        facts.update({"new": new, "position": "Генеральный директор"})
        if form.get("old", "").strip():
            facts["old"] = form["old"].strip()[:200]
    elif kind == "leasing":
        facts.update({"kind": "лизинг", "subject": subject or None, "counterparty": customer or None})
    elif kind == "news_mention":
        if not subject:
            return None, "Для новости нужен заголовок (в поле «Предмет»)."
        facts.update({"title": subject, "source": form.get("source", "").strip() or "СМИ"})
    elif kind == "stop":
        facts["reason"] = subject or "банкротство"
    source = form.get("source", "").strip()[:60] or DEFAULT_SOURCE[kind]
    return Event(inn, kind, when, facts, source, url or None), ""


def add_event(db: Storage, form: dict[str, str], km_ids: list[str], today: date, now: datetime,
              lookup: Callable[[str], dict | None] | None = None) -> str:
    """Записать повод и разложить карточки по КМ. Возвращает сообщение для страницы."""
    event, error = build_event(form, today)
    if error:
        return "Ошибка: " + error
    companies = db.companies()
    if event.inn not in companies:
        name = form.get("company_name", "").strip()
        state = lookup(event.inn) if (lookup and not name) else None
        if state:
            db.save_company(Company.from_state(state))
        elif name:
            db.save_company(Company(event.inn, name[:200], form.get("company_region", "").strip()[:3],
                                    form.get("company_okved", "").strip()[:10]))
        else:
            return ("Ошибка: компании нет в базе, а Checko недоступен или не нашёл её. "
                    "Впишите название компании (и по возможности регион и ОКВЭД).")
    # выбранным КМ компания попадает в территорию, даже если не подходит под профиль
    for km_id in km_ids:
        if db.km(km_id):
            db.add_watch(km_id, [event.inn], 10_000, now)
    db.save_events([event], today)
    digests = make_digests(db, today, skip_weak=False)
    got = [km for km, d in digests.items() if any(c.inn == event.inn for c in d.cards)]
    name = db.companies()[event.inn].name
    if got:
        return f"Повод добавлен: {name}. Карточка у КМ: {', '.join(got)}."
    return (f"Повод записан: {name}, но карточки нет ни у одного КМ. Возможные причины: компания "
            f"не подходит под профили (отметьте КМ галочкой), по ней уже была карточка на этой неделе, "
            f"повод старый или слабый (ниже порога).")


def page_body(db: Storage, token: str, msg: str = "", today: date | None = None) -> str:
    today = today or date.today()
    kms = db.kms()
    kind_opts = "".join(f'<option value="{k}">{esc(v)}</option>' for k, v in KINDS.items())
    km_boxes = "".join(
        f'<label class="row"><input type="checkbox" name="km" value="{esc(k.km_id)}"> {esc(k.name)}</label>'
        for k in kms) or '<p class="muted">КМ ещё не загружены (команда add-km).</p>'
    recent = db.manual_events()
    names = db.companies()
    recent_html = "".join(
        f"<li><b>{esc(names[e.inn].name if e.inn in names else e.inn)}</b> — {esc(describe(e))}</li>"
        for e in recent) or "<li class='muted'>Пока ничего не добавлено.</li>"

    def field(name: str, label: str, placeholder: str = "", type_: str = "text", value: str = "") -> str:
        return (f'<label for="{name}" class="muted">{label}</label>'
                f'<input id="{name}" name="{name}" type="{type_}" placeholder="{esc(placeholder)}" value="{esc(value)}">')

    flash = f'<p class="flash">{esc(msg)}</p>' if msg else ""
    return (
        "<h1>Радар · добавить повод</h1>"
        "<p class='muted'>Для аналитика. Найдите настоящий повод (например, итоги закупки на "
        "zakupki.gov.ru) и внесите его: карточка уйдёт подходящим КМ. Только факты из источника, "
        "без догадок — текст «зачем звонить» радар подставит сам.</p>"
        f"{flash}"
        f'<form class="box" method="post" action="/a/{token}/event">'
        + field("inn", "ИНН компании *", "10 цифр")
        + f'<label for="kind" class="muted">Тип повода *</label><select id="kind" name="kind">{kind_opts}</select>'
        + field("date", "Дата события (протокола, записи, публикации)", "", "date", today.isoformat())
        + field("amount", "Сумма контракта — для закупок", "например: 186 млн")
        + field("subject", "Предмет закупки / предмет лизинга / заголовок новости / причина стоп-сигнала")
        + field("customer", "Заказчик (закупки) или лизингодатель")
        + field("number", "Номер закупки / контракта в ЕИС")
        + '<label for="law" class="muted">Закон</label><select id="law" name="law">'
          '<option value="44">44-ФЗ</option><option value="223">223-ФЗ</option><option value="">—</option></select>'
        + field("old", "Прежний руководитель — для смены руководителя")
        + field("new", "Новый руководитель — для смены руководителя")
        + field("url", "Ссылка на источник *", "https://zakupki.gov.ru/…")
        + field("source", "Название источника", "ЕИС, Федресурс, название СМИ…")
        + "<details><summary class='muted'>Если компании нет в базе и Checko не найдёт её</summary>"
        + field("company_name", "Название компании", "ООО «…»")
        + field("company_region", "Код региона", "69")
        + field("company_okved", "Основной ОКВЭД", "42.11")
        + "</details>"
        + "<p class='muted'>Кому показать. Без галочек — всем КМ, под чей профиль подходит компания. "
          "С галочкой — этому КМ в любом случае.</p>" + km_boxes
        + '<div class="row"><button type="submit">Добавить повод</button></div></form>'
        + f"<h2>Последние добавленные</h2><ul class='hist box'>{recent_html}</ul>"
    )


ADMIN_CSS = """
input, select { width: 100%; font: inherit; padding: 8px; border-radius: 9px; border: 1px solid var(--line);
  background: var(--bg); color: var(--ink); }
input[type=checkbox] { width: auto; }
label.row { display: flex; gap: 8px; align-items: center; }
details { display: grid; gap: 6px; }
summary { cursor: pointer; }
"""
