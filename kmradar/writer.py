"""Текстовая часть карточки: «Зачем звонить», «Первая фраза», «Спросить».

LLM пишет только эти три поля. Факты (цифры, даты, номера) в карточку
подставляются шаблоном из данных и LLM не передаются на переписывание.
Любой ответ LLM проверяется правилами; если не прошёл — берём шаблон.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Protocol

from .config import PRODUCTS, SIGNALS
from .models import Event, KMProfile

TEMPLATES: dict[str, dict[str, str]] = {
    "tender_win": {
        "why": "До подписания контракта нужно внести обеспечение исполнения. Скорее всего, "
               "ищут гарантию прямо сейчас; под контракт может понадобиться оборотка.",
        "phrase": "Видел, что вы выиграли контракт — поздравляю. Обеспечение исполнения уже "
                  "решили, или можем быстро предложить гарантию?",
        "ask": "Как планируете финансировать закупку материалов до первой оплаты по контракту?",
    },
    "contract_signed": {
        "why": "Компания заключила госконтракт и начинает исполнение. Скорее всего, понадобятся "
               "оборотные средства до первой оплаты; может понадобиться гарантия на аванс.",
        "phrase": "Видел, что вы заключили госконтракт — поздравляю. Как планируете финансировать "
                  "исполнение до первой оплаты?",
        "ask": "Заказчик платит авансом или по факту этапов?",
    },
    "news_mention": {
        "why": "О компании написали в новостях. Возможно, за этим стоят изменения в бизнесе — "
               "хороший повод узнать, нужна ли поддержка банка.",
        "phrase": "Увидел новость о вашей компании — хотел узнать, как продвигается этот проект.",
        "ask": "Что сейчас главное в планах компании на ближайшие месяцы?",
    },
    "leasing": {
        "why": "Компания вкладывается в технику и оборудование. Возможно, выбирает, "
               "как финансировать следующие покупки.",
        "phrase": "Увидел, что вы обновляете технику. Как сейчас финансируете такие покупки?",
        "ask": "Какие ещё закупки техники или оборудования планируете в этом году?",
    },
    "director_change": {
        "why": "Новый руководитель часто пересматривает банки и условия обслуживания. "
               "Скорее всего, сейчас удачный момент познакомиться.",
        "phrase": "Увидел, что в компании новый руководитель — хотел познакомиться "
                  "и поздравить с назначением.",
        "ask": "Что для вас сейчас важнее всего в работе с банками?",
    },
    "founder_change": {
        "why": "Смена собственников или рост капитала может означать сделку, инвестиции "
               "или реструктуризацию.",
        "phrase": "Заметил изменения в составе собственников — похоже, у компании новый этап.",
        "ask": "Планируете сделки или инвестиции, под которые может понадобиться финансирование?",
    },
    "expansion": {
        "why": "Компания расширяется: новые направления или регионы. Скорее всего, "
               "понадобятся расчёты и финансирование на новом месте.",
        "phrase": "Вижу, что вы открываете новое направление — интересно, куда растёте.",
        "ask": "Как планируете организовать расчёты и финансирование на новом направлении?",
    },
    "msp_upgrade": {
        "why": "Компания выросла из своей категории МСП. Возможно, продукты для малого "
               "бизнеса ей уже тесны.",
        "phrase": "Заметил, что компания перешла в новую категорию МСП — поздравляю с ростом.",
        "ask": "Хватает ли текущих лимитов и условий под новые объёмы?",
    },
    "revenue_growth": {
        "why": "Выручка заметно выросла. Скорее всего, растёт и потребность в оборотных средствах.",
        "phrase": "Посмотрел свежую отчётность — у вас сильный рост, поздравляю.",
        "ask": "Как финансируете рост оборотки — свои средства, кредиты, отсрочки поставщиков?",
    },
}

GENERIC = {
    "why": "У компании произошли изменения. Возможно, это повод узнать, нужна ли поддержка банка.",
    "phrase": "Увидел новости о вашей компании — хотел узнать, как у вас дела.",
    "ask": "Что сейчас главное в планах компании на ближайшие месяцы?",
}

HEDGES = ("скорее всего", "может", "возможно", "вероятно", "похоже")
PRODUCT_WORDS = ("гарант", "кредит", "лизинг", "факторинг", "рко", "эквайринг",
                 "депозит", "вклад", "страхов", "зарплатн", "овердрафт")
FIELDS = ("why", "phrase", "ask")


def first_sentence(text: str) -> str:
    return re.split(r"(?<=[.!?…])\s+", text.strip(), maxsplit=1)[0]


def validate(texts: dict[str, str]) -> list[str]:
    """Правила карточки. Пустой список — текст годится."""
    problems = []
    for key in FIELDS:
        value = (texts.get(key) or "").strip()
        if not value:
            problems.append(f"{key}: пусто")
        elif re.search(r"\d", value):
            problems.append(f"{key}: цифры только из фактов, LLM их не пишет")
        elif len(value) > 400:
            problems.append(f"{key}: слишком длинно")
    why = (texts.get("why") or "").lower()
    if why and not any(h in why for h in HEDGES):
        problems.append("why: гипотеза должна быть помечена как гипотеза")
    phrase = first_sentence(texts.get("phrase") or "").lower()
    if any(w in phrase for w in PRODUCT_WORDS):
        problems.append("phrase: продукт в первом предложении")
    return problems


class TextWriter(Protocol):
    def write(self, primary: Event, facts_text: str, km: KMProfile) -> dict[str, str]: ...


class TemplateWriter:
    """Без LLM: шаблоны по типу события. Так работает пилот на старте."""

    def write(self, primary: Event, facts_text: str, km: KMProfile) -> dict[str, str]:
        return dict(TEMPLATES.get(primary.type, GENERIC))


PROMPT = """Ты помогаешь клиентскому менеджеру банка (средний бизнес) позвонить компании по поводу.
Событие: {signal}.
Проверенные факты (не пересказывай их, не добавляй новых):
{facts}
Продукты, которые продаёт КМ: {products}.

Верни JSON с тремя полями:
"why" — 1–2 предложения, зачем звонить. Это гипотеза: пиши «скорее всего», «может понадобиться». Ничего не утверждай о намерениях компании.
"phrase" — первая фраза для звонка, разговорная, её можно сказать вслух. В первом предложении нет названия продукта.
"ask" — один открытый вопрос клиенту.
Не используй цифры, даты, суммы и названия — они уже есть в карточке."""


class LLMWriter:
    """Любая LLM через функцию complete(prompt) -> str (например, GigaChat API).

    Данные публичные, но правила те же, что и внутри банка: цифры и факты
    LLM не пишет, ответ проверяется, при ошибке — шаблон.
    """

    def __init__(self, complete: Callable[[str], str], fallback: TextWriter | None = None):
        self.complete = complete
        self.fallback = fallback or TemplateWriter()
        self.rejected: list[tuple[str, list[str]]] = []

    def write(self, primary: Event, facts_text: str, km: KMProfile) -> dict[str, str]:
        prompt = PROMPT.format(
            signal=SIGNALS[primary.type].title,
            facts=facts_text,
            products=", ".join(PRODUCTS[p] for p in km.products if p in PRODUCTS) or "любые",
        )
        try:
            raw = self.complete(prompt)
            match = re.search(r"\{.*\}", raw, re.S)
            texts = json.loads(match.group(0) if match else raw)
            texts = {k: str(texts.get(k, "")).strip() for k in FIELDS}
        except Exception as exc:  # noqa: BLE001 — любая ошибка LLM → шаблон
            self.rejected.append((primary.inn, [f"ошибка LLM: {exc}"]))
            return self.fallback.write(primary, facts_text, km)
        problems = validate(texts)
        if problems:
            self.rejected.append((primary.inn, problems))
            return self.fallback.write(primary, facts_text, km)
        return texts
