"""Справочники и настройки MVP: типы событий, продукты, веса и лимиты.

Все числа здесь — стартовые правила из документа MVP. Их меняют руками,
ML в MVP нет: КМ должен понимать, почему карточка наверху.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Signal:
    code: str
    title: str
    source: str
    weight: float          # W — вес события
    window_days: int       # окно, за которое F падает с 1 до 0
    products: tuple[str, ...]
    urgent: bool = False   # срочная карточка (сверх дневного лимита)
    stop: bool = False     # стоп-сигнал: подавляет карточки по компании


SIGNALS: dict[str, Signal] = {s.code: s for s in [
    Signal("tender_win", "Победа в закупке", "ЕИС", 1.0, 7,
           ("guarantee", "contract_loan"), urgent=True),
    Signal("leasing", "Лизинг / залог имущества", "Федресурс", 0.6, 30,
           ("loan", "leasing", "insurance")),
    Signal("director_change", "Смена руководителя", "ЕГРЮЛ", 0.6, 30,
           ("rko", "service")),
    Signal("founder_change", "Смена учредителей / капитала", "ЕГРЮЛ", 0.5, 30,
           ("ma", "loan", "deposits")),
    Signal("expansion", "Расширение: ОКВЭД, филиал, регион", "ЕГРЮЛ", 0.5, 45,
           ("rko", "acquiring", "loan")),
    Signal("msp_upgrade", "Переход в категорию МСП выше", "Реестр МСП", 0.4, 60,
           ("credit_line", "salary")),
    Signal("revenue_growth", "Рост выручки > 30%", "ГИР БО", 0.4, 90,
           ("credit_line", "factoring")),
    Signal("stop", "Стоп-сигнал: ликвидация, банкротство, долги", "Федресурс, ФССП",
           0.0, 90, (), stop=True),
]}

PRODUCTS: dict[str, str] = {
    "guarantee": "Банковская гарантия",
    "contract_loan": "Кредит на исполнение контракта",
    "loan": "Кредит",
    "leasing": "Лизинг партнёра",
    "insurance": "Страхование",
    "rko": "РКО",
    "service": "Обслуживание / знакомство",
    "ma": "Финансирование сделок",
    "deposits": "Депозиты",
    "acquiring": "Эквайринг",
    "credit_line": "Кредитная линия",
    "factoring": "Факторинг",
    "salary": "Зарплатный проект",
}


@dataclass(frozen=True)
class Limits:
    max_cards: int = 7              # обычных карточек в день
    max_urgent: int = 3             # срочные — сверх лимита, но не больше
    company_cooldown_days: int = 7  # одна компания — одна карточка в неделю
    min_strong_cards: int = 3       # меньше 3 сильных два дня подряд — пропуск
    score_threshold: float = 0.25
    stop_memory_days: int = 90      # сколько помним стоп-сигнал по компании


@dataclass(frozen=True)
class ScoringConfig:
    size_cap: float = 2.0           # потолок S для закупок
    size_floor: float = 0.5         # пол S, чтобы мелкий контракт не обнулял score
    product_match: float = 1.0
    product_miss: float = 0.5
    default_median_contract: int = 50_000_000
    feedback_step: float = 0.1      # шаг личного веса КМ за «полезно» / «мимо»
    feedback_min: float = 0.5
    feedback_max: float = 1.5


@dataclass(frozen=True)
class DiffConfig:
    revenue_growth_pct: float = 30.0
    min_contract_amount: int = 10_000_000  # N для сигнала 1
    enforcement_stop_amount: int = 5_000_000


LIMITS = Limits()
SCORING = ScoringConfig()
DIFF = DiffConfig()

FEEDBACK_VALUES = {
    "useful": "Полезно",
    "miss": "Мимо",
    "knew": "Уже знал",
    "contacted": "Связался",
}
OUTCOME_VALUES = {
    "meeting": "Встреча",
    "application": "Заявка",
    "refusal": "Отказ",
    "nothing": "Ничего",
}
FOLLOWUP_DAYS = 7
MISS_STREAK_ASK = 3
