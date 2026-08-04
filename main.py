# -*- coding: utf-8 -*-
"""BibiBike Bot — единый структурированный файл приложения.

КАРТА ФАЙЛА — ищи метку через Ctrl+F:

    [01-CONFIG]          токен, домен, города, группы, темы и роли
    [02-RUNTIME]         логирование, Telegram Bot и общие помощники
    [03-DATABASE]        схема БД, миграции и подключение
    [04-USERS-SHIFTS]    пользователи, смены, декады и действия
    [05-PARSER]          основной парсер и специальные правила городов
    [06-MANUAL-SHIFTS]   ручные сообщения о начале/окончании смены
    [07-REPORTS]         живые Telegram-отчёты
    [08-TELEGRAM]        фильтры и обработчики сообщений Telegram
    [09-WEB-COMMON]      авторизация и общие функции Mini App API
    [10-WEB-EMPLOYEE]    API профиля, смен и истории сотрудника
    [11-METRICS]         КПД, месячные итоги и фоновые задачи
    [12-ADMIN]           авторизация и API админки
    [13-HTTP]            маршруты HTTP, index.html и health-check
    [14-STARTUP]         запуск API, фоновых задач и polling

Быстрые изменения:
    • новая группа/тема города — [01-CONFIG];
    • новое слово или формат действия — [05-PARSER];
    • вид Telegram-отчёта — [07-REPORTS];
    • операция Mini App — [10-WEB-EMPLOYEE] или [12-ADMIN].

Логика сохранена совместимой с существующей bibibike_work.db.
"""

import asyncio
import logging
import re
import os
import sys
import json
import hmac
import hashlib
import base64
import html
import math
import uuid
import time
import shutil
import aiosqlite
from datetime import datetime, timezone, timedelta
from urllib.parse import parse_qsl
from aiohttp import web
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import BaseFilter, Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, FSInputFile
from aiogram.exceptions import TelegramBadRequest

# Необязательно: подхватываем .env, если он есть (на BotHost переменные и так заданы).
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ============================================================================
# [01-CONFIG] КОНФИГУРАЦИЯ: БОТ, ГОРОДА, ГРУППЫ, ТЕМЫ, ОПЛАТА И MINI APP
# ============================================================================
# Токен берём из переменных окружения. Разные хостинги называют её по-разному:
# BotHost отдаёт TOKEN / API_TOKEN / TELEGRAM_BOT_TOKEN, где-то это BOT_TOKEN.
BOT_TOKEN = (
    os.getenv("BOT_TOKEN")
    or os.getenv("TELEGRAM_BOT_TOKEN")
    or os.getenv("API_TOKEN")
    or os.getenv("TOKEN")
)

GROUP_ID = -1003431950710   # Логистика Краснодар (t.me/c/3431950710)
CHAT1_THREAD_ID = 1        # Тех. Задания (рабочий)
CHAT2_THREAD_ID = 3        # ОТЧЕТЫ

# === Тема NPB (замены АКБ) ===
NPB_THREAD_ID = 866        # тема «NPB»

# Мультигород хранится в таблице cities. Эта запись нужна для бесшовной
# миграции существующей базы Краснодара. Дополнительные города задаются одной
# JSON-переменной CITIES_CONFIG_JSON, после запуска они также сохраняются в БД:
# [{"key":"stavropol","name":"Ставрополь","group_id":-100..., 
#   "topic_tasks":1,"topic_npb":2,"topic_reports":3,"timezone_offset":3}]
DEFAULT_CITY_KEY = "krasnodar"
DEFAULT_CITY_NAME = "Краснодар"
CITIES_CONFIG_JSON = os.getenv("CITIES_CONFIG_JSON", "").strip()

# ============================================================
# ГОРОДА TELEGRAM
# ============================================================
# NO_TOPIC означает, что отдельной темы у города нет. При отправке отчёта
# такой ID не передаётся Telegram: сообщение публикуется в общем чате.
NO_TOPIC = -1
# GENERAL_TOPIC — сообщения общего раздела форума: Bot API присылает для них
# message_thread_id=None. В базе храним 0, чтобы отличать от «темы нет совсем».
GENERAL_TOPIC = 0

# --- Ставрополь: общая группа отчётов + отдельные рабочие группы по ролям ---
# Общая группа отчётов (ранее: web.telegram.org/k/#-4456873256).
STAVROPOL_GROUP_ID      = -1004456873256
STAVROPOL_TOPIC_TASKS   = NO_TOPIC
STAVROPOL_TOPIC_NPB     = NO_TOPIC
STAVROPOL_TOPIC_REPORTS = NO_TOPIC

# Скауты: t.me/c/3930962000/3 — обычный парсер по словам, как в Краснодаре.
STAVROPOL_SCOUTS_GROUP_ID    = -1003930962000
STAVROPOL_SCOUTS_TOPIC_WORK  = 3

# Водители и чарджеры находятся в одной форумной группе t.me/c/3944046511.
# Тема 2 — действия водителей по словам; тема 4 — голые номера замен АКБ.
STAVROPOL_TRANSPORT_GROUP_ID     = -1003944046511
STAVROPOL_DRIVERS_TOPIC_WORK     = 2
STAVROPOL_CHARGERS_TOPIC_BATTERY = 4

# --- Красная Поляна: одна группа с отдельными темами ---
POLYANA_GROUP_ID        = -1002866630249   # t.me/c/2866630249
POLYANA_TOPIC_TASKS     = GENERAL_TOPIC    # рабочий раздел приходит как thread_id=None
POLYANA_TOPIC_NPB       = 3128             # тема чарджеров / замены АКБ
POLYANA_TOPIC_REPORTS   = 3127             # тема отчётов

# --- Химки: ОДИН город, но у каждой роли своя телеграм-группа ---
# В приложении сотрудник выбирает просто «Химки», а роль решает, в какую
# группу уйдёт его смена. Для базы это ОДИН город (один city_id), поэтому
# админка, история, КПД и месячные итоги видят весь город целиком —
# и скаутов, и водителей — без каких-либо изменений в запросах.
#
# Скауты Химки (t.me/c/3951407451)
KHIMKI_SCOUTS_GROUP_ID       = -1003951407451
KHIMKI_SCOUTS_TOPIC_MOVES    = 2            # «Перемещения»: 4-значные номера = перемещения
KHIMKI_SCOUTS_TOPIC_REPORTS  = 3            # «Отчёты»: начало и конец смены
KHIMKI_SCOUTS_TOPIC_REPAIR   = 4485         # «Ремонт»: 4-значные номера с любым текстом
KHIMKI_SCOUTS_TOPIC_STICKER  = 2290         # «Оклейка»: номер + слово об оклейке

# Водители Химки (t.me/c/4375614106)
KHIMKI_DRIVERS_GROUP_ID      = -1004375614106
KHIMKI_DRIVERS_TOPIC_MOVES   = 11           # «Подвозы»: 4-значные номера = перемещения
KHIMKI_DRIVERS_TOPIC_REPORTS = 2            # «Отчёты»: начало и конец смены
KHIMKI_DRIVERS_TOPIC_REPAIR  = 365          # «Ремонт»: обычный парсер по словам

# Чарджеры Химки (t.me/c/4390770669). Бот слушает только тему 4
# «Пустые АКБ»: любые 4-значные номера считаются заменами АКБ. Тема 3
# используется для начала/окончания смены и живых отчётов.
KHIMKI_CHARGERS_GROUP_ID      = -1004390770669
KHIMKI_CHARGERS_TOPIC_BATTERY = 4
KHIMKI_CHARGERS_TOPIC_REPORTS = 3

# Дополнительные рабочие темы, которые бот слушает сверх основных.
# Тип парсера каждой темы выбирается отдельными таблицами ниже.
# Формат: {chat_id группы: (id темы, ...)}. Добавить тему — одна цифра сюда.
EXTRA_WORK_TOPICS = {
    KHIMKI_SCOUTS_GROUP_ID: (
        KHIMKI_SCOUTS_TOPIC_REPAIR,
        KHIMKI_SCOUTS_TOPIC_STICKER,
    ),
    KHIMKI_DRIVERS_GROUP_ID: (KHIMKI_DRIVERS_TOPIC_REPAIR,),
}

# Темы, где сообщение = список номеров + подпись словом про поломку.
# Формат: {chat_id группы: (id темы, ...)}.
REPAIR_TOPICS = {
    KHIMKI_DRIVERS_GROUP_ID: (KHIMKI_DRIVERS_TOPIC_REPAIR,),
}

# Тема ремонта скаутов: берём все 4-значные номера, текст описания не мешает.
BARE_REPAIR_TOPICS = {
    KHIMKI_SCOUTS_GROUP_ID: (KHIMKI_SCOUTS_TOPIC_REPAIR,),
}

# Тема оклейки скаутов: 4-значный номер + слово об оклейке.
STICKER_TOPICS = {
    KHIMKI_SCOUTS_GROUP_ID: (KHIMKI_SCOUTS_TOPIC_STICKER,),
}

# Одна Telegram-группа может обслуживать несколько ролей в разных темах.
# Здесь фиксируем ожидаемую роль для каждой рабочей темы.
WORK_TOPIC_ROLES = {
    (STAVROPOL_SCOUTS_GROUP_ID, STAVROPOL_SCOUTS_TOPIC_WORK): "Скаут",
    (STAVROPOL_TRANSPORT_GROUP_ID, STAVROPOL_DRIVERS_TOPIC_WORK): "Водитель",
    (STAVROPOL_TRANSPORT_GROUP_ID, STAVROPOL_CHARGERS_TOPIC_BATTERY): "Чарджер",
}

# === НОВОЕ: живое сообщение обновляется не чаще, чем раз в N секунд ===
DEBOUNCE_SEC = 20

# ============================================================
# === НОВОЕ: МИНИ-ПРИЛОЖЕНИЕ (ЗАРПЛАТА) =====================
# ============================================================
# BotHost передаёт порт reverse proxy через стандартную переменную PORT.
# WEB_PORT оставлен запасным вариантом для совместимости со старой настройкой.
# Значение в панели BotHost и фактически прослушиваемый порт должны совпадать.
WEBAPP_PORT = int(os.getenv("PORT") or os.getenv("WEB_PORT") or "3000")

# Имя бота (без @) и short-name Mini App из BotFather (/newapp) —
# нужны, чтобы под отчётом появилась кнопка «Моя зарплата».
# Юзернейм основного бота — для кнопок открытия приложения в группе.
BOT_USERNAME = os.getenv("BOT_USERNAME", "bbbotdelaetbot")
WEBAPP_SHORTNAME = os.getenv("WEBAPP_SHORTNAME", "zp")

# === НОВОЕ: прямой https-адрес страницы приложения (бот сам её отдаёт на BotHost).
# Нужен для web_app-кнопки, которая открывает Mini App в один тап прямо из отчёта.
# Если пусто — кнопка откатится на старую url-ссылку t.me/бот/shortname.
# Задавать ТОЛЬКО через переменную окружения, дефолт — публичный адрес бота. ===
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://bot-1784606726-6491-kponamarev.bothost.tech/")

# Домен, с которого открывается сама страница мини-приложения (GitHub Pages).
# Нужен для CORS, чтобы браузер разрешил запросы к API бота.
WEBAPP_ALLOW_ORIGIN = os.getenv("WEBAPP_ALLOW_ORIGIN", "https://voglogpro.github.io")

# === НОВОЕ: бот сам отдаёт страницу мини-приложения (index.html рядом с этим файлом) ===
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(BASE_DIR, "index.html")
CRM_INDEX_PATH = os.path.join(BASE_DIR, "crm.html")

# Краснодар = московское время (UTC+3)
MSK = timezone(timedelta(hours=3))

# Модель оплаты по умолчанию для новых сотрудников
# Метка сборки: видна в логах при старте и в мини-приложении (Настройки).
# По ней сразу понятно, какая версия реально запущена на хостинге.
BUILD_VERSION = "2026-08-04 · CRM Operations v3 + календарь + сигналы + поиск байка"

DEFAULT_PAY_TYPE = "hourly"       # hourly | salary | piece
DEFAULT_PAY_AMOUNT = 350.0        # ₽/час, ₽/смену или ₽/замену — зависит от типа

# Авто-закрытие смены: сотрудник выбирает длительность, бот добавляет фору
# (GRACE), чтобы человек успел закрыть сам/дописать комментарий до дедлайна.
AUTO_CLOSE_CHOICES = (8, 10, 12)  # часы на выбор в мини-приложении
DEFAULT_AUTO_CLOSE_HOURS = 10
AUTO_CLOSE_GRACE_MIN = 10

# Пароль админки не хранится в репозитории. Если ADMIN_PASSWORD пуст, админка
# отключена. После проверки сервер выдаёт подписанную сессию на несколько часов.
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
ADMIN_SESSION_SECRET = os.getenv("ADMIN_SESSION_SECRET", "").strip()
# CRM запоминает успешный вход на устройстве. Серверная подпись всё равно
# ограничена по времени и может быть отозвана через session_version.
ADMIN_SESSION_TTL_SEC = int(os.getenv("ADMIN_SESSION_TTL_SEC", str(30 * 24 * 60 * 60)))
INIT_DATA_MAX_AGE_SEC = int(os.getenv("INIT_DATA_MAX_AGE_SEC", str(24 * 60 * 60)))
CITY_MEMBERSHIP_TTL_SEC = int(os.getenv("CITY_MEMBERSHIP_TTL_SEC", "300"))
# Telegram ID владельцев сети. Только эти ID автоматически получают роль
# network_admin при старте. Старые самопривязанные admin_city_access намеренно
# не повышаются: доступ к CRM всегда выдаётся явно.
NETWORK_ADMIN_USER_IDS = {
    int(value.strip())
    for value in os.getenv("NETWORK_ADMIN_USER_IDS", "").split(",")
    if value.strip().isdigit()
}
CRM_MAX_RANGE_DAYS = max(1, int(os.getenv("CRM_MAX_RANGE_DAYS", "366")))
CRM_UPLOAD_MAX_FILES = max(1, min(5, int(os.getenv("CRM_UPLOAD_MAX_FILES", "5"))))
CRM_UPLOAD_MAX_BYTES = max(
    1024 * 1024,
    min(10 * 1024 * 1024, int(os.getenv("CRM_UPLOAD_MAX_BYTES", str(10 * 1024 * 1024)))),
)

def _webapp_button():
    """Кнопка под отчётом, открывающая мини-приложение прямо из группы.

    ВАЖНО: web_app-кнопки в inline-клавиатуре разрешены Telegram ТОЛЬКО в
    приватных чатах с ботом. В группе (а отчёты постятся в группу) такая кнопка
    вызывает Bad Request: BUTTON_TYPE_INVALID. Поэтому под отчётом в группе
    используем url-кнопку t.me/бот/shortname — она открывает то же Mini App
    в один тап и разрешена в группах.
    """
    if BOT_USERNAME:
        url = f"https://t.me/{BOT_USERNAME}/{WEBAPP_SHORTNAME}"
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⚡ Смена", url=url)]]
        )
    return None

# Список районов больше не ограничивает открытие смены (район — любой текст),
# оставлен для истории:
DISTRICTS = ["красная", "фмр", "юмр", "восточка", "ставрополька", "гмр"]

# ============================================================================
# [02-RUNTIME] TELEGRAM BOT, РОУТЕРЫ, ЛОГИРОВАНИЕ И ОБЩИЕ ПОМОЩНИКИ
# ============================================================================

# ИНИЦИАЛИЗАЦИЯ РОУТЕРОВ
work_router = Router()
cmd_router = Router()

# ============================================================
# ЛОГИРОВАНИЕ  (пишем в stdout, чтобы BotHost точно показывал логи)
# ============================================================
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stdout,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)
print("== BibiBike Bot: процесс стартовал, читаю настройки ==", flush=True)

# Проверка наличия токена перед запуском
if not BOT_TOKEN:
    print(
        "КРИТИЧЕСКАЯ ОШИБКА: токен бота не найден ни в одной переменной "
        "(BOT_TOKEN / TOKEN / API_TOKEN / TELEGRAM_BOT_TOKEN). "
        "Проверь переменные окружения бота.",
        flush=True,
    )
    logger.error("Токен не найден — выхожу.")
    sys.exit(1)

# === НОВОЕ: бот создаётся на уровне модуля, чтобы редактировать живое сообщение из любых функций ===
bot = Bot(token=BOT_TOKEN)

# ============================================================================
# [03-DATABASE] СХЕМА БАЗЫ, МИГРАЦИИ И КЭШ КОНФИГУРАЦИИ ГОРОДОВ
# ============================================================================
DB_PATH = os.path.join(os.getenv("DATA_DIR", BASE_DIR), "bibibike_work.db")
CRM_UPLOAD_DIR = os.path.join(os.getenv("DATA_DIR", BASE_DIR), "crm_uploads")
# База лежит в постоянной папке (на BotHost это /app/data), поэтому смены,
# зарплаты и история НЕ обнуляются при обновлении бота из GitHub.

CITIES_BY_ID = {}
CITIES_BY_GROUP = {}
# city_id -> {роль: вариант города с группой и темами этой роли}
CITY_ROLE_GROUPS = {}


class ActiveShiftExists(Exception):
    """У сотрудника уже есть активная смена в одном из городов."""


def _city_tz(city):
    """Часовой пояс города как фиксированный UTC offset (для городов РФ)."""
    try:
        offset = int((city or {}).get("timezone_offset", 3))
    except (TypeError, ValueError):
        offset = 3
    return timezone(timedelta(hours=max(-12, min(14, offset))))


# [01-CONFIG:CITY-MATRIX]
# Единая матрица городов и ролевых групп. При добавлении города сначала
# задаются его ID в [01-CONFIG], затем здесь собираются его роли и темы.
def _configured_cities():
    configs = [{
        "key": DEFAULT_CITY_KEY,
        "name": DEFAULT_CITY_NAME,
        "group_id": GROUP_ID,
        "topic_tasks": CHAT1_THREAD_ID,
        "topic_npb": NPB_THREAD_ID,
        "topic_reports": CHAT2_THREAD_ID,
        "timezone_offset": 3,
    }, {
        # Ставрополь: эта базовая группа принимает живые отчёты всех ролей.
        "key": "stavropol",
        "name": "Ставрополь",
        "group_id": STAVROPOL_GROUP_ID,
        "topic_tasks": STAVROPOL_TOPIC_TASKS,
        "topic_npb": STAVROPOL_TOPIC_NPB,
        "topic_reports": STAVROPOL_TOPIC_REPORTS,
        "timezone_offset": 3,
        "role_groups": [
            {
                "role": "Скаут",
                "group_id": STAVROPOL_SCOUTS_GROUP_ID,
                "topic_tasks": STAVROPOL_SCOUTS_TOPIC_WORK,
                "topic_moves": None,
                "topic_npb": NO_TOPIC,
                "topic_reports": NO_TOPIC,
            },
            {
                # Одна группа, две роли: маршрутизация уточняется номером темы.
                "role": "Водитель|Чарджер",
                "group_id": STAVROPOL_TRANSPORT_GROUP_ID,
                "topic_tasks": STAVROPOL_DRIVERS_TOPIC_WORK,
                "topic_moves": None,
                "topic_npb": STAVROPOL_CHARGERS_TOPIC_BATTERY,
                "topic_reports": NO_TOPIC,
            },
        ],
    }, {
        # Красная Поляна — работает на заглушках, впиши реальные ID выше
        "key": "krasnaya_polyana",
        "name": "Красная Поляна",
        "group_id": POLYANA_GROUP_ID,
        "topic_tasks": POLYANA_TOPIC_TASKS,
        "topic_npb": POLYANA_TOPIC_NPB,
        "topic_reports": POLYANA_TOPIC_REPORTS,
        "timezone_offset": 3,
    }, {
        # Химки — ОДИН город, две группы по ролям.
        # Базовые topic_* = группа скаутов (она же group_id города).
        # Водители описаны в role_groups: своя группа и свои темы.
        "key": "khimki",
        "name": "Химки",
        "group_id": KHIMKI_SCOUTS_GROUP_ID,
        "topic_tasks": KHIMKI_SCOUTS_TOPIC_MOVES,
        "topic_moves": KHIMKI_SCOUTS_TOPIC_MOVES,
        "topic_npb": NO_TOPIC,          # темы АКБ в Химках нет
        "topic_reports": KHIMKI_SCOUTS_TOPIC_REPORTS,
        "timezone_offset": 3,
        "role_groups": [
            {
                "role": "Скаут",
                "group_id": KHIMKI_SCOUTS_GROUP_ID,
                "topic_tasks": KHIMKI_SCOUTS_TOPIC_MOVES,
                "topic_moves": KHIMKI_SCOUTS_TOPIC_MOVES,
                "topic_npb": NO_TOPIC,
                "topic_reports": KHIMKI_SCOUTS_TOPIC_REPORTS,
            },
            {
                "role": "Водитель",
                "group_id": KHIMKI_DRIVERS_GROUP_ID,
                "topic_tasks": KHIMKI_DRIVERS_TOPIC_MOVES,
                "topic_moves": KHIMKI_DRIVERS_TOPIC_MOVES,
                "topic_npb": NO_TOPIC,
                "topic_reports": KHIMKI_DRIVERS_TOPIC_REPORTS,
            },
            {
                "role": "Чарджер",
                "group_id": KHIMKI_CHARGERS_GROUP_ID,
                # Только тема 4 «Пустые АКБ» является рабочей.
                "topic_tasks": KHIMKI_CHARGERS_TOPIC_BATTERY,
                "topic_moves": None,
                "topic_npb": KHIMKI_CHARGERS_TOPIC_BATTERY,
                "topic_reports": KHIMKI_CHARGERS_TOPIC_REPORTS,
            },
        ],
    }]
    if not CITIES_CONFIG_JSON:
        return configs
    try:
        raw = json.loads(CITIES_CONFIG_JSON)
        if isinstance(raw, dict):
            raw = [dict(value, key=key) for key, value in raw.items()]
        if not isinstance(raw, list):
            raise ValueError("ожидался список или объект")
        by_key = {item["key"]: item for item in configs}
        for item in raw:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "").strip().lower()
            # Переменная хостинга может менять отдельные поля города, но не
            # должна стирать встроенные role_groups Химок. Для нового города,
            # которого нет в коде, по-прежнему требуется полный набор полей.
            city = {**by_key.get(key, {}), **dict(item), "key": key}
            required = ("name", "group_id", "topic_tasks", "topic_npb", "topic_reports")
            if not key or any(city.get(field) is None for field in required):
                logger.warning("Пропущена неполная запись города в CITIES_CONFIG_JSON")
                continue
            if city.get("topic_moves") is not None:
                try:
                    city["topic_moves"] = int(city["topic_moves"])
                except (TypeError, ValueError):
                    city["topic_moves"] = None
            for field in ("group_id", "topic_tasks", "topic_npb", "topic_reports"):
                city[field] = int(city[field])
            city["timezone_offset"] = int(city.get("timezone_offset", 3))
            by_key[key] = city
        return list(by_key.values())
    except Exception as exc:
        logger.error(f"CITIES_CONFIG_JSON не прочитан: {exc}. Использую Краснодар.")
        return configs


async def refresh_cities_cache(db=None):
    own_connection = db is None
    if own_connection:
        db = await aiosqlite.connect(DB_PATH)
    try:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute("SELECT * FROM cities WHERE is_active = 1")).fetchall()
        try:
            role_rows = await (await db.execute("SELECT * FROM city_role_groups")).fetchall()
        except Exception:
            role_rows = []          # таблицы ещё нет (первый запуск) — работаем как раньше
        roles_by_city = {}
        for rrow in role_rows:
            rg = dict(rrow)
            roles_by_city.setdefault(rg["city_id"], []).append(rg)

        CITIES_BY_ID.clear()
        CITIES_BY_GROUP.clear()
        CITY_ROLE_GROUPS.clear()
        for row in rows:
            city = dict(row)
            city_id = city["id"]
            CITIES_BY_ID[city_id] = city
            CITIES_BY_GROUP[city["group_id"]] = city

            # Ролевые группы: на каждую делаем «вариант» города — та же запись
            # (тот же city["id"]!), но с group_id и темами этой группы.
            # Благодаря общему city_id вся статистика города остаётся единой.
            for rg in roles_by_city.get(city_id, []):
                roles = [item.strip() for item in str(rg["role"] or "").split("|") if item.strip()]
                variant = dict(city)
                variant["group_id"] = rg["group_id"]
                variant["topic_tasks"] = rg["topic_tasks"]
                variant["topic_npb"] = rg["topic_npb"]
                variant["topic_moves"] = rg["topic_moves"]
                variant["topic_reports"] = rg["topic_reports"]
                variant["role_groups"] = roles
                variant["role_group"] = roles[0] if len(roles) == 1 else None
                CITIES_BY_GROUP[rg["group_id"]] = variant
                for role in roles:
                    CITY_ROLE_GROUPS.setdefault(city_id, {})[_norm_role(role)] = variant
    finally:
        if own_connection:
            await db.close()


def _norm_role(role):
    return (role or "").strip().lower()


def get_city(city_id):
    return CITIES_BY_ID.get(city_id)


def _city_key(city):
    """Ключ города одинаково читается из конфига и строки таблицы cities."""
    return (city or {}).get("city_key") or (city or {}).get("key") or ""


def _is_single_chat_city(city):
    """Город без тем: управление сменой, отчёт и действия в одном чате."""
    return _city_key(city) == "stavropol" and not city_role_groups(city.get("id"))


def _uses_strict_work_topics(city):
    """Города, где парсер слушает только явно указанные рабочие темы."""
    return (
        bool((city or {}).get("role_group"))
        or bool((city or {}).get("role_groups"))
        or bool(city_role_groups((city or {}).get("id")))
        or _city_key(city) == "krasnaya_polyana"
    )


def _telegram_thread_id(value):
    """Преобразует NO_TOPIC/0/None в отсутствие message_thread_id."""
    try:
        thread_id = int(value)
    except (TypeError, ValueError):
        return None
    return thread_id if thread_id > 0 else None


def city_role_groups(city_id):
    """Ролевые группы города: {'скаут': вариант, 'водитель': вариант}.

    Пусто для обычных городов (Краснодар) — там одна группа на всех.
    """
    return CITY_ROLE_GROUPS.get(city_id) or {}


def city_for_role(city_id, role):
    """Куда писать смену сотрудника: вариант города под его роль.

    Для городов без ролевых групп возвращает город как есть — поведение
    Краснодара не меняется вообще.
    """
    city = get_city(city_id)
    if not city:
        return None
    groups = city_role_groups(city_id)
    if not groups:
        return city
    # Для ролевого города неизвестная роль не должна молча отправлять отчёт в
    # основную (скаутскую) группу. Лучше понятная ошибка, чем чужой отчёт.
    return groups.get(_norm_role(role))


def city_requires_role(city_id):
    """У города группы разделены по ролям — значит роль обязательна."""
    return bool(city_role_groups(city_id))


def city_supported_roles(city_id):
    """Роли, для которых в городе есть группа (для понятной ошибки)."""
    groups = city_role_groups(city_id)
    result = []
    for variant in groups.values():
        for role in variant.get("role_groups") or [variant.get("role_group")]:
            if role and role not in result:
                result.append(role)
    return result


def report_city_for_role(city_id, role):
    """Destination for live reports; Stavropol uses one shared report group."""
    city = get_city(city_id)
    if city and _city_key(city) == "stavropol":
        return city
    return city_for_role(city_id, role)


def get_city_by_group(group_id):
    return CITIES_BY_GROUP.get(group_id)


def get_default_city():
    for city in CITIES_BY_ID.values():
        if city.get("city_key") == DEFAULT_CITY_KEY:
            return city
    return next(iter(CITIES_BY_ID.values()), None)

async def init_db():
    _kpi_refreshed_hours.clear()
    repair_shift_ids = []
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        # WAL и увеличенное ожидание записи защищают от потери действий, когда
        # сотрудники подряд отправляют много сообщений с номерами байков.
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("PRAGMA synchronous = NORMAL")
        await db.execute("PRAGMA busy_timeout = 15000")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                city_key TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                group_id INTEGER NOT NULL UNIQUE,
                topic_tasks INTEGER NOT NULL,
                topic_npb INTEGER NOT NULL,
                topic_moves INTEGER,
                topic_reports INTEGER NOT NULL,
                timezone_offset INTEGER NOT NULL DEFAULT 3,
                is_active INTEGER NOT NULL DEFAULT 1,
                managed_by_config INTEGER NOT NULL DEFAULT 0
            )
        """)
        # Ролевые группы города: у одной роли — своя телеграм-группа со своими
        # темами (Химки). Город при этом остаётся ОДНИМ (один city_id), поэтому
        # админка, история и КПД видят весь город целиком без изменений.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS city_role_groups (
                city_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                group_id INTEGER NOT NULL UNIQUE,
                topic_tasks INTEGER NOT NULL,
                topic_npb INTEGER NOT NULL,
                topic_moves INTEGER,
                topic_reports INTEGER NOT NULL,
                PRIMARY KEY (city_id, role)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT,
                role TEXT,
                city_id INTEGER,
                pay_type TEXT DEFAULT 'hourly',
                pay_amount REAL DEFAULT 350,
                edit_mode INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS shifts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                full_name TEXT,
                role TEXT,
                start_time TEXT,
                end_time TEXT,
                district TEXT,
                comment TEXT,
                is_active INTEGER DEFAULT 1,
                report_msg_id INTEGER,
                created_at TEXT,
                earned REAL DEFAULT 0,
                pay_type_snap TEXT,
                pay_amount_snap REAL,
                city_id INTEGER,
                start_at TEXT,
                end_at TEXT,
                source TEXT DEFAULT 'bot',
                source_message_id INTEGER,
                on_lunch INTEGER NOT NULL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                shift_id INTEGER,
                message_id INTEGER,
                action_type TEXT,
                bike_codes TEXT,
                quantity INTEGER DEFAULT 0,
                city_id INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS kpi_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                city_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                snapshot_hour TEXT NOT NULL,
                actions_count INTEGER NOT NULL DEFAULT 0,
                worked_minutes INTEGER NOT NULL DEFAULT 0,
                efficiency REAL NOT NULL DEFAULT 0,
                UNIQUE(city_id, user_id, snapshot_hour)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS monthly_aggregates (
                city_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                month TEXT NOT NULL,
                full_name TEXT,
                role TEXT,
                shifts_count INTEGER NOT NULL DEFAULT 0,
                worked_minutes INTEGER NOT NULL DEFAULT 0,
                actions_count INTEGER NOT NULL DEFAULT 0,
                earned REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(city_id, user_id, month)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS manual_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                city_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                raw_text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'needs_review',
                parse_error TEXT,
                shift_id INTEGER,
                sender_name TEXT,
                pay_type_snap TEXT,
                pay_amount_snap REAL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(city_id, message_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS work_message_links (
                city_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL DEFAULT 0,
                user_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                shift_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                last_event_version REAL,
                PRIMARY KEY(city_id, chat_id, user_id, message_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admin_city_access (
                user_id INTEGER PRIMARY KEY,
                city_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        # CRM использует явные серверные права. Старая admin_city_access
        # оставлена только для совместимости и никогда не повышает человека
        # автоматически до CRM-администратора.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admin_accounts (
                user_id INTEGER PRIMARY KEY,
                role TEXT NOT NULL CHECK (
                    role IN ('city_viewer', 'city_manager', 'network_admin')
                ),
                role_scope TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                session_version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admin_city_permissions (
                user_id INTEGER NOT NULL,
                city_id INTEGER NOT NULL,
                PRIMARY KEY (user_id, city_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admin_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_user_id INTEGER NOT NULL,
                city_id INTEGER,
                operation TEXT NOT NULL,
                entity_type TEXT,
                entity_id TEXT,
                before_json TEXT,
                after_json TEXT,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS crm_planned_shifts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                city_id INTEGER NOT NULL,
                work_date TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                user_id INTEGER,
                role TEXT,
                district TEXT,
                note TEXT,
                work_kind TEXT NOT NULL DEFAULT 'regular' CHECK (
                    work_kind IN ('regular', 'extra')
                ),
                status TEXT NOT NULL DEFAULT 'scheduled' CHECK (
                    status IN ('scheduled', 'cancelled')
                ),
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_by INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK ((user_id IS NOT NULL AND role IS NULL) OR
                       (user_id IS NULL AND role IS NOT NULL))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS crm_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                city_id INTEGER NOT NULL,
                work_date TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                priority TEXT NOT NULL DEFAULT 'normal' CHECK (
                    priority IN ('low', 'normal', 'high', 'urgent')
                ),
                status TEXT NOT NULL DEFAULT 'draft' CHECK (
                    status IN ('draft', 'published', 'cancelled')
                ),
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_by INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                published_at TEXT,
                requires_photo INTEGER NOT NULL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS crm_task_targets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                target_type TEXT NOT NULL CHECK (target_type IN ('user', 'role')),
                user_id INTEGER,
                role TEXT,
                CHECK ((target_type = 'user' AND user_id IS NOT NULL AND role IS NULL) OR
                       (target_type = 'role' AND user_id IS NULL AND role IS NOT NULL))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS crm_task_assignees (
                task_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                full_name_snap TEXT,
                role_snap TEXT,
                status TEXT NOT NULL DEFAULT 'assigned' CHECK (
                    status IN ('assigned', 'seen', 'in_progress', 'submitted',
                               'accepted', 'blocked')
                ),
                status_comment TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (task_id, user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS crm_task_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                author_user_id INTEGER NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS crm_task_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                storage_key TEXT NOT NULL UNIQUE,
                original_name TEXT,
                mime_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'brief' CHECK (kind IN ('brief', 'result')),
                assignee_user_id INTEGER,
                uploaded_by INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS crm_task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                actor_user_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS crm_notification_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                city_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                entity_id INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK (
                    status IN ('pending', 'retry', 'sent', 'failed')
                ),
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                sent_at TEXT,
                UNIQUE(user_id, kind, entity_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS crm_planning_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                city_id INTEGER NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                date_from TEXT NOT NULL,
                date_to TEXT NOT NULL,
                work_days INTEGER NOT NULL,
                rest_days INTEGER NOT NULL,
                request_json TEXT NOT NULL,
                summary_json TEXT,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS crm_shift_task_sync (
                shift_id INTEGER PRIMARY KEY,
                processed_at TEXT NOT NULL,
                matched_tasks INTEGER NOT NULL DEFAULT 0
            )
        """)
        # Декады — расчётные периоды админки. «Обнуление» счётчиков это старт
        # новой декады: данные смен остаются в БД, меняется только точка отсчёта.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS payroll_periods (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                city_id INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                created_by INTEGER,
                created_at TEXT
            )
        """)
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_periods_city "
            "ON payroll_periods(city_id, ended_at)"
        )
        await db.commit()

        # === МИГРАЦИЯ: добавляем chat_id в ключ work_message_links ===
        # В городе с несколькими группами (Химки: скауты + водители) один
        # city_id обслуживает два чата. message_id в Telegram уникален только
        # ВНУТРИ чата, поэтому старый ключ (city_id, user_id, message_id)
        # давал коллизии: привязка из одной группы перекрывала другую, и
        # действия молча не записывались. Ключ теперь включает chat_id.
        try:
            cols = await (await db.execute("PRAGMA table_info(work_message_links)")).fetchall()
            has_chat = any((c[1] if not isinstance(c, dict) else c["name"]) == "chat_id"
                           for c in cols)
            if cols and not has_chat:
                logger.info("Миграция work_message_links: добавляю chat_id в ключ…")
                await db.execute("""
                    CREATE TABLE work_message_links_new (
                        city_id INTEGER NOT NULL,
                        chat_id INTEGER NOT NULL DEFAULT 0,
                        user_id INTEGER NOT NULL,
                        message_id INTEGER NOT NULL,
                        shift_id INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        last_event_version REAL,
                        PRIMARY KEY(city_id, chat_id, user_id, message_id)
                    )
                """)
                # chat_id старым связям проставляем по основной группе города
                await db.execute("""
                    INSERT OR IGNORE INTO work_message_links_new
                        (city_id, chat_id, user_id, message_id, shift_id, created_at,
                         last_event_version)
                    SELECT l.city_id,
                           COALESCE((SELECT c.group_id FROM cities c WHERE c.id = l.city_id), 0),
                           l.user_id, l.message_id, l.shift_id, l.created_at, l.last_event_version
                    FROM work_message_links l
                """)
                await db.execute("DROP TABLE work_message_links")
                await db.execute("ALTER TABLE work_message_links_new RENAME TO work_message_links")
                await db.commit()
                logger.info("Миграция work_message_links: готово")
        except Exception as exc:
            # Миграция не должна ронять старт бота ни при каких условиях.
            logger.warning(f"Миграция work_message_links пропущена: {exc}")

        # Автоматическая миграция для старых баз данных
        try:
            await db.execute("ALTER TABLE actions ADD COLUMN message_id INTEGER")
            await db.commit()
            logger.info("Миграция: Колонка message_id успешно добавлена в таблицу actions.")
        except aiosqlite.OperationalError:
            pass

        # === НОВОЕ: миграция под живое сообщение — храним id сообщения-отчёта смены ===
        try:
            await db.execute("ALTER TABLE shifts ADD COLUMN report_msg_id INTEGER")
            await db.commit()
            logger.info("Миграция: Колонка report_msg_id успешно добавлена в таблицу shifts.")
        except aiosqlite.OperationalError:
            pass

        # === НОВОЕ: модель оплаты у сотрудника (для мини-приложения) ===
        for ddl in [
            "ALTER TABLE users ADD COLUMN pay_type TEXT DEFAULT 'hourly'",
            "ALTER TABLE users ADD COLUMN pay_amount REAL DEFAULT 350",
            # === НОВОЕ: тумблер «Режим редактирования» (личный, у каждого свой) ===
            "ALTER TABLE users ADD COLUMN edit_mode INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN city_id INTEGER",
            # Авто-закрытие смены: личный дефолт сотрудника (вкл/выкл + часы)
            "ALTER TABLE users ADD COLUMN auto_close INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN auto_close_hours INTEGER DEFAULT 10",
        ]:
            try:
                await db.execute(ddl); await db.commit()
            except aiosqlite.OperationalError:
                pass

        # === НОВОЕ: дата смены + замороженный заработок (для истории/зарплаты) ===
        for ddl in [
            "ALTER TABLE shifts ADD COLUMN created_at TEXT",
            "ALTER TABLE shifts ADD COLUMN earned REAL DEFAULT 0",
            "ALTER TABLE shifts ADD COLUMN pay_type_snap TEXT",
            "ALTER TABLE shifts ADD COLUMN pay_amount_snap REAL",
            "ALTER TABLE shifts ADD COLUMN city_id INTEGER",
            "ALTER TABLE shifts ADD COLUMN start_at TEXT",
            "ALTER TABLE shifts ADD COLUMN end_at TEXT",
            "ALTER TABLE shifts ADD COLUMN source TEXT DEFAULT 'bot'",
            "ALTER TABLE shifts ADD COLUMN source_message_id INTEGER",
            # Дедлайн авто-закрытия (уже с учётом +10 мин форы), NULL = не закрывать
            "ALTER TABLE shifts ADD COLUMN auto_close_at TEXT",
            # Явная привязка смены к декаде. NULL у старых смен — для них
            # период определяется по дате старта (обратная совместимость).
            "ALTER TABLE shifts ADD COLUMN period_id INTEGER",
            # Дневные/ночные декады: группа расчётного периода.
            "ALTER TABLE payroll_periods ADD COLUMN segment TEXT",
            # Информационный статус для живого отчёта. Не участвует во времени,
            # заработке, KPI или подсчёте действий.
            "ALTER TABLE shifts ADD COLUMN on_lunch INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE actions ADD COLUMN city_id INTEGER",
            "ALTER TABLE cities ADD COLUMN managed_by_config INTEGER NOT NULL DEFAULT 0",
            # Тема, где голые 4-значные номера = перемещения (Химки). NULL —
            # у города такой темы нет, парсер работает как раньше, по глаголам.
            "ALTER TABLE cities ADD COLUMN topic_moves INTEGER",
            # chat_id в actions: message_id уникален внутри ЧАТА, а не глобально.
            # Без этого в городе с двумя группами (Химки) сообщения из разных
            # групп с одинаковым message_id затирали друг друга.
            "ALTER TABLE actions ADD COLUMN chat_id INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE manual_reports ADD COLUMN sender_name TEXT",
            "ALTER TABLE manual_reports ADD COLUMN pay_type_snap TEXT",
            "ALTER TABLE manual_reports ADD COLUMN pay_amount_snap REAL",
            "ALTER TABLE work_message_links ADD COLUMN last_event_version REAL",
            "ALTER TABLE admin_accounts ADD COLUMN role_scope TEXT",
            "ALTER TABLE crm_tasks ADD COLUMN requires_photo INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE crm_task_attachments ADD COLUMN kind TEXT NOT NULL DEFAULT 'brief'",
            "ALTER TABLE crm_task_attachments ADD COLUMN assignee_user_id INTEGER",
            "ALTER TABLE crm_tasks ADD COLUMN date_from TEXT",
            "ALTER TABLE crm_tasks ADD COLUMN date_to TEXT",
            "ALTER TABLE crm_planned_shifts ADD COLUMN work_kind TEXT NOT NULL DEFAULT 'regular'",
            "ALTER TABLE crm_tasks ADD COLUMN district TEXT",
            "ALTER TABLE crm_tasks ADD COLUMN completion_mode TEXT NOT NULL DEFAULT 'manual'",
            "ALTER TABLE crm_planned_shifts ADD COLUMN batch_id INTEGER",
        ]:
            try:
                await db.execute(ddl); await db.commit()
            except aiosqlite.OperationalError:
                pass

        # Города, которыми управляет CITIES_CONFIG_JSON, деактивируются при
        # удалении из конфига. Записи, добавленные админом напрямую в БД,
        # managed_by_config=0 и не затрагиваются.
        await db.execute(
            "UPDATE cities SET is_active = 0 WHERE managed_by_config = 1 "
            "AND id NOT IN (SELECT DISTINCT city_id FROM shifts WHERE is_active = 1)"
        )
        for city in _configured_cities():
            moves_topic = city.get("topic_moves")
            moves_topic = int(moves_topic) if moves_topic is not None else None
            params = (city["key"], city["name"], int(city["group_id"]),
                      int(city["topic_tasks"]), int(city["topic_npb"]), moves_topic,
                      int(city["topic_reports"]), int(city.get("timezone_offset", 3)))
            try:
                await db.execute(
                    "INSERT INTO cities (city_key, name, group_id, topic_tasks, topic_npb, "
                    "topic_moves, topic_reports, timezone_offset, is_active, managed_by_config) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 1) "
                    "ON CONFLICT(city_key) DO UPDATE SET name=excluded.name, "
                    "group_id=excluded.group_id, topic_tasks=excluded.topic_tasks, "
                    "topic_npb=excluded.topic_npb, topic_moves=excluded.topic_moves, "
                    "topic_reports=excluded.topic_reports, "
                    "timezone_offset=excluded.timezone_offset, is_active=1, managed_by_config=1",
                    params
                )
            except Exception:
                # Такой group_id уже занят записью с другим city_key: обновляем её,
                # не трогая ключ. Без этого запуск падает на боевой базе с
                # UNIQUE(cities.group_id), и веб-сервер вообще не поднимается —
                # именно так «умерла» Основа. Синхронизация городов ни при каком
                # раскладе не должна ронять старт бота.
                await db.execute(
                    "UPDATE cities SET name = ?, topic_tasks = ?, topic_npb = ?, "
                    "topic_moves = ?, topic_reports = ?, timezone_offset = ?, is_active = 1, "
                    "managed_by_config = 1 WHERE group_id = ?",
                    (city["name"], int(city["topic_tasks"]), int(city["topic_npb"]),
                     moves_topic, int(city["topic_reports"]),
                     int(city.get("timezone_offset", 3)), int(city["group_id"]))
                )
        # Ролевые группы: пересобираем под текущий конфиг. Города без
        # role_groups (Краснодар и др.) не затрагиваются вообще.
        for city in _configured_cities():
            role_groups = city.get("role_groups") or []
            row = await (await db.execute(
                "SELECT id FROM cities WHERE city_key = ?", (city["key"],)
            )).fetchone()
            if not row:
                continue
            cid = row[0]
            await db.execute("DELETE FROM city_role_groups WHERE city_id = ?", (cid,))
            for rg in role_groups:
                moves = rg.get("topic_moves")
                try:
                    await db.execute(
                        "INSERT INTO city_role_groups (city_id, role, group_id, topic_tasks, "
                        "topic_npb, topic_moves, topic_reports) VALUES (?, ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(group_id) DO UPDATE SET city_id=excluded.city_id, "
                        "role=excluded.role, topic_tasks=excluded.topic_tasks, "
                        "topic_npb=excluded.topic_npb, topic_moves=excluded.topic_moves, "
                        "topic_reports=excluded.topic_reports",
                        (cid, rg["role"], int(rg["group_id"]), int(rg["topic_tasks"]),
                         int(rg["topic_npb"]), int(moves) if moves is not None else None,
                         int(rg["topic_reports"]))
                    )
                except Exception as exc:
                    # Ролевые группы не должны ронять старт бота ни при каких условиях.
                    logger.warning(f"Не удалось записать ролевую группу {rg.get('role')}: {exc}")
        await db.commit()
        await refresh_cities_cache(db)
        default_city = get_default_city()
        if not default_city:
            raise RuntimeError("В таблице cities нет активного города")

        default_city_id = default_city["id"]
        await db.execute("UPDATE users SET city_id = ? WHERE city_id IS NULL", (default_city_id,))
        await db.execute("UPDATE shifts SET city_id = ? WHERE city_id IS NULL", (default_city_id,))
        await db.execute(
            "UPDATE actions SET city_id = COALESCE((SELECT city_id FROM shifts "
            "WHERE shifts.id = actions.shift_id), ?) WHERE city_id IS NULL",
            (default_city_id,)
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_shifts_user_city_active "
            "ON shifts(user_id, city_id, is_active)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_actions_shift_city ON actions(shift_id, city_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_shifts_city_active_start "
            "ON shifts(city_id, is_active, start_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_shifts_city_user_start "
            "ON shifts(city_id, user_id, start_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_work_message_links_shift "
            "ON work_message_links(shift_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_shifts_city_start_end "
            "ON shifts(city_id, start_at, end_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_shifts_city_role_start "
            "ON shifts(city_id, role, start_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_actions_city_type_shift "
            "ON actions(city_id, action_type, shift_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_kpi_city_hour_user "
            "ON kpi_snapshots(city_id, snapshot_hour, user_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_manual_reports_city_status_created "
            "ON manual_reports(city_id, status, created_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_admin_permissions_city_user "
            "ON admin_city_permissions(city_id, user_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_crm_planned_city_date "
            "ON crm_planned_shifts(city_id, work_date, status)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_crm_tasks_city_date_status "
            "ON crm_tasks(city_id, work_date, status)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_crm_targets_task "
            "ON crm_task_targets(task_id, target_type, user_id, role)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_crm_assignees_user_task "
            "ON crm_task_assignees(user_id, task_id, status)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_crm_attachments_task_assignee "
            "ON crm_task_attachments(task_id, kind, assignee_user_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_crm_outbox_delivery "
            "ON crm_notification_outbox(status, next_attempt_at, id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_crm_plans_batch "
            "ON crm_planned_shifts(batch_id, user_id, work_date)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_crm_tasks_city_range_status "
            "ON crm_tasks(city_id, date_from, date_to, status)"
        )
        now_iso = datetime.now(timezone.utc).isoformat()
        for admin_uid in NETWORK_ADMIN_USER_IDS:
            await db.execute(
                "INSERT INTO admin_accounts (user_id, role, is_active, session_version, "
                "created_at, updated_at) VALUES (?, 'network_admin', 1, 1, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET role='network_admin', is_active=1, "
                "updated_at=excluded.updated_at",
                (admin_uid, now_iso, now_iso),
            )
        duplicate_active = await (await db.execute(
            "SELECT user_id, MAX(id) AS keep_id, COUNT(*) AS amount FROM shifts "
            "WHERE is_active = 1 GROUP BY user_id HAVING COUNT(*) > 1"
        )).fetchall()
        for uid, keep_id, amount in duplicate_active:
            await db.execute(
                "UPDATE shifts SET is_active = 0, end_time = COALESCE(end_time, start_time), "
                "end_at = COALESCE(end_at, start_at), earned = 0, on_lunch = 0 "
                "WHERE user_id = ? AND is_active = 1 AND id <> ?",
                (uid, keep_id)
            )
            logger.warning(
                f"Миграция: у uid={uid} было {amount} активных смен; "
                "оставлена самая новая."
            )
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_shift_per_user "
            "ON shifts(user_id) WHERE is_active = 1"
        )
        # Версии до постоянной привязки сообщения хранили её только в actions.
        # Заполняем новую таблицу до начала обработки edit-событий.
        legacy_links = await (await db.execute(
            "SELECT a.city_id, a.user_id, a.message_id, a.shift_id, "
            "COALESCE(s.created_at, ?) FROM actions a JOIN shifts s ON s.id = a.shift_id "
            "WHERE a.city_id IS NOT NULL AND a.message_id IS NOT NULL AND a.id = "
            "(SELECT MAX(a2.id) FROM actions a2 WHERE a2.city_id = a.city_id "
            "AND a2.user_id = a.user_id AND a2.message_id = a.message_id)",
            (datetime.now(timezone.utc).isoformat(),)
        )).fetchall()
        await db.executemany(
            "INSERT OR IGNORE INTO work_message_links "
            "(city_id, user_id, message_id, shift_id, created_at) VALUES (?, ?, ?, ?, ?)",
            legacy_links
        )

        # Если предыдущий аварийный запуск успел создать две смены из одного
        # Telegram-отчёта, оставляем последнюю до создания UNIQUE-индекса.
        duplicate_sources = await (await db.execute(
            "SELECT city_id, source_message_id, MAX(id) AS keep_id FROM shifts "
            "WHERE source_message_id IS NOT NULL GROUP BY city_id, source_message_id "
            "HAVING COUNT(*) > 1"
        )).fetchall()
        for duplicate_city_id, source_message_id, keep_id in duplicate_sources:
            stale_ids = [row[0] for row in await (await db.execute(
                "SELECT id FROM shifts WHERE city_id = ? AND source_message_id = ? AND id <> ?",
                (duplicate_city_id, source_message_id, keep_id)
            )).fetchall()]
            for stale_id in stale_ids:
                await db.execute(
                    "UPDATE manual_reports SET shift_id = ? WHERE shift_id = ?", (keep_id, stale_id)
                )
                await db.execute("DELETE FROM actions WHERE shift_id = ?", (stale_id,))
                await db.execute("DELETE FROM shifts WHERE id = ?", (stale_id,))
            logger.warning(
                f"Миграция: дубли ручного отчёта {source_message_id} города "
                f"{duplicate_city_id} объединены в смену {keep_id}."
            )
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_manual_report_source "
            "ON shifts(city_id, source_message_id) WHERE source_message_id IS NOT NULL"
        )

        # Старые строки получают полноценные даты из created_at и HH:MM.
        db.row_factory = aiosqlite.Row
        old_rows = await (await db.execute(
            "SELECT id, city_id, start_time, end_time, created_at, start_at, end_at "
            "FROM shifts WHERE start_at IS NULL"
        )).fetchall()
        for row in old_rows:
            city = get_city(row["city_id"]) or default_city
            tz = _city_tz(city)
            try:
                base_date = datetime.fromisoformat(row["created_at"]).astimezone(tz).date() \
                    if row["created_at"] else datetime.now(tz).date()
            except Exception:
                base_date = datetime.now(tz).date()
            try:
                hour, minute = map(int, (row["start_time"] or "0:00").split(":"))
                start_at = datetime.combine(base_date, datetime.min.time(), tzinfo=tz).replace(
                    hour=hour, minute=minute)
                end_at = None
                if row["end_time"]:
                    eh, em = map(int, row["end_time"].split(":"))
                    end_at = datetime.combine(base_date, datetime.min.time(), tzinfo=tz).replace(
                        hour=eh, minute=em)
                    if end_at < start_at:
                        end_at += timedelta(days=1)
                await db.execute(
                    "UPDATE shifts SET start_at = ?, end_at = COALESCE(end_at, ?) WHERE id = ?",
                    (start_at.isoformat(), end_at.isoformat() if end_at else None, row["id"])
                )
            except Exception as exc:
                logger.warning(f"Не удалось восстановить дату смены {row['id']}: {exc}")
        await db.commit()

        repair_shift_ids = [row[0] for row in await (await db.execute(
            "SELECT id FROM shifts WHERE is_active = 0 AND pay_type_snap IS NULL "
            "AND COALESCE(earned, 0) = 0"
        )).fetchall()]

    logger.info("БД готова")
    for shift_id in repair_shift_ids:
        try:
            await freeze_earned(shift_id)
        except Exception as exc:
            logger.error(f"Не удалось восстановить расчёт закрытой смены {shift_id}: {exc}")

# ============================================================================
# [04-USERS-SHIFTS] ПОЛЬЗОВАТЕЛИ, ДЕКАДЫ, СМЕНЫ, ДЕЙСТВИЯ И ЗАРАБОТОК
# ============================================================================

async def add_user(uid, name, role, city_id=None):
    # ВАЖНО: не используем INSERT OR REPLACE — иначе стёрлись бы pay_type/pay_amount.
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users (user_id, full_name, role, city_id) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET full_name=excluded.full_name, "
            "role=excluded.role, city_id=COALESCE(excluded.city_id, users.city_id)",
            (uid, name, role, city_id)
        )
        await db.commit()
async def set_user_city(uid, city_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users (user_id, full_name, role, city_id) VALUES (?, '', '', ?) "
            "ON CONFLICT(user_id) DO UPDATE SET city_id=excluded.city_id",
            (uid, city_id)
        )
        await db.commit()

# === НОВОЕ: сохранить модель оплаты (из настроек мини-приложения) ===
async def set_user_pay(uid, pay_type, amount):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users (user_id, full_name, role, pay_type, pay_amount) "
            "VALUES (?, '', '', ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET pay_type=excluded.pay_type, pay_amount=excluded.pay_amount",
            (uid, pay_type, amount)
        )
        await db.commit()

# === НОВОЕ: сохранить тумблер «Режим редактирования» (из настроек мини-приложения).
# Обновляет ТОЛЬКО edit_mode, не затрагивая имя/роль/оплату. ===
async def set_user_edit_mode(uid, on):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users (user_id, full_name, role, edit_mode) "
            "VALUES (?, '', '', ?) "
            "ON CONFLICT(user_id) DO UPDATE SET edit_mode=excluded.edit_mode",
            (uid, 1 if on else 0)
        )
        await db.commit()

# Сохранить дефолт авто-закрытия (вкл/выкл + часы) — на все следующие смены.
async def set_user_auto_close(uid, enabled, hours):
    hours = hours if hours in AUTO_CLOSE_CHOICES else DEFAULT_AUTO_CLOSE_HOURS
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users (user_id, full_name, role, auto_close, auto_close_hours) "
            "VALUES (?, '', '', ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET auto_close=excluded.auto_close, "
            "auto_close_hours=excluded.auto_close_hours",
            (uid, 1 if enabled else 0, hours)
        )
        await db.commit()

# ── Декады (расчётные периоды админки) ────────────────────────
# Счётчики «обнуляются» стартом новой декады: смены остаются в БД,
# меняется только точка отсчёта. Ничего не удаляется.

# ── Дневные и ночные декады ───────────────────────────────────
# Сотрудники делятся на группы по времени ОТКРЫТИЯ смены:
#   день  — старт с 05:00 до 16:59;
#   ночь  — старт с 17:00 до 04:59.
# У каждой группы своя декада и своё обнуление, чтобы начальник
# считал зарплату дневным и ночным раздельно.

DAY_SEGMENT_START = 5    # 05:00 — начало «дневного» окна
DAY_SEGMENT_END = 17     # 17:00 — с этого часа старт считается ночным

SEGMENT_LABELS = {"day": "Дневные", "night": "Ночные"}


def _shift_segment(shift, city=None):
    """'day' или 'night' — по часу открытия смены в часовом поясе города."""
    raw = shift.get("start_at") or shift.get("created_at")
    hour = None
    dt = _parse_datetime(raw) if raw else None
    if dt:
        if city:
            dt = dt.astimezone(_city_tz(city))
        hour = dt.hour
    else:
        # Запасной путь: строка "ЧЧ:ММ" из start_time.
        m = re.match(r"^(\d{1,2}):\d{2}", str(shift.get("start_time") or ""))
        if m:
            hour = int(m.group(1)) % 24
    if hour is None:
        return "day"
    return "day" if DAY_SEGMENT_START <= hour < DAY_SEGMENT_END else "night"


async def ensure_city_period(city_id, segment="day"):
    """Возвращает открытую декаду города для группы (day/night), создавая её.

    Первая декада группы начинается с начала текущего месяца — чтобы после
    обновления цифры не обнулились сами по себе. Старые записи без segment
    считаются дневными.
    """
    segment = "night" if segment == "night" else "day"
    city = get_city(city_id)
    if not city:
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT * FROM payroll_periods WHERE city_id = ? AND ended_at IS NULL "
            "AND (segment = ? OR (? = 'day' AND segment IS NULL)) "
            "ORDER BY id DESC LIMIT 1", (city_id, segment, segment)
        )).fetchone()
        if row:
            period = dict(row)
            if not period.get("segment"):
                await db.execute(
                    "UPDATE payroll_periods SET segment = 'day' WHERE id = ?",
                    (period["id"],))
                # Бывшая общая декада становится дневной. Ночные смены,
                # привязанные к ней, отвязываем: дальше они сверяются с
                # ночной декадой по дате старта — как и вели себя раньше.
                legacy = await (await db.execute(
                    "SELECT id, start_at, created_at, start_time FROM shifts "
                    "WHERE period_id = ?", (period["id"],)
                )).fetchall()
                night_ids = [r["id"] for r in legacy
                             if _shift_segment(dict(r), city) == "night"]
                if night_ids:
                    marks = ",".join("?" * len(night_ids))
                    await db.execute(
                        f"UPDATE shifts SET period_id = NULL WHERE id IN ({marks})",
                        night_ids)
                await db.commit()
                period["segment"] = "day"
            return period
        now = datetime.now(_city_tz(city))
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        cur = await db.execute(
            "INSERT INTO payroll_periods (city_id, started_at, created_at, segment) "
            "VALUES (?, ?, ?, ?)",
            (city_id, month_start.isoformat(), now.isoformat(), segment)
        )
        await db.commit()
        return {"id": cur.lastrowid, "city_id": city_id,
                "started_at": month_start.isoformat(), "ended_at": None,
                "created_by": None, "created_at": now.isoformat(),
                "segment": segment}


async def city_periods(city_id):
    """Обе открытые декады города: {'day': {...}, 'night': {...}}."""
    return {
        "day": await ensure_city_period(city_id, "day"),
        "night": await ensure_city_period(city_id, "night"),
    }


def _shift_in_period(shift, periods, city=None):
    """Входит ли смена в ТЕКУЩУЮ декаду своей группы (день/ночь)."""
    segment = _shift_segment(shift, city)
    period = (periods or {}).get(segment) or {}
    pid = period.get("id")
    shift_pid = shift.get("period_id")
    if shift_pid:
        return shift_pid == pid
    started = shift.get("start_at") or shift.get("created_at")
    period_start = period.get("started_at")
    return bool(period_start and started and started >= period_start)


async def start_new_period(city_id, uid, segment="day"):
    """Закрывает текущую декаду группы и открывает новую.

    Обнуляется только выбранная группа: декада другой группы не трогается.
    Активные смены ЭТОЙ группы переезжают в новую декаду целиком.
    """
    segment = "night" if segment == "night" else "day"
    city = get_city(city_id)
    if not city:
        raise ValueError("Неизвестный город")
    # Гарантируем существование записи (и миграцию segment=NULL -> day).
    await ensure_city_period(city_id, segment)
    now = datetime.now(_city_tz(city))
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        current = await (await db.execute(
            "SELECT * FROM payroll_periods WHERE city_id = ? AND ended_at IS NULL "
            "AND (segment = ? OR (? = 'day' AND segment IS NULL)) "
            "ORDER BY id DESC LIMIT 1", (city_id, segment, segment)
        )).fetchone()
        if current:
            started = _parse_datetime(current["started_at"])
            # Две декады за одну минуту — почти наверняка двойной клик.
            if started and (now - started).total_seconds() < 60:
                await db.rollback()
                return dict(current)
            await db.execute(
                "UPDATE payroll_periods SET ended_at = ? WHERE id = ?",
                (now.isoformat(), current["id"])
            )
        cur = await db.execute(
            "INSERT INTO payroll_periods (city_id, started_at, created_by, created_at, segment) "
            "VALUES (?, ?, ?, ?, ?)",
            (city_id, now.isoformat(), uid, now.isoformat(), segment)
        )
        # Активные смены выбранной группы переносим в новую декаду: они ещё
        # не закрыты и не оплачены, поэтому относятся к периоду, в котором
        # завершатся. Смены другой группы не трогаем.
        active_rows = await (await db.execute(
            "SELECT id, start_at, created_at, start_time FROM shifts "
            "WHERE city_id = ? AND is_active = 1", (city_id,)
        )).fetchall()
        move_ids = [r["id"] for r in active_rows
                    if _shift_segment(dict(r), city) == segment]
        if move_ids:
            marks = ",".join("?" * len(move_ids))
            await db.execute(
                f"UPDATE shifts SET period_id = ? WHERE id IN ({marks})",
                [cur.lastrowid, *move_ids]
            )
        await db.commit()
        logger.info(
            f"Новая декада ({segment}) в городе {city_id} открыта админом {uid}; "
            f"перенесено активных смен: {len(move_ids)}."
        )
        return {"id": cur.lastrowid, "city_id": city_id,
                "started_at": now.isoformat(), "ended_at": None,
                "created_by": uid, "created_at": now.isoformat(),
                "segment": segment}


def _period_info(period, city, now=None):
    """Данные о декаде для фронта: дата старта и какой идёт день."""
    if not period:
        return None
    started = _parse_datetime(period.get("started_at"))
    now = now or datetime.now(_city_tz(city))
    day_number = 1
    if started:
        day_number = (now.date() - started.astimezone(_city_tz(city)).date()).days + 1
    return {
        "id": period.get("id"),
        "started_at": period.get("started_at"),
        "started_label": _fmt_date(period.get("started_at")),
        "day_number": max(1, day_number),
        "overdue": max(1, day_number) > 10,
        "opened_by": period.get("created_by"),
    }


async def get_user(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute("SELECT * FROM users WHERE user_id = ?", (uid,))
        r = await c.fetchone()
        return dict(r) if r else None

async def get_active_shift(uid, city_id=None):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if city_id is None:
            c = await db.execute(
                "SELECT * FROM shifts WHERE user_id = ? AND is_active = 1 ORDER BY id DESC LIMIT 1",
                (uid,)
            )
        else:
            c = await db.execute(
                "SELECT * FROM shifts WHERE user_id = ? AND city_id = ? AND is_active = 1 "
                "ORDER BY id DESC LIMIT 1", (uid, city_id)
            )
        r = await c.fetchone()
        return dict(r) if r else None

async def get_last_shift(uid, city_id=None):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if city_id is None:
            c = await db.execute(
                "SELECT * FROM shifts WHERE user_id = ? AND is_active = 0 ORDER BY id DESC LIMIT 1",
                (uid,)
            )
        else:
            c = await db.execute(
                "SELECT * FROM shifts WHERE user_id = ? AND city_id = ? AND is_active = 0 "
                "ORDER BY id DESC LIMIT 1", (uid, city_id)
            )
        r = await c.fetchone()
        return dict(r) if r else None

# === НОВОЕ: смена по id (нужно живому сообщению) ===
async def get_shift_by_id(sid):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute("SELECT * FROM shifts WHERE id = ?", (sid,))
        r = await c.fetchone()
        return dict(r) if r else None

def _parse_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _resolve_start_at(time_str, city, now=None):
    """Привязывает старт к сегодня; время >12ч «назад» считаем завтрашним
    (безопасный переход через полночь), позже текущего — отложенный старт сегодня."""
    tz = _city_tz(city)
    now = now.astimezone(tz) if now else datetime.now(tz)
    hour, minute = map(int, time_str.split(":"))
    candidate = datetime.combine(now.date(), datetime.min.time(), tzinfo=tz).replace(
        hour=hour, minute=minute)
    if now - candidate > timedelta(hours=12):
        candidate += timedelta(days=1)
    return candidate


def _resolve_end_at(shift, time_str, city, now=None):
    tz = _city_tz(city)
    now = now.astimezone(tz) if now else datetime.now(tz)
    start_at = _parse_datetime(shift.get("start_at"))
    if not start_at:
        start_at = _resolve_start_at(shift["start_time"], city, now)
    start_at = start_at.astimezone(tz)
    hour, minute = map(int, time_str.split(":"))
    # Будущую смену можно закрыть как отменённую, но она не
    # должна превращаться в оплаченную будущую смену.
    if now < start_at:
        return start_at

    candidate = datetime.combine(start_at.date(), datetime.min.time(), tzinfo=tz).replace(
        hour=hour, minute=minute)
    if candidate < start_at:
        candidate += timedelta(days=1)
    # Время окончания нельзя задавать в будущем: иначе бот начислит
    # ещё не отработанные часы. Две минуты — допуск на разницу часов.
    if candidate > now + timedelta(minutes=2):
        raise ValueError("Время окончания не может быть в будущем.")
    return candidate


def _resolve_manual_interval(start_time, end_time, city, message_time=None):
    """Привязывает закрытый ручной отчёт к дате его отправки."""
    tz = _city_tz(city)
    now = message_time.astimezone(tz) if message_time else datetime.now(tz)
    sh, sm = map(int, start_time.split(":"))
    eh, em = map(int, end_time.split(":"))
    end_at = datetime.combine(now.date(), datetime.min.time(), tzinfo=tz).replace(
        hour=eh, minute=em)
    # Ручной отчёт — уже завершённая смена. Если её конец ещё не
    # наступил сегодня, значит отчёт относится к прошлому дню.
    if end_at > now + timedelta(minutes=15):
        end_at -= timedelta(days=1)
    start_at = datetime.combine(end_at.date(), datetime.min.time(), tzinfo=tz).replace(
        hour=sh, minute=sm)
    if start_at > end_at:
        start_at -= timedelta(days=1)
    return start_at, end_at


def _shift_worked_min(shift, now=None):
    start_at = _parse_datetime(shift.get("start_at"))
    if not start_at:
        if shift.get("start_time") and shift.get("end_time"):
            return _worked_min(shift["start_time"], shift["end_time"])
        return 0
    end_at = _parse_datetime(shift.get("end_at"))
    if not end_at:
        city = get_city(shift.get("city_id")) or get_default_city()
        tz = _city_tz(city)
        end_at = now.astimezone(tz) if now else datetime.now(tz)
    return max(0, int((end_at - start_at).total_seconds() // 60))


def _shift_is_scheduled(shift, now=None):
    start_at = _parse_datetime(shift.get("start_at"))
    if not start_at or not shift.get("is_active"):
        return False
    city = get_city(shift.get("city_id")) or get_default_city()
    current = now.astimezone(_city_tz(city)) if now else datetime.now(_city_tz(city))
    return current < start_at


async def start_shift(uid, name, role, time, district, city_id, source="bot",
                      source_message_id=None, now=None, auto_close=None,
                      auto_close_hours=None):
    city = get_city(city_id)
    if not city:
        raise ValueError("Неизвестный город")
    start_at = _resolve_start_at(time, city, now)
    # Авто-закрытие: явные значения из мини-приложения или сохранённый дефолт.
    if auto_close is None:
        u = await get_user(uid) or {}
        auto_close = bool(u.get("auto_close"))
        auto_close_hours = u.get("auto_close_hours")
    hours = auto_close_hours if auto_close_hours in AUTO_CLOSE_CHOICES else DEFAULT_AUTO_CLOSE_HOURS
    auto_close_at = None
    if auto_close:
        auto_close_at = (start_at + timedelta(hours=hours,
                                              minutes=AUTO_CLOSE_GRACE_MIN)).isoformat()
    # Смена закрепляется за декадой СВОЕЙ группы: день или ночь — по часу старта.
    shift_segment = _shift_segment({"start_at": start_at.isoformat()}, city)
    period = await ensure_city_period(city_id, shift_segment)
    period_id = (period or {}).get("id")
    async with aiosqlite.connect(DB_PATH) as db:
        # BEGIN IMMEDIATE сериализует два почти одновременных старта.
        await db.execute("BEGIN IMMEDIATE")
        active = await (await db.execute(
            "SELECT id FROM shifts WHERE user_id = ? AND is_active = 1 LIMIT 1", (uid,)
        )).fetchone()
        if active:
            await db.rollback()
            raise ActiveShiftExists()
        now_iso = (now.astimezone(_city_tz(city)) if now else datetime.now(_city_tz(city))).isoformat()
        c = await db.execute(
            "INSERT INTO shifts (user_id, full_name, role, start_time, district, is_active, "
            "created_at, city_id, start_at, source, source_message_id, auto_close_at, period_id) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)",
            (uid, name, role, time, district, now_iso, city_id, start_at.isoformat(),
             source, source_message_id, auto_close_at, period_id)
        )
        await db.commit()
        return c.lastrowid

# === НОВОЕ: расчёт заработка ===
def _worked_min(start_time, end_time):
    sp = start_time.split(':'); ep = end_time.split(':')
    sm = int(sp[0]) * 60 + int(sp[1])
    em = int(ep[0]) * 60 + int(ep[1])
    if em < sm:
        em += 24 * 60
    return em - sm

def compute_earned(pay_type, amount, worked_min, battery_count):
    amount = amount or 0
    if pay_type == "salary":              # оклад за смену — фикс
        return round(amount, 2)
    if pay_type == "piece":               # сделка — за каждую замену АКБ
        return round(amount * (battery_count or 0), 2)
    return round(amount * (worked_min or 0) / 60.0, 2)   # почасовая

async def freeze_earned(sid):
    """Фиксируем сумму на момент закрытия смены — потом ставку можно менять, история не перепишется."""
    shift = await get_shift_by_id(sid)
    if not shift:
        return
    user = await get_user(shift['user_id']) or {}
    # Повторный /fix или правка ручного отчёта меняют цифры, но не
    # историческую ставку. Текущую ставку берём только при первом закрытии.
    pay_type = shift.get('pay_type_snap') or user.get('pay_type') or DEFAULT_PAY_TYPE
    amount = shift.get('pay_amount_snap')
    if amount is None:
        amount = user.get('pay_amount')
    if amount is None:
        amount = DEFAULT_PAY_AMOUNT
    stats = await get_stats(sid)
    wm = _shift_worked_min(shift) if shift.get('end_time') else 0
    start_at = _parse_datetime(shift.get("start_at"))
    end_at = _parse_datetime(shift.get("end_at"))
    if start_at and end_at and end_at <= start_at:
        earned = 0
    else:
        earned = compute_earned(pay_type, amount, wm, stats.get('battery', 0))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE shifts SET earned = ?, pay_type_snap = ?, pay_amount_snap = ? WHERE id = ?",
            (earned, pay_type, amount, sid)
        )
        await db.commit()
    if start_at and shift.get("city_id"):
        await refresh_monthly_aggregate(
            shift["city_id"], shift["user_id"], start_at.strftime("%Y-%m")
        )

async def end_shift(uid, time, comment="", city_id=None, now=None, end_at_override=None):
    shift = await get_active_shift(uid, city_id)
    if not shift:
        return None
    city = get_city(shift.get("city_id")) or get_default_city()
    scheduled = _shift_is_scheduled(shift, now)
    end_at = (end_at_override.astimezone(_city_tz(city)) if end_at_override is not None
              else _resolve_end_at(shift, time, city, now))
    stored_end_time = shift.get("start_time") if scheduled else time
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE shifts SET is_active = 0, end_time = ?, end_at = ?, comment = ?, "
            "on_lunch = 0 "
            "WHERE id = ? AND is_active = 1",
            (stored_end_time, end_at.isoformat(), comment, shift["id"])
        )
        if cursor.rowcount != 1:
            await db.rollback()
            return None
        await db.commit()
        sid = shift["id"]
    # === НОВОЕ: заморозить заработок закрытой смены ===
    if sid:
        await freeze_earned(sid)
    return sid

# === НОВОЕ: запомнить id живого сообщения смены ===
async def set_report_msg_id(sid, mid):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE shifts SET report_msg_id = ? WHERE id = ?", (mid, sid))
        await db.commit()

async def add_action(uid, sid, mid, atype, codes=None, qty=0, city_id=None):
    cstr = ",".join(codes) if codes else ""
    if city_id is None:
        shift = await get_shift_by_id(sid)
        city_id = shift.get("city_id") if shift else None
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO actions (user_id, shift_id, message_id, action_type, bike_codes, "
            "quantity, city_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uid, sid, mid, atype, cstr, qty, city_id)
        )
        await db.commit()

async def delete_actions_by_message(uid, mid, city_id=None):
    async with aiosqlite.connect(DB_PATH) as db:
        where = "user_id = ? AND message_id = ?"
        params = [uid, mid]
        if city_id is not None:
            where += " AND city_id = ?"
            params.append(city_id)
        rows = await (await db.execute(
            f"SELECT DISTINCT shift_id FROM actions WHERE {where} ORDER BY shift_id", params
        )).fetchall()
        await db.execute(
            f"DELETE FROM actions WHERE {where}", params
        )
        await db.commit()
        return [row[0] for row in rows]


async def get_action_shift_ids(uid, mid, city_id, chat_id=0):
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute(
            "SELECT DISTINCT shift_id FROM actions WHERE user_id = ? AND message_id = ? "
            "AND city_id = ? AND chat_id = ? ORDER BY shift_id",
            (uid, mid, city_id, chat_id)
        )).fetchall()
        return [row[0] for row in rows]


async def replace_message_actions(uid, mid, city_id, shift_id, actions, event_version,
                                  chat_id=0):
    """Атомарно заменяет результат разбора сообщения; последняя правка побеждает.

    chat_id входит в ключ: в городе с двумя группами (Химки) message_id
    из разных чатов совпадают, и без chat_id привязка из чужой группы
    приводила к тихому отказу записи.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA busy_timeout = 15000")
        await db.execute("BEGIN IMMEDIATE")
        version_row = await (await db.execute(
            "SELECT shift_id, last_event_version FROM work_message_links "
            "WHERE city_id = ? AND chat_id = ? AND user_id = ? AND message_id = ?",
            (city_id, chat_id, uid, mid)
        )).fetchone()
        shift_exists = await (await db.execute(
            "SELECT 1 FROM shifts WHERE id = ? AND user_id = ? AND city_id = ?",
            (shift_id, uid, city_id)
        )).fetchone()
        if not version_row or version_row[0] != shift_id or not shift_exists:
            await db.rollback()
            return [], False
        if (version_row[1] is not None
                and event_version < float(version_row[1])):
            await db.rollback()
            return [], False
        rows = await (await db.execute(
            "SELECT DISTINCT shift_id FROM actions WHERE user_id = ? AND message_id = ? "
            "AND city_id = ? AND chat_id = ? ORDER BY shift_id",
            (uid, mid, city_id, chat_id)
        )).fetchall()
        await db.execute(
            "DELETE FROM actions WHERE user_id = ? AND message_id = ? AND city_id = ? "
            "AND chat_id = ?",
            (uid, mid, city_id, chat_id)
        )
        for action in actions:
            await db.execute(
                "INSERT INTO actions (user_id, shift_id, message_id, action_type, bike_codes, "
                "quantity, city_id, chat_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (uid, shift_id, mid, action["action_type"],
                 ",".join(action.get("bike_codes") or []), action.get("quantity", 0),
                 city_id, chat_id)
            )
        await db.execute(
            "UPDATE work_message_links SET last_event_version = ? "
            "WHERE city_id = ? AND chat_id = ? AND user_id = ? AND message_id = ?",
            (event_version, city_id, chat_id, uid, mid)
        )
        await db.commit()
        return [row[0] for row in rows], True


async def get_work_message_shift(uid, mid, city_id, chat_id=0):
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute(
            "SELECT shift_id FROM work_message_links "
            "WHERE city_id = ? AND chat_id = ? AND user_id = ? AND message_id = ?",
            (city_id, chat_id, uid, mid)
        )).fetchone()
        return row[0] if row else None


async def link_work_message(uid, mid, city_id, shift_id, created_at=None, chat_id=0):
    """Привязка сообщения к смене.

    chat_id обязателен: message_id в Telegram уникален только ВНУТРИ чата.
    В городе с двумя группами (Химки) без него сообщения из разных групп
    с одинаковым message_id перетирали друг друга, и действия терялись.
    """
    created_at = created_at or datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA busy_timeout = 15000")
        await db.execute(
            "INSERT INTO work_message_links (city_id, chat_id, user_id, message_id, shift_id, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(city_id, chat_id, user_id, message_id) DO NOTHING",
            (city_id, chat_id, uid, mid, shift_id, created_at)
        )
        await db.commit()

async def get_stats(sid):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute(
            "SELECT action_type, bike_codes, quantity FROM actions WHERE shift_id = ?",
            (sid,)
        )
        rows = await c.fetchall()
        # sticker используется только сменами Химок, остальные города получают 0.
        s = {'move': 0, 'fix': 0, 'repair': 0, 'to_sc': 0, 'from_sc': 0,
             'battery': 0, 'sticker': 0}
        for r in rows:
            atype = r['action_type']
            if atype in s:
                codes = r['bike_codes']
                if codes:
                    s[atype] += len(codes.split(','))
                if r['quantity']:
                    s[atype] += r['quantity']
        return s

# ============================================================================
# [05-PARSER] ПАРСЕР ДЕЙСТВИЙ И СПЕЦИАЛЬНЫЕ ПРАВИЛА ГОРОДОВ
# ============================================================================
# [05-PARSER:KEYWORDS] Опечатки и общие шаблоны действий.
_WORK_TYPOS = {
    "перемистил": "переместил",
    "пиреместил": "переместил",
    "переместл": "переместил",
    "перемстил": "переместил",
    "перестил": "переместил",
    "попровил": "поправил",
    "паправил": "поправил",
    "попарвил": "поправил",
    "ремнот": "ремонт",
    "превез": "привез",
    "привз": "привез",
    "привезз": "привез",
    "вывезз": "вывез",
    "заменл": "заменил",
    "батерею": "батарею",
}

# Порядок важен: работа с СЦ и АКБ точнее общих глаголов.
_ACTION_PATTERNS = (
    ("to_sc", re.compile(
        r"(?:\b(?:прив[её]з|доставил|отв[её]з|зав[её]з)\w*\b[^;.!?\n]{0,24}"
        r"\b(?:на|в)\s*сц\b)|(?:\bна\s*сц\b)")),
    ("from_sc", re.compile(
        r"(?:\b(?:выв[её]з|забрал|ув[её]з)\w*\b[^;.!?\n]{0,24}"
        r"\b(?:из|с)\s*сц\b)|(?:\b(?:из|с)\s*сц\b)")),
    ("battery", re.compile(
        r"(?:\b(?:замен|помен|сменил|перестав)\w*\b[^;.!?\n]{0,20}\b(?:акб|батаре\w*)\b)"
        r"|(?:\b(?:акб|батаре\w*)\b[^;.!?\n]{0,20}\b(?:замен|помен|сменил|перестав)\w*\b)")),
    ("repair", re.compile(
        r"\b(?:ремонт\w*|отремонт\w*|почин\w*|чин[июяе]\w*)\b")),
    ("move", re.compile(
        r"\b(?:перемест\w*|перемещ\w*|перен[её]с\w*|перестав\w*|"
        r"перегнал\w*|передвин\w*|перекат\w*|перев[её]з\w*|"
        r"переброс\w*|расстав\w*|перетян\w*)\b")),
    ("fix", re.compile(
        r"\b(?:поправ(?:ил|ила|или|лено|лены|ить|лял|ляла)\w*|выровн\w*|"
        r"поднял\w*|почист\w*|очист\w*|прот[её]р\w*|помыл\w*)\b"
        r"|\bпоставил\s+ровно\b")),
)


# Мусор, из которого нельзя брать номера байков: @ники, ссылки, телефоны,
# слова с цифрами внутри (Иван_9999), хвосты вида «id12345».
_NOISE_PATTERNS = (
    re.compile(r"https?://\S+", re.IGNORECASE),      # ссылки
    re.compile(r"\bt\.me/\S+", re.IGNORECASE),        # телеграм-ссылки
    re.compile(r"@[\w\d_]+"),                         # @ники
    # Телефон: 11+ цифр СЛИТНО либо с разделителями -()+, но НЕ через пробел,
    # иначе список номеров байков «0905 0949 0708 0628 0828» принимается
    # за телефон и вырезается целиком.
    re.compile(r"\+?\d[\d\-\(\)]{9,}\d"),
    re.compile(r"\b[a-zA-Zа-яА-ЯёЁ]+_?\d+\b"),        # Иван9999, id12345
    re.compile(r"\b\d+_?[a-zA-Zа-яА-ЯёЁ]+\b"),        # 9999иван
)


def strip_noise_for_codes(text):
    """Убирает ники, ссылки и телефоны, чтобы их цифры не стали номерами байков.

    Возвращает текст той же структуры (переводы строк сохраняются) —
    парсеры разбирают его построчно, поэтому строки терять нельзя.
    """
    if not isinstance(text, str):
        return ""
    cleaned = text
    for pattern in _NOISE_PATTERNS:
        cleaned = pattern.sub(" ", cleaned)
    return cleaned


def _normalise_work_text(text):
    text = strip_noise_for_codes(text).lower().replace("cц", "сц").replace("сc", "сц")
    for typo, fixed in _WORK_TYPOS.items():
        text = re.sub(rf"\b{re.escape(typo)}\b", fixed, text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _distance_to_span(position, start, end):
    if start <= position <= end:
        return 0
    return min(abs(position - start), abs(position - end))


def _clause_action_matches(clause):
    if re.match(r"^(?:что|как|где|почему|зачем)\b", clause):
        return []
    found = []
    for priority, (atype, pattern) in enumerate(_ACTION_PATTERNS):
        for match in pattern.finditer(clause):
            found.append({
                "action_type": atype,
                "start": match.start(),
                "end": match.end(),
                "priority": priority,
            })
    # Указатель будущего может стоять после глагола. Привязываем его к
    # ближайшему действию, чтобы в «сделал 1234, завтра поправлю 5678»
    # не потерять уже выполненную первую часть.
    future_targets = set()
    for cue in re.finditer(r"\b(?:завтра|послезавтра|позже)\b", clause):
        if found:
            nearest = min(
                found,
                key=lambda item: _distance_to_span(cue.start(), item["start"], item["end"]),
            )
            between = clause[min(cue.start(), nearest["end"]):max(cue.start(), nearest["start"])]
            if "," not in between:
                future_targets.add(id(nearest))
    negative_targets = set()
    for cue in re.finditer(
        r"\bне\s+(?:делал\w*|выполнял\w*|ремонтировал\w*|чинил\w*|менял\w*)\b",
        clause,
    ):
        if found:
            nearest = min(
                found,
                key=lambda item: _distance_to_span(cue.start(), item["start"], item["end"]),
            )
            negative_targets.add(id(nearest))
    question_targets = set()
    for cue in re.finditer(r"\bли\b", clause):
        if found:
            nearest = min(
                found,
                key=lambda item: _distance_to_span(cue.start(), item["start"], item["end"]),
            )
            question_targets.add(id(nearest))

    # Убираем вложенные менее точные совпадения (например, «переставил АКБ»
    # не должно одновременно стать перемещением).
    selected = []
    for candidate in sorted(found, key=lambda x: (x["priority"], x["start"], -x["end"])):
        if id(candidate) in future_targets or id(candidate) in negative_targets \
                or id(candidate) in question_targets:
            continue
        prefix = clause[max(0, candidate["start"] - 32):candidate["start"]]
        if re.search(
            r"(?:^|[\s,:;—-])(?:не|буду|будет|будем|нужно|надо|план|"
            r"планирую|планируем|собираюсь|хочу|можно|кто|завтра|стоит|находится|остался|"
            r"был|была|были|сейчас|"
            r"отправь(?:те)?|отвези(?:те)?|забери(?:те)?)"
            r"[\s,:;—-]*(?:\w+[\s,:;—-]+){0,2}$", prefix
        ):
            continue
        matched_text = clause[candidate["start"]:candidate["end"]]
        if (candidate["action_type"] in {"to_sc", "from_sc"}
                and matched_text.strip() in {"на сц", "в сц", "с сц", "из сц"}
                and re.search(
                    r"\b(?:фото|вопрос|подскаж|статус|сломан|неисправ|был|была|были|"
                    r"сейчас|стоит|находится|остался)\w*\b",
                    clause,
                )):
            continue
        if re.search(
            r"\b(?:перемести(?:ть|те)?|перенеси(?:те)?|переставь(?:те)?|"
            r"поправь(?:те)?|поправить|почини(?:ть|те)?|"
            r"отремонтируй(?:те)?|отремонтировать|почисти(?:ть|те)?|"
            r"очисти(?:ть|те)?|помой(?:те)?|"
            r"замени(?:ть|те)?|поменяй(?:те)?|забери(?:ть|те)?|"
            r"отвези(?:ти|те)?|привези(?:ти|те)?|вывези(?:ти|те)?)\b",
            matched_text,
        ):
            continue
        overlaps = any(
            not (candidate["end"] <= current["start"] or candidate["start"] >= current["end"])
            for current in selected
        )
        if not overlaps:
            selected.append(candidate)
    return sorted(selected, key=lambda x: x["start"])


def _parse_message_extensions(text):
    """Разбирает живой текст, привязывая номера к ближайшему действию.

    Четырёхзначное число — номер байка. Одно-трёхзначное число считается
    количеством только рядом с распознанным действием и не считается, если в
    этой же части сообщения уже перечислены номера байков.
    """
    text = _normalise_work_text(text)
    if not text:
        return []

    # Даты не разрываем на псевдо-количества: «12.07.2026» не означает 12 байков.
    text = re.sub(r"(?<!\d)\d{1,4}[./-]\d{1,2}[./-]\d{1,4}(?!\d)", " ", text)
    # «в 9.30» — время, а не девять действий.
    text = re.sub(r"(?<!\d)(?:[01]?\d|2[0-3])[.]\d{2}(?!\d)", " ", text)

    totals = {}
    # Точка между цифрами остаётся частью десятичного числа; остальные точки
    # по-прежнему разделяют предложения.
    clauses = [
        part.strip() for part in re.split(r"[\n;!?]+|(?<!\d)\.|\.(?!\d)", text)
        if part.strip()
    ]
    for clause in clauses:
        clause = re.split(
            r"\b(?:и|а)\s+(?:завтра|послезавтра|позже)\b", clause, maxsplit=1
        )[0].strip()
        # План после запятой не должен ни отменять уже выполненную часть, ни
        # отдавать её парсеру свои номера байков.
        clause = ",".join(
            part for part in clause.split(",")
            if not re.search(r"\b(?:завтра|послезавтра|позже)\b", part)
        ).strip()
        if not clause:
            continue
        matches = _clause_action_matches(clause)
        if not matches:
            continue

        assigned_codes = {index: [] for index in range(len(matches))}
        for code_match in re.finditer(r"(?<!\d)(\d{4})(?!\d)", clause):
            code = code_match.group(1)
            suffix = clause[code_match.end():code_match.end() + 12]
            if 1900 <= int(code) <= 2099 and re.match(r"\s*(?:год|г\.)", suffix):
                continue
            nearest = min(
                range(len(matches)),
                key=lambda i: _distance_to_span(code_match.start(), matches[i]["start"], matches[i]["end"])
            )
            if code not in assigned_codes[nearest]:
                assigned_codes[nearest].append(code)

        # Совместимость со старым поведением: если в одной фразе указан один
        # байк и несколько действий над ним, этот номер относится ко всем.
        all_clause_codes = list(dict.fromkeys(
            code for codes in assigned_codes.values() for code in codes
        ))
        if len(all_clause_codes) == 1 and len(matches) > 1:
            for index in range(len(matches)):
                if not assigned_codes[index]:
                    assigned_codes[index] = all_clause_codes.copy()

        assigned_qty = {index: 0 for index in range(len(matches))}
        for qty_match in re.finditer(r"(?<![\d:])(\d{1,3})(?![\d:])", clause):
            suffix = clause[qty_match.end():qty_match.end() + 12]
            prefix = clause[max(0, qty_match.start() - 3):qty_match.start()]
            if re.match(r"[.,]\d", suffix) or re.search(r"\d[.,]\s*$", prefix):
                continue
            if re.match(r"\s*(?:км|мин|час|руб|₽|%|год)", suffix) or "%" in prefix:
                continue
            nearest = min(
                range(len(matches)),
                key=lambda i: _distance_to_span(qty_match.start(), matches[i]["start"], matches[i]["end"])
            )
            # Если перечислены конкретные байки, количество не дублирует их.
            if not assigned_codes[nearest]:
                assigned_qty[nearest] += int(qty_match.group(1))

        for index, match in enumerate(matches):
            codes = assigned_codes[index]
            qty = assigned_qty[index]
            if not codes and qty <= 0:
                continue
            item = totals.setdefault(match["action_type"], {"bike_codes": [], "quantity": 0})
            for code in codes:
                if code not in item["bike_codes"]:
                    item["bike_codes"].append(code)
            item["quantity"] += qty

    order = ("move", "fix", "repair", "battery", "to_sc", "from_sc")
    return [
        {"action_type": atype, "bike_codes": totals[atype]["bike_codes"],
         "quantity": totals[atype]["quantity"]}
        for atype in order if atype in totals
    ]


def get_action_type(kw):
    """Эталонное сопоставление из текущей версии GitHub без изменений."""
    if kw in ['привез на сц', 'привёз на сц', 'на сц привез', 'на сц']:
        return 'to_sc'
    if kw in ['вывез из сц', 'вывёз из сц', 'из сц вывез', 'вывез с сц', 'из сц']:
        return 'from_sc'
    if kw in ['ремонт', 'поломк', 'сломан']:
        return 'repair'
    if kw in ['переместил', 'перенес', 'перенёс', 'переставил', 'перемещ']:
        return 'move'
    if kw in ['поправил', 'выровнял', 'чист', 'поправ']:
        return 'fix'
    return None


def _parse_message_github(text):
    """Дословная логика parse_message из main ветки voglogpro/bibibike-bot."""
    # Ники/ссылки/телефоны вырезаем до поиска номеров байков.
    text = strip_noise_for_codes(text).lower().strip()
    all_codes = re.findall(r'\b(\d{4})\b', text)
    lines = text.split('\n')

    repair_codes = []
    for line in lines:
        if any(kw in line for kw in ['ремонт', 'поломк', 'сломан']):
            repair_codes.extend(re.findall(r'\b(\d{4})\b', line))

    # Номера, стоящие в одной строке со своим глаголом. Нужны, чтобы в
    # многострочном сообщении каждое действие забирало ТОЛЬКО свои байки:
    #   Перемещение 0905 0949 0708
    #   Поправил 0679
    # Раньше обоим действиям раздавались все номера сразу.
    _ALL_KEYWORDS = ['привез на сц', 'привёз на сц', 'на сц привез',
                     'вывез из сц', 'вывёз из сц', 'из сц вывез', 'вывез с сц',
                     'ремонт', 'поломк', 'сломан',
                     'переместил', 'перенес', 'перенёс', 'переставил', 'перемещ',
                     'поправил', 'выровнял', 'чист', 'поправ',
                     'на сц', 'из сц']

    line_codes = {}          # action_type -> номера из строк с этим глаголом
    for line in lines:
        line_types = set()
        for _kw in _ALL_KEYWORDS:
            if _kw in line:
                _at = get_action_type(_kw)
                if _at:
                    line_types.add(_at)
        if not line_types:
            continue
        found_codes = re.findall(r'\b(\d{4})\b', line)
        if not found_codes:
            continue
        # Если в одной строке несколько разных действий — номера строки
        # относим к каждому из них: разделить их надёжнее нельзя.
        for _at in line_types:
            line_codes.setdefault(_at, []).extend(found_codes)

    keywords_found = []

    for kw in ['привез на сц', 'привёз на сц', 'на сц привез',
               'вывез из сц', 'вывёз из сц', 'из сц вывез', 'вывез с сц',
               'ремонт', 'поломк', 'сломан',
               'переместил', 'перенес', 'перенёс', 'переставил', 'перемещ',
               'поправил', 'выровнял', 'чист', 'поправ',
               'на сц', 'из сц']:
        if kw in text:
            atype = get_action_type(kw)
            if atype and atype not in [a['action_type'] for a in keywords_found]:
                qty = 0
                for line in lines:
                    if kw in line:
                        qty_match = re.search(r'(?<!\d)(\d{1,3})(?!\d)(?![а-яa-z])', line)
                        if qty_match:
                            num = int(qty_match.group(1))
                            if not re.search(r'\b\d{4}\b', line):
                                qty = num
                        break
                keywords_found.append({'action_type': atype, 'quantity': qty})

    if not keywords_found:
        return []

    qty_actions = [kw for kw in keywords_found if kw['quantity'] > 0]
    code_actions = [kw for kw in keywords_found if kw['quantity'] == 0]
    results = []

    for kw in qty_actions:
        results.append({'action_type': kw['action_type'], 'bike_codes': [], 'quantity': kw['quantity']})

    # Раздаём номера по строкам, только если в сообщении реально несколько
    # разных действий со своими номерами. В привычном формате
    # «Переместил\n0341\n0344» (глагол сверху, номера ниже) поведение
    # остаётся прежним: единственное действие забирает все номера.
    own_codes_actions = [a for a in code_actions if line_codes.get(a['action_type'])]
    split_by_line = len(code_actions) > 1 and len(own_codes_actions) > 1

    for kw in code_actions:
        atype = kw['action_type']
        if atype == 'repair':
            codes = repair_codes.copy() if repair_codes else []
        elif split_by_line:
            # Каждое действие забирает номера только из своих строк.
            codes = list(dict.fromkeys(line_codes.get(atype, [])))
        else:
            codes = all_codes.copy() if all_codes else []
        results.append({'action_type': atype, 'bike_codes': codes, 'quantity': 0})

    return results


# ═══════════════════════════════════════════════════════════════
# ТРЕТИЙ СЛОЙ ПАРСЕРА: опечатки и Т9
# Включается ТОЛЬКО когда эталонный и расширенный слои вернули пусто,
# поэтому регрессия невозможна. Слой лишь чинит слово в тексте, а разбор
# номеров и количества выполняет прежний расширенный парсер.
# ═══════════════════════════════════════════════════════════════

# (корень для сравнения, канонная замена, тип действия)
_FUZZY_STEMS = (
    ("перемещ", "переместил", "move"),
    ("перемест", "переместил", "move"),
    ("перенес", "перенес", "move"),
    ("передвин", "передвинул", "move"),
    ("перекат", "перекатил", "move"),
    ("поправ", "поправил", "fix"),
    ("выровн", "выровнял", "fix"),
    ("почист", "почистил", "fix"),
    ("ремонт", "ремонт", "repair"),
    ("поломк", "поломка", "repair"),
    ("отремонт", "отремонтировал", "repair"),
    ("почин", "починил", "repair"),
    ("привез", "привез", "to_sc"),
    ("доставил", "доставил", "to_sc"),
    ("вывез", "вывез", "from_sc"),
    ("забрал", "забрал", "from_sc"),
    ("заменил", "заменил", "battery"),
    ("поменял", "поменял", "battery"),
)

# Слова, которые нельзя чинить: близки по написанию, но действием не являются
# (в т.ч. повелительные формы — просьбы, а не выполненная работа).
_FUZZY_STOP = {
    "поставил", "поставила", "посмотрел", "проверил", "потерял", "получил",
    "поехал", "пошел", "пошёл", "помог", "посчитал", "поработал", "поговорил",
    "привет", "подскажи", "подскажите", "покажи", "покажите", "помогите",
    "перерыв", "переписал", "перезвоню", "перекур", "передал", "переделал",
    "перемести", "переместите", "перенеси", "перенесите", "переставь",
    "поправь", "поправьте", "почини", "почините", "привези", "привезите",
    "вывези", "вывезите", "забери", "заберите", "замени", "замените",
    "поменяй", "поменяйте", "отвези", "отвезите", "доставь", "доставьте",
}


def _damerau_levenshtein(a, b, limit=3):
    """Расстояние с учётом перестановки соседних букв (частая опечатка Т9)."""
    la, lb = len(a), len(b)
    if abs(la - lb) > limit:
        return limit + 1
    prev2, prev = None, list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + cost)
        if min(cur) > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[lb]


def _fuzzy_action_for_word(word):
    """Подбирает действие для слова с опечаткой. None — если не уверены."""
    if len(word) < 5 or word in _FUZZY_STOP:
        return None
    matches = []
    for stem, canon, atype in _FUZZY_STEMS:
        if word[:2] != stem[:2]:          # защита: «поправил» ≠ «привёз»
            continue
        probe = word[:len(stem)]
        if len(probe) < len(stem) - 1:
            continue
        limit = 1 if len(stem) <= 6 else 2
        dist = _damerau_levenshtein(probe, stem, limit)
        if dist <= limit:
            matches.append((dist, atype, canon))
    if not matches:
        return None
    matches.sort(key=lambda item: item[0])
    best = matches[0]
    # Неоднозначность между разными типами действий — не угадываем.
    if len({item[1] for item in matches if item[0] == best[0]}) > 1:
        logger.info(f"fuzzy: '{word}' — неоднозначно, пропускаю")
        return None
    return best


def _fuzzy_correct_text(text):
    """Возвращает (исправленный текст, список правок)."""
    fixes = []

    def repl(match):
        word = match.group(0)
        found = _fuzzy_action_for_word(word)
        if not found:
            return word
        dist, atype, canon = found
        fixes.append((word, canon, atype, dist))
        return canon

    corrected = re.sub(r"[а-яa-z]{5,}", repl, text)
    return corrected, fixes


def _parse_message_fuzzy(text):
    """Разбор сообщения с опечатками. Работает только при наличии чисел."""
    if not isinstance(text, str):
        return []
    normalised = _normalise_work_text(text)
    if not normalised:
        return []
    # Без номеров байков или количества не реагируем — иначе бот начнёт
    # ловить обычную переписку в чате.
    if not re.search(r"(?<!\d)\d{1,4}(?!\d)", normalised):
        return []
    corrected, fixes = _fuzzy_correct_text(normalised)
    if not fixes:
        return []
    results = _parse_message_extensions(corrected)
    if not results and '\n' in corrected:
        results = _parse_message_extensions(re.sub(r"\s*\n+\s*", " ", corrected))
    if results:
        for word, canon, atype, dist in fixes:
            logger.info(f"fuzzy: '{word}' -> {atype} ('{canon}', dist={dist})")
    return results


def _enforce_quantity_policy(actions):
    """Количество без кодов разрешено только для поправок.

    Четырёхзначные номера байков сохраняются для всех действий. Значения
    quantity у перемещений, ремонта, АКБ, привоза и вывоза отбрасываются —
    такие действия теперь обязательно подтверждаются номерами байков.
    """
    cleaned = []
    for action in actions or []:
        item = dict(action)
        codes = list(dict.fromkeys(item.get("bike_codes") or []))
        try:
            quantity = int(item.get("quantity") or 0)
        except (TypeError, ValueError):
            quantity = 0
        if item.get("action_type") != "fix":
            quantity = 0
        if not codes and quantity <= 0:
            continue
        item["bike_codes"] = codes
        item["quantity"] = quantity
        cleaned.append(item)
    return cleaned


def parse_message(text):
    """Три слоя распознавания и единая политика количества.

    Старые сообщения с четырёхзначными кодами проходят через исходную функцию.
    Каждый следующий слой включается, только если предыдущий не нашёл ничего.
    После распознавания количество без кодов сохраняется только у поправок.
    """
    if not isinstance(text, str):
        return []
    legacy = _parse_message_github(text)
    if legacy:
        return _enforce_quantity_policy(legacy)
    additions = _parse_message_extensions(text)
    if not additions and '\n' in text:
        additions = _parse_message_extensions(re.sub(r"\s*\n+\s*", " ", text))
    if additions:
        return _enforce_quantity_policy(additions)
    return _enforce_quantity_policy(_parse_message_fuzzy(text))


def _codes_only_line(line):
    """Возвращает коды из строки, если кроме кодов и разделителей в ней нет текста."""
    codes = re.findall(r"(?<!\d)(\d{4})(?!\d)", line or "")
    if not codes:
        return []
    remainder = re.sub(r"(?<!\d)\d{4}(?!\d)", "", line or "")
    remainder = re.sub(r"[\s,;|]+", "", remainder)
    return list(dict.fromkeys(codes)) if not remainder else []


def _merge_parsed_actions(actions):
    """Объединяет одинаковые типы действий без повторов кодов."""
    totals = {}
    for action in actions or []:
        atype = action.get("action_type")
        if not atype:
            continue
        item = totals.setdefault(atype, {"bike_codes": [], "quantity": 0})
        for code in action.get("bike_codes") or []:
            if code not in item["bike_codes"]:
                item["bike_codes"].append(code)
        item["quantity"] += int(action.get("quantity") or 0)
    order = ("move", "fix", "repair", "battery", "to_sc", "from_sc")
    return [
        {"action_type": atype, "bike_codes": totals[atype]["bike_codes"],
         "quantity": totals[atype]["quantity"]}
        for atype in order if atype in totals
    ]


# [05-PARSER:CITY-RULES] Специальные форматы отдельных городов и тем.
def parse_polyana_message(text):
    """Дополнительный синтаксис Красной Поляны: ``+N`` и список кодов.

    ``+3`` служит маркером перемещения и контрольным количеством. Фактически
    засчитываются только перечисленные после него четырёхзначные номера. После
    следующей текстовой строки снова работает обычный эталонный парсер.
    """
    if not isinstance(text, str):
        return []
    lines = text.splitlines()
    consumed = set()
    move_codes = []
    index = 0
    while index < len(lines):
        marker = re.fullmatch(r"\s*\+\s*(\d{1,3})?\s*", lines[index])
        if not marker:
            index += 1
            continue
        consumed.add(index)
        declared = int(marker.group(1)) if marker.group(1) else None
        block_codes = []
        cursor = index + 1
        while cursor < len(lines):
            codes = _codes_only_line(lines[cursor])
            if not codes:
                break
            consumed.add(cursor)
            for code in codes:
                if code not in block_codes:
                    block_codes.append(code)
            cursor += 1
        if declared is not None and declared != len(block_codes):
            logger.warning(
                "Красная Поляна: после +%s перечислено кодов: %s; "
                "засчитываю только перечисленные коды",
                declared,
                len(block_codes),
            )
        for code in block_codes:
            if code not in move_codes:
                move_codes.append(code)
        index = max(cursor, index + 1)

    remainder = "\n".join(
        "" if line_index in consumed else line
        for line_index, line in enumerate(lines)
    )
    parsed = parse_message(remainder)
    if move_codes:
        parsed.append({"action_type": "move", "bike_codes": move_codes, "quantity": 0})
    return _merge_parsed_actions(parsed)

# === НОВОЕ: парсер темы NPB — голые 4-значные номера = замены АКБ ===
def parse_npb_message(text):
    """Эталонная логика NPB из текущей версии GitHub без изменений."""
    codes = re.findall(r'\b(\d{4})\b', strip_noise_for_codes(text))
    if not codes:
        return []
    return [{'action_type': 'battery', 'bike_codes': codes, 'quantity': 0}]


# === НОВОЕ: парсер темы «Ремонт» (водители Химок) ===
def parse_repair_message(text):
    """Номера столбиком, а слово «ремонт» — отдельной строкой ниже.

    Так пишут в теме «Ремонт» у водителей Химок:

        0812
        0409
        Отсутствует тормоза и детали корпуса отвез на склад, ремонт

    Обычный парсер такое не берёт: он ищет номера в той же строке, где
    стоит слово. Поэтому сначала пробуем эталонный parse_message (он
    покрывает «0579 — ремонт (описание)» и смешанные сообщения), а если
    он ничего не нашёл — при наличии слова про ремонт засчитываем все
    четырёхзначные номера сообщения как ремонт.

    Без слова про ремонт голые номера НЕ считаются: в этой теме сообщение
    обязано быть подписано.
    """
    parsed = parse_message(text)
    if parsed:
        return parsed
    if not isinstance(text, str) or not _REPAIR_TOPIC_HINT.search(text):
        return []
    codes = re.findall(r'\b(\d{4})\b', strip_noise_for_codes(text))
    if not codes:
        return []
    return [{'action_type': 'repair', 'bike_codes': codes, 'quantity': 0}]


# Слова, по которым сообщение в теме «Ремонт» считается подписанным.
_REPAIR_TOPIC_HINT = re.compile(
    r"\b(?:ремонт\w*|поломк\w*|сломан\w*|неисправ\w*|не\s*работает|"
    r"люфт\w*|отсутству\w*|течёт|течет|порван\w*|разбит\w*)",
    re.IGNORECASE,
)


def parse_bare_repair_message(text):
    """Ремонт скаутов Химок: все четырёхзначные номера сообщения.

    Описание неисправности, фото с подписью и остальные числа не мешают.
    Правило действует исключительно в теме 4485 группы скаутов Химок.
    """
    if not isinstance(text, str):
        return []
    codes = list(dict.fromkeys(re.findall(r"(?<!\d)(\d{4})(?!\d)", text)))
    if not codes:
        return []
    return [{"action_type": "repair", "bike_codes": codes, "quantity": 0}]


_STICKER_TOPIC_HINT = re.compile(
    r"\b(?:оклейк\w*|оклеил(?:а|и)?|поклеил(?:а|и)?|"
    r"нан(?:е|ё)с(?:ла|ли)?|наклеил(?:а|и)?)\b",
    re.IGNORECASE,
)


def parse_sticker_message(text):
    """Оклейка Химок: слово об оклейке и минимум один 4-значный номер."""
    if not isinstance(text, str):
        return []
    completed = False
    for match in _STICKER_TOPIC_HINT.finditer(text):
        prefix = text[max(0, match.start() - 24):match.start()]
        if not re.search(r"\bне\s*$", prefix, re.IGNORECASE):
            completed = True
            break
    if not completed:
        return []
    codes = list(dict.fromkeys(re.findall(r"(?<!\d)(\d{4})(?!\d)", text)))
    if not codes:
        return []
    return [{"action_type": "sticker", "bike_codes": codes, "quantity": 0}]


# === НОВОЕ: парсер темы «Перемещения»/«Подвозы» (Химки) ===
def parse_moves_message(text):
    """Действия разбираются эталонным парсером, голые номера = перемещения.

    Работает по образцу NPB, только результат — 'move', а не 'battery'.
    Применяется ТОЛЬКО в теме, указанной как topic_moves у города
    (Химки: «Перемещения» у скаутов, «Подвозы» у водителей).
    Если в сообщении есть «ремонт», «поправил», «привёз на СЦ» и другие
    знакомые слова, сначала работает прежний parse_message. Только когда он
    ничего не нашёл, все 4-значные номера считаются перемещениями.
    """
    parsed = parse_message(text)
    if parsed:
        return parsed
    codes = re.findall(r'\b(\d{4})\b', strip_noise_for_codes(text))
    if not codes:
        return []
    return [{'action_type': 'move', 'bike_codes': codes, 'quantity': 0}]


def topic_parser_kind(city, thread_id):
    """Каким парсером разбирать сообщение в этой теме.

    'moves'  — голые номера считаются перемещениями (тема topic_moves);
    'npb'    — голые номера считаются заменами АКБ (тема topic_npb);
    'tasks'  — обычный парсер по глаголам, как во всех остальных темах.
    """
    # Специальная тема перемещений используется только там, где она явно
    # настроена (сейчас это ролевые группы Химок). Краснодар не затрагивается:
    # у него topic_moves=NULL, поэтому остаётся прежний parse_message.
    if city.get("topic_moves") is not None and thread_id == city.get("topic_moves"):
        return "moves"
    if thread_id == city.get("topic_npb"):
        return "npb"
    # Скауты Химок: любые 4-значные номера в теме 4485 = ремонт.
    if thread_id in BARE_REPAIR_TOPICS.get(city.get("group_id"), ()):
        return "bare_repair"
    # Скауты Химок: номер + слово об оклейке в теме 2290 = оклейка.
    if thread_id in STICKER_TOPICS.get(city.get("group_id"), ()):
        return "sticker"
    # Тема «Ремонт»: номера столбиком + подпись словом ниже.
    if thread_id in REPAIR_TOPICS.get(city.get("group_id"), ()):
        return "repair"
    return "tasks"


# ============================================================================
# [06-MANUAL-SHIFTS] РУЧНЫЕ СИГНАЛЫ И ОТЧЁТЫ О СМЕНАХ
# ============================================================================

def parse_manual_report(text):
    """Строгий разбор ручного итогового отчёта из темы ОТЧЁТЫ.

    Уверенным считаем сообщение с явно подписанными началом и окончанием
    либо ровно с двумя корректными временами. Дополнительное время обеда или
    перерыва не мешает, если границы смены подписаны. Неоднозначное сообщение
    сохраняется для проверки админом и само не влияет на статистику.
    """
    normalised = _normalise_work_text(text)
    if not normalised:
        return None, "пустое сообщение"
    if re.search(
        r"\b(?:завтра|послезавтра|план\w*|буду|будет|отмен\w*|"
        r"не\s+(?:работал\w*|выходил\w*))\b",
        normalised,
    ):
        return None, "сообщение похоже на план или отмену, а не завершённую смену"
    times = []
    for match in re.finditer(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", normalised):
        hour, minute = int(match.group(1)), int(match.group(2))
        if hour <= 23 and minute <= 59:
            times.append(f"{hour}:{minute:02d}")
    time_pattern = r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)"
    start_matches = re.findall(
        rf"\b(?:начал(?:а|и)?|начало|стартовал(?:а|и)?)\b[^\d\n]{{0,24}}{time_pattern}",
        normalised,
    )
    end_matches = re.findall(
        rf"\b(?:закончил(?:а|и)?|окончил(?:а|и)?|конец|окончание|финиш)\b"
        rf"[^\d\n]{{0,24}}{time_pattern}",
        normalised,
    )
    if len(start_matches) == 1 and len(end_matches) == 1:
        start_time = f"{int(start_matches[0][0])}:{start_matches[0][1]}"
        end_time = f"{int(end_matches[0][0])}:{end_matches[0][1]}"
    elif len(times) == 2:
        start_time, end_time = times
    else:
        return None, (
            "неоднозначные времена: подпишите начало и окончание смены"
            if times else "не найдены время начала и время окончания"
        )
    actions = parse_message(normalised)
    report_cue = re.search(
        r"\b(?:отч[её]т|смена|начал\w*|закончил\w*|скаут|водитель|чарджер|"
        r"перемещено|поправлено|акб|ремонт)\b", normalised
    )
    if not actions and not report_cue:
        return None, "сообщение не похоже на отчёт смены"
    return {
        "start_time": start_time,
        "end_time": end_time,
        "actions": actions,
    }, None


# Оба порядка слов: «начал смену» и «смену начал» (то же для закрытия).
_START_VERB = r"(?:начал|начала|начали|открыл|открыла|открыли)"
_END_VERB = (r"(?:закончил|закончила|закончили|завершил|завершила|завершили|"
             r"закрыл|закрыла|закрыли)")
_MANUAL_SHIFT_START_RE = re.compile(
    rf"^(?:я\s+)?(?:{_START_VERB}\s+смену|смену\s+{_START_VERB})$"
)
_MANUAL_SHIFT_END_RE = re.compile(
    rf"^(?:я\s+)?(?:{_END_VERB}\s+смену|смену\s+{_END_VERB})$"
)
# Открытие с именем и ролью: «Смену начал Иванов И.И. скаут».
_NAMED_SHIFT_START_RE = re.compile(
    rf"^(?:я\s+)?(?:{_START_VERB}\s+смену|смену\s+{_START_VERB})\s+(.+)$"
)
_ROLE_WORDS = {
    "скаут": "Скаут", "scout": "Скаут",
    "водитель": "Водитель", "driver": "Водитель", "вод": "Водитель",
    "чарджер": "Чарджер", "charger": "Чарджер", "чардж": "Чарджер",
}


def _clean_signal_tail(text):
    raw = re.sub(r"[\s.!?,;:…✅☑✔️👍]+$", "", str(text or "").strip()).strip()
    return raw, raw.lower().replace("ё", "е")


def _manual_shift_signal(text):
    """Короткая фраза о смене без имени (оба порядка слов)."""
    raw, low = _clean_signal_tail(text)
    low = re.sub(r"\s+", " ", low).strip()
    if _MANUAL_SHIFT_START_RE.fullmatch(low):
        return "start"
    if _MANUAL_SHIFT_END_RE.fullmatch(low):
        return "end"
    return None


def _named_shift_start(text):
    """«Смену начал Иванов И.И. скаут» → {'name','role'}. Иначе None.

    Регистр имени берём из исходного текста (lower() не меняет длину строки,
    поэтому позиции совпадают), роль — по последнему слову-должности.
    """
    raw, low = _clean_signal_tail(text)
    low = re.sub(r"\s+", " ", low)
    raw = re.sub(r"\s+", " ", raw)
    match = _NAMED_SHIFT_START_RE.match(low)
    if not match:
        return None
    tail = raw[match.start(1):].strip()
    words = tail.split()
    role = ""
    if words and words[-1].lower().replace("ё", "е") in _ROLE_WORDS:
        role = _ROLE_WORDS[words[-1].lower().replace("ё", "е")]
        words = words[:-1]
    name = " ".join(words).strip()
    if not name:
        return None
    return {"name": name, "role": role}


def _as_aware_datetime(value, default=None):
    """Приводит дату сообщения к aware-datetime (UTC).

    На некоторых версиях aiogram/хостинга message.date / edit_date приходят
    целым Unix-timestamp, а не datetime — тогда .tzinfo падает. Здесь
    поддерживаем оба варианта: int/float, наивный и aware datetime.
    """
    if value is None:
        return default if default is not None else datetime.now(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if getattr(value, "tzinfo", None) is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _message_time_in_city(message, city):
    value = _as_aware_datetime(getattr(message, "date", None))
    return value.astimezone(_city_tz(city))


async def _start_manual_signal_shift(message, city, event_time, name=None, role=None):
    """Идемпотентно создаёт смену из фразы «начал смену».

    Если имя передано (форма «Смену начал Иванов И.И. скаут») — обновляем
    профиль сотрудника этими имя+роль и открываем смену под ними.
    """
    uid = message.from_user.id
    message_id = message.message_id
    user = await get_user(uid) or {}
    full_name = name or user.get("full_name") or message.from_user.full_name or f"Сотрудник #{uid}"
    role = role if role else (user.get("role") or "")
    if name:
        await add_user(uid, full_name, role, city["id"])
    start_time = event_time.strftime("%H:%M")
    start_at = _resolve_start_at(start_time, city, event_time)
    seg = _shift_segment({"start_at": start_at.isoformat()}, city)
    period = await ensure_city_period(city["id"], seg)
    period_id = (period or {}).get("id")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        existing = await (await db.execute(
            "SELECT id FROM shifts WHERE city_id = ? AND source_message_id = ? LIMIT 1",
            (city["id"], message_id),
        )).fetchone()
        if existing:
            await db.rollback()
            return existing[0]
        active = await (await db.execute(
            "SELECT id FROM shifts WHERE user_id = ? AND is_active = 1 LIMIT 1", (uid,)
        )).fetchone()
        if active:
            await db.rollback()
            return None
        cursor = await db.execute(
            "INSERT INTO shifts (user_id, full_name, role, start_time, district, is_active, "
            "created_at, city_id, start_at, source, source_message_id, period_id) "
            "VALUES (?, ?, ?, ?, '', 1, ?, ?, ?, 'manual_signal', ?, ?)",
            (uid, full_name, role, start_time, event_time.isoformat(), city["id"],
             start_at.isoformat(), message_id, period_id),
        )
        await db.commit()
        return cursor.lastrowid


async def handle_manual_shift_signal(message, city):
    """Молча открывает/закрывает ручную смену; бот-смены не изменяет."""
    if not message.from_user or getattr(message.from_user, "is_bot", False):
        return False
    text = (message.text or message.caption or "").strip()
    event_time = _message_time_in_city(message, city)
    # Форма с именем: «Смену начал Иванов И.И. скаут» — регистрируем и открываем.
    named = _named_shift_start(text)
    if named:
        await _start_manual_signal_shift(
            message, city, event_time, name=named["name"], role=named["role"]
        )
        return True
    signal = _manual_shift_signal(text)
    if not signal:
        return False
    if signal == "start":
        await _start_manual_signal_shift(message, city, event_time)
        return True

    active = await get_active_shift(message.from_user.id)
    if (active and active.get("city_id") == city["id"]
            and active.get("source") == "manual_signal"):
        sid = await end_shift(
            message.from_user.id,
            event_time.strftime("%H:%M"),
            city_id=city["id"],
            now=event_time,
        )
        # У ручной смены обычно нет отдельного живого отчёта. Но если сотрудник
        # нажимал «Обед», отчёт уже создан — финально обновим именно его.
        if sid and active.get("report_msg_id"):
            await safe_flush_report_update(sid)
    return True


async def capture_manual_report(message: Message, city):
    """Молча сохраняет ручной отчёт, не отвечая сотруднику в теме."""
    if not message.from_user or getattr(message.from_user, "is_bot", False):
        return
    text = (message.text or message.caption or "").strip()
    parsed, error = parse_manual_report(text)
    uid = message.from_user.id
    sender_name = message.from_user.full_name or f"Сотрудник #{uid}"
    tz = _city_tz(city)
    message_time = _as_aware_datetime(getattr(message, "date", None)).astimezone(tz)
    raw_event = getattr(message, "edit_date", None) or getattr(message, "date", None)
    event_time = _as_aware_datetime(raw_event).astimezone(tz)
    user = await get_user(uid) or {}
    current_pay_type = user.get("pay_type") or DEFAULT_PAY_TYPE
    current_pay_amount = user.get("pay_amount")
    if current_pay_amount is None:
        current_pay_amount = DEFAULT_PAY_AMOUNT

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # Проверка пересечений и создание смены должны быть одной операцией:
        # иначе два одновременных отчёта успеют оба пройти SELECT.
        await db.execute("BEGIN IMMEDIATE")
        old = await (await db.execute(
            "SELECT mr.id, mr.shift_id, mr.updated_at, mr.pay_type_snap AS report_pay_type, "
            "mr.pay_amount_snap AS report_pay_amount, s.user_id AS shift_user_id, "
            "s.start_at AS shift_start_at, s.pay_type_snap AS shift_pay_type, "
            "s.pay_amount_snap AS shift_pay_amount, s.source AS shift_source, "
            "s.is_active AS shift_is_active, s.full_name AS shift_full_name, "
            "s.role AS shift_role, s.report_msg_id AS shift_report_msg_id "
            "FROM manual_reports mr LEFT JOIN shifts s ON s.id = mr.shift_id "
            "WHERE mr.city_id = ? AND mr.message_id = ?",
            (city["id"], message.message_id)
        )).fetchone()
        if old and old["updated_at"] and event_time.isoformat() < old["updated_at"]:
            await db.rollback()
            return
        old_shift_id = old["shift_id"] if old else None
        old_shift_user_id = old["shift_user_id"] if old else None
        target_shift_id = old_shift_id
        target_source = old["shift_source"] if old else None
        target_full_name = old["shift_full_name"] if old else None
        target_role = old["shift_role"] if old else None
        target_report_msg_id = old["shift_report_msg_id"] if old else None
        old_start_at = _parse_datetime(old["shift_start_at"]) if old else None
        old_month = old_start_at.strftime("%Y-%m") if old_start_at else None

        active_row = await (await db.execute(
            "SELECT * FROM shifts WHERE user_id = ? AND is_active = 1 ORDER BY id DESC LIMIT 1",
            (uid,),
        )).fetchone()
        if active_row and active_row["id"] != target_shift_id:
            if (target_shift_id is None and active_row["city_id"] == city["id"]
                    and active_row["source"] == "manual_signal"):
                target_shift_id = active_row["id"]
                target_source = active_row["source"]
                target_full_name = active_row["full_name"]
                target_role = active_row["role"]
                target_report_msg_id = active_row["report_msg_id"]
            else:
                parsed = None
                error = "у сотрудника уже есть активная смена бота"
        pay_type_snap = (
            (old["report_pay_type"] if old else None)
            or (old["shift_pay_type"] if old else None)
            or current_pay_type
        )
        pay_amount_snap = old["report_pay_amount"] if old else None
        if pay_amount_snap is None and old:
            pay_amount_snap = old["shift_pay_amount"]
        if pay_amount_snap is None:
            pay_amount_snap = current_pay_amount

        start_at = end_at = None
        if parsed:
            start_at, end_at = _resolve_manual_interval(
                parsed["start_time"], parsed["end_time"], city, message_time
            )
            duration = end_at - start_at
            if duration <= timedelta(0) or duration > timedelta(hours=18):
                parsed = None
                error = "неоднозначная или слишком длинная смена"
            else:
                if target_shift_id is None:
                    manual_candidates = await (await db.execute(
                        "SELECT * FROM shifts WHERE user_id = ? AND city_id = ? "
                        "AND source = 'manual_signal' "
                        "AND start_at IS NOT NULL AND julianday(start_at) < julianday(?) "
                        "AND julianday(COALESCE(end_at, '9999-12-31T23:59:59+00:00')) "
                        "> julianday(?) ORDER BY id DESC LIMIT 2",
                        (uid, city["id"], end_at.isoformat(), start_at.isoformat()),
                    )).fetchall()
                    if len(manual_candidates) == 1:
                        candidate = manual_candidates[0]
                        target_shift_id = candidate["id"]
                        target_source = candidate["source"]
                        target_full_name = candidate["full_name"]
                        target_role = candidate["role"]
                        target_report_msg_id = candidate["report_msg_id"]
                conflict = await (await db.execute(
                    "SELECT id FROM shifts WHERE user_id = ? "
                    "AND id <> COALESCE(?, -1) AND start_at IS NOT NULL "
                    "AND julianday(start_at) < julianday(?) "
                    "AND julianday(COALESCE(end_at, '9999-12-31T23:59:59+00:00')) > julianday(?) "
                    "LIMIT 1",
                    (uid, target_shift_id, end_at.isoformat(), start_at.isoformat())
                )).fetchone()
                if conflict:
                    parsed = None
                    error = f"интервал пересекается с уже учтённой сменой #{conflict[0]}"

        if not parsed:
            review_shift_id = target_shift_id if target_source == "manual_signal" else None
            if target_shift_id and target_source == "manual_signal":
                # Ручная сигнальная смена является реальным журналом работы.
                # Невалидная правка итогового отчёта не должна её удалять.
                await db.execute(
                    "DELETE FROM actions WHERE shift_id = ? AND message_id = ?",
                    (target_shift_id, message.message_id),
                )
            elif old_shift_id:
                await db.execute("DELETE FROM actions WHERE shift_id = ?", (old_shift_id,))
                await db.execute(
                    "DELETE FROM shifts WHERE id = ? AND source = 'manual_chat'", (old_shift_id,)
                )
            await db.execute(
                "INSERT INTO manual_reports (city_id, user_id, message_id, raw_text, status, "
                "parse_error, shift_id, sender_name, pay_type_snap, pay_amount_snap, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'needs_review', ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(city_id, message_id) DO UPDATE SET raw_text=excluded.raw_text, "
                "status='needs_review', parse_error=excluded.parse_error, "
                "shift_id=excluded.shift_id, "
                "sender_name=excluded.sender_name, "
                "pay_type_snap=COALESCE(manual_reports.pay_type_snap, excluded.pay_type_snap), "
                "pay_amount_snap=COALESCE(manual_reports.pay_amount_snap, excluded.pay_amount_snap), "
                "updated_at=excluded.updated_at",
                (city["id"], uid, message.message_id, text, error, review_shift_id, sender_name,
                 pay_type_snap, pay_amount_snap,
                 message_time.isoformat(), event_time.isoformat())
            )
            await db.commit()
            if review_shift_id:
                preserved = await get_shift_by_id(review_shift_id)
                if preserved and not preserved.get("is_active"):
                    await freeze_earned(review_shift_id)
            if old_month and old_shift_user_id is not None:
                await refresh_monthly_aggregate(city["id"], old_shift_user_id, old_month)
            return

        if target_source == "manual_signal":
            full_name = target_full_name or sender_name
            role = target_role or user.get("role") or ""
        else:
            full_name = user.get("full_name") or sender_name
            role = user.get("role") or ""
        worked_minutes = int((end_at - start_at).total_seconds() // 60)
        battery_count = sum(
            len(action.get("bike_codes") or []) + int(action.get("quantity") or 0)
            for action in parsed["actions"] if action["action_type"] == "battery"
        )
        earned = compute_earned(
            pay_type_snap, pay_amount_snap, worked_minutes, battery_count
        )

        store_report_actions = True
        if target_shift_id:
            shift_id = target_shift_id
            source = "manual_signal" if target_source == "manual_signal" else "manual_chat"
            await db.execute(
                "UPDATE shifts SET user_id=?, full_name=?, role=?, start_time=?, end_time=?, "
                "start_at=?, end_at=?, created_at=?, is_active=0, on_lunch=0, city_id=?, source=?, "
                "earned=?, pay_type_snap=COALESCE(pay_type_snap, ?), "
                "pay_amount_snap=COALESCE(pay_amount_snap, ?) WHERE id=?",
                (uid, full_name, role, parsed["start_time"], parsed["end_time"],
                 start_at.isoformat(), end_at.isoformat(), start_at.isoformat(), city["id"],
                 source, earned, pay_type_snap, pay_amount_snap, shift_id)
            )
            if source == "manual_signal":
                # Сохраняем действия, уже собранные эталонным парсером в рабочих темах.
                # Итоговый отчёт добавляет сводные действия лишь если других записей нет.
                await db.execute(
                    "DELETE FROM actions WHERE shift_id = ? AND message_id = ?",
                    (shift_id, message.message_id),
                )
                has_live_actions = await (await db.execute(
                    "SELECT 1 FROM actions WHERE shift_id = ? LIMIT 1", (shift_id,)
                )).fetchone()
                store_report_actions = not bool(has_live_actions)
            else:
                await db.execute("DELETE FROM actions WHERE shift_id = ?", (shift_id,))
        else:
            cursor = await db.execute(
                "INSERT INTO shifts (user_id, full_name, role, start_time, end_time, is_active, "
                "created_at, city_id, start_at, end_at, source, source_message_id, "
                "earned, pay_type_snap, pay_amount_snap) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, 'manual_chat', ?, ?, ?, ?)",
                (uid, full_name, role, parsed["start_time"], parsed["end_time"],
                 start_at.isoformat(), city["id"], start_at.isoformat(), end_at.isoformat(),
                 message.message_id, earned, pay_type_snap, pay_amount_snap)
            )
            shift_id = cursor.lastrowid

        if store_report_actions:
            for action in parsed["actions"]:
                await db.execute(
                    "INSERT INTO actions (user_id, shift_id, message_id, action_type, bike_codes, "
                    "quantity, city_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (uid, shift_id, message.message_id, action["action_type"],
                     ",".join(action.get("bike_codes") or []), action.get("quantity", 0), city["id"])
                )
        await db.execute(
            "INSERT INTO manual_reports (city_id, user_id, message_id, raw_text, status, "
            "parse_error, shift_id, sender_name, pay_type_snap, pay_amount_snap, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'accepted', NULL, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(city_id, message_id) DO UPDATE SET raw_text=excluded.raw_text, "
            "status='accepted', parse_error=NULL, shift_id=excluded.shift_id, "
            "sender_name=excluded.sender_name, "
            "pay_type_snap=COALESCE(manual_reports.pay_type_snap, excluded.pay_type_snap), "
            "pay_amount_snap=COALESCE(manual_reports.pay_amount_snap, excluded.pay_amount_snap), "
            "updated_at=excluded.updated_at",
            (city["id"], uid, message.message_id, text, shift_id, sender_name,
             pay_type_snap, pay_amount_snap,
             message_time.isoformat(), event_time.isoformat())
        )
        await db.commit()
    await freeze_earned(shift_id)
    if target_source == "manual_signal" and target_report_msg_id:
        await safe_flush_report_update(shift_id)
    new_month = start_at.strftime("%Y-%m")
    if (old_month and old_shift_user_id is not None
            and (old_month != new_month or old_shift_user_id != uid)):
        await refresh_monthly_aggregate(city["id"], old_shift_user_id, old_month)

# ============================================================
# ФУНКЦИЯ АВТОУДАЛЕНИЯ КОМАНД  (оригинальная)
# ============================================================
async def auto_delete(msg: Message, delay: int = 60):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except:
        pass

# ============================================================================
# [07-REPORTS] ЖИВЫЕ TELEGRAM-ОТЧЁТЫ СМЕН
# ============================================================================
_pending_updates = {}   # shift_id -> asyncio.Task (дебаунс)
_report_update_locks = {}  # shift_id -> {lock, users}; защита от дублей/гонок

def _role_text(role):
    role_emoji = ""
    if role == "Скаут":
        role_emoji = " 🚶"
    elif role == "Водитель":
        role_emoji = " 🚚"
    elif role == "Чарджер":       # === НОВОЕ: роль чарджера ===
        role_emoji = " ⚡"
    return f" | {role}{role_emoji}" if role else ""

def _duration(start_time, end_time):
    sp = start_time.split(':')
    ep = end_time.split(':')
    sm = int(sp[0]) * 60 + int(sp[1])
    em = int(ep[0]) * 60 + int(ep[1])
    if em < sm:
        em += 24 * 60
    diff = em - sm
    return f"{diff // 60} ч. {diff % 60} мин."


def _duration_shift(shift):
    diff = _shift_worked_min(shift)
    return f"{diff // 60} ч. {diff % 60} мин."

def build_report_text(shift, stats):
    """Формат сохранён из оригинального отчёта + строка АКБ."""
    full_name = html.escape(shift.get('full_name') or "Сотрудник")
    report = f"<b>{full_name}</b>{_role_text(shift.get('role'))}\n"
    waiting = _shift_is_scheduled(shift)
    closed = not shift.get('is_active') and shift.get('end_time')
    # Цветовой индикатор статуса смены
    if closed:
        report += "🔴 Смена закрыта\n"
    elif waiting:
        report += "🟢 Ожидает начала\n"
    else:
        report += "🟢 Смена активна\n"
        if shift.get("on_lunch"):
            report += "🍽 Сейчас на обеде\n"
    report += f"Начал: {html.escape(shift['start_time'])}"
    if waiting:
        report += " (ожидает начала)"
    report += "\n"

    if closed:
        report += f"Закончил: {html.escape(shift['end_time'])}\n"
        report += f"Отработано: {_duration_shift(shift)}\n"

    if shift.get('district'):
        report += f"Район: {html.escape(shift['district'].upper())}\n"

    report += "\nСтатистика за смену:\n"

    has_any = False
    if stats['move'] > 0:
        report += f"🛵 Перемещено: {stats['move']}\n"; has_any = True
    if stats['fix'] > 0:
        report += f"💚 Поправлено: {stats['fix']}\n"; has_any = True
    if stats['repair'] > 0:
        report += f"🔧 Ремонт: {stats['repair']}\n"; has_any = True
    if stats['battery'] > 0:
        report += f"🔋 Поменял АКБ: {stats['battery']}\n"; has_any = True
    report_city = get_city(shift.get("city_id")) or {}
    if _city_key(report_city) == "khimki" and stats.get('sticker', 0) > 0:
        report += f"🏷 Оклейка: {stats['sticker']}\n"; has_any = True
    if stats['to_sc'] > 0:
        report += f"Привез на СЦ: {stats['to_sc']}\n"; has_any = True
    if stats['from_sc'] > 0:
        report += f"Вывез из СЦ: {stats['from_sc']}\n"; has_any = True
    if not has_any:
        report += "— пока нет действий\n"

    if closed and shift.get('comment'):
        report += f"\nКомментарий: {html.escape(shift['comment'])}"

    return report

async def update_report_message(shift_id, force_new=False):
    """Сериализует полную отправку/правку отчёта одной смены.

    Без этого два одновременных запроса могли оба увидеть пустой
    report_msg_id и создать два сообщения. Свежая смена читается уже внутри
    блокировки, поэтому закрытие всегда оставляет финальное закрытое состояние.
    """
    entry = _report_update_locks.get(shift_id)
    if entry is None:
        entry = {"lock": asyncio.Lock(), "users": 0}
        _report_update_locks[shift_id] = entry
    entry["users"] += 1
    try:
        async with entry["lock"]:
            await _update_report_message_locked(shift_id, force_new=force_new)
    finally:
        entry["users"] -= 1
        if entry["users"] == 0 and _report_update_locks.get(shift_id) is entry:
            _report_update_locks.pop(shift_id, None)


async def _update_report_message_locked(shift_id, force_new=False):
    """Отредактировать живое сообщение смены (или пересоздать при /fix)."""
    shift = await get_shift_by_id(shift_id)
    if not shift:
        return
    # Группа выбирается по роли сотрудника: в городах с ролевыми группами
    # (Химки) смена скаута уходит в группу скаутов, водителя — в водительскую.
    city = report_city_for_role(shift.get("city_id"), shift.get("role"))
    if not city:
        logger.error(f"Не найден город смены {shift_id}")
        return
    stats = await get_stats(shift_id)
    text = build_report_text(shift, stats)
    msg_id = shift.get('report_msg_id')
    # === Кнопка «Открыть приложение» ТОЛЬКО пока смена активна.
    # На закрытии смены (is_active=0) markup=None → кнопка исчезает, без спама. ===
    markup = _webapp_button() if shift.get('is_active') else None

    if force_new and msg_id:
        try:
            await bot.delete_message(city["group_id"], msg_id)
        except TelegramBadRequest:
            pass
        msg_id = None

    if msg_id:
        try:
            await bot.edit_message_text(
                text, chat_id=city["group_id"], message_id=msg_id,
                parse_mode="HTML", reply_markup=markup
            )
            return
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                return
            # сообщение удалили вручную — пришлём новое ниже

    msg = await bot.send_message(
        city["group_id"],
        text,
        message_thread_id=_telegram_thread_id(city.get("topic_reports")),
        parse_mode="HTML",
        reply_markup=markup,
    )
    await set_report_msg_id(shift_id, msg.message_id)

def schedule_report_update(shift_id):
    """Дебаунс: не редактируем чаще, чем раз в DEBOUNCE_SEC (защита от флуд-лимита)."""
    task = _pending_updates.get(shift_id)
    if task and not task.done():
        return

    async def _later():
        await asyncio.sleep(DEBOUNCE_SEC)
        _pending_updates.pop(shift_id, None)
        try:
            await update_report_message(shift_id)
        except Exception as e:
            logger.error(f"Не удалось обновить отчёт смены {shift_id}: {e}")

    _pending_updates[shift_id] = asyncio.create_task(_later())

async def flush_report_update(shift_id, force_new=False):
    """Немедленное обновление (открытие/закрытие смены, /fix) — отменяем дебаунс."""
    task = _pending_updates.pop(shift_id, None)
    if task and not task.done():
        task.cancel()
    await update_report_message(shift_id, force_new=force_new)


async def safe_flush_report_update(shift_id, force_new=False):
    """Отчёт в Telegram — вторичный шаг после записи смены в БД.

    Если Telegram временно недоступен, не возвращаем ложную ошибку
    клиенту, когда смена уже успешно открыта/закрыта.
    """
    try:
        await flush_report_update(shift_id, force_new=force_new)
        return True
    except Exception as exc:
        logger.error(f"Смена {shift_id} сохранена, но Telegram-отчёт не обновился: {exc}")
        return False

# === НОВОЕ: команда /app — постим и закрепляем в теме кнопку открытия приложения ===
async def post_app_button(message: Message):
    markup = _webapp_button()
    try:
        await message.delete()
    except Exception:
        pass
    if not markup:
        return
    m = await bot.send_message(
        message.chat.id,
        "💰 <b>BibiBike</b> — смена, заработок и рейтинг в приложении.",
        message_thread_id=message.message_thread_id,
        parse_mode="HTML",
        reply_markup=markup,
    )
    try:
        await bot.pin_chat_message(message.chat.id, m.message_id, disable_notification=True)
    except Exception:
        pass

# ============================================================================
# [08-TELEGRAM] ФИЛЬТРЫ И ОБРАБОТЧИКИ СООБЩЕНИЙ TELEGRAM
# ============================================================================
@cmd_router.message(Command("topicid"))
async def topic_id_any_chat(message: Message):
    """Показывает реальные ID даже в ещё не настроенной группе или теме."""
    msg = await message.answer(
        f"chat_id: {message.chat.id}\nmessage_thread_id: {message.message_thread_id}"
    )
    asyncio.create_task(auto_delete(msg))


class CityTopicFilter(BaseFilter):
    def __init__(self, topic_kind):
        self.topic_kind = topic_kind

    async def __call__(self, message: Message):
        city = get_city_by_group(message.chat.id)
        # Диагностика выполняется в первом (reports) фильтре и не меняет
        # маршрутизацию. Если этой строки нет в логах, Telegram вообще не отдал
        # сообщение боту: нужно проверять права администратора/Privacy Mode.
        if (self.topic_kind == "reports" and city
                and _city_key(city) != DEFAULT_CITY_KEY):
            sender = getattr(message, "from_user", None)
            logger.info(
                "ВХОД ГОРОДА: город=%s chat=%s тема=%s msg=%s uid=%s "
                "sender_bot=%s текст=%r",
                city.get("name") or _city_key(city),
                message.chat.id,
                message.message_thread_id,
                message.message_id,
                getattr(sender, "id", None),
                getattr(sender, "is_bot", None),
                ((message.text or message.caption or "")[:160]),
            )
        if not city:
            return False
        thread_id = message.message_thread_id
        # Telegram присылает сообщения общего раздела форума с thread_id=None.
        # В конфигурации/БД этот раздел обозначается GENERAL_TOPIC (0).
        route_thread_id = GENERAL_TOPIC if thread_id is None else thread_id
        if self.topic_kind == "reports" and _is_single_chat_city(city):
            # В Ставрополе один общий чат: этот обработчик сам различит
            # команды/ручное начало смены и обычные рабочие действия.
            matches = True
        elif self.topic_kind == "reports":
            matches = (
                thread_id is None if city["topic_reports"] == NO_TOPIC
                else route_thread_id == city["topic_reports"]
            )
        elif _uses_strict_work_topics(city):
            # В Химках и Красной Поляне не читаем General, штрафы, срочные
            # задачи и остальные темы. Только явно настроенные рабочие темы.
            allowed_topics = {
                topic_id for topic_id in (
                    city.get("topic_tasks"),
                    city.get("topic_moves"),
                    city.get("topic_npb"),
                ) if topic_id is not None and topic_id != NO_TOPIC
            }
            # Дополнительные рабочие темы группы (например «Ремонт» у водителей
            # Химок): слушаем их обычным парсером по словам.
            allowed_topics.update(
                topic_id for topic_id in EXTRA_WORK_TOPICS.get(message.chat.id, ())
                if topic_id is not None and topic_id != NO_TOPIC
            )
            matches = route_thread_id in allowed_topics
        else:
            # Сохраняем рабочий контрак бота: слушать все темы группы
            # обычного города, кроме «ОТЧЁТОВ». Краснодар работает как раньше.
            matches = thread_id != city["topic_reports"]
        return {"city": city} if matches else False


async def _process_work_message_locked(message: Message, city, npb=False, edited=False,
                                       moves=False, repair_topic=False,
                                       bare_repair_topic=False, sticker_topic=False):
    text = message.text or message.caption or ""
    if not message.from_user or getattr(message.from_user, "is_bot", False):
        return

    uid = message.from_user.id
    # chat_id обязателен: message_id уникален только внутри чата, а у города
    # может быть несколько групп (Химки: скауты и водители).
    chat_id = message.chat.id
    linked_shift_id = await get_work_message_shift(uid, message.message_id, city["id"], chat_id)
    existing_shift_ids = await get_action_shift_ids(uid, message.message_id, city["id"], chat_id)
    if edited:
        # Постоянная привязка сохраняется даже когда правка временно сделала
        # сообщение нераспознаваемым. Для старой БД используем shift_id удалённых
        # actions, а для впервые распознанной правки — дату исходного сообщения.
        fallback_shift_id = linked_shift_id or (existing_shift_ids[0] if existing_shift_ids else None)
        shift = await get_shift_by_id(fallback_shift_id) if fallback_shift_id else None
        if not shift:
            candidate = await get_active_shift(uid, city["id"])
            message_date = getattr(message, "date", None)
            if candidate and message_date:
                message_date = _as_aware_datetime(message_date).astimezone(_city_tz(city))
                bounds = [
                    value for value in (
                        _parse_datetime(candidate.get("start_at")),
                        _parse_datetime(candidate.get("created_at")),
                    ) if value
                ]
                lower_bound = min(value.astimezone(_city_tz(city)) for value in bounds) \
                    if bounds else None
                if lower_bound and message_date + timedelta(minutes=2) >= lower_bound:
                    shift = candidate
    else:
        shift = await get_active_shift(uid, city["id"])
    if not shift:
        # Не смешиваем действия разных городов, но явно объясняем ситуацию в
        # логах. Раньше активная смена в Химках и сообщение из краснодарской
        # группы выглядели как обычное «нет активной смены».
        active_elsewhere = await get_active_shift(uid)
        if active_elsewhere:
            shift_city = get_city(active_elsewhere.get("city_id")) or {}
            logger.warning(
                "ПРОПУЩЕНО: сообщение из одного города, а активная смена в другом. "
                "uid=%s чат_город=%s(%s) смена_город=%s(%s) chat=%s title=%r "
                "тема=%s msg=%s смена=%s",
                uid,
                city.get("name") or "неизвестен",
                city.get("id"),
                shift_city.get("name") or "неизвестен",
                active_elsewhere.get("city_id"),
                chat_id,
                getattr(message.chat, "title", None),
                message.message_thread_id,
                message.message_id,
                active_elsewhere.get("id"),
            )
        else:
            logger.info(
                "ПРОПУЩЕНО: у пользователя действительно нет активной смены. "
                "uid=%s город_чата=%s(%s) chat=%s title=%r тема=%s msg=%s",
                uid,
                city.get("name") or "неизвестен",
                city.get("id"),
                chat_id,
                getattr(message.chat, "title", None),
                message.message_thread_id,
                message.message_id,
            )
        return
    expected_role = WORK_TOPIC_ROLES.get(
        (chat_id, message.message_thread_id), city.get("role_group")
    )
    if expected_role and _norm_role(shift.get("role")) != _norm_role(expected_role):
        logger.info(
            "ПРОПУЩЕНО: роль смены не совпадает с группой. uid=%s город=%s "
            "chat=%s тема=%s роль_смены=%s роль_группы=%s msg=%s",
            uid,
            city["name"],
            chat_id,
            message.message_thread_id,
            shift.get("role") or "не указана",
            expected_role,
            message.message_id,
        )
        return

    message_date = getattr(message, "date", None)
    raw_event = getattr(message, "edit_date", None) if edited else message_date
    event_date = _as_aware_datetime(raw_event)
    event_version = event_date.timestamp()
    if edited or existing_shift_ids:
        await link_work_message(
            uid, message.message_id, city["id"], shift["id"],
            message_date.isoformat() if message_date else None, chat_id,
        )

    if not text or text.startswith('/') or re.match(r'^\d{1,2}:\d{2}\s*', text):
        # Фото/стикер без подписи может получить корректную подпись уже после
        # закрытия смены, поэтому пустое рабочее сообщение тоже привязываем.
        if not text:
            await link_work_message(
                uid, message.message_id, city["id"], shift["id"],
                message_date.isoformat() if message_date else None, chat_id,
            )
        removed_shift_ids, applied = await replace_message_actions(
            uid, message.message_id, city["id"], shift["id"], [], event_version, chat_id
        )
        if not applied:
            return
        for sid in removed_shift_ids:
            changed = await get_shift_by_id(sid)
            if changed and not changed.get("is_active"):
                await freeze_earned(sid)
            schedule_report_update(sid)
        return

    await link_work_message(
        uid, message.message_id, city["id"], shift["id"],
        message_date.isoformat() if message_date else None, chat_id,
    )

    # === НОВОЕ: в теме NPB считаем голые номера как замены АКБ ===
    if bare_repair_topic:
        actions = parse_bare_repair_message(text)  # тема 4485: номера с любым описанием
    elif sticker_topic:
        actions = parse_sticker_message(text)      # тема 2290: номер + слово
    elif repair_topic:
        actions = parse_repair_message(text)  # тема ремонта: номера + подпись
    elif moves:
        actions = parse_moves_message(text)   # тема перемещений: голые номера
    elif npb:
        actions = parse_npb_message(text)
    elif _city_key(city) == "krasnaya_polyana":
        actions = parse_polyana_message(text)
    else:
        actions = parse_message(text)
    logger.info(
        f"РАЗБОР: город={city['name']} роль_группы={city.get('role_group') or '—'} "
        f"chat={chat_id} тема={message.message_thread_id} смена={shift['id']} "
        f"msg={message.message_id} правка={edited} -> {actions}"
    )

    removed_shift_ids, applied = await replace_message_actions(
        uid, message.message_id, city["id"], shift["id"], actions, event_version, chat_id
    )
    if not applied:
        logger.warning(
            f"ЗАПИСЬ ОТКЛОНЕНА: chat={chat_id} msg={message.message_id} "
            f"смена={shift['id']} город={city['name']} — привязка не совпала "
            f"или смена изменилась"
        )
        return
    for action in actions:
        logger.info(f"Записано: {shift['full_name']} — {action}")

    # === НОВОЕ: обновляем живое сообщение (с дебаунсом) ===
    if actions or removed_shift_ids:
        changed = await get_shift_by_id(shift['id'])
        if changed and not changed.get("is_active"):
            await freeze_earned(shift['id'])
        for sid in removed_shift_ids:
            if sid != shift['id']:
                old_shift = await get_shift_by_id(sid)
                if old_shift and not old_shift.get("is_active"):
                    await freeze_earned(sid)
                schedule_report_update(sid)
        schedule_report_update(shift['id'])


# Aiogram обрабатывает несколько входящих обновлений параллельно. Без этой
# очереди серия сообщений одного сотрудника могла одновременно открыть
# несколько SQLite-транзакций: одно сообщение записывалось, остальные могли
# завершиться ошибкой "database is locked". Очередь отдельна для каждого
# сотрудника и чата, поэтому чужие сообщения друг друга не задерживают.
_work_ingest_locks = {}  # (city_id, chat_id, user_id) -> {lock, users}


async def process_work_message(message: Message, city, npb=False, edited=False,
                               moves=False, repair_topic=False,
                               bare_repair_topic=False, sticker_topic=False):
    sender = getattr(message, "from_user", None)
    if not sender or getattr(sender, "is_bot", False):
        return
    key = (city["id"], message.chat.id, sender.id)
    entry = _work_ingest_locks.get(key)
    if entry is None:
        entry = {"lock": asyncio.Lock(), "users": 0}
        _work_ingest_locks[key] = entry
    entry["users"] += 1
    try:
        async with entry["lock"]:
            await _process_work_message_locked(
                message, city, npb=npb, edited=edited, moves=moves,
                repair_topic=repair_topic,
                bare_repair_topic=bare_repair_topic,
                sticker_topic=sticker_topic,
            )
    finally:
        entry["users"] -= 1
        if entry["users"] == 0 and _work_ingest_locks.get(key) is entry:
            _work_ingest_locks.pop(key, None)

# ============================================================
# ЧАТ 1 (и остальные темы, кроме ОТЧЕТОВ) — НОВЫЕ СООБЩЕНИЯ
# ============================================================
@work_router.message(CityTopicFilter("work"))
async def work_chat(message: Message, city):
    # === НОВОЕ: /topicid — узнать ID темы (для настройки конфига) ===
    if re.fullmatch(r"/topicid(?:@\w+)?", (message.text or "").strip(), re.IGNORECASE):
        msg = await message.answer(
            f"chat_id: {message.chat.id}\nmessage_thread_id: {message.message_thread_id}"
        )
        asyncio.create_task(auto_delete(msg))
        return

    # === НОВОЕ: /app — закрепить кнопку приложения в этой теме ===
    if (message.text or "").strip() == "/app":
        await post_app_button(message)
        return

    # === НОВОЕ: у каждой темы свой парсер (перемещения / NPB / обычный) ===
    kind = topic_parser_kind(city, message.message_thread_id)
    await process_work_message(message, city, npb=(kind == "npb"),
                               moves=(kind == "moves"), repair_topic=(kind == "repair"),
                               bare_repair_topic=(kind == "bare_repair"),
                               sticker_topic=(kind == "sticker"))

# ============================================================
# РЕДАКТИРОВАННЫЕ РАБОЧИЕ СООБЩЕНИЯ  (оригинал + NPB)
# ============================================================
@work_router.edited_message(CityTopicFilter("work"))
async def work_chat_edit(message: Message, city):
    logger.info(f"СООБЩЕНИЕ ОТРЕДАКТИРОВАНО: {message.message_id}")
    kind = topic_parser_kind(city, message.message_thread_id)
    await process_work_message(message, city, npb=(kind == "npb"),
                               moves=(kind == "moves"),
                               repair_topic=(kind == "repair"),
                               bare_repair_topic=(kind == "bare_repair"),
                               sticker_topic=(kind == "sticker"), edited=True)

# ============================================================
# ЧАТ 2 — УПРАВЛЕНИЕ СМЕНАМИ И ОТЧЕТАМИ
# ============================================================
@cmd_router.message(CityTopicFilter("reports"))
async def cmd_chat(message: Message, city):
    user_id = message.from_user.id
    active_any = await get_active_shift(user_id)
    if not active_any or active_any.get("city_id") == city["id"]:
        await set_user_city(user_id, city["id"])
    user = await get_user(user_id)
    full_name = (user or {}).get('full_name') or message.from_user.full_name
    role = (user or {}).get('role') or ""
    text = (message.text or message.caption or "").strip()

    # Ручной отчёт по-прежнему не вызывает ответа бота, но строгий парсер
    # сохраняет его для админской статистики. Неоднозначное попадает в проверку.
    if not text.startswith('/'):
        if await handle_manual_shift_signal(message, city):
            return
        if _is_single_chat_city(city):
            # Ставрополь: в одном чате находятся и отчёты, и рабочие действия.
            # После проверки ручного начала/конца смены запускаем тот же
            # эталонный парсер действий, что используется в Краснодаре.
            kind = topic_parser_kind(city, message.message_thread_id)
            await process_work_message(
                message, city, npb=(kind == "npb"), moves=(kind == "moves"),
                repair_topic=(kind == "repair"),
                bare_repair_topic=(kind == "bare_repair"),
                sticker_topic=(kind == "sticker")
            )
        else:
            await capture_manual_report(message, city)
        return

    # === НОВОЕ: /app — закрепить кнопку приложения и в теме ОТЧЁТЫ ===
    if text == "/app":
        await post_app_button(message)
        return

    # /help
    if text == "/help":
        try:
            await message.delete()
        except:
            pass
        msg = await message.answer(
            "BibiBike - команды:\n\n"
            "Начать смену (район — любое слово или без него):\n"
            "/09:00 фмр\n/09:00 весь город, загрузил 35\n/09:00\n\n"
            "Закончить смену:\n/18:00\n/18:00 Комментарий\n\n"
            "Установить имя и роль:\n/setname Фамилия И.О. скаут\n"
            "(роли: скаут, водитель, чарджер)\n\n"
            "Статус: /status\n"
            "ID темы: /topicid"
        )
        asyncio.create_task(auto_delete(msg))
        return

    # === НОВОЕ: /topicid и в теме отчётов ===
    if re.fullmatch(r"/topicid(?:@\w+)?", text, re.IGNORECASE):
        try:
            await message.delete()
        except:
            pass
        msg = await message.answer(
            f"chat_id: {message.chat.id}\nmessage_thread_id: {message.message_thread_id}"
        )
        asyncio.create_task(auto_delete(msg))
        return

    # /status  (оригинальный)
    if text == "/status":
        try:
            await message.delete()
        except:
            pass
        shift = await get_active_shift(user_id, city["id"])
        if shift:
            shift_status = (
                f"Смена начнётся в {shift['start_time']}" if _shift_is_scheduled(shift)
                else f"Активная смена с {shift['start_time']}"
            )
            msg = await message.answer(
                f"{full_name}{_role_text(shift.get('role'))}\n"
                f"{shift_status}\n"
                + (f"Район: {shift['district'].upper()}" if shift.get('district') else "")
            )
        else:
            msg = await message.answer("Нет активной смены.")
        asyncio.create_task(auto_delete(msg))
        return

    # /setname ...  (оригинальный + роль Чарджер)
    if text.startswith("/setname"):
        try:
            await message.delete()
        except:
            pass
        parts = text.split(maxsplit=1)
        if len(parts) >= 2:
            args = parts[1].strip().split()
            if len(args) >= 2:
                new_role = args[-1].lower()
                if new_role in ["скаут", "scout"]:
                    new_role = "Скаут"
                elif new_role in ["водитель", "driver", "вод"]:
                    new_role = "Водитель"
                elif new_role in ["чарджер", "charger", "чардж"]:   # === НОВОЕ ===
                    new_role = "Чарджер"
                else:
                    msg = await message.answer("Укажите роль: скаут, водитель или чарджер\nПример: /setname Иванов И.И. чарджер")
                    asyncio.create_task(auto_delete(msg))
                    return
                new_name = " ".join(args[:-1])
                group_role = city.get("role_group")
                if group_role and _norm_role(new_role) != _norm_role(group_role):
                    msg = await message.answer(
                        f"Эта группа предназначена для роли «{group_role}». "
                        "Выберите роль в своей рабочей группе."
                    )
                    asyncio.create_task(auto_delete(msg))
                    return
                current_shift = await get_active_shift(user_id)
                if (current_shift
                        and (_norm_role(new_role) != _norm_role(current_shift.get("role"))
                             or city["id"] != current_shift.get("city_id"))):
                    msg = await message.answer(
                        "Сначала закройте активную смену, затем меняйте город или роль."
                    )
                    asyncio.create_task(auto_delete(msg))
                    return
                await add_user(user_id, new_name, new_role, city["id"])
                msg = await message.answer(f"Сохранено: {new_name} | {new_role}")
            else:
                msg = await message.answer("Формат: /setname Фамилия И.О. роль\nПример: /setname Иванов И.И. скаут")
        else:
            msg = await message.answer("Формат: /setname Фамилия И.О. роль\nПример: /setname Иванов И.И. скаут")
        asyncio.create_task(auto_delete(msg))
        return

    # Обработка команд времени (Начало / Конец смены)
    # Бот реагирует ТОЛЬКО на слеш — кто не хочет пользоваться,
    # пишет отчёты вручную как раньше, бот его не трогает.
    if not text.startswith('/'):
        return

    text = text[1:]
    active_shift = await get_active_shift(user_id)
    time_match = re.match(r'(\d{1,2}:\d{2})\s*(.*)', text)

    if time_match:
        try:
            await message.delete()
        except:
            pass

        time_str = _valid_time(time_match.group(1))
        if not time_str:
            msg = await message.answer("Ошибка: время должно быть от 00:00 до 23:59.")
            asyncio.create_task(auto_delete(msg))
            return
        extra = time_match.group(2).strip()

        if active_shift and active_shift.get("city_id") != city["id"]:
            active_city = get_city(active_shift.get("city_id")) or {}
            msg = await message.answer(
                f"У вас уже открыта смена в городе {active_city.get('name', 'другом городе')}. "
                "Закройте её в группе этого города."
            )
            asyncio.create_task(auto_delete(msg))
            return

        if not active_shift:
            # НАЧАЛО СМЕНЫ
            # === НОВОЕ: район/зона — любой текст целиком (или пусто).
            # Чарджер может указать зону и порог: /20:55 весь город, загрузил 35 ===
            district = extra.lower()
            role_for_shift = role if role else ""
            try:
                sid = await start_shift(
                    user_id, full_name, role_for_shift, time_str, district, city["id"]
                )
            except ActiveShiftExists:
                msg = await message.answer("У вас уже открыта смена.")
                asyncio.create_task(auto_delete(msg))
                return

            # === НОВОЕ: создаётся ЖИВОЕ сообщение, бот редактирует его всю смену ===
            await safe_flush_report_update(sid)
            logger.info(f"Смена начата: {full_name}, {time_str}, {district or '—'}")
            return

        else:
            # КОНЕЦ СМЕНЫ
            comment = extra if extra else ""
            try:
                sid = await end_shift(user_id, time_str, comment, city["id"])
            except ValueError as exc:
                msg = await message.answer(str(exc))
                asyncio.create_task(auto_delete(msg))
                return
            if not sid:
                msg = await message.answer("Ошибка завершения смены.")
                asyncio.create_task(auto_delete(msg))
                return

            # === НОВОЕ: финальная правка живого сообщения ===
            await safe_flush_report_update(sid)
            logger.info(f"Смена завершена: {full_name}")
            return

    return


@cmd_router.edited_message(CityTopicFilter("reports"))
async def cmd_chat_edit(message: Message, city):
    text = (message.text or message.caption or "").strip()
    if not text.startswith('/'):
        if await handle_manual_shift_signal(message, city):
            return
        if _is_single_chat_city(city):
            kind = topic_parser_kind(city, message.message_thread_id)
            await process_work_message(
                message,
                city,
                npb=(kind == "npb"),
                repair_topic=(kind == "repair"),
                bare_repair_topic=(kind == "bare_repair"),
                sticker_topic=(kind == "sticker"),
                moves=(kind == "moves"),
                edited=True,
            )
        else:
            await capture_manual_report(message, city)

# ============================================================================
# [09-WEB-COMMON] ОБЩИЕ ФУНКЦИИ, АВТОРИЗАЦИЯ И БЕЗОПАСНОСТЬ MINI APP
# ============================================================================
MAX_LEVEL = 100

# XP за одно действие по сложности: перемещение/СЦ=10, ремонт/АКБ=5, поправка=3.
XP_WEIGHTS = {
    "move": 10,
    "to_sc": 10,
    "from_sc": 10,
    "repair": 5,
    "battery": 5,
    "fix": 3,
    "sticker": 3,
}

# Звание каждые 10 уровней + тир (цвет бейджа в приложении)
LEVEL_TITLES = [
    (1,  "Пеший",        "bronze"),
    (10, "Велик",        "bronze"),
    (20, "Скутер",       "silver"),
    (30, "Молния",       "silver"),
    (40, "Гонщик",       "gold"),
    (50, "Профи",        "gold"),
    (60, "Мастер",       "platinum"),
    (70, "Ас",           "platinum"),
    (80, "Легенда",      "diamond"),
    (90, "Бог Асфальта", "diamond"),
]

def _title_for_level(lvl):
    title, tier = LEVEL_TITLES[0][1], LEVEL_TITLES[0][2]
    for need, name, t in LEVEL_TITLES:
        if lvl >= need:
            title, tier = name, t
    return title, tier

def _level_from_xp(total):
    # Нелинейная прогрессия: на уровень L нужно 60 + 12·L + 0.35·L² XP.
    lvl, rem = 1, total
    while lvl < MAX_LEVEL:
        need = int(60 + 12 * lvl + 0.35 * lvl * lvl)
        if rem < need:
            return lvl, rem, need
        rem -= need
        lvl += 1
    return MAX_LEVEL, 0, 0   # потолок достигнут

async def get_lifetime(uid, city_id=None):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        # === НОВОЕ: XP считается с учётом веса типа действия (см. XP_WEIGHTS).
        # count — сколько "штук" в строке (номера байков + quantity),
        # затем умножаем на вес типа. quantity может быть -1 (отмена действия
        # из приложения) — тогда XP корректно уменьшается. ===
        if city_id is None:
            c = await db.execute(
                "SELECT action_type, bike_codes, quantity FROM actions WHERE user_id = ?", (uid,)
            )
        else:
            c = await db.execute(
                "SELECT action_type, bike_codes, quantity FROM actions WHERE user_id = ? AND city_id = ?",
                (uid, city_id)
            )
        total = 0
        for r in await c.fetchall():
            count = 0
            if r['bike_codes']:
                count += len(r['bike_codes'].split(','))
            if r['quantity']:
                count += r['quantity']
            total += count * XP_WEIGHTS.get(r['action_type'], 1)
        if total < 0:
            total = 0
        if city_id is None:
            c2 = await db.execute(
                "SELECT COALESCE(SUM(earned), 0), COUNT(*) FROM shifts "
                "WHERE user_id = ? AND is_active = 0", (uid,)
            )
        else:
            c2 = await db.execute(
                "SELECT COALESCE(SUM(earned), 0), COUNT(*) FROM shifts "
                "WHERE user_id = ? AND city_id = ? AND is_active = 0", (uid, city_id)
            )
        row2 = await c2.fetchone()
        total_earned, shifts_count = row2[0], row2[1]
        return total, total_earned, shifts_count

async def get_history(uid, city_id=None, limit=90):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        if city_id is None:
            c = await db.execute(
                "SELECT * FROM shifts WHERE user_id = ? AND is_active = 0 ORDER BY id DESC LIMIT ?",
                (uid, limit)
            )
        else:
            c = await db.execute(
                "SELECT * FROM shifts WHERE user_id = ? AND city_id = ? AND is_active = 0 "
                "ORDER BY id DESC LIMIT ?", (uid, city_id, limit)
            )
        return [dict(r) for r in await c.fetchall()]

def _fmt_date(created_at):
    if not created_at:
        return "—"
    try:
        return datetime.fromisoformat(created_at).strftime("%d.%m.%Y")
    except Exception:
        return "—"

def _check_webapp_auth(init_data: str):
    """Проверяем подпись Telegram initData — так мы точно знаем, кто открыл приложение."""
    if not init_data:
        return None
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None
    recv_hash = parsed.pop("hash", None)
    if not recv_hash:
        return None
    data_check = "\n".join(f"{k}={parsed[k]}" for k in sorted(parsed))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, recv_hash):
        return None
    try:
        auth_date = int(parsed.get("auth_date", "0"))
        age = int(datetime.now(timezone.utc).timestamp()) - auth_date
        if auth_date <= 0 or age < -60 or age > INIT_DATA_MAX_AGE_SEC:
            return None
    except (TypeError, ValueError):
        return None
    try:
        return json.loads(parsed.get("user", "{}"))
    except Exception:
        return None

def _get_init_data(request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("tma "):
        return auth[4:]
    return request.headers.get("X-Init-Data", "")

async def _auth_user(request):
    tg_user = _check_webapp_auth(_get_init_data(request))
    if not tg_user or "id" not in tg_user:
        return None
    return tg_user


_city_membership_cache = {}
_city_role_membership_cache = {}


async def _is_city_member(uid, city_id):
    """Не даёт открыть смену в чужой закрытой группе через Mini App.

    В городах с ролевыми группами (Химки) достаточно состоять хотя бы
    в одной из групп города — скаутской или водительской.
    """
    city = get_city(city_id)
    if not city:
        return False
    now_ts = datetime.now(timezone.utc).timestamp()
    cached = _city_membership_cache.get((uid, city_id))
    if cached and cached[1] > now_ts:
        return cached[0]
    group_ids = [city["group_id"]]
    for variant in city_role_groups(city_id).values():
        if variant["group_id"] not in group_ids:
            group_ids.append(variant["group_id"])
    allowed = False
    for group_id in group_ids:
        try:
            member = await bot.get_chat_member(group_id, uid)
            status = getattr(member.status, "value", str(member.status)).lower().split(".")[-1]
            if status == "restricted":
                allowed = bool(getattr(member, "is_member", False))
            else:
                allowed = status in {"creator", "administrator", "member"}
        except Exception as exc:
            logger.warning(
                f"Не удалось проверить участие uid={uid} в группе {group_id} города {city_id}: {exc}"
            )
            continue
        if allowed:
            break
    ttl = max(30, CITY_MEMBERSHIP_TTL_SEC if allowed else min(60, CITY_MEMBERSHIP_TTL_SEC))
    _city_membership_cache[(uid, city_id)] = (allowed, now_ts + ttl)
    return allowed


async def _is_city_role_member(uid, city_id, role):
    """Для ролевого города проверяет именно группу выбранной роли."""
    if not city_requires_role(city_id):
        return await _is_city_member(uid, city_id)
    variant = city_for_role(city_id, role)
    if not variant:
        return False
    cache_key = (uid, city_id, _norm_role(role))
    now_ts = datetime.now(timezone.utc).timestamp()
    cached = _city_role_membership_cache.get(cache_key)
    if cached and cached[1] > now_ts:
        return cached[0]
    allowed = False
    try:
        member = await bot.get_chat_member(variant["group_id"], uid)
        status = getattr(member.status, "value", str(member.status)).lower().split(".")[-1]
        if status == "restricted":
            allowed = bool(getattr(member, "is_member", False))
        else:
            allowed = status in {"creator", "administrator", "member"}
    except Exception as exc:
        logger.warning(
            "Не удалось проверить uid=%s в группе роли %s города %s: %s",
            uid, role, city_id, exc,
        )
    ttl = max(30, CITY_MEMBERSHIP_TTL_SEC if allowed else min(60, CITY_MEMBERSHIP_TTL_SEC))
    _city_role_membership_cache[cache_key] = (allowed, now_ts + ttl)
    return allowed


async def _request_json_object(request):
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None

@web.middleware
async def cors_mw(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = WEBAPP_ALLOW_ORIGIN
    resp.headers["Access-Control-Allow-Headers"] = (
        "Authorization, Content-Type, X-Init-Data, X-Admin-Token"
    )
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, OPTIONS"
    return resp

# ============================================================================
# [10-WEB-EMPLOYEE] API ПРОФИЛЯ, НАСТРОЕК, СМЕН И ИСТОРИИ СОТРУДНИКА
# ============================================================================

async def api_state(request):
    tg_user = await _auth_user(request)
    if not tg_user:
        return web.json_response({"error": "auth"}, status=401)
    uid = tg_user["id"]

    user = await get_user(uid)
    default_city = get_default_city()
    user_city = get_city((user or {}).get("city_id")) or default_city
    user_city_id = (user_city or {}).get("id")
    city = user_city
    if not city:
        return web.json_response({"error": "cities", "message": "Города не настроены."}, status=500)
    active_any = await get_active_shift(uid)
    if active_any and get_city(active_any.get("city_id")):
        city = get_city(active_any["city_id"])
    selected_city_id = city["id"]
    pay_type = (user or {}).get("pay_type") or DEFAULT_PAY_TYPE
    pay_amount = (user or {}).get("pay_amount")
    if pay_amount is None:
        pay_amount = DEFAULT_PAY_AMOUNT
    name = (user or {}).get("full_name") or ""
    role = (user or {}).get("role") or ""

    total, total_earned, shifts_count = await get_lifetime(uid, selected_city_id)
    lvl, xp, need = _level_from_xp(total)

    # Доход за текущую декаду СВОЕЙ группы (день/ночь) — по каждой смене
    # отдельно: дневные смены сверяются с дневной декадой, ночные с ночной.
    periods = await city_periods(selected_city_id)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        closed_rows = await (await db.execute(
            "SELECT earned, period_id, start_at, created_at, start_time "
            "FROM shifts WHERE user_id = ? AND city_id = ? AND is_active = 0 "
            "ORDER BY COALESCE(start_at, created_at) DESC",
            (uid, selected_city_id)
        )).fetchall()
    month_earned = sum(
        (r["earned"] or 0) for r in closed_rows
        if _shift_in_period(dict(r), periods, city)
    )
    # Подпись «с даты»: декада группы последней смены сотрудника.
    _last_seg = "day"
    if closed_rows:
        _last_seg = _shift_segment(dict(closed_rows[0]), city)
    period_start = ((periods.get(_last_seg) or {}).get("started_at")
                    or (periods.get("day") or {}).get("started_at")
                    or datetime.now(_city_tz(city)).replace(
                        day=1, hour=0, minute=0, second=0, microsecond=0
                    ).isoformat())

    shift = active_any if active_any and active_any.get("city_id") == selected_city_id else None
    shift_data = None
    if shift:
        stats = await get_stats(shift["id"])
        shift_data = {
            "start_time": shift["start_time"],
            "start_at": shift.get("start_at"),
            "district": (shift.get("district") or "").upper(),
            "stats": stats,
            "server_now": datetime.now(_city_tz(city)).isoformat(),
            "worked_min": _shift_worked_min(shift),
            "scheduled": _shift_is_scheduled(shift),
            "on_lunch": bool(shift.get("on_lunch")),
            "city": {"id": city["id"], "name": city["name"]},
        }

    last = await get_last_shift(uid, selected_city_id)
    last_data = None
    if last:
        last_data = {
            "date": _fmt_date(last.get("created_at")),
            "earned": last.get("earned") or 0,
            "worked": _duration_shift(last) if last.get("end_time") else "—",
        }

    title, tier = _title_for_level(lvl)
    return web.json_response({
        "user": {
            "id": uid,
            "name": name or tg_user.get("first_name", ""),
            "role": role,
            "pay_type": pay_type,
            "pay_amount": pay_amount,
            # === НОВОЕ: тумблер «Режим редактирования» ===
            "edit_mode": bool((user or {}).get("edit_mode")),
            # Дефолт авто-закрытия смены (для тумблера в форме старта)
            "auto_close": bool((user or {}).get("auto_close")),
            "auto_close_hours": (user or {}).get("auto_close_hours") or DEFAULT_AUTO_CLOSE_HOURS,
            # Город по умолчанию не подменяется городом активной смены.
            "city_id": user_city_id,
        },
        "registered": bool(user and name and role and user_city_id),
        "cities": [
            {"id": item["id"], "key": item["city_key"], "name": item["name"],
             "timezone_offset": item["timezone_offset"]}
            for item in sorted(CITIES_BY_ID.values(), key=lambda value: value["name"])
        ],
        "city": {"id": city["id"], "key": city["city_key"], "name": city["name"],
                 "timezone_offset": city["timezone_offset"]},
        "active": bool(shift),
        "shift": shift_data,
        "last": last_data,
        "level": {"level": lvl, "xp": xp, "need": need, "title": title, "tier": tier},
        "total_earned": total_earned,
        "lifetime": {"actions": total, "earned": total_earned, "shifts": shifts_count},
        "month_earned": month_earned,
        "build_version": BUILD_VERSION,
        "period_started_at": period_start,
        "period_started_label": _fmt_date(period_start),
    })

async def api_settings(request):
    tg_user = await _auth_user(request)
    if not tg_user:
        return web.json_response({"error": "auth"}, status=401)
    uid = tg_user["id"]

    body = await _request_json_object(request)
    if body is None:
        return web.json_response({"error": "json", "message": "Ожидается JSON-объект."}, status=400)

    current_user = await get_user(uid) or {}
    active_shift = await get_active_shift(uid)
    city_was_sent = "city_id" in body and body.get("city_id") is not None
    city_id = body.get("city_id") if city_was_sent else (
        current_user.get("city_id") or (get_default_city() or {}).get("id")
    )
    if not isinstance(city_id, int) or not get_city(city_id):
        return web.json_response({"error": "city_id", "message": "Неизвестный город."}, status=400)

    # Имя и роль — необязательно (можно зарегистрироваться прямо в приложении).
    name = (body.get("name") or "").strip()
    role_raw = (body.get("role") or "").strip().lower()
    role_map = {"скаут": "Скаут", "водитель": "Водитель", "чарджер": "Чарджер"}
    if role_raw and role_raw not in role_map:
        return web.json_response(
            {"error": "role", "message": "Выберите роль: скаут, водитель или чарджер."},
            status=400,
        )
    requested_role = role_map.get(role_raw)
    effective_role = requested_role or current_user.get("role") or ""

    # Во время активной смены профиль не должен «переезжать» в другой город
    # или роль: действия иначе окажутся отделены от открытого отчёта.
    if active_shift:
        if city_id != active_shift.get("city_id"):
            return web.json_response(
                {"error": "active_city_change",
                 "message": "Сначала закройте активную смену, затем меняйте город."},
                status=409,
            )
        if (requested_role
                and _norm_role(requested_role) != _norm_role(active_shift.get("role"))):
            return web.json_response(
                {"error": "active_role_change",
                 "message": "Сначала закройте активную смену, затем меняйте роль."},
                status=409,
            )

    if city_requires_role(city_id):
        supported = city_supported_roles(city_id)
        if not effective_role:
            return web.json_response(
                {"error": "role_required",
                 "message": "В Химках выберите роль: скаут или водитель."},
                status=400,
            )
        if _norm_role(effective_role) not in {_norm_role(item) for item in supported}:
            return web.json_response(
                {"error": "role_unsupported",
                 "message": "Для этой роли в Химках пока нет рабочей группы. "
                            f"Доступны: {', '.join(supported)}."},
                status=400,
            )
        if (city_was_sent or requested_role) and not await _is_city_role_member(
                uid, city_id, effective_role):
            return web.json_response(
                {"error": "role_membership",
                 "message": f"Вы не состоите в группе роли «{effective_role}» города Химки."},
                status=403,
            )
    elif city_was_sent and not await _is_city_member(uid, city_id):
        return web.json_response(
            {"error": "city_membership", "message": "Вы не состоите в рабочей группе этого города."},
            status=403,
        )

    # Оплату сохраняем только если реально передана (регистрация шлёт лишь имя+роль,
    # иначе у нового сотрудника остались бы DEFAULT'ы, а не обнуление).
    pay_update = None
    if "pay_type" in body or "pay_amount" in body:
        pay_type = body.get("pay_type", DEFAULT_PAY_TYPE)
        if pay_type not in ("hourly", "salary", "piece"):
            return web.json_response({"error": "pay_type"}, status=400)
        try:
            pay_amount = float(body.get("pay_amount", 0))
        except (TypeError, ValueError):
            return web.json_response({"error": "pay_amount"}, status=400)
        if not math.isfinite(pay_amount) or pay_amount < 0 or pay_amount > 10_000_000:
            return web.json_response(
                {"error": "pay_amount", "message": "Укажи корректную ставку."}, status=400)
        pay_update = (pay_type, pay_amount)

    # Все проверки завершены — только теперь применяем изменения, чтобы при
    # ошибке одного поля город/роль не сохранились частично.
    if city_was_sent:
        await set_user_city(uid, city_id)
    if pay_update:
        await set_user_pay(uid, *pay_update)
    if "edit_mode" in body:
        await set_user_edit_mode(uid, bool(body.get("edit_mode")))

    if name and requested_role:
        await add_user(uid, name, requested_role, city_id)

    return web.json_response({"ok": True})

# === НОВОЕ: старт/стоп смены из мини-приложения (результат = текстовой команде) ===
_TIME_RE = re.compile(r'^\d{1,2}:\d{2}$')

def _valid_time(t):
    if not isinstance(t, str) or not t or not _TIME_RE.match(t):
        return None
    h, m = t.split(':')
    if int(h) > 23 or int(m) > 59:
        return None
    return f"{int(h)}:{m}"   # нормализуем как в чате: 9:20, 18:05

async def api_shift_start(request):
    tg_user = await _auth_user(request)
    if not tg_user:
        return web.json_response({"error": "auth"}, status=401)
    uid = tg_user["id"]

    user = await get_user(uid)
    if not user or not user.get("full_name"):
        return web.json_response(
            {"error": "no_name", "message": "Сначала укажи имя и роль в Настройках."},
            status=400)

    body = await _request_json_object(request)
    if body is None:
        return web.json_response({"error": "json", "message": "Ожидается JSON-объект."}, status=400)
    city_id = body.get("city_id", user.get("city_id"))
    if not isinstance(city_id, int) or not get_city(city_id):
        return web.json_response({"error": "city_id", "message": "Выбери город."}, status=400)
    # В городах с ролевыми группами (Химки) без подходящей роли смену
    # открыть нельзя — иначе непонятно, в какую группу писать отчёт.
    if city_requires_role(city_id):
        user_role = (user or {}).get("role") or ""
        supported = city_supported_roles(city_id)
        if _norm_role(user_role) not in {_norm_role(item) for item in supported}:
            return web.json_response(
                {"error": "role_required",
                 "message": "Укажи роль в Настройках — в этом городе у каждой роли "
                            f"своя группа. Доступны: {', '.join(supported)}."},
                status=400)
        if not await _is_city_role_member(uid, city_id, user_role):
            return web.json_response(
                {"error": "role_membership",
                 "message": f"Вы не состоите в группе роли «{user_role}» города Химки."},
                status=403,
            )
    elif not await _is_city_member(uid, city_id):
        return web.json_response(
            {"error": "city_membership", "message": "Вы не состоите в рабочей группе этого города."},
            status=403)
    if await get_active_shift(uid):
        return web.json_response(
            {"error": "already_active", "message": "Смена уже открыта."}, status=400)
    time_str = _valid_time((body.get("time") or "").strip())
    if not time_str:
        return web.json_response(
            {"error": "time", "message": "Время в формате ЧЧ:ММ."}, status=400)
    district = (body.get("district") or "").strip().lower()

    # Авто-закрытие: тумблер + выбор часов (8/10/12) из формы старта.
    auto_close = bool(body.get("auto_close"))
    try:
        auto_close_hours = int(body.get("auto_close_hours", DEFAULT_AUTO_CLOSE_HOURS))
    except (TypeError, ValueError):
        auto_close_hours = DEFAULT_AUTO_CLOSE_HOURS
    if auto_close_hours not in AUTO_CLOSE_CHOICES:
        auto_close_hours = DEFAULT_AUTO_CLOSE_HOURS
    await set_user_auto_close(uid, auto_close, auto_close_hours)

    try:
        sid = await start_shift(
            uid, user["full_name"], user.get("role") or "", time_str, district, city_id,
            auto_close=auto_close, auto_close_hours=auto_close_hours
        )
    except ActiveShiftExists:
        return web.json_response(
            {"error": "already_active", "message": "Смена уже открыта."}, status=400)
    await set_user_city(uid, city_id)
    report_ok = await safe_flush_report_update(sid)
    selected_city = get_city(city_id) or {}
    logger.info(
        "Смена начата (из приложения): %s, %s, %s; uid=%s город=%s(%s) роль=%s",
        user["full_name"],
        time_str,
        district or "—",
        uid,
        selected_city.get("name") or "неизвестен",
        city_id,
        user.get("role") or "не указана",
    )
    shift = await get_shift_by_id(sid)
    return web.json_response({
        "ok": True, "scheduled": _shift_is_scheduled(shift), "report_updated": report_ok
    })

async def api_shift_stop(request):
    tg_user = await _auth_user(request)
    if not tg_user:
        return web.json_response({"error": "auth"}, status=401)
    uid = tg_user["id"]
    active_shift = await get_active_shift(uid)
    city_id = active_shift.get("city_id") if active_shift else None
    if not active_shift:
        return web.json_response(
            {"error": "not_active", "message": "Нет открытой смены."}, status=400)

    body = await _request_json_object(request)
    if body is None:
        return web.json_response({"error": "json", "message": "Ожидается JSON-объект."}, status=400)
    time_str = _valid_time((body.get("time") or "").strip())
    if not time_str:
        return web.json_response(
            {"error": "time", "message": "Время в формате ЧЧ:ММ."}, status=400)
    comment = (body.get("comment") or "").strip()

    try:
        sid = await end_shift(uid, time_str, comment, city_id)
    except ValueError as exc:
        return web.json_response({"error": "end_time", "message": str(exc)}, status=400)
    if not sid:
        return web.json_response({"error": "fail", "message": "Ошибка завершения."}, status=500)
    report_ok = await safe_flush_report_update(sid)
    logger.info(f"Смена завершена (из приложения): uid={uid}")
    return web.json_response({"ok": True, "report_updated": report_ok})


async def api_shift_lunch(request):
    """Включает или снимает информационный статус «на обеде».

    Статус хранится отдельно от действий и расчётов смены: он только меняет
    строку в живом Telegram-отчёте и отметку в админке.
    """
    tg_user = await _auth_user(request)
    if not tg_user:
        return web.json_response({"error": "auth"}, status=401)

    body = await _request_json_object(request)
    if body is None:
        return web.json_response(
            {"error": "json", "message": "Ожидается JSON-объект."}, status=400
        )
    active = body.get("active")
    if not isinstance(active, bool):
        return web.json_response(
            {"error": "active", "message": "Статус обеда должен быть true или false."},
            status=400,
        )

    uid = tg_user["id"]
    shift = await get_active_shift(uid)
    if not shift:
        return web.json_response(
            {"error": "not_active", "message": "Нет открытой смены."}, status=400
        )
    if _shift_is_scheduled(shift):
        return web.json_response(
            {"error": "scheduled", "message": "Обед можно отметить после начала смены."},
            status=409,
        )

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE shifts SET on_lunch = ? WHERE id = ? AND is_active = 1",
            (1 if active else 0, shift["id"]),
        )
        if cursor.rowcount != 1:
            await db.rollback()
            return web.json_response(
                {"error": "not_active", "message": "Смена уже закрыта."}, status=409
            )
        await db.commit()

    report_ok = await safe_flush_report_update(shift["id"])
    logger.info(
        f"Статус обеда {'включён' if active else 'снят'}: uid={uid}, смена={shift['id']}"
    )
    return web.json_response({
        "ok": True, "on_lunch": active, "report_updated": report_ok
    })

# === НОВОЕ: изменить счётчики на ±1 из приложения (режим редактирования) ===
# Разрешённые типы действий, которые можно править из приложения.
EDITABLE_ACTIONS = ("move", "fix", "repair", "battery", "sticker", "to_sc", "from_sc")

async def api_action_add(request):
    tg_user = await _auth_user(request)
    if not tg_user:
        return web.json_response({"error": "auth"}, status=401)
    uid = tg_user["id"]

    user = await get_user(uid) or {}
    shift = await get_active_shift(uid)
    if not shift:
        return web.json_response(
            {"error": "not_active", "message": "Сначала открой смену."}, status=400)
    if not user.get("edit_mode"):
        return web.json_response(
            {"error": "edit_mode", "message": "Сначала включи режим редактирования."}, status=403)

    body = await _request_json_object(request)
    if body is None:
        return web.json_response({"error": "json", "message": "Ожидается JSON-объект."}, status=400)
    atype = body.get("action_type")
    if atype not in EDITABLE_ACTIONS:
        return web.json_response({"error": "action_type"}, status=400)
    if atype == "sticker" and _city_key(get_city(shift.get("city_id")) or {}) != "khimki":
        return web.json_response(
            {"error": "action_type", "message": "Оклейка доступна только в Химках."},
            status=400,
        )

    # delta: +1 (добавить) или -1 (убрать). По умолчанию +1.
    # Приводим к int строго: bool/float/строку "1" не принимаем как валидные.
    delta = body.get("delta", 1)
    if not isinstance(delta, int) or isinstance(delta, bool) or delta not in (1, -1):
        return web.json_response({"error": "delta"}, status=400)

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        current_shift = await (await db.execute(
            "SELECT * FROM shifts WHERE user_id = ? AND is_active = 1 "
            "ORDER BY id DESC LIMIT 1", (uid,)
        )).fetchone()
        if not current_shift:
            await db.rollback()
            return web.json_response(
                {"error": "not_active", "message": "Смена уже закрыта."}, status=400)
        shift = dict(current_shift)
        # Проверка и -1 записываются в одной write-транзакции: два клика
        # не смогут одновременно увести счётчик в минус.
        if delta == -1:
            rows = await (await db.execute(
                "SELECT bike_codes, quantity FROM actions WHERE shift_id = ? AND action_type = ?",
                (shift["id"], atype)
            )).fetchall()
            current_amount = max(0, sum(_action_units(row) for row in rows))
            if current_amount <= 0:
                await db.rollback()
                return web.json_response(
                    {"error": "empty", "message": "Счётчик уже пустой."}, status=400)
        await db.execute(
            "INSERT INTO actions (user_id, shift_id, message_id, action_type, bike_codes, quantity, city_id) "
            "VALUES (?, ?, 0, ?, '', ?, ?)",
            (uid, shift["id"], atype, delta, shift["city_id"])
        )
        await db.commit()

    schedule_report_update(shift["id"])
    sign = "+" if delta > 0 else ""
    logger.info(f"Действие из приложения: {atype} {sign}{delta} (uid={uid}, смена {shift['id']})")
    return web.json_response({"ok": True})

async def api_shift_delete(request):
    tg_user = await _auth_user(request)
    if not tg_user:
        return web.json_response({"error": "auth"}, status=401)
    uid = tg_user["id"]

    body = await _request_json_object(request)
    if body is None:
        return web.json_response({"error": "json", "message": "Ожидается JSON-объект."}, status=400)
    sid = body.get("shift_id")
    if not isinstance(sid, int):
        return web.json_response({"error": "shift_id"}, status=400)

    shift = await get_shift_by_id(sid)
    # Удалять можно ТОЛЬКО свою закрытую смену
    if not shift or shift.get("user_id") != uid:
        return web.json_response(
            {"error": "not_found", "message": "Смена не найдена."}, status=404)
    if shift.get("is_active"):
        return web.json_response(
            {"error": "active", "message": "Активную смену нельзя удалить — сначала закрой её."},
            status=400)

    # 1) Удаляем сообщение-отчёт из темы ОТЧЁТЫ (если оно ещё там)
    msg_id = shift.get("report_msg_id")
    # Группа по роли: в Химках отчёт водителя лежит в водительской группе.
    city = report_city_for_role(shift.get("city_id"), shift.get("role"))
    if msg_id:
        try:
            if city:
                await bot.delete_message(city["group_id"], msg_id)
        except Exception as e:
            logger.warning(f"Не удалось удалить отчёт смены {sid} из группы: {e}")

    # 2) Удаляем смену и её действия из базы
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM actions WHERE shift_id = ?", (sid,))
        await db.execute("DELETE FROM manual_reports WHERE shift_id = ?", (sid,))
        await db.execute("DELETE FROM work_message_links WHERE shift_id = ?", (sid,))
        await db.execute("DELETE FROM shifts WHERE id = ?", (sid,))
        await db.commit()

    start_at = _parse_datetime(shift.get("start_at"))
    if start_at and shift.get("city_id"):
        await refresh_monthly_aggregate(
            shift["city_id"], uid, start_at.strftime("%Y-%m")
        )

    logger.info(f"Смена {sid} удалена пользователем {uid} (вместе с отчётом)")
    return web.json_response({"ok": True})

async def api_history(request):
    tg_user = await _auth_user(request)
    if not tg_user:
        return web.json_response({"error": "auth"}, status=401)
    uid = tg_user["id"]
    user = await get_user(uid) or {}
    active = await get_active_shift(uid)
    city_id = (active or {}).get("city_id") or user.get("city_id") or (get_default_city() or {}).get("id")
    rows = await get_history(uid, city_id)
    periods = await city_periods(city_id)
    home_city = get_city(city_id) or {}
    period_start = ((periods.get("day") or {}).get("started_at"))
    items = []
    for s in rows:
        worked = _duration_shift(s) if s.get("end_time") else "—"
        city = get_city(s.get("city_id")) or {}
        # Смена сверяется с декадой СВОЕЙ группы: день или ночь.
        in_period = _shift_in_period(s, periods, city or home_city)
        items.append({
            "shift_id": s["id"],
            "date": _fmt_date(s.get("created_at")),
            "start": s.get("start_time"),
            "end": s.get("end_time"),
            "worked": worked,
            "earned": s.get("earned") or 0,
            "pay_type": s.get("pay_type_snap") or "hourly",
            "district": (s.get("district") or "").upper(),
            "city_name": city.get("name", ""),
            "source": s.get("source") or "bot",
            "comment": s.get("comment") or "",
            # Смена входит в текущую декаду — по ней считается «Всего заработано».
            "in_period": in_period,
        })
    return web.json_response({"items": items, "period_started_at": period_start})


# ============================================================================
# [11-METRICS] КПД, МЕСЯЧНЫЕ ИТОГИ, АВТОЗАКРЫТИЕ И ФОНОВЫЕ ЗАДАЧИ
# ============================================================================

def _action_units(row):
    count = len((row["bike_codes"] or "").split(",")) if row["bike_codes"] else 0
    count += row["quantity"] or 0
    return count


async def get_shift_action_count(shift_id, db=None):
    own_connection = db is None
    if own_connection:
        db = await aiosqlite.connect(DB_PATH)
    try:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT bike_codes, quantity FROM actions WHERE shift_id = ?", (shift_id,)
        )).fetchall()
        return max(0, sum(_action_units(row) for row in rows))
    finally:
        if own_connection:
            await db.close()


async def refresh_monthly_aggregate(city_id, uid, month):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        shifts = await (await db.execute(
            "SELECT * FROM shifts WHERE city_id = ? AND user_id = ? AND is_active = 0 "
            "AND start_at LIKE ? ORDER BY id", (city_id, uid, month + "%")
        )).fetchall()
        if not shifts:
            await db.execute(
                "DELETE FROM monthly_aggregates WHERE city_id = ? AND user_id = ? AND month = ?",
                (city_id, uid, month)
            )
            await db.commit()
            return
        worked = 0
        actions = 0
        earned = 0.0
        for raw in shifts:
            shift = dict(raw)
            worked += _shift_worked_min(shift)
            actions += await get_shift_action_count(shift["id"], db)
            earned += shift.get("earned") or 0
        last = dict(shifts[-1])
        await db.execute(
            "INSERT INTO monthly_aggregates (city_id, user_id, month, full_name, role, "
            "shifts_count, worked_minutes, actions_count, earned, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(city_id, user_id, month) DO UPDATE SET "
            "full_name=excluded.full_name, role=excluded.role, shifts_count=excluded.shifts_count, "
            "worked_minutes=excluded.worked_minutes, actions_count=excluded.actions_count, "
            "earned=excluded.earned, updated_at=excluded.updated_at",
            (city_id, uid, month, last.get("full_name") or "Сотрудник", last.get("role") or "",
             len(shifts), worked, actions, round(earned, 2), datetime.now(timezone.utc).isoformat())
        )
        await db.commit()


async def rebuild_monthly_aggregates():
    """Один раз при старте заполняет/восстанавливает месячные агрегаты.

    В обычной работе они обновляются точечно при закрытии, /fix,
    правке ручного отчёта и удалении смены, а не пересчитываются
    при каждом открытии админки.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute(
            "SELECT DISTINCT city_id, user_id, substr(start_at, 1, 7) AS month "
            "FROM shifts WHERE is_active = 0 AND city_id IS NOT NULL "
            "AND start_at IS NOT NULL AND length(start_at) >= 7"
        )).fetchall()
    for city_id, uid, month in rows:
        await refresh_monthly_aggregate(city_id, uid, month)


_kpi_refreshed_hours = {}


async def refresh_city_metrics(city_id):
    city = get_city(city_id)
    if not city:
        return
    now = datetime.now(_city_tz(city))
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    hour = now.replace(minute=0, second=0, microsecond=0).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        shifts = await (await db.execute(
            "SELECT * FROM shifts WHERE city_id = ? AND start_at < ? "
            "AND COALESCE(end_at, ?) > ?",
            (city_id, day_end.isoformat(), day_end.isoformat(), day_start.isoformat())
        )).fetchall()
        by_user = {}
        for raw in shifts:
            shift = dict(raw)
            item = by_user.setdefault(shift["user_id"], {"worked": 0, "actions": 0})
            item["worked"] += _shift_worked_min(shift, now)
            item["actions"] += await get_shift_action_count(shift["id"], db)
        # Снимок — полное состояние дня. Если последнюю смену сотрудника
        # удалили, его старый KPI не должен висеть в админке до полуночи.
        if by_user:
            user_ids = list(by_user)
            placeholders = ",".join("?" for _ in user_ids)
            await db.execute(
                f"DELETE FROM kpi_snapshots WHERE city_id = ? AND snapshot_hour >= ? "
                f"AND snapshot_hour < ? AND user_id NOT IN ({placeholders})",
                (city_id, day_start.isoformat(), day_end.isoformat(), *user_ids)
            )
        else:
            await db.execute(
                "DELETE FROM kpi_snapshots WHERE city_id = ? AND snapshot_hour >= ? "
                "AND snapshot_hour < ?",
                (city_id, day_start.isoformat(), day_end.isoformat())
            )
        for uid, item in by_user.items():
            efficiency = round(item["actions"] * 60 / item["worked"], 2) if item["worked"] else 0
            await db.execute(
                "INSERT INTO kpi_snapshots (city_id, user_id, snapshot_hour, actions_count, "
                "worked_minutes, efficiency) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(city_id, user_id, snapshot_hour) DO UPDATE SET "
                "actions_count=excluded.actions_count, worked_minutes=excluded.worked_minutes, "
                "efficiency=excluded.efficiency",
                (city_id, uid, hour, item["actions"], item["worked"], efficiency)
            )
        await db.commit()
    _kpi_refreshed_hours[city_id] = hour


async def ensure_city_metrics_current(city_id):
    """Не пересчитывает почасовой KPI при каждом 20-секундном обновлении UI."""
    city = get_city(city_id)
    if not city:
        return
    hour = datetime.now(_city_tz(city)).replace(
        minute=0, second=0, microsecond=0
    ).isoformat()
    if _kpi_refreshed_hours.get(city_id) == hour:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        exists = await (await db.execute(
            "SELECT 1 FROM kpi_snapshots WHERE city_id = ? AND snapshot_hour = ? LIMIT 1",
            (city_id, hour),
        )).fetchone()
    if exists:
        _kpi_refreshed_hours[city_id] = hour
        return
    await refresh_city_metrics(city_id)


async def kpi_background_worker():
    while True:
        for city_id in list(CITIES_BY_ID):
            try:
                await refresh_city_metrics(city_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"Не удалось обновить KPI города {city_id}: {exc}")
        now = datetime.now(timezone.utc)
        wait_seconds = 3600 - (now.minute * 60 + now.second)
        await asyncio.sleep(max(60, wait_seconds))


_started_report_updates = set()


async def _auto_close_shift(shift):
    """Закрывает смену по дедлайну auto_close_at. Идемпотентно: если смену
    уже закрыли сами — UPDATE не затронет строк и мы просто выходим."""
    city = get_city(shift.get("city_id")) or get_default_city()
    deadline = _parse_datetime(shift.get("auto_close_at"))
    if not deadline:
        return
    end_time = deadline.astimezone(_city_tz(city)).strftime("%H:%M")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE shifts SET is_active = 0, end_time = ?, end_at = ?, on_lunch = 0 "
            "WHERE id = ? AND is_active = 1",
            (end_time, deadline.isoformat(), shift["id"])
        )
        await db.commit()
        if cur.rowcount != 1:
            return
    await freeze_earned(shift["id"])
    await safe_flush_report_update(shift["id"])
    logger.info(f"Смена {shift['id']} закрыта автоматически (дедлайн {end_time}).")


async def auto_close_worker():
    """Раз в 30 сек закрывает активные смены, у которых наступил дедлайн."""
    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                rows = await (await db.execute(
                    "SELECT * FROM shifts WHERE is_active = 1 AND auto_close_at IS NOT NULL"
                )).fetchall()
            for row in rows:
                deadline = _parse_datetime(row["auto_close_at"])
                if deadline and now_utc >= deadline.astimezone(timezone.utc):
                    await _auto_close_shift(dict(row))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Авто-закрытие смен не сработало: {exc}")
        await asyncio.sleep(30)


async def scheduled_report_status_worker():
    """Снимает пометку «ожидает начала» без долгих ненадёжных таймеров."""
    while True:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                rows = await (await db.execute(
                    "SELECT id, city_id, start_at FROM shifts WHERE is_active = 1 "
                    "AND start_at IS NOT NULL"
                )).fetchall()
            active_ids = {row["id"] for row in rows}
            _started_report_updates.intersection_update(active_ids)
            for row in rows:
                city = get_city(row["city_id"])
                start_at = _parse_datetime(row["start_at"])
                if (city and start_at and row["id"] not in _started_report_updates
                        and datetime.now(_city_tz(city)) >= start_at):
                    if await safe_flush_report_update(row["id"]):
                        _started_report_updates.add(row["id"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"Не удалось обновить статус отложенной смены: {exc}")
        await asyncio.sleep(15)


# ============================================================================
# [12-ADMIN] АВТОРИЗАЦИЯ И API АДМИНИСТРАТОРА
# ============================================================================

def _admin_key():
    material = ADMIN_SESSION_SECRET or (BOT_TOKEN + "\0" + ADMIN_PASSWORD)
    return hashlib.sha256(material.encode()).digest()


def _issue_admin_token(uid, session_version=1):
    expires = int(datetime.now(timezone.utc).timestamp()) + ADMIN_SESSION_TTL_SEC
    payload = json.dumps(
        {"uid": uid, "exp": expires, "ver": int(session_version)},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(_admin_key(), encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}", expires


def _verify_admin_token(token, uid):
    if not token or "." not in token or not ADMIN_PASSWORD:
        return False
    encoded, received = token.rsplit(".", 1)
    expected = hmac.new(_admin_key(), encoded.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return False
    try:
        padding = "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded + padding))
        if payload.get("uid") != uid or int(payload.get("exp", 0)) < int(
            datetime.now(timezone.utc).timestamp()
        ):
            return None
        return payload
    except Exception:
        return None


_admin_login_failures = {}


async def _admin_user(request):
    tg_user = await _auth_user(request)
    if not tg_user:
        return None
    uid = tg_user["id"]
    token_payload = _verify_admin_token(request.headers.get("X-Admin-Token", ""), uid)
    if not token_payload:
        return None
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        account = await (await db.execute(
            "SELECT * FROM admin_accounts WHERE user_id = ? AND is_active = 1", (uid,)
        )).fetchone()
    if not account or int(account["session_version"] or 1) != int(token_payload.get("ver", 0)):
        return None
    return tg_user


async def _admin_account(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT * FROM admin_accounts WHERE user_id = ? AND is_active = 1", (uid,)
        )).fetchone()
        if not row:
            return None
        account = dict(row)
        permissions = await (await db.execute(
            "SELECT city_id FROM admin_city_permissions WHERE user_id = ? ORDER BY city_id",
            (uid,),
        )).fetchall()
    account["city_ids"] = [item[0] for item in permissions if get_city(item[0])]
    return account


async def _password_crm_account(uid):
    """Grant password holders CRM access scoped to the city in their profile."""
    account = await _admin_account(uid)
    if account and (account["role"] == "network_admin" or account["city_ids"]):
        return account, None

    user = await get_user(uid)
    city = get_city((user or {}).get("city_id"))
    if not city:
        return None, (
            "admin_city",
            "Сначала зарегистрируйтесь в приложении и выберите свой город.",
        )

    now_iso = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        if account:
            # Keep explicitly assigned roles, only repair a missing city binding.
            await db.execute(
                "INSERT OR IGNORE INTO admin_city_permissions (user_id,city_id) VALUES (?,?)",
                (uid, city["id"]),
            )
        else:
            await db.execute(
                "INSERT INTO admin_accounts (user_id,role,role_scope,is_active,session_version,"
                "created_at,updated_at) VALUES (?,'city_manager',NULL,1,1,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET role='city_manager',role_scope=NULL,is_active=1,"
                "session_version=admin_accounts.session_version+1,updated_at=excluded.updated_at",
                (uid, now_iso, now_iso),
            )
            await db.execute("DELETE FROM admin_city_permissions WHERE user_id=?", (uid,))
            await db.execute(
                "INSERT INTO admin_city_permissions (user_id,city_id) VALUES (?,?)",
                (uid, city["id"]),
            )
        await db.commit()
    logger.info("CRM доступ по паролю выдан: uid=%s город=%s", uid, city["name"])
    return await _admin_account(uid), None


async def _admin_city(uid, bind_if_missing=False):
    """Совместимый город старой админки, выбранный только из явных CRM-прав."""
    account = await _admin_account(uid)
    if not account:
        return None
    if account["role"] != "network_admin":
        return get_city(account["city_ids"][0]) if account["city_ids"] else None
    user = await get_user(uid)
    return get_city((user or {}).get("city_id")) or get_default_city()


async def _admin_context(request):
    """Возвращает администратора и его серверно закреплённый город.

    Город никогда не берётся из параметров запроса: так подмена city_id в
    браузере не открывает данные другого филиала.
    """
    tg_user = await _admin_user(request)
    if not tg_user:
        return None
    user = await get_user(tg_user["id"])
    account = await _admin_account(tg_user["id"])
    if not account:
        return None
    city = await _admin_city(tg_user["id"])
    return {
        "telegram_user": tg_user,
        "user": user or {},
        "city": city,
        "admin": account,
        "allowed_city_ids": (
            sorted(CITIES_BY_ID) if account["role"] == "network_admin"
            else account["city_ids"]
        ),
    }


def _legacy_admin_scope_error(context):
    """Старая городская админка не умеет безопасно фильтровать по роли."""
    if context and _crm_scope_role(context):
        return web.json_response(
            {"error": "admin_role_scope",
             "message": "Для ролевого доступа используйте новую CRM."}, status=403
        )
    return None


async def api_admin_login(request):
    tg_user = await _auth_user(request)
    if not tg_user:
        return web.json_response({"error": "auth"}, status=401)
    uid = tg_user["id"]
    if not ADMIN_PASSWORD:
        return web.json_response(
            {"error": "admin_disabled", "message": "ADMIN_PASSWORD не настроен."}, status=503
        )
    now_ts = int(datetime.now(timezone.utc).timestamp())
    failures = [stamp for stamp in _admin_login_failures.get(uid, []) if now_ts - stamp < 600]
    if len(failures) >= 5:
        return web.json_response(
            {"error": "rate_limit", "message": "Слишком много попыток. Повтори через 10 минут."},
            status=429
        )
    body = await _request_json_object(request)
    if body is None:
        return web.json_response({"error": "json", "message": "Ожидается JSON-объект."}, status=400)
    password = body.get("password")
    if not isinstance(password, str) or not hmac.compare_digest(password, ADMIN_PASSWORD):
        failures.append(now_ts)
        _admin_login_failures[uid] = failures
        return web.json_response({"error": "password", "message": "Неверный пароль."}, status=403)
    account, access_error = await _password_crm_account(uid)
    if access_error is not None:
        error_code, message = access_error
        return web.json_response({"error": error_code, "message": message}, status=409)
    city = await _admin_city(uid)
    if not city:
        return web.json_response(
            {
                "error": "admin_city",
                "message": "Администратору не назначен доступ ни к одному городу.",
            },
            status=409,
        )
    _admin_login_failures.pop(uid, None)
    token, expires = _issue_admin_token(uid, account["session_version"])
    city_ids = sorted(CITIES_BY_ID) if account["role"] == "network_admin" else account["city_ids"]
    return web.json_response({
        "ok": True,
        "token": token,
        "expires_at": expires,
        "role": account["role"],
        "role_scope": account.get("role_scope"),
        "city": {"id": city["id"], "name": city["name"]},
        "cities": [
            {"id": item_id, "name": get_city(item_id)["name"]}
            for item_id in city_ids if get_city(item_id)
        ],
    })


async def approve_manual_report(report_id, start_time, end_time, expected_updated_at=None,
                                allowed_city_id=None):
    """Принимает ручной отчёт, переиспользуя связанную сигнальную смену."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN IMMEDIATE")
        if allowed_city_id is None:
            report = await (await db.execute(
                "SELECT * FROM manual_reports WHERE id = ? AND status = 'needs_review'",
                (report_id,)
            )).fetchone()
        else:
            report = await (await db.execute(
                "SELECT * FROM manual_reports WHERE id = ? AND city_id = ? "
                "AND status = 'needs_review'",
                (report_id, allowed_city_id),
            )).fetchone()
        if not report:
            raise LookupError("Ручной отчёт не найден или уже обработан.")
        if expected_updated_at is not None and report["updated_at"] != expected_updated_at:
            raise ValueError("Отчёт изменился в Telegram. Обновите админку и проверьте его снова.")
        city = get_city(report["city_id"])
        if not city:
            raise ValueError("Город отчёта больше не активен.")
        message_time = _parse_datetime(report["created_at"]) or datetime.now(_city_tz(city))
        start_at, end_at = _resolve_manual_interval(
            start_time, end_time, city, message_time
        )
        duration = end_at - start_at
        if duration <= timedelta(0) or duration > timedelta(hours=18):
            raise ValueError("Смена должна длиться больше 0 и не больше 18 часов.")

        target_shift = None
        if report["shift_id"]:
            linked = await (await db.execute(
                "SELECT * FROM shifts WHERE id = ? AND user_id = ? AND city_id = ?",
                (report["shift_id"], report["user_id"], report["city_id"]),
            )).fetchone()
            if linked and linked["source"] == "manual_signal":
                target_shift = linked
        if target_shift is None:
            candidates = await (await db.execute(
                "SELECT * FROM shifts WHERE user_id = ? AND city_id = ? "
                "AND source = 'manual_signal' AND start_at IS NOT NULL "
                "AND julianday(start_at) < julianday(?) "
                "AND julianday(COALESCE(end_at, '9999-12-31T23:59:59+00:00')) "
                "> julianday(?) ORDER BY id DESC LIMIT 2",
                (report["user_id"], report["city_id"], end_at.isoformat(),
                 start_at.isoformat()),
            )).fetchall()
            if len(candidates) > 1:
                raise ValueError("Интервал совпал с несколькими ручными сменами.")
            if len(candidates) == 1:
                target_shift = candidates[0]

        target_shift_id = target_shift["id"] if target_shift else None
        conflict = await (await db.execute(
            "SELECT id FROM shifts WHERE user_id = ? "
            "AND id <> COALESCE(?, -1) AND start_at IS NOT NULL "
            "AND julianday(start_at) < julianday(?) "
            "AND julianday(COALESCE(end_at, '9999-12-31T23:59:59+00:00')) > julianday(?) "
            "LIMIT 1",
            (report["user_id"], target_shift_id, end_at.isoformat(), start_at.isoformat())
        )).fetchone()
        if conflict:
            raise ValueError(f"Интервал пересекается со сменой #{conflict[0]}.")

        user = await (await db.execute(
            "SELECT full_name, role, pay_type, pay_amount FROM users WHERE user_id = ?",
            (report["user_id"],)
        )).fetchone()
        if target_shift:
            full_name = target_shift["full_name"] or report["sender_name"] \
                or f"Сотрудник #{report['user_id']}"
            role = target_shift["role"] \
                or (user["role"] if user and user["role"] else "")
        else:
            full_name = (user["full_name"] if user and user["full_name"] else None) \
                or report["sender_name"] \
                or f"Сотрудник #{report['user_id']}"
            role = user["role"] if user and user["role"] else ""
        pay_type = report["pay_type_snap"] \
            or (target_shift["pay_type_snap"] if target_shift else None) \
            or (user["pay_type"] if user and user["pay_type"] else None) \
            or DEFAULT_PAY_TYPE
        pay_amount = report["pay_amount_snap"]
        if pay_amount is None and target_shift:
            pay_amount = target_shift["pay_amount_snap"]
        if pay_amount is None:
            pay_amount = user["pay_amount"] if user and user["pay_amount"] is not None \
                else DEFAULT_PAY_AMOUNT
        actions = parse_message(report["raw_text"])
        battery_count = sum(
            len(action.get("bike_codes") or []) + int(action.get("quantity") or 0)
            for action in actions if action["action_type"] == "battery"
        )
        worked_minutes = int(duration.total_seconds() // 60)
        earned = compute_earned(pay_type, pay_amount, worked_minutes, battery_count)
        store_report_actions = True
        if target_shift:
            shift_id = target_shift["id"]
            await db.execute(
                "UPDATE shifts SET full_name=?, role=?, start_time=?, end_time=?, "
                "is_active=0, on_lunch=0, created_at=?, start_at=?, end_at=?, earned=?, "
                "pay_type_snap=COALESCE(pay_type_snap, ?), "
                "pay_amount_snap=COALESCE(pay_amount_snap, ?) "
                "WHERE id=? AND source='manual_signal'",
                (full_name, role, start_time, end_time, start_at.isoformat(),
                 start_at.isoformat(), end_at.isoformat(), earned, pay_type, pay_amount,
                 shift_id),
            )
            await db.execute(
                "DELETE FROM actions WHERE shift_id = ? AND message_id = ?",
                (shift_id, report["message_id"]),
            )
            has_live_actions = await (await db.execute(
                "SELECT 1 FROM actions WHERE shift_id = ? LIMIT 1", (shift_id,)
            )).fetchone()
            store_report_actions = not bool(has_live_actions)
        else:
            cursor = await db.execute(
                "INSERT INTO shifts (user_id, full_name, role, start_time, end_time, is_active, "
                "created_at, city_id, start_at, end_at, source, source_message_id, earned, "
                "pay_type_snap, pay_amount_snap) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, 'manual_chat', ?, ?, ?, ?)",
                (report["user_id"], full_name, role, start_time, end_time,
                 start_at.isoformat(), report["city_id"], start_at.isoformat(),
                 end_at.isoformat(), report["message_id"], earned, pay_type, pay_amount),
            )
            shift_id = cursor.lastrowid
        if store_report_actions:
            for action in actions:
                await db.execute(
                    "INSERT INTO actions (user_id, shift_id, message_id, action_type, bike_codes, "
                    "quantity, city_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (report["user_id"], shift_id, report["message_id"], action["action_type"],
                     ",".join(action.get("bike_codes") or []), action.get("quantity", 0),
                     report["city_id"])
                )
        await db.execute(
            "UPDATE manual_reports SET status = 'accepted', parse_error = NULL, shift_id = ?, "
            "pay_type_snap = ?, pay_amount_snap = ?, updated_at = ? WHERE id = ?",
            (shift_id, pay_type, pay_amount, datetime.now(_city_tz(city)).isoformat(), report_id)
        )
        await db.commit()
    try:
        await freeze_earned(shift_id)
    except Exception as exc:
        logger.error(f"Смена {shift_id} учтена, но месячная сводка не обновилась: {exc}")
    if target_shift and target_shift["report_msg_id"]:
        await safe_flush_report_update(shift_id)
    return shift_id


async def api_admin_manual_approve(request):
    context = await _admin_context(request)
    if not context:
        return web.json_response(
            {"error": "admin_auth", "message": "Нужен вход в админку."}, status=401
        )
    scoped_error = _legacy_admin_scope_error(context)
    if scoped_error is not None: return scoped_error
    city = context.get("city")
    if not city:
        return web.json_response(
            {"error": "admin_city", "message": "У администратора не выбран город."}, status=403
        )
    body = await _request_json_object(request)
    if body is None:
        return web.json_response(
            {"error": "json", "message": "Ожидается JSON-объект."}, status=400
        )
    report_id = body.get("report_id")
    start_time = _valid_time(body.get("start_time"))
    end_time = _valid_time(body.get("end_time"))
    expected_updated_at = body.get("updated_at")
    if (not isinstance(report_id, int) or not start_time or not end_time
            or not isinstance(expected_updated_at, str) or not expected_updated_at):
        return web.json_response(
            {"error": "fields", "message": "Укажите корректные начало и окончание."}, status=400
        )
    try:
        shift_id = await approve_manual_report(
            report_id, start_time, end_time, expected_updated_at, city["id"]
        )
    except LookupError as exc:
        return web.json_response({"error": "not_found", "message": str(exc)}, status=404)
    except (ValueError, aiosqlite.IntegrityError) as exc:
        return web.json_response({"error": "manual_report", "message": str(exc)}, status=409)
    return web.json_response({"ok": True, "shift_id": shift_id})


async def _admin_shift_payload(shift, city, now, db):
    action_rows = await (await db.execute(
        "SELECT action_type, bike_codes, quantity FROM actions WHERE shift_id = ?",
        (shift["id"],),
    )).fetchall()
    # Разбивка действий по типам (без денег — админ видит только работу и время).
    stats = {"move": 0, "fix": 0, "repair": 0, "battery": 0, "sticker": 0,
             "to_sc": 0, "from_sc": 0}
    for row in action_rows:
        t = row["action_type"]
        if t in stats:
            stats[t] += _action_units(row)
    stats = {k: max(0, v) for k, v in stats.items()}
    actions = sum(stats.values())
    worked = _shift_worked_min(shift, now)
    if shift.get("is_active"):
        status = "scheduled" if _shift_is_scheduled(shift, now) else "active"
    else:
        status = "closed"
    return {
        "shift_id": shift["id"],
        "user_id": shift["user_id"],
        "name": shift.get("full_name") or "Сотрудник",
        "role": shift.get("role") or "",
        "source": shift.get("source") or "bot",
        "status": status,
        "date": _fmt_date(shift.get("start_at") or shift.get("created_at")),
        "start": shift.get("start_time"),
        "end": shift.get("end_time"),
        "start_at": shift.get("start_at"),
        "end_at": shift.get("end_at"),
        "district": shift.get("district") or "",
        "on_lunch": bool(shift.get("on_lunch")) if status == "active" else False,
        "worked_minutes": worked,
        "actions": actions,
        "stats": stats,
        "efficiency": round(actions * 60 / worked, 2) if worked else None,
    }


async def api_admin_dashboard(request):
    context = await _admin_context(request)
    if not context:
        return web.json_response(
            {"error": "admin_auth", "message": "Нужен вход в админку."}, status=401
        )
    scoped_error = _legacy_admin_scope_error(context)
    if scoped_error is not None: return scoped_error
    city = context.get("city")
    if not city:
        return web.json_response(
            {"error": "admin_city", "message": "У администратора не выбран город."}, status=403
        )
    city_id = city["id"]
    requested_city = request.query.get("city_id")
    if requested_city not in (None, ""):
        try:
            requested_city_id = int(requested_city)
        except (TypeError, ValueError):
            return web.json_response(
                {"error": "city_id", "message": "Некорректный город."}, status=400
            )
        if requested_city_id != city_id:
            return web.json_response(
                {"error": "admin_city", "message": "Доступ разрешён только к своему городу."},
                status=403,
            )

    await ensure_city_metrics_current(city_id)
    now = datetime.now(_city_tz(city))
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    month = now.strftime("%Y-%m")
    periods = await city_periods(city_id)
    period = periods.get("day")
    period_start = min(
        [p.get("started_at") for p in periods.values() if p and p.get("started_at")]
        or [day_start.isoformat()]
    )
    period_ids = {p.get("id") for p in periods.values() if p}
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        open_rows = await (await db.execute(
            "SELECT * FROM shifts WHERE city_id = ? AND is_active = 1 "
            "ORDER BY start_at, id",
            (city_id,),
        )).fetchall()
        closed_today_rows = await (await db.execute(
            "SELECT * FROM shifts WHERE city_id = ? AND is_active = 0 AND start_at < ? "
            "AND COALESCE(end_at, start_at) > ? ORDER BY start_at, id",
            (city_id, day_end.isoformat(), day_start.isoformat()),
        )).fetchall()
        open_items = [
            await _admin_shift_payload(dict(row), city, now, db) for row in open_rows
        ]
        closed_today_items = [
            await _admin_shift_payload(dict(row), city, now, db) for row in closed_today_rows
        ]

        monthly = await (await db.execute(
            "SELECT * FROM monthly_aggregates WHERE city_id = ? AND month = ? "
            "ORDER BY full_name", (city_id, month)
        )).fetchall()
        kpi_rows = await (await db.execute(
            "SELECT k.user_id, k.snapshot_hour, k.actions_count, k.worked_minutes, "
            "k.efficiency, COALESCE(NULLIF(u.full_name, ''), "
            "(SELECT s.full_name FROM shifts s WHERE s.city_id = k.city_id "
            "AND s.user_id = k.user_id ORDER BY s.id DESC LIMIT 1), 'Сотрудник') AS full_name, "
            "COALESCE(NULLIF(u.role, ''), (SELECT s.role FROM shifts s WHERE s.city_id = k.city_id "
            "AND s.user_id = k.user_id ORDER BY s.id DESC LIMIT 1), '') AS role "
            "FROM kpi_snapshots k JOIN (SELECT user_id, MAX(snapshot_hour) AS snapshot_hour "
            "FROM kpi_snapshots WHERE city_id = ? AND snapshot_hour >= ? AND snapshot_hour < ? "
            "GROUP BY user_id) latest "
            "ON latest.user_id = k.user_id AND latest.snapshot_hour = k.snapshot_hour "
            "LEFT JOIN users u ON u.user_id = k.user_id WHERE k.city_id = ? "
            "ORDER BY full_name",
            (city_id, day_start.isoformat(), day_end.isoformat(), city_id)
        )).fetchall()
        latest = await (await db.execute(
            "SELECT MAX(snapshot_hour) FROM kpi_snapshots WHERE city_id = ? "
            "AND snapshot_hour >= ? AND snapshot_hour < ?",
            (city_id, day_start.isoformat(), day_end.isoformat())
        )).fetchone()

        # База возвращает по одной агрегированной строке на сотрудника, а не
        # всю многолетнюю историю при каждом автообновлении админки.
        employee_shift_rows = await (await db.execute(
            "SELECT s.user_id, "
            "COALESCE(NULLIF(u.full_name, ''), NULLIF((SELECT latest.full_name FROM shifts latest "
            "WHERE latest.city_id = ? AND latest.user_id = s.user_id "
            "ORDER BY COALESCE(latest.start_at, latest.created_at) DESC, latest.id DESC LIMIT 1), ''), '') "
            "AS full_name, "
            "COALESCE(NULLIF(u.role, ''), NULLIF((SELECT latest.role FROM shifts latest "
            "WHERE latest.city_id = ? AND latest.user_id = s.user_id "
            "ORDER BY COALESCE(latest.start_at, latest.created_at) DESC, latest.id DESC LIMIT 1), ''), '') "
            "AS role, COUNT(*) AS shifts_count, "
            "SUM(CASE WHEN s.is_active = 0 THEN 1 ELSE 0 END) AS closed_shifts, "
            "MAX(CASE WHEN s.is_active = 1 THEN 1 ELSE 0 END) AS has_open_shift, "
            "MAX(COALESCE(s.start_at, s.created_at)) AS last_shift_at "
            "FROM shifts s LEFT JOIN users u ON u.user_id = s.user_id AND u.city_id = ? "
            "WHERE s.city_id = ? GROUP BY s.user_id, u.full_name, u.role",
            (city_id, city_id, city_id, city_id),
        )).fetchall()
        registered_rows = await (await db.execute(
            "SELECT user_id, full_name, role FROM users WHERE city_id = ? ORDER BY full_name",
            (city_id,),
        )).fetchall()

        # Смены декад — берём кандидатов широким запросом (по любой из двух
        # декад или по дате), а точную принадлежность каждой смены к декаде
        # СВОЕЙ группы (день/ночь) проверяем в Python.
        _pid_a, _pid_b = (list(period_ids) + [None, None])[:2]
        period_shift_rows = await (await db.execute(
            "SELECT * FROM shifts WHERE city_id = ? AND (period_id IN (?, ?) OR "
            "(period_id IS NULL AND COALESCE(start_at, created_at) >= ?)) "
            "ORDER BY start_at, id",
            (city_id, _pid_a, _pid_b, period_start),
        )).fetchall()
        period_action_rows = await (await db.execute(
            "SELECT a.shift_id, a.action_type, a.bike_codes, a.quantity "
            "FROM actions a JOIN shifts s ON s.id = a.shift_id "
            "WHERE s.city_id = ? AND (s.period_id IN (?, ?) OR "
            "(s.period_id IS NULL AND COALESCE(s.start_at, s.created_at) >= ?))",
            (city_id, _pid_a, _pid_b, period_start),
        )).fetchall()

    employees = {}
    for row in employee_shift_rows:
        employees[row["user_id"]] = {
            "user_id": row["user_id"],
            "name": row["full_name"] or f"Сотрудник #{row['user_id']}",
            "role": row["role"] or "",
            "shifts": row["shifts_count"],
            "closed_shifts": row["closed_shifts"],
            "has_open_shift": bool(row["has_open_shift"]),
            "last_shift_at": row["last_shift_at"],
        }
    for row in registered_rows:
        employee = employees.setdefault(row["user_id"], {
            "user_id": row["user_id"],
            "name": row["full_name"] or f"Сотрудник #{row['user_id']}",
            "role": row["role"] or "",
            "shifts": 0,
            "closed_shifts": 0,
            "has_open_shift": False,
            "last_shift_at": None,
        })
        if row["full_name"]:
            employee["name"] = row["full_name"]
        if row["role"]:
            employee["role"] = row["role"]

    role_order = {"скаут": 0, "водитель": 1, "чарджер": 2}
    employee_items = sorted(
        employees.values(),
        key=lambda item: (
            role_order.get((item.get("role") or "").strip().lower(), 3),
            (item.get("name") or "").casefold(),
        ),
    )
    kpi_by_user = {row["user_id"]: row["efficiency"] for row in kpi_rows}
    for item in open_items + closed_today_items:
        if item["user_id"] in kpi_by_user:
            item["efficiency"] = kpi_by_user[item["user_id"]]

    open_today = []
    for raw, item in zip(open_rows, open_items):
        start_at = _parse_datetime(raw["start_at"])
        if not start_at or start_at < day_end:
            open_today.append(item)
    today_items = open_today + closed_today_items

    # Агрегат за декаду по группам: дневные и ночные считаются раздельно,
    # каждая смена сверяется с текущей декадой СВОЕЙ группы.
    actions_by_shift = {}
    for row in period_action_rows:
        actions_by_shift[row["shift_id"]] = (
            actions_by_shift.get(row["shift_id"], 0) + _action_units(row)
        )
    segment_totals = {"day": {}, "night": {}}
    for row in period_shift_rows:
        shift = dict(row)
        if not _shift_in_period(shift, periods, city):
            continue
        seg = _shift_segment(shift, city)
        item = segment_totals[seg].setdefault(shift["user_id"], {
            "user_id": shift["user_id"],
            "name": shift.get("full_name") or f"Сотрудник #{shift['user_id']}",
            "role": shift.get("role") or "",
            "shifts": 0, "worked_minutes": 0, "actions": 0, "open_now": False,
        })
        if shift.get("full_name"):
            item["name"] = shift["full_name"]
        if shift.get("role"):
            item["role"] = shift["role"]
        item["shifts"] += 1
        item["worked_minutes"] += _shift_worked_min(shift, now)
        item["actions"] += max(0, actions_by_shift.get(shift["id"], 0))
        if shift.get("is_active"):
            item["open_now"] = True

    def _sorted_segment(seg):
        return sorted(
            segment_totals[seg].values(),
            key=lambda item: (
                role_order.get((item.get("role") or "").strip().lower(), 3),
                (item.get("name") or "").casefold(),
            ),
        )

    period_day_items = _sorted_segment("day")
    period_night_items = _sorted_segment("night")
    period_items = period_day_items + [
        item for item in period_night_items
        if item["user_id"] not in segment_totals["day"]
    ]
    return web.json_response({
        "city": {"id": city["id"], "name": city["name"]},
        "generated_at": now.isoformat(),
        "kpi_updated_at": latest[0] if latest else None,
        "open": open_items,
        "closed_today": closed_today_items,
        # Оставлено для совместимости со старой версией Mini App.
        "today": today_items,
        "employees": employee_items,
        # Декады: дневная и ночная группы раздельно + сведения о каждой.
        "period": period_items,
        "period_day": period_day_items,
        "period_night": period_night_items,
        "period_info": _period_info(period, city, now),
        "period_info_day": _period_info(periods.get("day"), city, now),
        "period_info_night": _period_info(periods.get("night"), city, now),
        "month": [{
            "user_id": row["user_id"], "name": row["full_name"], "role": row["role"],
            "shifts": row["shifts_count"], "worked_minutes": row["worked_minutes"],
            "actions": row["actions_count"]
        } for row in monthly],
        "kpi": [{
            "user_id": row["user_id"], "name": row["full_name"], "role": row["role"],
            "snapshot_hour": row["snapshot_hour"], "actions": row["actions_count"],
            "worked_minutes": row["worked_minutes"], "efficiency": row["efficiency"]
        } for row in kpi_rows],
    })


async def api_admin_history(request):
    context = await _admin_context(request)
    if not context:
        return web.json_response(
            {"error": "admin_auth", "message": "Нужен вход в админку."}, status=401
        )
    scoped_error = _legacy_admin_scope_error(context)
    if scoped_error is not None: return scoped_error
    city = context.get("city")
    if not city:
        return web.json_response(
            {"error": "admin_city", "message": "У администратора не выбран город."}, status=403
        )
    try:
        user_id = int(request.query.get("user_id", ""))
        limit = int(request.query.get("limit", "50"))
        offset = int(request.query.get("offset", "0"))
    except (TypeError, ValueError):
        return web.json_response(
            {"error": "fields", "message": "Некорректный сотрудник или лимит."}, status=400
        )
    if user_id <= 0 or limit < 1 or limit > 100 or offset < 0:
        return web.json_response(
            {"error": "fields", "message": "Лимит истории должен быть от 1 до 100."}, status=400
        )

    city_id = city["id"]
    scope = (request.query.get("scope") or "period").strip().lower()
    periods = await city_periods(city_id)
    period_start = min(
        [p.get("started_at") for p in periods.values() if p and p.get("started_at")]
        or ["0000"]
    )
    _pid_a, _pid_b = (list({p.get("id") for p in periods.values() if p})
                      + [None, None])[:2]
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        profile = await (await db.execute(
            "SELECT full_name, role FROM users WHERE user_id = ? AND city_id = ?",
            (user_id, city_id),
        )).fetchone()
        latest_shift = await (await db.execute(
            "SELECT full_name, role FROM shifts WHERE user_id = ? AND city_id = ? "
            "ORDER BY COALESCE(start_at, created_at) DESC, id DESC LIMIT 1",
            (user_id, city_id),
        )).fetchone()
        if not profile and not latest_shift:
            return web.json_response(
                {"error": "not_found", "message": "Сотрудник в вашем городе не найден."},
                status=404,
            )
        # По умолчанию — только текущая декада; ?scope=all вернёт всю историю.
        if scope == "all":
            rows = await (await db.execute(
                "SELECT * FROM shifts WHERE user_id = ? AND city_id = ? AND is_active = 0 "
                "ORDER BY COALESCE(start_at, created_at) DESC, id DESC LIMIT ? OFFSET ?",
                (user_id, city_id, limit + 1, offset),
            )).fetchall()
        else:
            # Кандидаты широким запросом, точная сверка с декадой СВОЕЙ
            # группы (день/ночь) — в Python по каждой смене.
            raw_rows = await (await db.execute(
                "SELECT * FROM shifts WHERE user_id = ? AND city_id = ? AND is_active = 0 "
                "AND (period_id IN (?, ?) OR (period_id IS NULL "
                "AND COALESCE(start_at, created_at) >= ?)) "
                "ORDER BY COALESCE(start_at, created_at) DESC, id DESC LIMIT ? OFFSET ?",
                (user_id, city_id, _pid_a, _pid_b, period_start,
                 limit + 1, offset),
            )).fetchall()
            rows = [r for r in raw_rows if _shift_in_period(dict(r), periods, city)]
        has_more = len(rows) > limit
        rows = rows[:limit]
        shift_ids = [row["id"] for row in rows]
        action_rows = []
        if shift_ids:
            placeholders = ",".join("?" for _ in shift_ids)
            action_rows = await (await db.execute(
                f"SELECT shift_id, action_type, bike_codes, quantity FROM actions "
                f"WHERE shift_id IN ({placeholders})",
                shift_ids,
            )).fetchall()

    stats_by_shift = {
        shift_id: {"move": 0, "fix": 0, "repair": 0, "battery": 0,
                   "sticker": 0, "to_sc": 0, "from_sc": 0}
        for shift_id in shift_ids
    }
    for row in action_rows:
        stats = stats_by_shift.get(row["shift_id"])
        if stats is not None and row["action_type"] in stats:
            stats[row["action_type"]] += _action_units(row)

    items = []
    for raw in rows:
        shift = dict(raw)
        stats = {
            action_type: max(0, count)
            for action_type, count in stats_by_shift.get(shift["id"], {}).items()
        }
        items.append({
            "shift_id": shift["id"],
            "date": _fmt_date(shift.get("start_at") or shift.get("created_at")),
            "start": shift.get("start_time"),
            "end": shift.get("end_time"),
            "start_at": shift.get("start_at"),
            "end_at": shift.get("end_at"),
            "worked_minutes": _shift_worked_min(shift),
            "district": shift.get("district") or "",
            "comment": shift.get("comment") or "",
            "source": shift.get("source") or "bot",
            "role": shift.get("role") or "",
            "actions": stats,
            "actions_total": sum(stats.values()),
        })
    name = ((profile["full_name"] if profile else None)
            or (latest_shift["full_name"] if latest_shift else None)
            or f"Сотрудник #{user_id}")
    role = ((profile["role"] if profile else None)
            or (latest_shift["role"] if latest_shift else None) or "")
    return web.json_response({
        "city": {"id": city["id"], "name": city["name"]},
        "employee": {"user_id": user_id, "name": name, "role": role},
        "items": items,
        "page": {
            "offset": offset,
            "limit": limit,
            "has_more": has_more,
            "next_offset": offset + len(items) if has_more else None,
        },
    })

async def api_admin_force_close(request):
    """Админ принудительно закрывает активную смену сотрудника своего города."""
    context = await _admin_context(request)
    if not context:
        return web.json_response(
            {"error": "admin_auth", "message": "Нужен вход в админку."}, status=401)
    scoped_error = _legacy_admin_scope_error(context)
    if scoped_error is not None: return scoped_error
    city = context.get("city")
    if not city:
        return web.json_response(
            {"error": "admin_city", "message": "У администратора не выбран город."}, status=403)
    body = await _request_json_object(request)
    if body is None:
        return web.json_response({"error": "json", "message": "Ожидается JSON-объект."}, status=400)
    sid = body.get("shift_id")
    if not isinstance(sid, int):
        return web.json_response({"error": "shift_id"}, status=400)
    shift = await get_shift_by_id(sid)
    if not shift or shift.get("city_id") != city["id"]:
        return web.json_response({"error": "not_found", "message": "Смена не найдена."}, status=404)
    if not shift.get("is_active"):
        return web.json_response({"ok": True, "already_closed": True})
    now = datetime.now(_city_tz(city))
    try:
        closed_id = await end_shift(shift["user_id"], now.strftime("%H:%M"), "", city["id"], now=now)
    except ValueError as exc:
        return web.json_response({"error": "end_time", "message": str(exc)}, status=400)
    if closed_id:
        await safe_flush_report_update(closed_id)
    logger.info(f"Смена {sid} закрыта админом {context['telegram_user']['id']}.")
    return web.json_response({"ok": True})


async def api_admin_period_new(request):
    """Открывает новую декаду: счётчики админки и заработка стартуют с нуля.

    Смены и суммы из базы не удаляются — меняется только точка отсчёта.
    """
    context = await _admin_context(request)
    if not context:
        return web.json_response(
            {"error": "admin_auth", "message": "Нужен вход в админку."}, status=401)
    scoped_error = _legacy_admin_scope_error(context)
    if scoped_error is not None: return scoped_error
    city = context.get("city")
    if not city:
        return web.json_response(
            {"error": "admin_city", "message": "У администратора не выбран город."}, status=403)
    body = await _request_json_object(request)
    if body is None:
        return web.json_response({"error": "json", "message": "Ожидается JSON-объект."}, status=400)
    if body.get("confirm") is not True:
        return web.json_response(
            {"error": "confirm", "message": "Нужно подтверждение."}, status=400)
    segment = "night" if body.get("segment") == "night" else "day"
    try:
        period = await start_new_period(
            city["id"], context["telegram_user"]["id"], segment)
    except ValueError as exc:
        return web.json_response({"error": "city", "message": str(exc)}, status=400)
    return web.json_response({
        "ok": True, "segment": segment, "period": _period_info(period, city)})


# ============================================================================
# [12A-CRM] АНАЛИТИКА, КАЛЕНДАРЬ И ЗАДАНИЯ (НЕ МЕНЯЮТ ПАРСЕР И СМЕНЫ)
# ============================================================================

CRM_ACTION_TYPES = ("move", "fix", "repair", "battery", "sticker", "to_sc", "from_sc")
CRM_ADMIN_WRITE_ROLES = {"city_manager", "network_admin"}


async def _crm_admin(request, write=False, network=False):
    context = await _admin_context(request)
    if not context:
        return None, web.json_response(
            {"error": "admin_auth", "message": "Нужен вход в CRM."}, status=401
        )
    role = context["admin"]["role"]
    if network and role != "network_admin":
        return None, web.json_response(
            {"error": "admin_scope", "message": "Нужны права администратора сети."}, status=403
        )
    if write and role not in CRM_ADMIN_WRITE_ROLES:
        return None, web.json_response(
            {"error": "admin_read_only", "message": "У вас доступ только для просмотра."},
            status=403,
        )
    return context, None


def _crm_scope_role(context):
    value = (context.get("admin", {}).get("role_scope") or "").strip()
    return value or None


def _crm_scoped_role(context, requested=None):
    scope = _crm_scope_role(context)
    requested = (requested or "").strip() or None
    if scope and requested and scope.casefold() != requested.casefold():
        return None, web.json_response(
            {"error": "admin_role_scope", "message": f"Доступ ограничен ролью {scope}."},
            status=403,
        )
    return scope or requested, None


def _crm_city(context, request=None, body=None):
    raw = body.get("city_id") if isinstance(body, dict) else None
    if raw in (None, "") and request is not None:
        raw = request.query.get("city_id")
    if raw in (None, ""):
        city = context.get("city")
        return (city, None) if city else (
            None,
            web.json_response(
                {"error": "city_id", "message": "Выберите город."}, status=400
            ),
        )
    try:
        city_id = int(raw)
    except (TypeError, ValueError):
        return None, web.json_response(
            {"error": "city_id", "message": "Некорректный город."}, status=400
        )
    if city_id not in context["allowed_city_ids"] or not get_city(city_id):
        return None, web.json_response(
            {"error": "admin_city", "message": "Нет доступа к этому городу."}, status=403
        )
    return get_city(city_id), None


def _crm_date(value):
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _crm_range(request, city, default_days=1):
    today = datetime.now(_city_tz(city)).date()
    start_date = _crm_date(request.query.get("from")) or (today - timedelta(days=default_days - 1))
    end_date = _crm_date(request.query.get("to")) or today
    if end_date < start_date:
        return None, web.json_response(
            {"error": "date_range", "message": "Дата окончания раньше даты начала."}, status=400
        )
    if (end_date - start_date).days + 1 > CRM_MAX_RANGE_DAYS:
        return None, web.json_response(
            {"error": "date_range", "message": f"Максимальный диапазон — {CRM_MAX_RANGE_DAYS} дней."},
            status=400,
        )
    tz = _city_tz(city)
    start_at = datetime.combine(start_date, datetime.min.time(), tzinfo=tz)
    end_at = datetime.combine(end_date + timedelta(days=1), datetime.min.time(), tzinfo=tz)
    return {
        "from": start_date.isoformat(), "to": end_date.isoformat(),
        "start_at": start_at, "end_at": end_at,
    }, None


def _crm_paging(request, default=50, maximum=200):
    try:
        limit = int(request.query.get("limit", default))
        offset = int(request.query.get("offset", 0))
    except (TypeError, ValueError):
        return None
    if limit < 1 or limit > maximum or offset < 0:
        return None
    return limit, offset


async def _crm_action_stats(db, shift_ids):
    result = {
        shift_id: {action_type: 0 for action_type in CRM_ACTION_TYPES}
        for shift_id in shift_ids
    }
    for pos in range(0, len(shift_ids), 500):
        chunk = shift_ids[pos:pos + 500]
        if not chunk:
            continue
        placeholders = ",".join("?" for _ in chunk)
        rows = await (await db.execute(
            f"SELECT shift_id, action_type, bike_codes, quantity FROM actions "
            f"WHERE shift_id IN ({placeholders})", chunk
        )).fetchall()
        for row in rows:
            action_type = row["action_type"]
            if action_type in result.get(row["shift_id"], {}):
                result[row["shift_id"]][action_type] += _action_units(row)
    for stats in result.values():
        for action_type in stats:
            stats[action_type] = max(0, stats[action_type])
    return result


async def _crm_load_shifts(city, date_range, user_id=None, role=None, status=None, source=None):
    clauses = [
        "city_id = ?", "COALESCE(start_at, created_at) >= ?",
        "COALESCE(start_at, created_at) < ?",
    ]
    params = [city["id"], date_range["start_at"].isoformat(), date_range["end_at"].isoformat()]
    if user_id is not None:
        clauses.append("user_id = ?"); params.append(user_id)
    if role:
        clauses.append("LOWER(role) = LOWER(?)"); params.append(role)
    if status == "active":
        clauses.append("is_active = 1")
    elif status == "closed":
        clauses.append("is_active = 0")
    if source:
        clauses.append("source = ?"); params.append(source)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM shifts WHERE " + " AND ".join(clauses) +
            " ORDER BY COALESCE(start_at, created_at) DESC, id DESC", params
        )).fetchall()
        shift_ids = [row["id"] for row in rows]
        stats = await _crm_action_stats(db, shift_ids)
    return [dict(row) for row in rows], stats


def _crm_shift_item(shift, city, stats, now=None):
    now = now or datetime.now(_city_tz(city))
    worked = _shift_worked_min(shift, now)
    actions = stats or {action_type: 0 for action_type in CRM_ACTION_TYPES}
    total = sum(actions.values())
    if shift.get("is_active"):
        status = "scheduled" if _shift_is_scheduled(shift, now) else "active"
    else:
        status = "closed"
    start_dt = _parse_datetime(shift.get("start_at") or shift.get("created_at"))
    local_date = start_dt.astimezone(_city_tz(city)).date().isoformat() if start_dt else None
    return {
        "shift_id": shift["id"], "user_id": shift.get("user_id"),
        "name": shift.get("full_name") or f"Сотрудник #{shift.get('user_id')}",
        "role": shift.get("role") or "", "status": status,
        "date": local_date, "start_time": shift.get("start_time"),
        "end_time": shift.get("end_time"), "start_at": shift.get("start_at"),
        "end_at": shift.get("end_at"), "worked_minutes": worked,
        "district": shift.get("district") or "", "comment": shift.get("comment") or "",
        "source": shift.get("source") or "bot", "on_lunch": bool(shift.get("on_lunch")),
        "segment": _shift_segment(shift, city), "actions": actions,
        "actions_total": total,
        "actions_per_hour": round(total * 60 / worked, 2) if worked else None,
    }


async def api_crm_overview(request):
    context, error = await _crm_admin(request)
    if error is not None: return error
    city, error = _crm_city(context, request=request)
    if error is not None: return error
    date_range, error = _crm_range(request, city)
    if error is not None: return error
    role, error = _crm_scoped_role(context)
    if error is not None: return error
    rows, stats_by_shift = await _crm_load_shifts(city, date_range, role=role)
    now = datetime.now(_city_tz(city))
    items = [_crm_shift_item(row, city, stats_by_shift.get(row["id"]), now) for row in rows]
    action_totals = {action_type: 0 for action_type in CRM_ACTION_TYPES}
    for item in items:
        for action_type, units in item["actions"].items():
            action_totals[action_type] += units
    worked = sum(item["worked_minutes"] for item in items)
    actions_total = sum(action_totals.values())
    current = {
        "active": sum(item["status"] == "active" for item in items),
        "scheduled": sum(item["status"] == "scheduled" for item in items),
        "on_lunch": sum(item["status"] == "active" and item["on_lunch"] for item in items),
    }
    long_open = sum(
        item["status"] == "active" and item["worked_minutes"] > 14 * 60 for item in items
    )
    async with aiosqlite.connect(DB_PATH) as db:
        scope = _crm_scope_role(context)
        waiting_sql = (
            "SELECT COUNT(*) FROM manual_reports m LEFT JOIN users u ON u.user_id=m.user_id "
            "WHERE m.city_id = ? AND m.status = 'needs_review' AND m.created_at >= ? "
            "AND m.created_at < ?"
        )
        waiting_params = [city["id"], date_range["start_at"].isoformat(),
                          date_range["end_at"].isoformat()]
        if scope: waiting_sql += " AND LOWER(u.role)=LOWER(?)"; waiting_params.append(scope)
        waiting = await (await db.execute(
            waiting_sql, waiting_params,
        )).fetchone()
        task_sql = (
            "SELECT COUNT(a.user_id), SUM(CASE WHEN a.status='accepted' THEN 1 ELSE 0 END) "
            "FROM crm_tasks t LEFT JOIN crm_task_assignees a ON a.task_id=t.id "
            "WHERE t.city_id=? AND COALESCE(t.date_to,t.work_date)>=? "
            "AND COALESCE(t.date_from,t.work_date)<=? AND t.status='published'"
        )
        task_params = [city["id"], date_range["from"], date_range["to"]]
        if scope: task_sql += " AND LOWER(a.role_snap)=LOWER(?)"; task_params.append(scope)
        task_counts = await (await db.execute(
            task_sql, task_params,
        )).fetchone()
    return web.json_response({
        "city": {"id": city["id"], "name": city["name"]},
        "range": {"from": date_range["from"], "to": date_range["to"]},
        "generated_at": now.isoformat(),
        "totals": {
            "employees": len({item["user_id"] for item in items}),
            "shifts": len(items), "worked_minutes": worked,
            "actions": actions_total,
            "actions_per_hour": round(actions_total * 60 / worked, 2) if worked else None,
        },
        "current": current, "actions": action_totals,
        "tasks": {"assignees": int(task_counts[0] or 0),
                  "accepted": int(task_counts[1] or 0),
                  "done": int(task_counts[1] or 0)},
        "data_quality": {
            "manual_reports_waiting": int(waiting[0] or 0), "long_open_shifts": long_open,
        },
    })


async def api_crm_employees(request):
    context, error = await _crm_admin(request)
    if error is not None: return error
    city, error = _crm_city(context, request=request)
    if error is not None: return error
    date_range, error = _crm_range(request, city, default_days=10)
    if error is not None: return error
    paging = _crm_paging(request)
    if not paging:
        return web.json_response({"error": "paging"}, status=400)
    role, error = _crm_scoped_role(context, request.query.get("role"))
    if error is not None: return error
    rows, stats_by_shift = await _crm_load_shifts(city, date_range, role=role or None)
    employees = {}
    for shift in rows:
        item = employees.setdefault(shift["user_id"], {
            "user_id": shift["user_id"], "name": shift.get("full_name") or "Сотрудник",
            "role": shift.get("role") or "", "shifts": 0, "worked_minutes": 0,
            "actions": {kind: 0 for kind in CRM_ACTION_TYPES}, "has_open_shift": False,
            "on_lunch": False, "last_shift_at": None,
        })
        payload = _crm_shift_item(shift, city, stats_by_shift.get(shift["id"]))
        item["shifts"] += 1; item["worked_minutes"] += payload["worked_minutes"]
        item["has_open_shift"] = item["has_open_shift"] or payload["status"] == "active"
        item["on_lunch"] = item["on_lunch"] or payload["on_lunch"]
        item["last_shift_at"] = max(
            filter(None, [item["last_shift_at"], shift.get("start_at"), shift.get("created_at")]),
            default=None,
        )
        for kind, units in payload["actions"].items(): item["actions"][kind] += units
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        users = await (await db.execute(
            "SELECT user_id, full_name, role FROM users WHERE city_id = ?", (city["id"],)
        )).fetchall()
    for user in users:
        if role and (user["role"] or "").casefold() != role.casefold(): continue
        item = employees.setdefault(user["user_id"], {
            "user_id": user["user_id"], "name": user["full_name"] or "Сотрудник",
            "role": user["role"] or "", "shifts": 0, "worked_minutes": 0,
            "actions": {kind: 0 for kind in CRM_ACTION_TYPES}, "has_open_shift": False,
            "on_lunch": False, "last_shift_at": None,
        })
        item["name"] = user["full_name"] or item["name"]
        item["role"] = user["role"] or item["role"]
    search = (request.query.get("search") or "").strip().casefold()
    status = (request.query.get("status") or "").strip()
    result = []
    for item in employees.values():
        if search and search not in item["name"].casefold(): continue
        if status == "active" and not item["has_open_shift"]: continue
        if status == "inactive" and item["has_open_shift"]: continue
        item["actions_total"] = sum(item["actions"].values())
        item["actions_per_hour"] = (
            round(item["actions_total"] * 60 / item["worked_minutes"], 2)
            if item["worked_minutes"] else None
        )
        result.append(item)
    result.sort(key=lambda item: (not item["has_open_shift"], item["name"].casefold()))
    limit, offset = paging; total = len(result)
    return web.json_response({
        "city": {"id": city["id"], "name": city["name"]},
        "range": {"from": date_range["from"], "to": date_range["to"]},
        "items": result[offset:offset + limit],
        "page": {"limit": limit, "offset": offset, "total": total,
                 "has_more": offset + limit < total},
    })


async def api_crm_employee(request):
    context, error = await _crm_admin(request)
    if error is not None: return error
    city, error = _crm_city(context, request=request)
    if error is not None: return error
    try: user_id = int(request.match_info["user_id"])
    except (KeyError, ValueError): return web.json_response({"error": "user_id"}, status=400)
    date_range, error = _crm_range(request, city, default_days=30)
    if error is not None: return error
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        user = await (await db.execute(
            "SELECT user_id, full_name, role FROM users WHERE user_id=? AND city_id=?",
            (user_id, city["id"]),
        )).fetchone()
    scoped_role, error = _crm_scoped_role(context)
    if error is not None: return error
    if user and scoped_role and (user["role"] or "").casefold() != scoped_role.casefold():
        return web.json_response({"error": "not_found"}, status=404)
    rows, stats_by_shift = await _crm_load_shifts(
        city, date_range, user_id=user_id, role=scoped_role
    )
    if not user and not rows:
        return web.json_response({"error": "not_found"}, status=404)
    items = [_crm_shift_item(row, city, stats_by_shift.get(row["id"])) for row in rows]
    action_totals = {kind: sum(item["actions"][kind] for item in items) for kind in CRM_ACTION_TYPES}
    worked = sum(item["worked_minutes"] for item in items); total_actions = sum(action_totals.values())
    return web.json_response({
        "city": {"id": city["id"], "name": city["name"]},
        "employee": {"user_id": user_id,
                     "name": (user["full_name"] if user else rows[0].get("full_name")) or "Сотрудник",
                     "role": (user["role"] if user else rows[0].get("role")) or ""},
        "range": {"from": date_range["from"], "to": date_range["to"]},
        "totals": {"shifts": len(items), "worked_minutes": worked, "actions": total_actions,
                   "actions_per_hour": round(total_actions * 60 / worked, 2) if worked else None},
        "actions": action_totals, "shifts": items,
    })


async def api_crm_shifts(request):
    context, error = await _crm_admin(request)
    if error is not None: return error
    city, error = _crm_city(context, request=request)
    if error is not None: return error
    date_range, error = _crm_range(request, city, default_days=10)
    if error is not None: return error
    paging = _crm_paging(request)
    if not paging: return web.json_response({"error": "paging"}, status=400)
    try: user_id = int(request.query["user_id"]) if request.query.get("user_id") else None
    except ValueError: return web.json_response({"error": "user_id"}, status=400)
    role, error = _crm_scoped_role(context, request.query.get("role"))
    if error is not None: return error
    rows, stats = await _crm_load_shifts(
        city, date_range, user_id=user_id, role=role,
        status=request.query.get("status"), source=request.query.get("source"),
    )
    items = [_crm_shift_item(row, city, stats.get(row["id"])) for row in rows]
    limit, offset = paging
    return web.json_response({
        "city": {"id": city["id"], "name": city["name"]},
        "range": {"from": date_range["from"], "to": date_range["to"]},
        "items": items[offset:offset + limit],
        "page": {"limit": limit, "offset": offset, "total": len(items),
                 "has_more": offset + limit < len(items)},
    })


async def api_crm_shift_close(request):
    """Закрывает конкретную активную смену из CRM в пределах доступного города и роли."""
    context, error = await _crm_admin(request, write=True)
    if error is not None:
        return error
    body = await _request_json_object(request)
    if body is None:
        return web.json_response(
            {"error": "json", "message": "Ожидается JSON-объект."}, status=400
        )
    if body.get("confirm") is not True:
        return web.json_response(
            {"error": "confirm", "message": "Подтвердите закрытие смены."}, status=400
        )
    try:
        duration_hours = int(body.get("duration_hours"))
    except (TypeError, ValueError):
        duration_hours = 0
    if duration_hours not in {10, 12}:
        return web.json_response(
            {"error": "duration_hours", "message": "Выберите продолжительность смены: 10 или 12 часов."},
            status=400,
        )
    city, error = _crm_city(context, body=body)
    if error is not None:
        return error
    try:
        shift_id = int(request.match_info["shift_id"])
    except (KeyError, TypeError, ValueError):
        return web.json_response({"error": "shift_id"}, status=400)

    shift = await get_shift_by_id(shift_id)
    role_scope = _crm_scope_role(context)
    if (not shift or shift.get("city_id") != city["id"] or
            (role_scope and (shift.get("role") or "").casefold() != role_scope.casefold())):
        return web.json_response(
            {"error": "not_found", "message": "Смена не найдена в доступном городе."},
            status=404,
        )
    if not shift.get("is_active"):
        return web.json_response({"ok": True, "already_closed": True, "shift_id": shift_id})

    before = dict(shift)
    tz = _city_tz(city)
    start_at = _parse_datetime(shift.get("start_at"))
    if not start_at and shift.get("start_time"):
        start_at = _resolve_start_at(shift["start_time"], city)
    if not start_at:
        return web.json_response(
            {"error": "start_at", "message": "У смены не найдено время начала."}, status=409
        )
    start_at = start_at.astimezone(tz)
    target_end = start_at + timedelta(hours=duration_hours)
    comment = str(body.get("comment") or "").strip()[:2000]
    try:
        closed_id = await end_shift(
            shift["user_id"], target_end.strftime("%H:%M"), comment, city["id"],
            now=target_end, end_at_override=target_end,
        )
    except ValueError as exc:
        return web.json_response({"error": "end_time", "message": str(exc)}, status=400)
    if closed_id != shift_id:
        current = await get_shift_by_id(shift_id)
        if current and not current.get("is_active"):
            return web.json_response({"ok": True, "already_closed": True, "shift_id": shift_id})
        return web.json_response(
            {"error": "conflict", "message": "Смена изменилась. Обновите CRM и повторите."},
            status=409,
        )

    after = await get_shift_by_id(shift_id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA busy_timeout=15000")
        await _crm_audit(
            db, context, "shift.force_close", "shift", shift_id, city["id"],
            before=before, after=after,
        )
        await db.commit()
    report_ok = await safe_flush_report_update(shift_id)
    logger.info(
        "Смена %s закрыта через CRM администратором %s; город=%s сотрудник=%s",
        shift_id, context["telegram_user"]["id"], city["name"], shift["user_id"],
    )
    return web.json_response({
        "ok": True,
        "shift_id": shift_id,
        "duration_hours": duration_hours,
        "end_time": after.get("end_time") if after else target_end.strftime("%H:%M"),
        "end_at": after.get("end_at") if after else target_end.isoformat(),
        "report_updated": report_ok,
    })


async def api_crm_trends(request):
    context, error = await _crm_admin(request)
    if error is not None: return error
    city, error = _crm_city(context, request=request)
    if error is not None: return error
    date_range, error = _crm_range(request, city, default_days=10)
    if error is not None: return error
    bucket = request.query.get("bucket", "day")
    if bucket not in {"day", "hour"}:
        return web.json_response(
            {"error": "bucket", "message": "Доступна группировка day или hour."}, status=400)
    action_filter = request.query.get("action_type")
    if action_filter and action_filter not in CRM_ACTION_TYPES:
        return web.json_response({"error": "action_type"}, status=400)
    role, error = _crm_scoped_role(context, request.query.get("role"))
    if error is not None: return error

    if bucket == "hour":
        if date_range["from"] != date_range["to"]:
            return web.json_response(
                {"error": "date_range", "message": "Почасовой график доступен для одного дня."},
                status=400,
            )
        event_time_sql = "COALESCE(w.created_at,m.created_at,s.start_at,s.created_at)"
        sql = (
            "SELECT a.user_id,a.action_type,a.bike_codes,a.quantity," + event_time_sql +
            " AS event_at FROM actions a JOIN shifts s ON s.id=a.shift_id "
            "LEFT JOIN work_message_links w ON w.city_id=a.city_id "
            "AND w.chat_id=COALESCE(a.chat_id,0) AND w.user_id=a.user_id "
            "AND w.message_id=a.message_id AND w.shift_id=a.shift_id "
            "LEFT JOIN manual_reports m ON m.city_id=a.city_id AND m.user_id=a.user_id "
            "AND m.message_id=a.message_id AND m.shift_id=a.shift_id "
            "WHERE a.city_id=? AND datetime(" + event_time_sql + ")>=datetime(?) "
            "AND datetime(" + event_time_sql + ")<datetime(?)"
        )
        params = [city["id"], date_range["start_at"].isoformat(),
                  date_range["end_at"].isoformat()]
        if role:
            sql += " AND LOWER(COALESCE(s.role,''))=LOWER(?)"
            params.append(role)
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            action_rows = await (await db.execute(sql, params)).fetchall()
        hours = [{"hour": hour, "label": f"{hour:02d}:00", "actions": 0,
                  "types": {kind: 0 for kind in CRM_ACTION_TYPES}, "employees": set()}
                 for hour in range(24)]
        tz = _city_tz(city)
        target_date = _crm_date(date_range["from"])
        for row in action_rows:
            event_at = _parse_datetime(row["event_at"])
            if not event_at:
                continue
            local = event_at.astimezone(tz)
            if local.date() != target_date or row["action_type"] not in CRM_ACTION_TYPES:
                continue
            item = hours[local.hour]
            units = _action_units(row)
            item["types"][row["action_type"]] += units
            item["employees"].add(row["user_id"])
        for item in hours:
            for kind in CRM_ACTION_TYPES:
                item["types"][kind] = max(0, item["types"][kind])
            if action_filter:
                item["actions"] = item["types"][action_filter]
            else:
                item["actions"] = sum(item["types"].values())
            item["employees"] = len(item["employees"])
        return web.json_response({
            "city": {"id": city["id"], "name": city["name"]},
            "range": {"from": date_range["from"], "to": date_range["to"]},
            "bucket": "hour", "action_type": action_filter, "series": hours,
        })

    rows, stats = await _crm_load_shifts(city, date_range, role=role)
    days = {}
    for row in rows:
        item = _crm_shift_item(row, city, stats.get(row["id"]))
        day = days.setdefault(item["date"], {"date": item["date"], "shifts": 0,
            "employees": set(), "worked_minutes": 0, "actions": 0})
        day["shifts"] += 1; day["employees"].add(item["user_id"])
        day["worked_minutes"] += item["worked_minutes"]
        day["actions"] += item["actions"].get(action_filter, 0) if action_filter else item["actions_total"]
    series = []
    cursor = _crm_date(date_range["from"]); finish = _crm_date(date_range["to"])
    while cursor <= finish:
        item = days.get(cursor.isoformat(), {"date": cursor.isoformat(), "shifts": 0,
            "employees": set(), "worked_minutes": 0, "actions": 0})
        item["employees"] = len(item["employees"])
        item["actions_per_hour"] = round(item["actions"] * 60 / item["worked_minutes"], 2) \
            if item["worked_minutes"] else None
        series.append(item); cursor += timedelta(days=1)
    return web.json_response({"city": {"id": city["id"], "name": city["name"]},
        "range": {"from": date_range["from"], "to": date_range["to"]},
        "bucket": "day", "action_type": action_filter, "series": series})


def _crm_action_codes(value):
    return [code.strip() for code in str(value or "").split(",") if re.fullmatch(r"\d{4}", code.strip())]


async def api_crm_activity(request):
    """Recent parser events and exact bike history without changing parser rules."""
    context, error = await _crm_admin(request)
    if error is not None: return error
    city, error = _crm_city(context, request=request)
    if error is not None: return error
    role, error = _crm_scoped_role(context)
    if error is not None: return error
    try: limit = min(100, max(1, int(request.query.get("limit", 30))))
    except (TypeError, ValueError): return web.json_response({"error": "limit"}, status=400)
    bike_code = str(request.query.get("bike_code") or "").strip()
    if bike_code and not re.fullmatch(r"\d{4}", bike_code):
        return web.json_response(
            {"error": "bike_code", "message": "Номер байка — ровно 4 цифры."}, status=400
        )
    event_sql = "COALESCE(w.created_at,m.created_at,s.start_at,s.created_at)"
    sql = (
        "SELECT a.id,a.user_id,a.shift_id,a.message_id,a.action_type,a.bike_codes,a.quantity,"
        "s.full_name,s.role," + event_sql + " AS event_at,"
        "CASE WHEN m.id IS NOT NULL THEN 'manual_report' ELSE 'telegram' END AS source "
        "FROM actions a JOIN shifts s ON s.id=a.shift_id "
        "LEFT JOIN work_message_links w ON w.city_id=a.city_id "
        "AND w.chat_id=COALESCE(a.chat_id,0) AND w.user_id=a.user_id "
        "AND w.message_id=a.message_id AND w.shift_id=a.shift_id "
        "LEFT JOIN manual_reports m ON m.city_id=a.city_id AND m.user_id=a.user_id "
        "AND m.message_id=a.message_id AND m.shift_id=a.shift_id WHERE a.city_id=?"
    )
    params = [city["id"]]
    if role:
        sql += " AND LOWER(COALESCE(s.role,''))=LOWER(?)"; params.append(role)
    if bike_code:
        sql += " AND (','||REPLACE(COALESCE(a.bike_codes,''),' ','')||',') LIKE ?"
        params.append(f"%,{bike_code},%")
    sql += " ORDER BY datetime(" + event_sql + ") DESC,a.id DESC LIMIT ?"
    params.append(limit * 4)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(sql, params)).fetchall()
    items = []
    for row in rows:
        units = _action_units(row)
        if units <= 0 or row["action_type"] not in CRM_ACTION_TYPES:
            continue
        codes = _crm_action_codes(row["bike_codes"])
        if bike_code and bike_code not in codes:
            continue
        items.append({
            "action_id": row["id"], "event_at": row["event_at"],
            "action_type": row["action_type"], "bike_codes": codes,
            "units": units, "user_id": row["user_id"],
            "employee_name": row["full_name"] or f"Сотрудник #{row['user_id']}",
            "role": row["role"] or "", "shift_id": row["shift_id"],
            "message_id": row["message_id"], "source": row["source"],
        })
        if len(items) >= limit: break
    return web.json_response({"city": {"id": city["id"], "name": city["name"]},
                              "bike_code": bike_code or None, "items": items})


async def api_crm_operational_signals(request):
    """Only actionable live signals: inactivity, rate drop, overrun and blocked tasks."""
    context, error = await _crm_admin(request)
    if error is not None: return error
    city, error = _crm_city(context, request=request)
    if error is not None: return error
    role, error = _crm_scoped_role(context)
    if error is not None: return error
    tz = _city_tz(city); now = datetime.now(tz)
    event_sql = "COALESCE(w.created_at,m.created_at,s.start_at,s.created_at)"
    shift_sql = "SELECT * FROM shifts WHERE city_id=? AND is_active=1"
    shift_params = [city["id"]]
    if role: shift_sql += " AND LOWER(COALESCE(role,''))=LOWER(?)"; shift_params.append(role)
    action_sql = (
        "SELECT a.shift_id,a.bike_codes,a.quantity," + event_sql + " AS event_at "
        "FROM actions a JOIN shifts s ON s.id=a.shift_id "
        "LEFT JOIN work_message_links w ON w.city_id=a.city_id "
        "AND w.chat_id=COALESCE(a.chat_id,0) AND w.user_id=a.user_id "
        "AND w.message_id=a.message_id AND w.shift_id=a.shift_id "
        "LEFT JOIN manual_reports m ON m.city_id=a.city_id AND m.user_id=a.user_id "
        "AND m.message_id=a.message_id AND m.shift_id=a.shift_id "
        "WHERE a.city_id=? AND datetime(" + event_sql + ")>=datetime(?)"
    )
    action_params = [city["id"], (now - timedelta(hours=3)).isoformat()]
    if role: action_sql += " AND LOWER(COALESCE(s.role,''))=LOWER(?)"; action_params.append(role)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        shifts = await (await db.execute(shift_sql, shift_params)).fetchall()
        events = await (await db.execute(action_sql, action_params)).fetchall()
        task_sql = (
            "SELECT a.task_id,a.user_id,a.full_name_snap,a.role_snap,a.updated_at,t.title "
            "FROM crm_task_assignees a JOIN crm_tasks t ON t.id=a.task_id "
            "WHERE t.city_id=? AND t.status='published' AND a.status='blocked'"
        )
        task_params = [city["id"]]
        if role: task_sql += " AND LOWER(COALESCE(a.role_snap,''))=LOWER(?)"; task_params.append(role)
        blocked = await (await db.execute(task_sql, task_params)).fetchall()
        plan_sql = (
            "SELECT p.*,u.full_name AS user_name,u.role AS user_role FROM crm_planned_shifts p "
            "LEFT JOIN users u ON u.user_id=p.user_id AND u.city_id=p.city_id "
            "WHERE p.city_id=? AND p.work_date=? AND p.status='scheduled'"
        )
        plan_params = [city["id"], now.date().isoformat()]
        if role:
            plan_sql += " AND LOWER(COALESCE(p.role,u.role,''))=LOWER(?)"; plan_params.append(role)
        today_plans = await (await db.execute(plan_sql, plan_params)).fetchall()
    today_range = {"from": now.date().isoformat(), "to": now.date().isoformat(),
                   "start_at": datetime.combine(now.date(), datetime.min.time(), tzinfo=tz),
                   "end_at": datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), tzinfo=tz)}
    actual_rows, _ = await _crm_load_shifts(city, today_range, role=role)
    by_shift = {}
    for event in events:
        event_at = _parse_datetime(event["event_at"])
        units = _action_units(event)
        if not event_at or units <= 0: continue
        by_shift.setdefault(event["shift_id"], []).append((event_at.astimezone(tz), units))
    items = []
    working_now = 0; on_lunch = 0
    actions_last_hour = sum(units for events_for_shift in by_shift.values()
                            for at, units in events_for_shift if now - timedelta(hours=1) <= at <= now)
    for row in shifts:
        shift = dict(row); start = _parse_datetime(shift.get("start_at") or shift.get("created_at"))
        if not start: continue
        start = start.astimezone(tz); worked = max(0, int((now - start).total_seconds() // 60))
        shift_events = by_shift.get(shift["id"], [])
        recent = sum(units for at, units in shift_events if now - timedelta(hours=1) <= at <= now)
        previous = sum(units for at, units in shift_events
                       if now - timedelta(hours=3) <= at < now - timedelta(hours=1))
        if shift.get("on_lunch"):
            on_lunch += 1; continue
        working_now += 1
        last_at = max((at for at, _ in shift_events), default=start)
        idle_minutes = max(0, int((now - last_at).total_seconds() // 60))
        base = {"user_id": shift.get("user_id"), "shift_id": shift["id"],
                "name": shift.get("full_name") or f"Сотрудник #{shift.get('user_id')}",
                "role": shift.get("role") or "", "worked_minutes": worked,
                "recent_actions": recent, "previous_actions": previous}
        if worked >= 75 and idle_minutes >= 60:
            items.append({**base, "type": "no_activity", "severity": "high",
                          "minutes_without_actions": idle_minutes,
                          "title": f"Нет действий {idle_minutes // 60} ч {idle_minutes % 60:02d} мин"})
        elif worked >= 180 and previous >= 8 and recent * 2 <= previous * 0.4:
            items.append({**base, "type": "rate_drop", "severity": "medium",
                          "title": "Темп заметно снизился"})
        if worked > 12 * 60 + 10:
            items.append({**base, "type": "shift_overrun", "severity": "high",
                          "title": "Смена длится больше 12 часов"})
    for task in blocked:
        items.append({"type": "task_blocked", "severity": "high",
                      "task_id": task["task_id"], "user_id": task["user_id"],
                      "name": task["full_name_snap"] or f"Сотрудник #{task['user_id']}",
                      "role": task["role_snap"] or "", "title": f"Задание заблокировано: {task['title']}"})
    for plan_row in today_plans:
        plan = dict(plan_row)
        plan_start = datetime.combine(
            now.date(), datetime.strptime(plan["start_time"], "%H:%M").time(), tzinfo=tz
        )
        if now < plan_start + timedelta(minutes=30):
            continue
        segment = _crm_segment_from_time(plan["start_time"])
        matched = any(
            _shift_segment(actual, city) == segment and (
                (plan["user_id"] is not None and actual.get("user_id") == plan["user_id"])
                or (plan["user_id"] is None and
                    (actual.get("role") or "").casefold() == (plan.get("role") or "").casefold())
            ) for actual in actual_rows
        )
        if not matched:
            items.append({"type": "plan_no_show", "severity": "high",
                          "plan_id": plan["id"], "user_id": plan.get("user_id"),
                          "name": plan.get("user_name") or plan.get("role") or "Сотрудник",
                          "role": plan.get("user_role") or plan.get("role") or "",
                          "title": "Не открыл смену по плану"})
    severity_order = {"high": 0, "medium": 1}
    items.sort(key=lambda item: severity_order.get(item.get("severity"), 9))
    counts = {"total": len(items), "high": sum(i["severity"] == "high" for i in items),
              "medium": sum(i["severity"] == "medium" for i in items)}
    return web.json_response({"city": {"id": city["id"], "name": city["name"]},
        "generated_at": now.isoformat(), "counts": counts, "items": items[:50],
        "summary": {"active": len(shifts), "working_now": working_now,
                    "on_lunch": on_lunch, "actions_last_hour": actions_last_hour}})


async def api_crm_data_quality(request):
    context, error = await _crm_admin(request)
    if error is not None: return error
    city, error = _crm_city(context, request=request)
    if error is not None: return error
    date_range, error = _crm_range(request, city, default_days=30)
    if error is not None: return error
    role, error = _crm_scoped_role(context)
    if error is not None: return error
    rows, stats = await _crm_load_shifts(city, date_range, role=role)
    issues = []
    valid_roles = {role.casefold() for role in city_supported_roles(city["id"])}
    for row in rows:
        item = _crm_shift_item(row, city, stats.get(row["id"]))
        if item["status"] == "active" and item["worked_minutes"] > 14 * 60:
            issues.append({"type": "long_open_shift", "severity": "high", "shift": item})
        if not row.get("start_at"):
            issues.append({"type": "missing_start_at", "severity": "high", "shift": item})
        if not row.get("is_active") and not row.get("end_at"):
            issues.append({"type": "missing_end_at", "severity": "high", "shift": item})
        if (row.get("role") or "").casefold() not in valid_roles:
            issues.append({"type": "unknown_role", "severity": "medium", "shift": item})
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        manual_sql = (
            "SELECT m.id,m.user_id,m.sender_name,m.raw_text,m.parse_error,m.created_at,m.updated_at "
            "FROM manual_reports m LEFT JOIN users u ON u.user_id=m.user_id "
            "WHERE m.city_id=? AND m.status='needs_review' AND m.created_at>=? AND m.created_at<?"
        )
        manual_params = [city["id"], date_range["start_at"].isoformat(),
                         date_range["end_at"].isoformat()]
        if role:
            manual_sql += " AND LOWER(u.role)=LOWER(?)"; manual_params.append(role)
        manual_sql += " ORDER BY m.created_at DESC LIMIT 200"
        manual = await (await db.execute(
            manual_sql, manual_params,
        )).fetchall()
        attachment_sql = (
            "SELECT DISTINCT a.id,a.storage_key,t.id AS task_id FROM crm_task_attachments a "
            "JOIN crm_tasks t ON t.id=a.task_id LEFT JOIN crm_task_assignees ta ON ta.task_id=t.id "
            "WHERE t.city_id=?"
        )
        attachment_params = [city["id"]]
        if role:
            attachment_sql += " AND LOWER(ta.role_snap)=LOWER(?)"; attachment_params.append(role)
        attachments = await (await db.execute(attachment_sql, attachment_params)).fetchall()
    for row in manual:
        issues.append({"type": "manual_report_waiting", "severity": "medium", "report": dict(row)})
    for row in attachments:
        if not os.path.isfile(os.path.join(CRM_UPLOAD_DIR, row["storage_key"])):
            issues.append({"type": "attachment_missing", "severity": "medium",
                           "attachment_id": row["id"], "task_id": row["task_id"]})
    counts = {}
    for issue in issues: counts[issue["type"]] = counts.get(issue["type"], 0) + 1
    return web.json_response({"city": {"id": city["id"], "name": city["name"]},
        "range": {"from": date_range["from"], "to": date_range["to"]},
        "counts": counts, "items": issues})


async def _crm_audit(db, context, operation, entity_type, entity_id, city_id,
                     before=None, after=None):
    await db.execute(
        "INSERT INTO admin_audit_log (admin_user_id, city_id, operation, entity_type, "
        "entity_id, before_json, after_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (context["telegram_user"]["id"], city_id, operation, entity_type, str(entity_id),
         json.dumps(before, ensure_ascii=False, default=str) if before is not None else None,
         json.dumps(after, ensure_ascii=False, default=str) if after is not None else None,
         datetime.now(timezone.utc).isoformat()),
    )


def _crm_segment_from_time(value):
    try: hour = int(str(value).split(":", 1)[0])
    except (TypeError, ValueError): return None
    return "day" if 5 <= hour < 17 else "night"


async def _crm_validate_plan_target(db, city, user_id, role, role_scope=None):
    if (user_id is None) == (not role):
        return "Укажите либо сотрудника, либо роль."
    if user_id is not None:
        row = await (await db.execute(
            "SELECT role FROM users WHERE user_id=? AND city_id=?", (user_id, city["id"])
        )).fetchone()
        if not row: return "Сотрудник не найден в этом городе."
        if role_scope and (row[0] or "").casefold() != role_scope.casefold():
            return f"Доступ ограничен ролью {role_scope}."
    elif role.casefold() not in {item.casefold() for item in city_supported_roles(city["id"])}:
        return "Роль не поддерживается в этом городе."
    elif role_scope and role.casefold() != role_scope.casefold():
        return f"Доступ ограничен ролью {role_scope}."
    return None


async def api_crm_calendar(request):
    context, error = await _crm_admin(request)
    if error is not None: return error
    city, error = _crm_city(context, request=request)
    if error is not None: return error
    date_range, error = _crm_range(request, city, default_days=31)
    if error is not None: return error
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        plans = await (await db.execute(
            "SELECT p.*, u.full_name AS user_name FROM crm_planned_shifts p "
            "LEFT JOIN users u ON u.user_id=p.user_id AND u.city_id=p.city_id "
            "WHERE p.city_id=? AND p.work_date>=? AND p.work_date<=? "
            "ORDER BY p.work_date, p.start_time, p.id",
            (city["id"], date_range["from"], date_range["to"]),
        )).fetchall()
    role_scope, error = _crm_scoped_role(context)
    if error is not None: return error
    if role_scope:
        plans = [row for row in plans if
                 ((row["role"] or "").casefold() == role_scope.casefold()) or row["user_id"] is not None]
        if any(row["user_id"] is not None for row in plans):
            async with aiosqlite.connect(DB_PATH) as scope_db:
                allowed_users = {item[0] for item in await (await scope_db.execute(
                    "SELECT user_id FROM users WHERE city_id=? AND LOWER(role)=LOWER(?)",
                    (city["id"], role_scope),
                )).fetchall()}
            plans = [row for row in plans if row["user_id"] is None or row["user_id"] in allowed_users]
    task_clauses = ["city_id=?", "COALESCE(date_to,work_date)>=?",
                    "COALESCE(date_from,work_date)<=?"]
    task_params = [city["id"], date_range["from"], date_range["to"]]
    if role_scope:
        task_clauses.append("(EXISTS (SELECT 1 FROM crm_task_targets tt LEFT JOIN users u "
                            "ON u.user_id=tt.user_id AND u.city_id=crm_tasks.city_id "
                            "WHERE tt.task_id=crm_tasks.id AND ((tt.target_type='role' AND "
                            "LOWER(tt.role)=LOWER(?)) OR (tt.target_type='user' AND "
                            "LOWER(u.role)=LOWER(?)))) OR EXISTS (SELECT 1 FROM crm_task_assignees a "
                            "WHERE a.task_id=crm_tasks.id AND LOWER(a.role_snap)=LOWER(?)))")
        task_params.extend([role_scope, role_scope, role_scope])
    async with aiosqlite.connect(DB_PATH) as task_db:
        task_db.row_factory = aiosqlite.Row
        task_rows = await (await task_db.execute(
            "SELECT * FROM crm_tasks WHERE " + " AND ".join(task_clauses) +
            " ORDER BY COALESCE(date_from,work_date),id", task_params,
        )).fetchall()
        calendar_tasks = await _crm_task_payloads(task_db, task_rows)
    actual_rows, actual_stats = await _crm_load_shifts(city, date_range, role=role_scope)
    actual_items = [_crm_shift_item(row, city, actual_stats.get(row["id"])) for row in actual_rows]
    matched = set(); plan_items = []
    for raw in plans:
        plan = dict(raw); segment = _crm_segment_from_time(plan["start_time"])
        if plan["status"] == "cancelled":
            candidates = []
        elif plan["user_id"] is not None:
            candidates = [item for item in actual_items if item["user_id"] == plan["user_id"]
                          and item["date"] == plan["work_date"] and item["segment"] == segment]
        else:
            candidates = [item for item in actual_items
                          if (item["role"] or "").casefold() == (plan["role"] or "").casefold()
                          and item["date"] == plan["work_date"] and item["segment"] == segment]
        plan_start = datetime.combine(
            _crm_date(plan["work_date"]),
            datetime.strptime(plan["start_time"], "%H:%M").time(),
            tzinfo=_city_tz(city),
        )
        if plan["status"] == "cancelled": match_status = "отменено"
        elif candidates and len(candidates) == 1: match_status = "вышел"
        elif candidates: match_status = "неоднозначно"
        elif datetime.now(_city_tz(city)) < plan_start + timedelta(minutes=30):
            match_status = "ожидается"
        else: match_status = "не вышел"
        for item in candidates: matched.add(item["shift_id"])
        plan_items.append({
            "plan_id": plan["id"], "city_id": plan["city_id"],
            "work_date": plan["work_date"], "start_time": plan["start_time"],
            "end_time": plan["end_time"], "segment": segment,
            "user_id": plan["user_id"], "user_name": plan.get("user_name"),
            "role": plan["role"], "district": plan["district"] or "",
            "note": plan["note"] or "", "work_kind": plan.get("work_kind") or "regular",
            "status": plan["status"],
            "match_status": match_status, "actual_shifts": candidates,
            "updated_at": plan["updated_at"],
        })
    unplanned = []
    for item in actual_items:
        if item["shift_id"] not in matched:
            extra = dict(item)
            extra["match_status"] = "подработка"
            extra["work_kind"] = "extra"
            unplanned.append(extra)
    day_totals = {}
    for plan in plan_items:
        day = day_totals.setdefault(plan["work_date"], {"planned": 0, "came": 0,
            "missed": 0, "expected": 0, "ambiguous": 0, "unplanned": 0, "extra": 0})
        if plan["status"] != "cancelled": day["planned"] += 1
        if plan.get("work_kind") == "extra" and plan["status"] != "cancelled": day["extra"] += 1
        if plan["match_status"] == "вышел": day["came"] += 1
        elif plan["match_status"] == "не вышел": day["missed"] += 1
        elif plan["match_status"] == "ожидается": day["expected"] += 1
        elif plan["match_status"] == "неоднозначно": day["ambiguous"] += 1
    for item in unplanned:
        day_totals.setdefault(item["date"], {"planned": 0, "came": 0, "missed": 0,
            "expected": 0, "ambiguous": 0, "unplanned": 0, "extra": 0})["unplanned"] += 1
    return web.json_response({"city": {"id": city["id"], "name": city["name"]},
        "range": {"from": date_range["from"], "to": date_range["to"]},
        "planned": plan_items, "actual_unplanned": unplanned, "days": day_totals,
        "tasks": calendar_tasks})


async def api_crm_planned_shift_create(request):
    context, error = await _crm_admin(request, write=True)
    if error is not None: return error
    body = await _request_json_object(request)
    if body is None: return web.json_response({"error": "json"}, status=400)
    city, error = _crm_city(context, body=body)
    if error is not None: return error
    work_date = _crm_date(body.get("work_date")); start = _valid_time(body.get("start_time"))
    end = _valid_time(body.get("end_time")); role = (body.get("role") or "").strip() or None
    work_kind = str(body.get("work_kind") or "regular").strip().lower()
    try: user_id = int(body["user_id"]) if body.get("user_id") not in (None, "") else None
    except (TypeError, ValueError): return web.json_response({"error": "user_id"}, status=400)
    if not work_date or not start or not end or work_kind not in {"regular", "extra"}:
        return web.json_response({"error": "fields", "message": "Нужны дата и время."}, status=400)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row; await db.execute("PRAGMA busy_timeout=15000")
        target_error = await _crm_validate_plan_target(
            db, city, user_id, role, _crm_scope_role(context)
        )
        if target_error: return web.json_response({"error": "target", "message": target_error}, status=400)
        segment = _crm_segment_from_time(start)
        if user_id is not None:
            duplicate = await (await db.execute(
                "SELECT id FROM crm_planned_shifts WHERE city_id=? AND work_date=? "
                "AND user_id=? AND status='scheduled' AND "
                "CASE WHEN CAST(substr(start_time,1,2) AS INTEGER)>=5 AND "
                "CAST(substr(start_time,1,2) AS INTEGER)<17 THEN 'day' ELSE 'night' END=? LIMIT 1",
                (city["id"], work_date.isoformat(), user_id, segment),
            )).fetchone()
            if duplicate:
                return web.json_response({"error": "duplicate", "plan_id": duplicate[0]}, status=409)
        now_iso = datetime.now(timezone.utc).isoformat(); admin_uid = context["telegram_user"]["id"]
        cur = await db.execute(
            "INSERT INTO crm_planned_shifts (city_id,work_date,start_time,end_time,user_id,role,"
            "district,note,work_kind,status,created_by,created_at,updated_by,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?, 'scheduled',?,?,?,?)",
            (city["id"], work_date.isoformat(), start, end, user_id, role,
             str(body.get("district") or "")[:200], str(body.get("note") or "")[:2000], work_kind,
             admin_uid, now_iso, admin_uid, now_iso),
        )
        plan_id = cur.lastrowid
        row = await (await db.execute("SELECT * FROM crm_planned_shifts WHERE id=?", (plan_id,))).fetchone()
        if user_id is not None:
            await _enqueue_crm_notification(
                db, city["id"], user_id, "planned_shift", plan_id,
                {"plan_id": plan_id, "work_date": work_date.isoformat(),
                 "start_time": start, "end_time": end,
                 "work_kind": work_kind,
                 "district": str(body.get("district") or "")[:200],
                 "note": str(body.get("note") or "")[:2000]},
            )
        await _crm_audit(db, context, "planned_shift.create", "planned_shift", plan_id,
                         city["id"], after=dict(row)); await db.commit()
    return web.json_response({"ok": True, "plan": dict(row)}, status=201)


async def api_crm_planned_shifts_batch(request):
    context, error = await _crm_admin(request, write=True)
    if error is not None: return error
    body = await _request_json_object(request)
    if body is None: return web.json_response({"error": "json"}, status=400)
    city, error = _crm_city(context, body=body)
    if error is not None: return error
    start_date = _crm_date(body.get("date_from")); end_date = _crm_date(body.get("date_to"))
    start = _valid_time(body.get("start_time")); end = _valid_time(body.get("end_time"))
    district = str(body.get("district") or "")[:200]
    note = str(body.get("note") or "")[:2000]
    work_kind = str(body.get("work_kind") or "regular").strip().lower()
    idempotency_key = str(body.get("idempotency_key") or "").strip()[:200]
    try:
        work_days = int(body.get("work_days")); rest_days = int(body.get("rest_days"))
        user_ids = list(dict.fromkeys(int(item) for item in body.get("user_ids", [])))
    except (TypeError, ValueError):
        return web.json_response({"error": "fields"}, status=400)
    if (not start_date or not end_date or end_date < start_date or not start or not end
            or not user_ids or not idempotency_key or work_kind not in {"regular", "extra"}
            or (work_days, rest_days) not in {(2, 1), (2, 2), (3, 1)}
            or (end_date - start_date).days + 1 > CRM_MAX_RANGE_DAYS):
        return web.json_response({"error": "fields"}, status=400)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA busy_timeout=15000")
        await db.execute("BEGIN IMMEDIATE")
        existing = await (await db.execute(
            "SELECT id,city_id,summary_json FROM crm_planning_batches WHERE idempotency_key=?",
            (idempotency_key,),
        )).fetchone()
        if existing:
            await db.rollback()
            if existing["city_id"] != city["id"]:
                return web.json_response({"error": "idempotency_key_conflict"}, status=409)
            summary = json.loads(existing["summary_json"] or "{}")
            summary.update({"ok": True, "batch_id": existing["id"], "idempotent_replay": True})
            return web.json_response(summary)
        placeholders = ",".join("?" for _ in user_ids)
        users = await (await db.execute(
            f"SELECT user_id,full_name,role FROM users WHERE city_id=? AND user_id IN ({placeholders})",
            [city["id"], *user_ids],
        )).fetchall()
        by_id = {row["user_id"]: row for row in users}
        role_scope = _crm_scope_role(context)
        if len(by_id) != len(user_ids) or (role_scope and any(
                (row["role"] or "").casefold() != role_scope.casefold() for row in users)):
            await db.rollback()
            return web.json_response({"error": "target", "message": "Сотрудник не найден или недоступен."}, status=400)
        now_iso = datetime.now(timezone.utc).isoformat(); admin_uid = context["telegram_user"]["id"]
        request_snapshot = {"city_id": city["id"], "user_ids": user_ids,
                            "date_from": start_date.isoformat(), "date_to": end_date.isoformat(),
                            "work_days": work_days, "rest_days": rest_days, "start_time": start,
                            "end_time": end, "district": district, "note": note,
                            "work_kind": work_kind}
        cur = await db.execute(
            "INSERT INTO crm_planning_batches (city_id,idempotency_key,date_from,date_to,work_days,"
            "rest_days,request_json,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (city["id"], idempotency_key, start_date.isoformat(), end_date.isoformat(),
             work_days, rest_days, json.dumps(request_snapshot, ensure_ascii=False), admin_uid, now_iso),
        )
        batch_id = cur.lastrowid; created = 0; skipped = 0
        counts = {str(uid): {"created": 0, "skipped": 0} for uid in user_ids}
        current = start_date; cycle = work_days + rest_days; segment = _crm_segment_from_time(start)
        while current <= end_date:
            if (current - start_date).days % cycle < work_days:
                work_date = current.isoformat()
                for uid in user_ids:
                    duplicate = await (await db.execute(
                        "SELECT id FROM crm_planned_shifts WHERE city_id=? AND work_date=? AND user_id=? "
                        "AND status='scheduled' AND CASE WHEN CAST(substr(start_time,1,2) AS INTEGER)>=5 "
                        "AND CAST(substr(start_time,1,2) AS INTEGER)<17 THEN 'day' ELSE 'night' END=? LIMIT 1",
                        (city["id"], work_date, uid, segment),
                    )).fetchone()
                    if duplicate:
                        skipped += 1; counts[str(uid)]["skipped"] += 1; continue
                    await db.execute(
                        "INSERT INTO crm_planned_shifts (city_id,work_date,start_time,end_time,user_id,role,"
                        "district,note,work_kind,status,created_by,created_at,updated_by,updated_at,batch_id) "
                        "VALUES (?,?,?,?,?,?,?,?,?,'scheduled',?,?,?,?,?)",
                        (city["id"], work_date, start, end, uid, None, district, note, work_kind,
                         admin_uid, now_iso, admin_uid, now_iso, batch_id),
                    )
                    created += 1; counts[str(uid)]["created"] += 1
            current += timedelta(days=1)
        for uid in user_ids:
            await _enqueue_crm_notification(
                db, city["id"], uid, "plan_batch", batch_id,
                {"batch_id": batch_id, "date_from": start_date.isoformat(),
                 "date_to": end_date.isoformat(), "start_time": start, "end_time": end,
                 "district": district, "note": note, **counts[str(uid)]},
            )
        summary = {"created": created, "skipped": skipped, "by_user": counts}
        await db.execute("UPDATE crm_planning_batches SET summary_json=? WHERE id=?",
                         (json.dumps(summary, ensure_ascii=False), batch_id))
        await _crm_audit(db, context, "planned_shift.batch_create", "planning_batch", batch_id,
                         city["id"], after={**request_snapshot, **summary})
        await db.commit()
    return web.json_response({"ok": True, "batch_id": batch_id, **summary,
                              "idempotent_replay": False}, status=201)


async def api_crm_planned_shift_update(request):
    context, error = await _crm_admin(request, write=True)
    if error is not None: return error
    body = await _request_json_object(request)
    if body is None: return web.json_response({"error": "json"}, status=400)
    try: plan_id = int(request.match_info["plan_id"])
    except (KeyError, ValueError): return web.json_response({"error": "plan_id"}, status=400)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row; await db.execute("BEGIN IMMEDIATE")
        current = await (await db.execute("SELECT * FROM crm_planned_shifts WHERE id=?", (plan_id,))).fetchone()
        if not current or current["city_id"] not in context["allowed_city_ids"]:
            await db.rollback(); return web.json_response({"error": "not_found"}, status=404)
        if _crm_scope_role(context) and await _crm_validate_plan_target(
            db, get_city(current["city_id"]), current["user_id"], current["role"],
            _crm_scope_role(context)
        ):
            await db.rollback(); return web.json_response({"error": "not_found"}, status=404)
        merged = dict(current)
        for field in ("work_date", "start_time", "end_time", "user_id", "role", "district",
                      "note", "work_kind", "status"):
            if field in body: merged[field] = body[field]
        work_date = _crm_date(merged["work_date"]); start = _valid_time(merged["start_time"])
        end = _valid_time(merged["end_time"]); role = (merged.get("role") or "").strip() or None
        try: user_id = int(merged["user_id"]) if merged.get("user_id") not in (None, "") else None
        except (TypeError, ValueError): await db.rollback(); return web.json_response({"error": "user_id"}, status=400)
        work_kind = str(merged.get("work_kind") or "regular").strip().lower()
        if (not work_date or not start or not end
                or merged.get("status") not in {"scheduled", "cancelled"}
                or work_kind not in {"regular", "extra"}):
            await db.rollback(); return web.json_response({"error": "fields"}, status=400)
        target_error = await _crm_validate_plan_target(
            db, get_city(current["city_id"]), user_id, role, _crm_scope_role(context)
        )
        if target_error:
            await db.rollback(); return web.json_response({"error": "target", "message": target_error}, status=400)
        now_iso = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "UPDATE crm_planned_shifts SET work_date=?,start_time=?,end_time=?,user_id=?,role=?,"
            "district=?,note=?,work_kind=?,status=?,updated_by=?,updated_at=? WHERE id=?",
            (work_date.isoformat(), start, end, user_id, role, str(merged.get("district") or "")[:200],
             str(merged.get("note") or "")[:2000], work_kind, merged["status"],
             context["telegram_user"]["id"], now_iso, plan_id),
        )
        updated = await (await db.execute("SELECT * FROM crm_planned_shifts WHERE id=?", (plan_id,))).fetchone()
        await _crm_audit(db, context, "planned_shift.update", "planned_shift", plan_id,
                         current["city_id"], before=dict(current), after=dict(updated)); await db.commit()
    return web.json_response({"ok": True, "plan": dict(updated)})


async def api_crm_context(request):
    context, error = await _crm_admin(request)
    if error is not None: return error
    return web.json_response({
        "ok": True,
        "user": {"id": context["telegram_user"]["id"],
                 "name": context["user"].get("full_name") or context["telegram_user"].get("first_name")},
        "role": context["admin"]["role"],
        "role_scope": context["admin"].get("role_scope"),
        "can_write": context["admin"]["role"] in CRM_ADMIN_WRITE_ROLES,
        "cities": [{"id": city_id, "name": get_city(city_id)["name"]}
                   for city_id in context["allowed_city_ids"] if get_city(city_id)],
        "default_city_id": context["city"]["id"] if context.get("city") else None,
    })


async def _crm_target_users(db, city_id, target_type, user_id=None, role=None, role_scope=None):
    db.row_factory = aiosqlite.Row
    if target_type == "user":
        sql = "SELECT user_id, full_name, role FROM users WHERE city_id=? AND user_id=?"
        params = [city_id, user_id]
        if role_scope: sql += " AND LOWER(role)=LOWER(?)"; params.append(role_scope)
        rows = await (await db.execute(
            sql, params,
        )).fetchall()
    elif target_type == "role":
        if role_scope and (role or "").casefold() != role_scope.casefold():
            return []
        rows = await (await db.execute(
            "SELECT user_id, full_name, role FROM users WHERE city_id=? AND LOWER(role)=LOWER(?) "
            "ORDER BY full_name", (city_id, role),
        )).fetchall()
    else:
        rows = []
    return [dict(row) for row in rows]


async def _crm_task_in_scope(db, task_id, context):
    db.row_factory = aiosqlite.Row
    task = await (await db.execute("SELECT * FROM crm_tasks WHERE id=?", (task_id,))).fetchone()
    if not task or task["city_id"] not in context["allowed_city_ids"]:
        return None
    scope = _crm_scope_role(context)
    if not scope: return task
    allowed = await (await db.execute(
        "SELECT 1 FROM crm_task_targets tt LEFT JOIN users u ON u.user_id=tt.user_id "
        "AND u.city_id=? WHERE tt.task_id=? AND ((tt.target_type='role' AND LOWER(tt.role)=LOWER(?)) "
        "OR (tt.target_type='user' AND LOWER(u.role)=LOWER(?))) UNION SELECT 1 FROM "
        "crm_task_assignees a WHERE a.task_id=? AND LOWER(a.role_snap)=LOWER(?) LIMIT 1",
        (task["city_id"], task_id, scope, scope, task_id, scope),
    )).fetchone()
    return task if allowed else None


async def _crm_task_role_denied(db, task_id, context):
    if not _crm_scope_role(context): return False
    row = await (await db.execute("SELECT city_id FROM crm_tasks WHERE id=?", (task_id,))).fetchone()
    return bool(row and row[0] in context["allowed_city_ids"])


async def _enqueue_crm_notification(db, city_id, user_id, kind, entity_id, payload):
    """Transactional outbox: delivery happens only after the caller commits."""
    now_iso = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT OR IGNORE INTO crm_notification_outbox "
        "(city_id,user_id,kind,entity_id,payload_json,status,attempt_count,next_attempt_at,created_at) "
        "VALUES (?,?,?,?,?,'pending',0,?,?)",
        (city_id, user_id, kind, entity_id,
         json.dumps(payload, ensure_ascii=False, default=str), now_iso, now_iso),
    )


async def api_crm_task_assignee_preview(request):
    context, error = await _crm_admin(request)
    if error is not None: return error
    city, error = _crm_city(context, request=request)
    if error is not None: return error
    target_type = request.query.get("target_type")
    try: user_id = int(request.query["user_id"]) if request.query.get("user_id") else None
    except ValueError: return web.json_response({"error": "user_id"}, status=400)
    role = (request.query.get("role") or "").strip() or None
    if (target_type == "user" and user_id is None) or (target_type == "role" and not role):
        return web.json_response({"error": "target"}, status=400)
    async with aiosqlite.connect(DB_PATH) as db:
        items = await _crm_target_users(
            db, city["id"], target_type, user_id, role, _crm_scope_role(context)
        )
    return web.json_response({"city_id": city["id"], "target_type": target_type,
                              "count": len(items), "items": items})


async def _crm_publish_task(db, task_id, expected_city_id=None, actor_user_id=None):
    db.row_factory = aiosqlite.Row
    task = await (await db.execute("SELECT * FROM crm_tasks WHERE id=?", (task_id,))).fetchone()
    if not task or (expected_city_id is not None and task["city_id"] != expected_city_id):
        raise LookupError("Задание не найдено.")
    if task["status"] == "cancelled": raise ValueError("Отменённое задание нельзя опубликовать.")
    if task["status"] == "published": return dict(task), False
    targets = await (await db.execute(
        "SELECT * FROM crm_task_targets WHERE task_id=? ORDER BY id", (task_id,)
    )).fetchall()
    recipients = {}
    for target in targets:
        for user in await _crm_target_users(
            db, task["city_id"], target["target_type"], target["user_id"], target["role"]
        ):
            recipients[user["user_id"]] = user
    if not recipients: raise ValueError("У задания нет получателей в выбранном городе.")
    now_iso = datetime.now(timezone.utc).isoformat()
    await db.executemany(
        "INSERT OR IGNORE INTO crm_task_assignees "
        "(task_id,user_id,full_name_snap,role_snap,status,updated_at) "
        "VALUES (?,?,?,?, 'assigned', ?)",
        [(task_id, uid, user.get("full_name"), user.get("role"), now_iso)
         for uid, user in recipients.items()],
    )
    brief = await (await db.execute(
        "SELECT id FROM crm_task_attachments WHERE task_id=? AND kind='brief' ORDER BY id LIMIT 1",
        (task_id,),
    )).fetchone()
    date_from = task["date_from"] or task["work_date"]
    date_to = task["date_to"] or date_from
    for uid in recipients:
        await _enqueue_crm_notification(
            db, task["city_id"], uid, "task_assigned", task_id,
            {"task_id": task_id, "title": task["title"], "date_from": date_from,
             "date_to": date_to, "district": task["district"] or "",
             "description": task["description"] or "",
             "brief_attachment_id": brief[0] if brief else None},
        )
    await db.execute(
        "UPDATE crm_tasks SET status='published',published_at=?,updated_at=? WHERE id=?",
        (now_iso, now_iso, task_id),
    )
    if actor_user_id is not None:
        await _crm_task_event(db, task_id, actor_user_id, "task.published",
                              {"assignee_count": len(recipients)})
    updated = await (await db.execute("SELECT * FROM crm_tasks WHERE id=?", (task_id,))).fetchone()
    return dict(updated), True


def _crm_progress(rows):
    counts = {key: 0 for key in
              ("assigned", "seen", "in_progress", "submitted", "accepted", "blocked")}
    for row in rows:
        if row["status"] in counts: counts[row["status"]] += 1
    counts["total"] = sum(counts.values())
    counts["completed"] = counts["accepted"]
    return counts


async def _crm_task_payloads(db, rows, detailed=False, viewer_user_id=None):
    result = []
    for raw in rows:
        task = dict(raw)
        targets = await (await db.execute(
            "SELECT target_type,user_id,role FROM crm_task_targets WHERE task_id=? ORDER BY id",
            (task["id"],),
        )).fetchall()
        assignees = await (await db.execute(
            "SELECT task_id,user_id,full_name_snap,role_snap,status,status_comment,updated_at "
            "FROM crm_task_assignees WHERE task_id=? ORDER BY full_name_snap,user_id", (task["id"],)
        )).fetchall()
        attachment_sql = (
            "SELECT id,original_name,mime_type,size_bytes,sha256,kind,assignee_user_id,created_at "
            "FROM crm_task_attachments WHERE task_id=?"
        )
        attachment_params = [task["id"]]
        if viewer_user_id is not None:
            attachment_sql += " AND (kind='brief' OR (kind='result' AND assignee_user_id=?))"
            attachment_params.append(viewer_user_id)
        attachment_sql += " ORDER BY id"
        attachments = await (await db.execute(attachment_sql, attachment_params)).fetchall()
        attachment_items = [
            {**dict(row), "download_url": f"/api/crm/task-attachments/{row['id']}"}
            for row in attachments
        ]
        item = {
            "task_id": task["id"], "city_id": task["city_id"], "work_date": task["work_date"],
            "date_from": task.get("date_from") or task["work_date"],
            "date_to": task.get("date_to") or task.get("date_from") or task["work_date"],
            "district": task.get("district") or "",
            "completion_mode": task.get("completion_mode") or "manual",
            "title": task["title"], "description": task["description"],
            "priority": task["priority"], "status": task["status"],
            "requires_photo": bool(task.get("requires_photo")),
            "created_by": task["created_by"], "created_at": task["created_at"],
            "updated_at": task["updated_at"], "published_at": task["published_at"],
            "targets": [dict(row) for row in targets], "progress": _crm_progress(assignees),
            "attachments": [item for item in attachment_items if item["kind"] == "brief"],
            "result_attachments": [item for item in attachment_items if item["kind"] == "result"],
        }
        if detailed:
            item["assignees"] = [dict(row) for row in assignees]
            comments = await (await db.execute(
                "SELECT id,author_user_id,body,created_at FROM crm_task_comments "
                "WHERE task_id=? ORDER BY id", (task["id"],)
            )).fetchall()
            item["comments"] = [dict(row) for row in comments]
        result.append(item)
    return result


async def api_crm_tasks(request):
    context, error = await _crm_admin(request)
    if error is not None: return error
    city, error = _crm_city(context, request=request)
    if error is not None: return error
    date_range, error = _crm_range(request, city, default_days=31)
    if error is not None: return error
    paging = _crm_paging(request)
    if not paging: return web.json_response({"error": "paging"}, status=400)
    clauses = ["city_id=?", "COALESCE(date_to,work_date)>=?", "COALESCE(date_from,work_date)<=?"]
    params = [city["id"], date_range["from"], date_range["to"]]
    status = request.query.get("status")
    if status: clauses.append("status=?"); params.append(status)
    role_scope = _crm_scope_role(context)
    if role_scope:
        clauses.append("(EXISTS (SELECT 1 FROM crm_task_targets tt LEFT JOIN users u "
                       "ON u.user_id=tt.user_id AND u.city_id=crm_tasks.city_id "
                       "WHERE tt.task_id=crm_tasks.id AND ((tt.target_type='role' AND "
                       "LOWER(tt.role)=LOWER(?)) OR (tt.target_type='user' AND "
                       "LOWER(u.role)=LOWER(?)))) OR EXISTS (SELECT 1 FROM crm_task_assignees a "
                       "WHERE a.task_id=crm_tasks.id AND LOWER(a.role_snap)=LOWER(?)))")
        params.extend([role_scope, role_scope, role_scope])
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM crm_tasks WHERE " + " AND ".join(clauses) +
            " ORDER BY COALESCE(date_from,work_date) DESC,id DESC", params
        )).fetchall()
        items = await _crm_task_payloads(db, rows)
    limit, offset = paging
    return web.json_response({"city": {"id": city["id"], "name": city["name"]},
        "range": {"from": date_range["from"], "to": date_range["to"]},
        "items": items[offset:offset + limit], "page": {"limit": limit, "offset": offset,
        "total": len(items), "has_more": offset + limit < len(items)}})


async def api_crm_task_detail(request):
    context, error = await _crm_admin(request)
    if error is not None: return error
    try: task_id = int(request.match_info["task_id"])
    except (KeyError, ValueError): return web.json_response({"error": "task_id"}, status=400)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await _crm_task_in_scope(db, task_id, context)
        if not row:
            denied = await _crm_task_role_denied(db, task_id, context)
            return web.json_response({"error": "admin_role_scope" if denied else "not_found"},
                                     status=403 if denied else 404)
        item = (await _crm_task_payloads(db, [row], detailed=True))[0]
    return web.json_response({"task": item})


async def api_crm_task_create(request):
    context, error = await _crm_admin(request, write=True)
    if error is not None: return error
    body = await _request_json_object(request)
    if body is None: return web.json_response({"error": "json"}, status=400)
    city, error = _crm_city(context, body=body)
    if error is not None: return error
    date_from = _crm_date(body.get("date_from") or body.get("work_date"))
    date_to = _crm_date(body.get("date_to") or body.get("date_from") or body.get("work_date"))
    title = str(body.get("title") or "").strip()
    description = str(body.get("description") or "").strip(); priority = body.get("priority", "normal")
    district = str(body.get("district") or "").strip()[:200]
    completion_mode = body.get("completion_mode", "manual")
    target_type = body.get("target_type")
    try:
        raw_ids = body.get("target_user_ids")
        if raw_ids is None:
            raw_ids = [body["target_user_id"]] if body.get("target_user_id") else []
        target_user_ids = list(dict.fromkeys(int(item) for item in raw_ids))
    except (TypeError, ValueError): return web.json_response({"error": "target_user_ids"}, status=400)
    if not target_type and target_user_ids: target_type = "user"
    target_role = str(body.get("target_role") or "").strip() or None
    if (not date_from or not date_to or date_to < date_from
            or (date_to - date_from).days + 1 > CRM_MAX_RANGE_DAYS
            or not title or len(title) > 200 or len(description) > 10000
            or priority not in {"low", "normal", "high", "urgent"}
            or completion_mode not in {"manual", "shift_end"}):
        return web.json_response({"error": "fields"}, status=400)
    if body.get("requires_photo") and completion_mode == "shift_end":
        return web.json_response({"error": "completion_mode",
                                  "message": "Автозавершение несовместимо с обязательным фото."}, status=400)
    if (target_type == "user" and not target_user_ids) or (target_type == "role" and not target_role):
        return web.json_response({"error": "target"}, status=400)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row; await db.execute("BEGIN IMMEDIATE")
        if target_type == "user":
            recipients = []
            for uid in target_user_ids:
                recipients.extend(await _crm_target_users(
                    db, city["id"], "user", uid, None, _crm_scope_role(context)))
        else:
            recipients = await _crm_target_users(
                db, city["id"], target_type, None, target_role, _crm_scope_role(context))
        if not recipients:
            await db.rollback(); return web.json_response(
                {"error": "target", "message": "Получатели не найдены."}, status=400)
        if target_type == "user" and len({item["user_id"] for item in recipients}) != len(target_user_ids):
            await db.rollback(); return web.json_response(
                {"error": "target", "message": "Один или несколько сотрудников недоступны."}, status=400)
        now_iso = datetime.now(timezone.utc).isoformat(); admin_uid = context["telegram_user"]["id"]
        cur = await db.execute(
            "INSERT INTO crm_tasks (city_id,work_date,title,description,priority,status,created_by,"
            "created_at,updated_by,updated_at,requires_photo,date_from,date_to,district,completion_mode) "
            "VALUES (?,?,?,?,?,'draft',?,?,?,?,?,?,?,?,?)",
            (city["id"], date_from.isoformat(), title, description, priority,
             admin_uid, now_iso, admin_uid, now_iso, 1 if body.get("requires_photo") else 0,
             date_from.isoformat(), date_to.isoformat(), district, completion_mode),
        )
        task_id = cur.lastrowid
        if target_type == "user":
            await db.executemany(
                "INSERT INTO crm_task_targets (task_id,target_type,user_id,role) VALUES (?,'user',?,NULL)",
                [(task_id, uid) for uid in target_user_ids],
            )
        else:
            await db.execute(
                "INSERT INTO crm_task_targets (task_id,target_type,user_id,role) VALUES (?,'role',NULL,?)",
                (task_id, target_role),
            )
        task = dict(await (await db.execute("SELECT * FROM crm_tasks WHERE id=?", (task_id,))).fetchone())
        if body.get("publish") is True:
            task, _ = await _crm_publish_task(db, task_id, city["id"], admin_uid)
        await _crm_audit(db, context, "task.create", "task", task_id, city["id"], after=task)
        await db.commit()
        row = await (await db.execute("SELECT * FROM crm_tasks WHERE id=?", (task_id,))).fetchone()
        payload = (await _crm_task_payloads(db, [row], detailed=True))[0]
    return web.json_response({"ok": True, "task": payload}, status=201)


async def api_crm_task_publish(request):
    context, error = await _crm_admin(request, write=True)
    if error is not None: return error
    try: task_id = int(request.match_info["task_id"])
    except (KeyError, ValueError): return web.json_response({"error": "task_id"}, status=400)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row; await db.execute("BEGIN IMMEDIATE")
        row = await _crm_task_in_scope(db, task_id, context)
        if not row:
            denied = await _crm_task_role_denied(db, task_id, context)
            await db.rollback(); return web.json_response(
                {"error": "admin_role_scope" if denied else "not_found"}, status=403 if denied else 404)
        before = dict(row)
        try: task, changed = await _crm_publish_task(
            db, task_id, row["city_id"], context["telegram_user"]["id"]
        )
        except ValueError as exc:
            await db.rollback(); return web.json_response({"error": "publish", "message": str(exc)}, status=409)
        if changed: await _crm_audit(db, context, "task.publish", "task", task_id,
                                     row["city_id"], before=before, after=task)
        await db.commit()
        payload = (await _crm_task_payloads(db, [await (await db.execute(
            "SELECT * FROM crm_tasks WHERE id=?", (task_id,))).fetchone()], detailed=True))[0]
    return web.json_response({"ok": True, "task": payload, "already_published": not changed})


async def api_crm_task_update(request):
    context, error = await _crm_admin(request, write=True)
    if error is not None: return error
    body = await _request_json_object(request)
    if body is None: return web.json_response({"error": "json"}, status=400)
    try: task_id = int(request.match_info["task_id"])
    except (KeyError, ValueError): return web.json_response({"error": "task_id"}, status=400)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row; await db.execute("BEGIN IMMEDIATE")
        row = await _crm_task_in_scope(db, task_id, context)
        if not row:
            denied = await _crm_task_role_denied(db, task_id, context)
            await db.rollback(); return web.json_response(
                {"error": "admin_role_scope" if denied else "not_found"}, status=403 if denied else 404)
        merged = dict(row)
        for field in ("work_date", "date_from", "date_to", "district", "completion_mode",
                      "title", "description", "priority", "status", "requires_photo"):
            if field in body: merged[field] = body[field]
        date_from = _crm_date(merged.get("date_from") or merged["work_date"])
        date_to = _crm_date(merged.get("date_to") or merged.get("date_from") or merged["work_date"])
        if not date_from or not date_to or date_to < date_from \
                or (date_to - date_from).days + 1 > CRM_MAX_RANGE_DAYS \
                or not str(merged["title"]).strip() \
                or merged["priority"] not in {"low", "normal", "high", "urgent"} \
                or merged["status"] not in {"draft", "published", "cancelled"} \
                or merged.get("completion_mode", "manual") not in {"manual", "shift_end"}:
            await db.rollback(); return web.json_response({"error": "fields"}, status=400)
        if merged.get("requires_photo") and merged.get("completion_mode", "manual") == "shift_end":
            await db.rollback(); return web.json_response({"error": "completion_mode"}, status=400)
        if row["status"] == "published" and merged["status"] == "draft":
            await db.rollback(); return web.json_response({"error": "status"}, status=409)
        now_iso = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "UPDATE crm_tasks SET work_date=?,date_from=?,date_to=?,district=?,completion_mode=?,"
            "title=?,description=?,priority=?,status=?,requires_photo=?,updated_by=?,updated_at=? WHERE id=?",
            (date_from.isoformat(), date_from.isoformat(), date_to.isoformat(),
             str(merged.get("district") or "")[:200], merged.get("completion_mode", "manual"),
             str(merged["title"]).strip()[:200],
             str(merged["description"] or "")[:10000], merged["priority"], merged["status"],
             1 if merged.get("requires_photo") else 0,
             context["telegram_user"]["id"], now_iso, task_id),
        )
        updated = await (await db.execute("SELECT * FROM crm_tasks WHERE id=?", (task_id,))).fetchone()
        await _crm_audit(db, context, "task.update", "task", task_id, row["city_id"],
                         before=dict(row), after=dict(updated)); await db.commit()
    return web.json_response({"ok": True, "task": dict(updated)})


async def api_crm_task_assignee_status(request):
    context, error = await _crm_admin(request, write=True)
    if error is not None: return error
    body = await _request_json_object(request)
    if body is None: return web.json_response({"error": "json"}, status=400)
    try: task_id = int(request.match_info["task_id"]); user_id = int(request.match_info["user_id"])
    except (KeyError, ValueError): return web.json_response({"error": "id"}, status=400)
    status = body.get("status"); comment = str(body.get("comment") or "").strip()[:2000]
    if status not in {"accepted", "in_progress"}:
        return web.json_response({"error": "status"}, status=400)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row; await db.execute("BEGIN IMMEDIATE")
        task = await _crm_task_in_scope(db, task_id, context)
        assignee = await (await db.execute(
            "SELECT * FROM crm_task_assignees WHERE task_id=? AND user_id=?", (task_id, user_id)
        )).fetchone()
        if not task:
            denied = await _crm_task_role_denied(db, task_id, context)
            await db.rollback(); return web.json_response(
                {"error": "admin_role_scope" if denied else "not_found"}, status=403 if denied else 404)
        if not assignee or (_crm_scope_role(context) and
                (assignee["role_snap"] or "").casefold() != _crm_scope_role(context).casefold()):
            await db.rollback(); return web.json_response({"error": "not_found"}, status=404)
        if assignee["status"] != "submitted":
            await db.rollback(); return web.json_response(
                {"error": "review", "message": "Проверять можно только отправленный результат."}, status=409)
        if status == "in_progress" and not comment:
            await db.rollback(); return web.json_response(
                {"error": "return_reason", "message": "Укажите причину возврата в работу."}, status=400)
        now_iso = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "UPDATE crm_task_assignees SET status=?,status_comment=?,updated_at=? "
            "WHERE task_id=? AND user_id=?", (status, comment, now_iso, task_id, user_id),
        )
        if comment:
            await db.execute("INSERT INTO crm_task_comments (task_id,author_user_id,body,created_at) "
                             "VALUES (?,?,?,?)", (task_id, context["telegram_user"]["id"], comment, now_iso))
        updated = await (await db.execute(
            "SELECT * FROM crm_task_assignees WHERE task_id=? AND user_id=?", (task_id, user_id)
        )).fetchone()
        await _crm_task_event(db, task_id, context["telegram_user"]["id"],
                              "assignee.review", {"user_id": user_id, "status": status,
                                                  "comment": comment})
        await _crm_audit(db, context, "task.assignee_status", "task_assignee",
                         f"{task_id}:{user_id}", task["city_id"], before=dict(assignee),
                         after=dict(updated)); await db.commit()
    return web.json_response({"ok": True, "assignee": dict(updated)})


async def api_crm_task_admin_comment(request):
    context, error = await _crm_admin(request, write=True)
    if error is not None: return error
    body = await _request_json_object(request)
    if body is None: return web.json_response({"error": "json"}, status=400)
    try: task_id = int(request.match_info["task_id"])
    except (KeyError, ValueError): return web.json_response({"error": "task_id"}, status=400)
    text_body = str(body.get("body") or "").strip()
    if not text_body or len(text_body) > 4000: return web.json_response({"error": "body"}, status=400)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row; await db.execute("BEGIN IMMEDIATE")
        task = await _crm_task_in_scope(db, task_id, context)
        if not task:
            denied = await _crm_task_role_denied(db, task_id, context)
            await db.rollback(); return web.json_response(
                {"error": "admin_role_scope" if denied else "not_found"}, status=403 if denied else 404)
        now_iso = datetime.now(timezone.utc).isoformat()
        cur = await db.execute("INSERT INTO crm_task_comments (task_id,author_user_id,body,created_at) "
                               "VALUES (?,?,?,?)", (task_id, context["telegram_user"]["id"], text_body, now_iso))
        await _crm_task_event(db, task_id, context["telegram_user"]["id"],
                              "task.comment", {"comment_id": cur.lastrowid})
        await _crm_audit(db, context, "task.comment", "task_comment", cur.lastrowid,
                         task["city_id"], after={"task_id": task_id, "body": text_body}); await db.commit()
    return web.json_response({"ok": True, "comment_id": cur.lastrowid}, status=201)


async def api_employee_tasks_mine(request):
    tg_user = await _auth_user(request)
    if not tg_user: return web.json_response({"error": "auth"}, status=401)
    user = await get_user(tg_user["id"])
    city = get_city((user or {}).get("city_id"))
    if not city: return web.json_response({"error": "city"}, status=409)
    date_range, error = _crm_range(request, city, default_days=31)
    if error is not None: return error
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT t.*,a.status AS my_status,a.status_comment AS my_status_comment,"
            "a.updated_at AS my_updated_at FROM crm_tasks t JOIN crm_task_assignees a "
            "ON a.task_id=t.id WHERE a.user_id=? AND t.city_id=? AND t.status='published' "
            "AND COALESCE(t.date_to,t.work_date)>=? AND COALESCE(t.date_from,t.work_date)<=? "
            "ORDER BY COALESCE(t.date_from,t.work_date),t.id",
            (tg_user["id"], city["id"], date_range["from"], date_range["to"]),
        )).fetchall()
        payloads = await _crm_task_payloads(db, rows, viewer_user_id=tg_user["id"])
        for payload, row in zip(payloads, rows):
            payload["my_status"] = row["my_status"]
            payload["my_status_comment"] = row["my_status_comment"] or ""
            payload["my_updated_at"] = row["my_updated_at"]
    return web.json_response({"city": {"id": city["id"], "name": city["name"]},
                              "items": payloads})


async def api_employee_task_progress(request):
    tg_user = await _auth_user(request)
    if not tg_user: return web.json_response({"error": "auth"}, status=401)
    body = await _request_json_object(request)
    if body is None: return web.json_response({"error": "json"}, status=400)
    try: task_id = int(request.match_info["task_id"])
    except (KeyError, ValueError): return web.json_response({"error": "task_id"}, status=400)
    status = body.get("status"); comment = str(body.get("comment") or "").strip()[:2000]
    if status not in {"seen", "in_progress", "submitted", "blocked"}:
        return web.json_response({"error": "status"}, status=400)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row; await db.execute("BEGIN IMMEDIATE")
        row = await (await db.execute(
            "SELECT a.*,t.status AS task_status,t.requires_photo FROM crm_task_assignees a JOIN crm_tasks t "
            "ON t.id=a.task_id WHERE a.task_id=? AND a.user_id=?", (task_id, tg_user["id"])
        )).fetchone()
        if not row or row["task_status"] != "published":
            await db.rollback(); return web.json_response({"error": "not_found"}, status=404)
        allowed_transitions = {
            "assigned": {"seen", "in_progress"},
            "seen": {"in_progress"},
            "in_progress": {"submitted", "blocked"},
            "blocked": {"in_progress"},
            "submitted": set(),
            "accepted": set(),
        }
        if status not in allowed_transitions.get(row["status"], set()):
            await db.rollback(); return web.json_response(
                {"error": "invalid_transition", "from": row["status"], "to": status}, status=409
            )
        if status == "submitted" and row["requires_photo"]:
            photo = await (await db.execute(
                "SELECT 1 FROM crm_task_attachments WHERE task_id=? AND kind='result' "
                "AND assignee_user_id=? LIMIT 1", (task_id, tg_user["id"]),
            )).fetchone()
            if not photo:
                await db.rollback(); return web.json_response(
                    {"error": "result_photo_required",
                     "message": "Перед отправкой результата приложите фото."}, status=409
                )
        now_iso = datetime.now(timezone.utc).isoformat()
        await db.execute("UPDATE crm_task_assignees SET status=?,status_comment=?,updated_at=? "
                         "WHERE task_id=? AND user_id=?",
                         (status, comment, now_iso, task_id, tg_user["id"]))
        if comment:
            await db.execute("INSERT INTO crm_task_comments (task_id,author_user_id,body,created_at) "
                             "VALUES (?,?,?,?)", (task_id, tg_user["id"], comment, now_iso))
        await _crm_task_event(db, task_id, tg_user["id"], "assignee.progress",
                              {"status": status, "comment": comment})
        await db.commit()
    return web.json_response({"ok": True, "status": status, "updated_at": now_iso})


async def api_employee_task_comment(request):
    tg_user = await _auth_user(request)
    if not tg_user: return web.json_response({"error": "auth"}, status=401)
    body = await _request_json_object(request)
    if body is None: return web.json_response({"error": "json"}, status=400)
    try: task_id = int(request.match_info["task_id"])
    except (KeyError, ValueError): return web.json_response({"error": "task_id"}, status=400)
    text_body = str(body.get("body") or "").strip()
    if not text_body or len(text_body) > 4000: return web.json_response({"error": "body"}, status=400)
    async with aiosqlite.connect(DB_PATH) as db:
        assignee = await (await db.execute(
            "SELECT 1 FROM crm_task_assignees a JOIN crm_tasks t ON t.id=a.task_id "
            "WHERE a.task_id=? AND a.user_id=? AND t.status='published'",
            (task_id, tg_user["id"]),
        )).fetchone()
        if not assignee: return web.json_response({"error": "not_found"}, status=404)
        now_iso = datetime.now(timezone.utc).isoformat()
        cur = await db.execute("INSERT INTO crm_task_comments (task_id,author_user_id,body,created_at) "
                               "VALUES (?,?,?,?)", (task_id, tg_user["id"], text_body, now_iso))
        await _crm_task_event(db, task_id, tg_user["id"], "assignee.comment",
                              {"comment_id": cur.lastrowid})
        await db.commit()
    return web.json_response({"ok": True, "comment_id": cur.lastrowid}, status=201)


def _crm_image_type(data):
    if data.startswith(b"\xff\xd8\xff"): return "image/jpeg", ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"): return "image/png", ".png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None, None


async def _crm_task_event(db, task_id, actor_user_id, event_type, payload=None):
    await db.execute(
        "INSERT INTO crm_task_events (task_id,actor_user_id,event_type,payload_json,created_at) "
        "VALUES (?,?,?,?,?)",
        (task_id, actor_user_id, event_type,
         json.dumps(payload, ensure_ascii=False, default=str) if payload is not None else None,
         datetime.now(timezone.utc).isoformat()),
    )


def _crm_miniapp_link(task_id=None):
    username = (BOT_USERNAME or "").lstrip("@")
    base = f"https://t.me/{username}/{WEBAPP_SHORTNAME}" if username else WEBAPP_URL
    return f"{base}?startapp=task_{task_id}" if task_id is not None else base


def _crm_notification_text(kind, payload):
    details = str(payload.get("description") or payload.get("note") or "").strip()
    details = f"\n\n{details[:1200]}" if details else ""
    if kind == "task_assigned":
        dates = payload.get("date_from") or ""
        if payload.get("date_to") and payload.get("date_to") != dates:
            dates += f" — {payload['date_to']}"
        district = f"\nРайон: {payload['district']}" if payload.get("district") else ""
        return (f"📋 Новое задание: {payload.get('title') or 'Без названия'}\n"
                f"Срок: {dates}{district}{details}")
    if kind == "planned_shift":
        district = f"\nРайон: {payload['district']}" if payload.get("district") else ""
        return (f"🗓 Вам назначена смена\n{payload.get('work_date', '')} · "
                f"{payload.get('start_time', '')}–{payload.get('end_time', '')}{district}{details}")
    district = f"\nРайон: {payload['district']}" if payload.get("district") else ""
    return (f"🗓 Опубликован график смен\n{payload.get('date_from', '')} — "
            f"{payload.get('date_to', '')}\nВремя: {payload.get('start_time', '')}–"
            f"{payload.get('end_time', '')}\nСмен: {payload.get('created', 0)}{district}{details}")


async def deliver_crm_notifications_once(limit=50):
    now_iso = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM crm_notification_outbox WHERE status IN ('pending','retry') "
            "AND (next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY id LIMIT ?",
            (now_iso, int(limit)),
        )).fetchall()
    delivered = 0
    for row in rows:
        payload = json.loads(row["payload_json"] or "{}")
        text = _crm_notification_text(row["kind"], payload)
        task_id = payload.get("task_id") if row["kind"] == "task_assigned" else None
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Открыть Mini App", url=_crm_miniapp_link(task_id))
        ]])
        try:
            photo_sent = False
            if row["kind"] == "task_assigned" and payload.get("brief_attachment_id"):
                async with aiosqlite.connect(DB_PATH) as photo_db:
                    attachment = await (await photo_db.execute(
                        "SELECT storage_key FROM crm_task_attachments WHERE id=? AND kind='brief'",
                        (payload["brief_attachment_id"],),
                    )).fetchone()
                photo_path = os.path.join(CRM_UPLOAD_DIR, attachment[0]) if attachment else None
                if photo_path and os.path.isfile(photo_path):
                    try:
                        await bot.send_photo(row["user_id"], FSInputFile(photo_path),
                                             caption=text[:1024], reply_markup=keyboard)
                        photo_sent = True
                    except Exception as photo_error:
                        logger.warning("CRM outbox: фото не отправлено, использую текст: %s", photo_error)
            if not photo_sent:
                await bot.send_message(row["user_id"], text, reply_markup=keyboard)
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE crm_notification_outbox SET status='sent',attempt_count=attempt_count+1,"
                    "sent_at=?,last_error=NULL WHERE id=? AND status IN ('pending','retry')",
                    (datetime.now(timezone.utc).isoformat(), row["id"]),
                )
                await db.commit()
            delivered += 1
        except Exception as exc:
            attempts = int(row["attempt_count"] or 0) + 1
            status = "failed" if attempts >= 5 else "retry"
            delay = min(3600, 60 * (2 ** max(0, attempts - 1)))
            next_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE crm_notification_outbox SET status=?,attempt_count=?,next_attempt_at=?,"
                    "last_error=? WHERE id=? AND status IN ('pending','retry')",
                    (status, attempts, next_at, str(exc)[:1000], row["id"]),
                )
                await db.commit()
    return delivered


async def crm_notification_worker():
    while True:
        try:
            await deliver_crm_notifications_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка фоновой доставки CRM-уведомлений")
        await asyncio.sleep(15)


def _crm_normalized_district(value):
    return " ".join(str(value or "").casefold().split())


async def sync_closed_shift_tasks_once(limit=100):
    """Idempotently accepts shift_end tasks for newly closed shifts."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        shifts = await (await db.execute(
            "SELECT s.* FROM shifts s LEFT JOIN crm_shift_task_sync x ON x.shift_id=s.id "
            "WHERE s.is_active=0 AND x.shift_id IS NULL ORDER BY s.id DESC LIMIT ?", (int(limit),)
        )).fetchall()
    processed = 0
    for shift in shifts:
        city = get_city(shift["city_id"])
        start_at = _parse_datetime(shift["start_at"] or shift["created_at"])
        if not city or not start_at:
            shift_date = None
        else:
            shift_date = start_at.astimezone(_city_tz(city)).date().isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA busy_timeout=15000")
            await db.execute("BEGIN IMMEDIATE")
            already = await (await db.execute(
                "SELECT 1 FROM crm_shift_task_sync WHERE shift_id=?", (shift["id"],)
            )).fetchone()
            if already:
                await db.rollback(); continue
            matched = 0
            if shift_date:
                tasks = await (await db.execute(
                    "SELECT t.*,a.status AS assignee_status FROM crm_tasks t "
                    "JOIN crm_task_assignees a ON a.task_id=t.id "
                    "WHERE t.city_id=? AND t.status='published' AND t.completion_mode='shift_end' "
                    "AND a.user_id=? AND a.status!='accepted' "
                    "AND COALESCE(t.date_from,t.work_date)<=? AND COALESCE(t.date_to,t.work_date)>=?",
                    (shift["city_id"], shift["user_id"], shift_date, shift_date),
                )).fetchall()
                shift_district = _crm_normalized_district(shift["district"])
                now_iso = datetime.now(timezone.utc).isoformat()
                for task in tasks:
                    task_district = _crm_normalized_district(task["district"])
                    if task_district and task_district != shift_district:
                        continue
                    await db.execute(
                        "UPDATE crm_task_assignees SET status='accepted',status_comment=?,updated_at=? "
                        "WHERE task_id=? AND user_id=? AND status!='accepted'",
                        ("Автоматически по окончании смены", now_iso, task["id"], shift["user_id"]),
                    )
                    await _crm_task_event(db, task["id"], 0, "auto_completed.shift_end",
                                          {"shift_id": shift["id"], "user_id": shift["user_id"]})
                    matched += 1
            await db.execute(
                "INSERT INTO crm_shift_task_sync (shift_id,processed_at,matched_tasks) VALUES (?,?,?)",
                (shift["id"], datetime.now(timezone.utc).isoformat(), matched),
            )
            await db.commit()
        processed += 1
    return processed


async def crm_shift_task_sync_worker():
    while True:
        try:
            await sync_closed_shift_tasks_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка автозавершения CRM-заданий по сменам")
        await asyncio.sleep(30)


async def api_crm_task_upload(request):
    context, error = await _crm_admin(request, write=True)
    if error is not None: return error
    try: task_id = int(request.match_info["task_id"])
    except (KeyError, ValueError): return web.json_response({"error": "task_id"}, status=400)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        task = await _crm_task_in_scope(db, task_id, context)
        if not task:
            denied = await _crm_task_role_denied(db, task_id, context)
            return web.json_response({"error": "admin_role_scope" if denied else "not_found"},
                                     status=403 if denied else 404)
        existing = (await (await db.execute(
            "SELECT COUNT(*) FROM crm_task_attachments WHERE task_id=? AND kind='brief'", (task_id,)
        )).fetchone())[0]
    if existing >= CRM_UPLOAD_MAX_FILES:
        return web.json_response({"error": "attachment_limit"}, status=409)
    if not request.content_type.startswith("multipart/"):
        return web.json_response({"error": "multipart"}, status=400)
    os.makedirs(CRM_UPLOAD_DIR, exist_ok=True)
    reader = await request.multipart(); saved = []
    try:
        while True:
            field = await reader.next()
            if field is None: break
            if field.name != "file": continue
            if existing + len(saved) >= CRM_UPLOAD_MAX_FILES:
                raise ValueError(f"Можно прикрепить не больше {CRM_UPLOAD_MAX_FILES} фото.")
            data = bytearray()
            while True:
                chunk = await field.read_chunk(size=64 * 1024)
                if not chunk: break
                data.extend(chunk)
                if len(data) > CRM_UPLOAD_MAX_BYTES:
                    raise ValueError("Фото превышает лимит 10 МБ.")
            mime, extension = _crm_image_type(data)
            if not mime: raise ValueError("Разрешены только JPEG, PNG и WebP.")
            if shutil.disk_usage(CRM_UPLOAD_DIR).free < len(data) + 50 * 1024 * 1024:
                raise ValueError("Недостаточно места для нового фото.")
            storage_key = uuid.uuid4().hex + extension
            temp_path = os.path.join(CRM_UPLOAD_DIR, storage_key + ".tmp")
            final_path = os.path.join(CRM_UPLOAD_DIR, storage_key)
            with open(temp_path, "wb") as upload_file: upload_file.write(data)
            os.replace(temp_path, final_path)
            saved.append({"storage_key": storage_key,
                          "original_name": os.path.basename(field.filename or "photo"),
                          "mime_type": mime, "size_bytes": len(data),
                          "sha256": hashlib.sha256(data).hexdigest()})
        if not saved: return web.json_response({"error": "file"}, status=400)
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row; await db.execute("BEGIN IMMEDIATE")
            task = await _crm_task_in_scope(db, task_id, context)
            if not task: raise LookupError("Задание больше недоступно.")
            count = (await (await db.execute(
                "SELECT COUNT(*) FROM crm_task_attachments WHERE task_id=? AND kind='brief'", (task_id,)
            )).fetchone())[0]
            if count + len(saved) > CRM_UPLOAD_MAX_FILES: raise ValueError("Лимит фото уже изменился.")
            now_iso = datetime.now(timezone.utc).isoformat(); uploaded = []
            for item in saved:
                cur = await db.execute(
                    "INSERT INTO crm_task_attachments (task_id,storage_key,original_name,mime_type,"
                    "size_bytes,sha256,kind,assignee_user_id,uploaded_by,created_at) "
                    "VALUES (?,?,?,?,?,?,'brief',NULL,?,?)",
                    (task_id, item["storage_key"], item["original_name"], item["mime_type"],
                     item["size_bytes"], item["sha256"], context["telegram_user"]["id"], now_iso),
                )
                uploaded.append({**item, "id": cur.lastrowid, "kind": "brief",
                                 "assignee_user_id": None,
                                 "download_url": f"/api/crm/task-attachments/{cur.lastrowid}"})
            await _crm_audit(db, context, "task.attachments", "task", task_id,
                             task["city_id"], after={"attachments": uploaded}); await db.commit()
        return web.json_response({"ok": True, "items": uploaded}, status=201)
    except (ValueError, LookupError) as exc:
        for item in saved:
            try: os.remove(os.path.join(CRM_UPLOAD_DIR, item["storage_key"]))
            except OSError: pass
        return web.json_response({"error": "attachment", "message": str(exc)}, status=409)
    except Exception:
        for item in saved:
            try: os.remove(os.path.join(CRM_UPLOAD_DIR, item["storage_key"]))
            except OSError: pass
        raise


async def api_employee_task_upload(request):
    tg_user = await _auth_user(request)
    if not tg_user: return web.json_response({"error": "auth"}, status=401)
    try: task_id = int(request.match_info["task_id"])
    except (KeyError, ValueError): return web.json_response({"error": "task_id"}, status=400)
    user = await get_user(tg_user["id"])
    if not user or not get_city(user.get("city_id")):
        return web.json_response({"error": "city"}, status=409)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT t.city_id,t.status AS task_status,a.status AS assignee_status "
            "FROM crm_tasks t LEFT JOIN crm_task_assignees a ON a.task_id=t.id AND a.user_id=? "
            "WHERE t.id=?", (tg_user["id"], task_id),
        )).fetchone()
        if not row or row["assignee_status"] is None or row["city_id"] != user.get("city_id"):
            return web.json_response({"error": "not_assigned"}, status=403)
        if row["task_status"] != "published" or row["assignee_status"] == "accepted":
            return web.json_response({"error": "task_locked"}, status=409)
        existing = (await (await db.execute(
            "SELECT COUNT(*) FROM crm_task_attachments WHERE task_id=? AND kind='result' "
            "AND assignee_user_id=?", (task_id, tg_user["id"]),
        )).fetchone())[0]
    if existing >= CRM_UPLOAD_MAX_FILES:
        return web.json_response({"error": "attachment_limit"}, status=409)
    if not request.content_type.startswith("multipart/"):
        return web.json_response({"error": "multipart"}, status=400)
    os.makedirs(CRM_UPLOAD_DIR, exist_ok=True); reader = await request.multipart(); saved = []
    try:
        while True:
            field = await reader.next()
            if field is None: break
            if field.name != "file": continue
            if existing + len(saved) >= CRM_UPLOAD_MAX_FILES:
                raise ValueError(f"Можно прикрепить не больше {CRM_UPLOAD_MAX_FILES} фото результата.")
            data = bytearray()
            while True:
                chunk = await field.read_chunk(size=64 * 1024)
                if not chunk: break
                data.extend(chunk)
                if len(data) > CRM_UPLOAD_MAX_BYTES: raise ValueError("Фото превышает лимит 10 МБ.")
            mime, extension = _crm_image_type(data)
            if not mime: raise ValueError("Разрешены только JPEG, PNG и WebP.")
            if shutil.disk_usage(CRM_UPLOAD_DIR).free < len(data) + 50 * 1024 * 1024:
                raise ValueError("Недостаточно места для нового фото.")
            storage_key = uuid.uuid4().hex + extension
            temp_path = os.path.join(CRM_UPLOAD_DIR, storage_key + ".tmp")
            with open(temp_path, "wb") as upload_file: upload_file.write(data)
            os.replace(temp_path, os.path.join(CRM_UPLOAD_DIR, storage_key))
            saved.append({"storage_key": storage_key,
                          "original_name": os.path.basename(field.filename or "result-photo"),
                          "mime_type": mime, "size_bytes": len(data),
                          "sha256": hashlib.sha256(data).hexdigest()})
        if not saved: return web.json_response({"error": "file"}, status=400)
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row; await db.execute("BEGIN IMMEDIATE")
            row = await (await db.execute(
                "SELECT t.city_id,t.status AS task_status,a.status AS assignee_status "
                "FROM crm_tasks t JOIN crm_task_assignees a ON a.task_id=t.id "
                "WHERE t.id=? AND a.user_id=?", (task_id, tg_user["id"]),
            )).fetchone()
            if not row or row["city_id"] != user.get("city_id"):
                raise PermissionError("Задание больше не назначено сотруднику.")
            if row["task_status"] != "published" or row["assignee_status"] == "accepted":
                raise ValueError("Задание уже закрыто или отменено.")
            count = (await (await db.execute(
                "SELECT COUNT(*) FROM crm_task_attachments WHERE task_id=? AND kind='result' "
                "AND assignee_user_id=?", (task_id, tg_user["id"]),
            )).fetchone())[0]
            if count + len(saved) > CRM_UPLOAD_MAX_FILES: raise ValueError("Лимит фото уже изменился.")
            now_iso = datetime.now(timezone.utc).isoformat(); uploaded = []
            for item in saved:
                cur = await db.execute(
                    "INSERT INTO crm_task_attachments (task_id,storage_key,original_name,mime_type,"
                    "size_bytes,sha256,kind,assignee_user_id,uploaded_by,created_at) "
                    "VALUES (?,?,?,?,?,?,'result',?,?,?)",
                    (task_id, item["storage_key"], item["original_name"], item["mime_type"],
                     item["size_bytes"], item["sha256"], tg_user["id"], tg_user["id"], now_iso),
                )
                uploaded.append({**item, "id": cur.lastrowid, "kind": "result",
                                 "assignee_user_id": tg_user["id"], "created_at": now_iso,
                                 "download_url": f"/api/crm/task-attachments/{cur.lastrowid}"})
            await _crm_task_event(db, task_id, tg_user["id"], "result.attachments",
                                  {"attachments": uploaded}); await db.commit()
        return web.json_response({"ok": True, "items": uploaded}, status=201)
    except (ValueError, PermissionError) as exc:
        for item in saved:
            try: os.remove(os.path.join(CRM_UPLOAD_DIR, item["storage_key"]))
            except OSError: pass
        status = 403 if isinstance(exc, PermissionError) else 409
        return web.json_response({"error": "attachment", "message": str(exc)}, status=status)
    except Exception:
        for item in saved:
            try: os.remove(os.path.join(CRM_UPLOAD_DIR, item["storage_key"]))
            except OSError: pass
        raise


async def api_crm_attachment(request):
    tg_user = await _auth_user(request)
    if not tg_user: return web.json_response({"error": "auth"}, status=401)
    try: attachment_id = int(request.match_info["attachment_id"])
    except (KeyError, ValueError): return web.json_response({"error": "attachment_id"}, status=400)
    context = await _admin_context(request) if request.headers.get("X-Admin-Token") else None
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        row = await (await db.execute(
            "SELECT a.*,t.city_id,t.status AS task_status FROM crm_task_attachments a "
            "JOIN crm_tasks t ON t.id=a.task_id WHERE a.id=?", (attachment_id,)
        )).fetchone()
        allowed = False
        if row and context:
            allowed = bool(await _crm_task_in_scope(db, row["task_id"], context))
        elif row:
            assigned = await (await db.execute(
                "SELECT 1 FROM crm_task_assignees WHERE task_id=? AND user_id=?",
                (row["task_id"], tg_user["id"]),
            )).fetchone()
            if row["kind"] == "result":
                allowed = bool(assigned and row["assignee_user_id"] == tg_user["id"]
                               and row["task_status"] == "published")
            else:
                allowed = bool(assigned and row["task_status"] == "published")
    if not row or not allowed: return web.json_response({"error": "not_found"}, status=404)
    path = os.path.join(CRM_UPLOAD_DIR, row["storage_key"])
    if not os.path.isfile(path): return web.json_response({"error": "file_missing"}, status=404)
    response = web.FileResponse(path, headers={"Cache-Control": "private, max-age=300",
                                               "X-Content-Type-Options": "nosniff"})
    response.content_type = row["mime_type"]
    response.headers["Content-Disposition"] = f'inline; filename="attachment-{attachment_id}"'
    return response


async def cleanup_crm_uploads():
    """Удаляет только временные и не привязанные к БД старые файлы."""
    os.makedirs(CRM_UPLOAD_DIR, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        referenced = {row[0] for row in await (await db.execute(
            "SELECT storage_key FROM crm_task_attachments"
        )).fetchall()}
    now_ts = time.time(); removed = 0
    for name in os.listdir(CRM_UPLOAD_DIR):
        path = os.path.join(CRM_UPLOAD_DIR, name)
        if not os.path.isfile(path): continue
        try: age = now_ts - os.path.getmtime(path)
        except OSError: continue
        is_temp = name.endswith(".tmp")
        if (is_temp and age > 3600) or (not is_temp and name not in referenced and age > 86400):
            try: os.remove(path); removed += 1
            except OSError: pass
    if removed: logger.info("CRM uploads: удалено orphan/temp файлов: %s", removed)


async def api_crm_admins(request):
    context, error = await _crm_admin(request, network=True)
    if error is not None: return error
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM admin_accounts ORDER BY role,user_id"
        )).fetchall()
        items = []
        for row in rows:
            permissions = await (await db.execute(
                "SELECT city_id FROM admin_city_permissions WHERE user_id=? ORDER BY city_id",
                (row["user_id"],),
            )).fetchall()
            item = dict(row); item["city_ids"] = [value[0] for value in permissions]
            items.append(item)
    return web.json_response({"items": items})


async def api_crm_admin_upsert(request):
    context, error = await _crm_admin(request, write=True, network=True)
    if error is not None: return error
    body = await _request_json_object(request)
    if body is None: return web.json_response({"error": "json"}, status=400)
    try: user_id = int(body.get("user_id")); city_ids = [int(value) for value in body.get("city_ids", [])]
    except (TypeError, ValueError): return web.json_response({"error": "fields"}, status=400)
    role = body.get("role"); role_scope = str(body.get("role_scope") or "").strip() or None
    is_active = 1 if body.get("is_active", True) else 0
    if role not in {"city_viewer", "city_manager", "network_admin"} or user_id <= 0:
        return web.json_response({"error": "role"}, status=400)
    if role != "network_admin" and (not city_ids or any(city_id not in CITIES_BY_ID for city_id in city_ids)):
        return web.json_response({"error": "city_ids"}, status=400)
    if role == "network_admin": role_scope = None; city_ids = []
    if role_scope and any(
        role_scope.casefold() not in {item.casefold() for item in city_supported_roles(city_id)}
        for city_id in city_ids
    ):
        return web.json_response({"error": "role_scope"}, status=400)
    now_iso = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row; await db.execute("BEGIN IMMEDIATE")
        before_row = await (await db.execute("SELECT * FROM admin_accounts WHERE user_id=?", (user_id,))).fetchone()
        before = dict(before_row) if before_row else None
        await db.execute(
            "INSERT INTO admin_accounts (user_id,role,role_scope,is_active,session_version,created_at,updated_at) "
            "VALUES (?,?,?,?,1,?,?) ON CONFLICT(user_id) DO UPDATE SET role=excluded.role,"
            "role_scope=excluded.role_scope,is_active=excluded.is_active,"
            "session_version=admin_accounts.session_version+1,updated_at=excluded.updated_at",
            (user_id, role, role_scope, is_active, now_iso, now_iso),
        )
        await db.execute("DELETE FROM admin_city_permissions WHERE user_id=?", (user_id,))
        await db.executemany(
            "INSERT INTO admin_city_permissions (user_id,city_id) VALUES (?,?)",
            [(user_id, city_id) for city_id in sorted(set(city_ids))],
        )
        after_row = await (await db.execute("SELECT * FROM admin_accounts WHERE user_id=?", (user_id,))).fetchone()
        after = dict(after_row); after["city_ids"] = sorted(set(city_ids))
        await _crm_audit(db, context, "admin.upsert", "admin_account", user_id, None,
                         before=before, after=after); await db.commit()
    return web.json_response({"ok": True, "admin": after})


async def api_shift_comment(request):
    """Сотрудник редактирует комментарий к своей смене (в т.ч. закрытой)."""
    tg_user = await _auth_user(request)
    if not tg_user:
        return web.json_response({"error": "auth"}, status=401)
    uid = tg_user["id"]
    body = await _request_json_object(request)
    if body is None:
        return web.json_response({"error": "json", "message": "Ожидается JSON-объект."}, status=400)
    sid = body.get("shift_id")
    if not isinstance(sid, int):
        return web.json_response({"error": "shift_id"}, status=400)
    comment = (body.get("comment") or "").strip()
    if len(comment) > 500:
        return web.json_response(
            {"error": "comment", "message": "Комментарий до 500 символов."}, status=400)
    shift = await get_shift_by_id(sid)
    if not shift or shift.get("user_id") != uid:
        return web.json_response({"error": "not_found", "message": "Смена не найдена."}, status=404)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE shifts SET comment = ? WHERE id = ?", (comment, sid))
        await db.commit()
    # Обновляем сообщение-отчёт в теме (комментарий видно в закрытом отчёте).
    await safe_flush_report_update(sid)
    return web.json_response({"ok": True, "comment": comment})


# ============================================================================
# [13-HTTP] РАЗДАЧА MINI APP, HEALTH-CHECK И HTTP-МАРШРУТЫ
# ============================================================================

async def serve_index(request):
    # Отдаём саму страницу мини-приложения с того же адреса, что и API —
    # тогда не нужен ни GitHub Pages, ни CORS.
    if os.path.exists(INDEX_PATH):
        # Без этих заголовков Telegram и браузер держат старую копию страницы,
        # и обновления мини-приложения не доезжают до сотрудников.
        return web.FileResponse(INDEX_PATH, headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        })
    return web.Response(text="BibiBike API ok")


async def serve_crm(request):
    if os.path.exists(CRM_INDEX_PATH):
        return web.FileResponse(CRM_INDEX_PATH, headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache", "Expires": "0",
        })
    return web.Response(text="BibiBike CRM frontend is not installed", status=404)

async def api_health(request):
    return web.json_response({
        "ok": True,
        "service": "bibibike-bot",
        "build_version": BUILD_VERSION,
        "index_html": os.path.exists(INDEX_PATH),
        "crm_html": os.path.exists(CRM_INDEX_PATH),
    })

async def start_api_server():
    try:
        app = web.Application(
            middlewares=[cors_mw],
            client_max_size=CRM_UPLOAD_MAX_FILES * CRM_UPLOAD_MAX_BYTES + 1024 * 1024,
        )
        app.router.add_get("/api/state", api_state)
        app.router.add_post("/api/settings", api_settings)
        app.router.add_post("/api/shift/start", api_shift_start)
        app.router.add_post("/api/shift/stop", api_shift_stop)
        app.router.add_post("/api/shift/lunch", api_shift_lunch)
        app.router.add_post("/api/shift/delete", api_shift_delete)
        app.router.add_post("/api/shift/comment", api_shift_comment)
        app.router.add_post("/api/action/add", api_action_add)
        app.router.add_get("/api/history", api_history)
        app.router.add_post("/api/admin/login", api_admin_login)
        app.router.add_get("/api/admin/dashboard", api_admin_dashboard)
        app.router.add_get("/api/admin/history", api_admin_history)
        app.router.add_post("/api/admin/force-close", api_admin_force_close)
        app.router.add_post("/api/admin/period/new", api_admin_period_new)
        app.router.add_post("/api/admin/manual/approve", api_admin_manual_approve)
        # CRM: маршруты с фиксированными суффиксами регистрируются раньше
        # динамических /{task_id}, чтобы aiohttp не принял имя за ID.
        app.router.add_get("/api/admin/crm/context", api_crm_context)
        app.router.add_get("/api/admin/crm/overview", api_crm_overview)
        app.router.add_get("/api/admin/crm/employees", api_crm_employees)
        app.router.add_get("/api/admin/crm/employees/{user_id}", api_crm_employee)
        app.router.add_get("/api/admin/crm/shifts", api_crm_shifts)
        app.router.add_post("/api/admin/crm/shifts/{shift_id}/close", api_crm_shift_close)
        app.router.add_get("/api/admin/crm/trends", api_crm_trends)
        app.router.add_get("/api/admin/crm/activity", api_crm_activity)
        app.router.add_get("/api/admin/crm/operational-signals", api_crm_operational_signals)
        app.router.add_get("/api/admin/crm/data-quality", api_crm_data_quality)
        app.router.add_get("/api/admin/crm/calendar", api_crm_calendar)
        app.router.add_post("/api/admin/crm/planned-shifts/batch", api_crm_planned_shifts_batch)
        app.router.add_post("/api/admin/crm/planned-shifts", api_crm_planned_shift_create)
        app.router.add_patch("/api/admin/crm/planned-shifts/{plan_id}", api_crm_planned_shift_update)
        app.router.add_get("/api/admin/crm/tasks/assignee-preview", api_crm_task_assignee_preview)
        app.router.add_get("/api/admin/crm/tasks", api_crm_tasks)
        app.router.add_post("/api/admin/crm/tasks", api_crm_task_create)
        app.router.add_get("/api/admin/crm/tasks/{task_id}", api_crm_task_detail)
        app.router.add_patch("/api/admin/crm/tasks/{task_id}", api_crm_task_update)
        app.router.add_post("/api/admin/crm/tasks/{task_id}/publish", api_crm_task_publish)
        app.router.add_post("/api/admin/crm/tasks/{task_id}/comments", api_crm_task_admin_comment)
        app.router.add_post("/api/admin/crm/tasks/{task_id}/attachments", api_crm_task_upload)
        app.router.add_post(
            "/api/admin/crm/tasks/{task_id}/assignees/{user_id}/status",
            api_crm_task_assignee_status,
        )
        app.router.add_get("/api/admin/crm/admins", api_crm_admins)
        app.router.add_post("/api/admin/crm/admins", api_crm_admin_upsert)
        app.router.add_get("/api/crm/tasks/mine", api_employee_tasks_mine)
        app.router.add_post("/api/crm/tasks/{task_id}/attachments", api_employee_task_upload)
        app.router.add_post("/api/crm/tasks/{task_id}/progress", api_employee_task_progress)
        app.router.add_post("/api/crm/tasks/{task_id}/comments", api_employee_task_comment)
        app.router.add_get("/api/crm/task-attachments/{attachment_id}", api_crm_attachment)
        app.router.add_get("/health", api_health)
        app.router.add_get("/crm.html", serve_crm)
        app.router.add_get("/index.html", serve_index)
        app.router.add_get("/", serve_index)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", WEBAPP_PORT)
        await site.start()
        logger.info(f"API мини-приложения слушает 0.0.0.0:{WEBAPP_PORT}")
    except Exception as e:
        # Веб-сервер не критичен для работы бота: если порт занят/закрыт —
        # просто пишем предупреждение, а бот продолжает работать как раньше.
        logger.warning(f"API мини-приложения не запустился ({e}). Бот работает без него.")

# ============================================================================
# [14-STARTUP] ЗАПУСК API, ФОНОВЫХ ЗАДАЧ И TELEGRAM POLLING
# ============================================================================
async def main():
    await init_db()
    await cleanup_crm_uploads()
    await rebuild_monthly_aggregates()
    await start_api_server()   # === НОВОЕ: поднимаем API рядом с ботом ===
    kpi_task = asyncio.create_task(kpi_background_worker())
    scheduled_report_task = asyncio.create_task(scheduled_report_status_worker())
    auto_close_task = asyncio.create_task(auto_close_worker())
    crm_notification_task = asyncio.create_task(crm_notification_worker())
    crm_shift_sync_task = asyncio.create_task(crm_shift_task_sync_worker())
    dp = Dispatcher()
    dp.include_router(cmd_router)
    dp.include_router(work_router)

    logger.info("=" * 50)
    logger.info("BibiBike Bot запущен! (живое сообщение + NPB + роль Чарджер)")
    logger.info(f"Версия сборки: {BUILD_VERSION}")
    for city in CITIES_BY_ID.values():
        logger.info(
            f"Город {city['name']}: группа {city['group_id']}, "
            f"задачи {city['topic_tasks']}, NPB {city['topic_npb']}, "
            f"перемещения {city.get('topic_moves') or '—'}, отчёты {city['topic_reports']}"
        )
        for role_city in city_role_groups(city["id"]).values():
            logger.info(
                "  Роль %s: группа %s, рабочая тема %s, перемещения %s, отчёты %s",
                role_city.get("role_group") or "—",
                role_city["group_id"],
                role_city["topic_tasks"],
                role_city.get("topic_moves") or "—",
                role_city["topic_reports"],
            )
    logger.info("=" * 50)

    try:
        await dp.start_polling(bot)
    finally:
        kpi_task.cancel()
        scheduled_report_task.cancel()
        auto_close_task.cancel()
        crm_notification_task.cancel()
        crm_shift_sync_task.cancel()
        try:
            await asyncio.gather(kpi_task, scheduled_report_task, auto_close_task,
                                 crm_notification_task, crm_shift_sync_task)
        except asyncio.CancelledError:
            pass

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        import traceback
        print(f"ФАТАЛЬНАЯ ОШИБКА при запуске бота: {e}", flush=True)
        traceback.print_exc()
        raise
