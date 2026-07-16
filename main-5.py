# -*- coding: utf-8 -*-
# ============================================================
# BibiBike Bot — ФИНАЛЬНАЯ ВЕРСИЯ (обновление поверх рабочей).
#
# СОХРАНЕНО ИЗ ОРИГИНАЛА (логика не менялась):
#   - схема БД и файл bibibike_work.db (старые данные продолжают работать)
#   - парсер parse_message / get_action_type (весь словарь глаголов)
#   - /setname, /status, /help, /fix с ручным переопределением цифр
#   - автоудаление команд и служебных ответов (auto_delete)
#   - пересчёт при редактировании сообщений
#   - work-роутер слушает ВСЕ темы, кроме ОТЧЕТОВ
#
# ДОБАВЛЕНО (помечено "# === НОВОЕ ==="):
#   1. ЖИВОЕ СООБЩЕНИЕ: одна смена = одно сообщение в ОТЧЕТАХ,
#      бот сам редактирует его по мере действий (дебаунс 20 сек),
#      при закрытии дописывает конец смены и отработанные часы.
#   2. Тема NPB: голые 4-значные номера = замена АКБ ("Поменял АКБ").
#   3. Район при открытии смены — ЛЮБОЙ текст (или пусто).
#      Чарджер может писать зону и порог: /20:55 весь город, загрузил 35
#   4. /fix удаляет старое сообщение отчёта и присылает новое
#      (+ необязательное 6-е число — АКБ; без него АКБ сохраняется).
#   5. Роль "Чарджер" ⚡ в /setname (в дополнение к Скауту и Водителю).
#   6. /topicid — узнать ID темы для настройки конфига.
#
# ФИЛОСОФИЯ:
#   - Бот реагирует ТОЛЬКО на сообщения со слешем при управлении сменой.
#     Кто не хочет пользоваться — пишет как раньше, бот не мешает.
#   - Роль — это подпись в отчёте, а не ограничение: считается любое
#     действие любому сотруднику; чего не делал — той графы просто нет.
#
# Формат /fix: /fix перем поправ рем в_СЦ из_СЦ [акб] Комментарий
#   ВАЖНО: если комментарий начинается с числа, оно посчитается как АКБ —
#   в таком случае укажи АКБ явно шестым числом.
# ============================================================

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
import aiosqlite
from datetime import datetime, timezone, timedelta
from urllib.parse import parse_qsl
from aiohttp import web
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import BaseFilter
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from aiogram.exceptions import TelegramBadRequest

# Необязательно: подхватываем .env, если он есть (на BotHost переменные и так заданы).
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ============================================================
# КОНФИГУРАЦИЯ
# ============================================================
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
# ГОРОДА-ЗАГЛУШКИ: Ставрополь, Красная Поляна, Химки
# ============================================================
# Такие же разделы, как у Краснодара выше. Пока стоят ЗАГЛУШКИ —
# когда получишь доступ к группе города, впиши реальные ID группы
# и тем (узнать: команда /topicid в нужной теме), и город заработает
# полностью: бот начнёт слушать и писать в его группе.
#
# Пока стоят заглушки: города видны в приложении (выбор города,
# регистрация, открытие смены), но отчёт в Telegram-группу города
# не постится — группы с таким ID не существует, ошибка уходит в лог,
# смена при этом сохраняется нормально (safe_flush_report_update).
#
# ВАЖНО: заглушки group_id обязаны быть РАЗНЫМИ у разных городов —
# в базе на group_id стоит UNIQUE. Не копируй одно значение в два города.

# --- Ставрополь (ЗАГЛУШКИ — заменить на реальные ID) ---
STAVROPOL_GROUP_ID      = -1000000000002   # ID группы «Логистика Ставрополь»
STAVROPOL_TOPIC_TASKS   = 1                # ID темы «Тех. Задания»
STAVROPOL_TOPIC_NPB     = 2                # ID темы «NPB»
STAVROPOL_TOPIC_REPORTS = 3                # ID темы «ОТЧЕТЫ»

# --- Красная Поляна (ЗАГЛУШКИ — заменить на реальные ID) ---
POLYANA_GROUP_ID        = -1000000000003   # ID группы «Логистика Красная Поляна»
POLYANA_TOPIC_TASKS     = 1                # ID темы «Тех. Задания»
POLYANA_TOPIC_NPB       = 2                # ID темы «NPB»
POLYANA_TOPIC_REPORTS   = 3                # ID темы «ОТЧЕТЫ»

# --- Химки (ЗАГЛУШКИ — заменить на реальные ID) ---
KHIMKI_GROUP_ID         = -1000000000004   # ID группы «Логистика Химки»
KHIMKI_TOPIC_TASKS      = 1                # ID темы «Тех. Задания»
KHIMKI_TOPIC_NPB        = 2                # ID темы «NPB»
KHIMKI_TOPIC_REPORTS    = 3                # ID темы «ОТЧЕТЫ»

# === НОВОЕ: живое сообщение обновляется не чаще, чем раз в N секунд ===
DEBOUNCE_SEC = 20

# ============================================================
# === НОВОЕ: МИНИ-ПРИЛОЖЕНИЕ (ЗАРПЛАТА) =====================
# ============================================================
# Порт нашего веб-сервера задаётся отдельной переменной WEB_PORT.
# Дефолт 3000 сохранён из рабочей версии; на хостинге WEB_PORT должен совпадать
# с портом, выделенным для Mini App.
# Этот же порт нужно указать в поле «Порт веб-приложения» при создании бота.
WEBAPP_PORT = int(os.getenv("WEB_PORT", "3000"))

# Имя бота (без @) и short-name Mini App из BotFather (/newapp) —
# нужны, чтобы под отчётом появилась кнопка «Моя зарплата».
# Юзернейм основного бота — для кнопок открытия приложения в группе.
BOT_USERNAME = os.getenv("BOT_USERNAME", "bbbotdelaetbot")
WEBAPP_SHORTNAME = os.getenv("WEBAPP_SHORTNAME", "zp")

# === НОВОЕ: прямой https-адрес страницы приложения (бот сам её отдаёт на BotHost).
# Нужен для web_app-кнопки, которая открывает Mini App в один тап прямо из отчёта.
# Если пусто — кнопка откатится на старую url-ссылку t.me/бот/shortname.
# Задавать ТОЛЬКО через переменную окружения, дефолт — публичный адрес бота. ===
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://bot-1783532208-8771-voglogpro.bothost.tech/")

# Домен, с которого открывается сама страница мини-приложения (GitHub Pages).
# Нужен для CORS, чтобы браузер разрешил запросы к API бота.
WEBAPP_ALLOW_ORIGIN = os.getenv("WEBAPP_ALLOW_ORIGIN", "https://voglogpro.github.io")

# === НОВОЕ: бот сам отдаёт страницу мини-приложения (index.html рядом с этим файлом) ===
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_PATH = os.path.join(BASE_DIR, "index.html")

# Краснодар = московское время (UTC+3)
MSK = timezone(timedelta(hours=3))

# Модель оплаты по умолчанию для новых сотрудников
DEFAULT_PAY_TYPE = "hourly"       # hourly | salary | piece
DEFAULT_PAY_AMOUNT = 350.0        # ₽/час, ₽/смену или ₽/замену — зависит от типа

# Пароль админки не хранится в репозитории. Если ADMIN_PASSWORD пуст, админка
# отключена. После проверки сервер выдаёт подписанную сессию на несколько часов.
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "123")
ADMIN_SESSION_TTL_SEC = int(os.getenv("ADMIN_SESSION_TTL_SEC", str(8 * 60 * 60)))
INIT_DATA_MAX_AGE_SEC = int(os.getenv("INIT_DATA_MAX_AGE_SEC", str(24 * 60 * 60)))
CITY_MEMBERSHIP_TTL_SEC = int(os.getenv("CITY_MEMBERSHIP_TTL_SEC", "300"))
# При первом успешном входе каждый администратор закрепляется за текущим
# городом в admin_city_access. Обычные настройки профиля эту связь не меняют.

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

# ============================================================
# БАЗА ДАННЫХ
# ============================================================
DB_PATH = os.path.join(os.getenv("DATA_DIR", BASE_DIR), "bibibike_work.db")
# База лежит в постоянной папке (на BotHost это /app/data), поэтому смены,
# зарплаты и история НЕ обнуляются при обновлении бота из GitHub.

CITIES_BY_ID = {}
CITIES_BY_GROUP = {}


class ActiveShiftExists(Exception):
    """У сотрудника уже есть активная смена в одном из городов."""


def _city_tz(city):
    """Часовой пояс города как фиксированный UTC offset (для городов РФ)."""
    try:
        offset = int((city or {}).get("timezone_offset", 3))
    except (TypeError, ValueError):
        offset = 3
    return timezone(timedelta(hours=max(-12, min(14, offset))))


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
        # Ставрополь — работает на заглушках, впиши реальные ID выше
        "key": "stavropol",
        "name": "Ставрополь",
        "group_id": STAVROPOL_GROUP_ID,
        "topic_tasks": STAVROPOL_TOPIC_TASKS,
        "topic_npb": STAVROPOL_TOPIC_NPB,
        "topic_reports": STAVROPOL_TOPIC_REPORTS,
        "timezone_offset": 3,
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
        # Химки — работает на заглушках, впиши реальные ID выше
        "key": "khimki",
        "name": "Химки",
        "group_id": KHIMKI_GROUP_ID,
        "topic_tasks": KHIMKI_TOPIC_TASKS,
        "topic_npb": KHIMKI_TOPIC_NPB,
        "topic_reports": KHIMKI_TOPIC_REPORTS,
        "timezone_offset": 3,
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
            city = dict(item)
            key = str(city.get("key") or "").strip().lower()
            required = ("name", "group_id", "topic_tasks", "topic_npb", "topic_reports")
            if not key or any(city.get(field) is None for field in required):
                logger.warning("Пропущена неполная запись города в CITIES_CONFIG_JSON")
                continue
            city["key"] = key
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
        CITIES_BY_ID.clear()
        CITIES_BY_GROUP.clear()
        for row in rows:
            city = dict(row)
            CITIES_BY_ID[city["id"]] = city
            CITIES_BY_GROUP[city["group_id"]] = city
    finally:
        if own_connection:
            await db.close()


def get_city(city_id):
    return CITIES_BY_ID.get(city_id)


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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                city_key TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                group_id INTEGER NOT NULL UNIQUE,
                topic_tasks INTEGER NOT NULL,
                topic_npb INTEGER NOT NULL,
                topic_reports INTEGER NOT NULL,
                timezone_offset INTEGER NOT NULL DEFAULT 3,
                is_active INTEGER NOT NULL DEFAULT 1,
                managed_by_config INTEGER NOT NULL DEFAULT 0
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
                source_message_id INTEGER
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
                user_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                shift_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                last_event_version REAL,
                PRIMARY KEY(city_id, user_id, message_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admin_city_access (
                user_id INTEGER PRIMARY KEY,
                city_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await db.commit()

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
            "ALTER TABLE actions ADD COLUMN city_id INTEGER",
            "ALTER TABLE cities ADD COLUMN managed_by_config INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE manual_reports ADD COLUMN sender_name TEXT",
            "ALTER TABLE manual_reports ADD COLUMN pay_type_snap TEXT",
            "ALTER TABLE manual_reports ADD COLUMN pay_amount_snap REAL",
            "ALTER TABLE work_message_links ADD COLUMN last_event_version REAL",
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
            await db.execute(
                "INSERT INTO cities (city_key, name, group_id, topic_tasks, topic_npb, "
                "topic_reports, timezone_offset, is_active, managed_by_config) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1) "
                "ON CONFLICT(city_key) DO UPDATE SET name=excluded.name, "
                "group_id=excluded.group_id, topic_tasks=excluded.topic_tasks, "
                "topic_npb=excluded.topic_npb, topic_reports=excluded.topic_reports, "
                "timezone_offset=excluded.timezone_offset, is_active=1, managed_by_config=1",
                (city["key"], city["name"], int(city["group_id"]),
                 int(city["topic_tasks"]), int(city["topic_npb"]),
                 int(city["topic_reports"]), int(city.get("timezone_offset", 3)))
            )
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
        duplicate_active = await (await db.execute(
            "SELECT user_id, MAX(id) AS keep_id, COUNT(*) AS amount FROM shifts "
            "WHERE is_active = 1 GROUP BY user_id HAVING COUNT(*) > 1"
        )).fetchall()
        for uid, keep_id, amount in duplicate_active:
            await db.execute(
                "UPDATE shifts SET is_active = 0, end_time = COALESCE(end_time, start_time), "
                "end_at = COALESCE(end_at, start_at), earned = 0 "
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
    """Привязывает время старта к сегодняшней дате.

    Любое время на часах позже текущего остаётся будущим сегодня — это
    основной контракт отложенного старта. Время на часах раньше текущего
    относится к сегодня, пока разница не превышает 12 часов; больший разрыв
    считаем безопасным переходом через полночь на завтра.
    """
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
                      source_message_id=None, now=None):
    city = get_city(city_id)
    if not city:
        raise ValueError("Неизвестный город")
    start_at = _resolve_start_at(time, city, now)
    async with aiosqlite.connect(DB_PATH) as db:
        # BEGIN IMMEDIATE сериализует два почти одновременных старта.
        # Частичный UNIQUE-индекс в БД дополнительно защищает инвариант.
        await db.execute("BEGIN IMMEDIATE")
        active = await (await db.execute(
            "SELECT id FROM shifts WHERE user_id = ? AND is_active = 1 LIMIT 1", (uid,)
        )).fetchone()
        if active:
            await db.rollback()
            raise ActiveShiftExists()
        # === НОВОЕ: сохраняем дату старта (для истории/зарплаты) ===
        now_iso = (now.astimezone(_city_tz(city)) if now else datetime.now(_city_tz(city))).isoformat()
        c = await db.execute(
            "INSERT INTO shifts (user_id, full_name, role, start_time, district, is_active, "
            "created_at, city_id, start_at, source, source_message_id) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)",
            (uid, name, role, time, district, now_iso, city_id, start_at.isoformat(),
             source, source_message_id)
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

async def end_shift(uid, time, comment="", city_id=None, now=None):
    shift = await get_active_shift(uid, city_id)
    if not shift:
        return None
    city = get_city(shift.get("city_id")) or get_default_city()
    scheduled = _shift_is_scheduled(shift, now)
    end_at = _resolve_end_at(shift, time, city, now)
    stored_end_time = shift.get("start_time") if scheduled else time
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE shifts SET is_active = 0, end_time = ?, end_at = ?, comment = ? "
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


async def get_action_shift_ids(uid, mid, city_id):
    async with aiosqlite.connect(DB_PATH) as db:
        rows = await (await db.execute(
            "SELECT DISTINCT shift_id FROM actions WHERE user_id = ? AND message_id = ? "
            "AND city_id = ? ORDER BY shift_id",
            (uid, mid, city_id)
        )).fetchall()
        return [row[0] for row in rows]


async def replace_message_actions(uid, mid, city_id, shift_id, actions, event_version):
    """Атомарно заменяет результат разбора сообщения; последняя правка побеждает."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("BEGIN IMMEDIATE")
        version_row = await (await db.execute(
            "SELECT shift_id, last_event_version FROM work_message_links "
            "WHERE city_id = ? AND user_id = ? AND message_id = ?",
            (city_id, uid, mid)
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
            "AND city_id = ? ORDER BY shift_id",
            (uid, mid, city_id)
        )).fetchall()
        await db.execute(
            "DELETE FROM actions WHERE user_id = ? AND message_id = ? AND city_id = ?",
            (uid, mid, city_id)
        )
        for action in actions:
            await db.execute(
                "INSERT INTO actions (user_id, shift_id, message_id, action_type, bike_codes, "
                "quantity, city_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (uid, shift_id, mid, action["action_type"],
                 ",".join(action.get("bike_codes") or []), action.get("quantity", 0), city_id)
            )
        await db.execute(
            "UPDATE work_message_links SET last_event_version = ? "
            "WHERE city_id = ? AND user_id = ? AND message_id = ?",
            (event_version, city_id, uid, mid)
        )
        await db.commit()
        return [row[0] for row in rows], True


async def get_work_message_shift(uid, mid, city_id):
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute(
            "SELECT shift_id FROM work_message_links "
            "WHERE city_id = ? AND user_id = ? AND message_id = ?",
            (city_id, uid, mid)
        )).fetchone()
        return row[0] if row else None


async def link_work_message(uid, mid, city_id, shift_id, created_at=None):
    created_at = created_at or datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO work_message_links (city_id, user_id, message_id, shift_id, created_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(city_id, user_id, message_id) DO NOTHING",
            (city_id, uid, mid, shift_id, created_at)
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
        # === НОВОЕ: добавлен счётчик 'battery' (замены АКБ из темы NPB) ===
        s = {'move': 0, 'fix': 0, 'repair': 0, 'to_sc': 0, 'from_sc': 0, 'battery': 0}
        for r in rows:
            atype = r['action_type']
            if atype in s:
                codes = r['bike_codes']
                if codes:
                    s[atype] += len(codes.split(','))
                if r['quantity']:
                    s[atype] += r['quantity']
        return s

# ============================================================
# ПАРСИНГ ТЕКСТА О ТЕКУЩЕЙ РАБОТЕ
# ============================================================
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


def _normalise_work_text(text):
    text = str(text or "").lower().replace("cц", "сц").replace("сc", "сц")
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
    text = text.lower().strip()
    all_codes = re.findall(r'\b(\d{4})\b', text)
    lines = text.split('\n')

    repair_codes = []
    for line in lines:
        if any(kw in line for kw in ['ремонт', 'поломк', 'сломан']):
            repair_codes.extend(re.findall(r'\b(\d{4})\b', line))

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

    for kw in code_actions:
        if kw['action_type'] == 'repair':
            codes = repair_codes.copy() if repair_codes else []
        else:
            codes = all_codes.copy() if all_codes else []
        results.append({'action_type': kw['action_type'], 'bike_codes': codes, 'quantity': 0})

    return results


def parse_message(text):
    """Сначала выполняет эталонный GitHub-парсер, затем только дополняет его.

    Старые распознанные сообщения всегда проходят через исходную функцию.
    Расширенный разбор включается лишь когда GitHub-парсер не нашёл ни одного
    действия. Если старый парсер что-то распознал, его результат возвращается
    байт-в-байт без перераспределения кодов, количества или порядка.
    """
    if not isinstance(text, str):
        return []
    legacy = _parse_message_github(text)
    if legacy:
        return legacy
    additions = _parse_message_extensions(text)
    if not additions and '\n' in text:
        additions = _parse_message_extensions(re.sub(r"\s*\n+\s*", " ", text))
    return additions

# === НОВОЕ: парсер темы NPB — голые 4-значные номера = замены АКБ ===
def parse_npb_message(text):
    """Эталонная логика NPB из текущей версии GitHub без изменений."""
    codes = re.findall(r'\b(\d{4})\b', text)
    if not codes:
        return []
    return [{'action_type': 'battery', 'bike_codes': codes, 'quantity': 0}]


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


_MANUAL_SHIFT_START_RE = re.compile(
    r"^(?:я\s+)?(?:начал|начала|начали|открыл|открыла|открыли)\s+смену$"
)
_MANUAL_SHIFT_END_RE = re.compile(
    r"^(?:я\s+)?(?:закончил|закончила|закончили|завершил|завершила|завершили|"
    r"закрыл|закрыла|закрыли)\s+смену$"
)


def _manual_shift_signal(text):
    """Распознаёт только самостоятельную короткую фразу о смене.

    Полный отчёт с временем сюда намеренно не попадает и продолжает
    обрабатываться штатным парсером ручных отчётов.
    """
    normalised = re.sub(r"\s+", " ", str(text or "").lower().replace("ё", "е")).strip()
    normalised = re.sub(r"[\s.!?,;:…✅☑✔️👍]+$", "", normalised).strip()
    if _MANUAL_SHIFT_START_RE.fullmatch(normalised):
        return "start"
    if _MANUAL_SHIFT_END_RE.fullmatch(normalised):
        return "end"
    return None


def _message_time_in_city(message, city):
    value = getattr(message, "date", None) or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(_city_tz(city))


async def _start_manual_signal_shift(message, city, event_time):
    """Идемпотентно создаёт смену из фразы «начала смену»."""
    uid = message.from_user.id
    message_id = message.message_id
    full_name = message.from_user.full_name or f"Сотрудник #{uid}"
    user = await get_user(uid) or {}
    role = user.get("role") or ""
    start_time = event_time.strftime("%H:%M")
    start_at = _resolve_start_at(start_time, city, event_time)
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
            "created_at, city_id, start_at, source, source_message_id) "
            "VALUES (?, ?, ?, ?, '', 1, ?, ?, ?, 'manual_signal', ?)",
            (uid, full_name, role, start_time, event_time.isoformat(), city["id"],
             start_at.isoformat(), message_id),
        )
        await db.commit()
        return cursor.lastrowid


async def handle_manual_shift_signal(message, city):
    """Молча открывает/закрывает ручную смену; бот-смены не изменяет."""
    if not message.from_user or getattr(message.from_user, "is_bot", False):
        return False
    text = (message.text or message.caption or "").strip()
    signal = _manual_shift_signal(text)
    if not signal:
        return False
    event_time = _message_time_in_city(message, city)
    if signal == "start":
        await _start_manual_signal_shift(message, city, event_time)
        return True

    active = await get_active_shift(message.from_user.id)
    if (active and active.get("city_id") == city["id"]
            and active.get("source") == "manual_signal"):
        await end_shift(
            message.from_user.id,
            event_time.strftime("%H:%M"),
            city_id=city["id"],
            now=event_time,
        )
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
    message_time = message.date.astimezone(tz) if message.date else datetime.now(tz)
    event_source = getattr(message, "edit_date", None) or message.date
    event_time = event_source.astimezone(tz) if event_source else datetime.now(tz)
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
            "s.role AS shift_role "
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
                "start_at=?, end_at=?, created_at=?, is_active=0, city_id=?, source=?, "
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

# ============================================================
# === НОВОЕ: ЖИВОЕ СООБЩЕНИЕ СМЕНЫ =========================
# ============================================================
_pending_updates = {}   # shift_id -> asyncio.Task (дебаунс)

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
    report += f"Начал: {html.escape(shift['start_time'])}"
    if waiting:
        report += " (ожидает начала)"
    report += "\n"

    closed = not shift.get('is_active') and shift.get('end_time')
    if closed:
        report += f"Закончил: {html.escape(shift['end_time'])}\n"
        report += f"Отработано: {_duration_shift(shift)}\n"

    if shift.get('district'):
        report += f"Район: {html.escape(shift['district'].upper())}\n"

    report += "\nСтатистика за смену:\n"

    has_any = False
    if stats['move'] > 0:
        report += f"Перемещено: {stats['move']}\n"; has_any = True
    if stats['fix'] > 0:
        report += f"Поправлено: {stats['fix']}\n"; has_any = True
    if stats['repair'] > 0:
        report += f"Ремонт: {stats['repair']}\n"; has_any = True
    if stats['battery'] > 0:
        report += f"Поменял АКБ: {stats['battery']}\n"; has_any = True
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
    """Отредактировать живое сообщение смены (или пересоздать при /fix)."""
    shift = await get_shift_by_id(shift_id)
    if not shift:
        return
    city = get_city(shift.get("city_id"))
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
        city["group_id"], text, message_thread_id=city["topic_reports"],
        parse_mode="HTML", reply_markup=markup
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

# ============================================================
# ОБРАБОТКА РАБОЧЕГО СООБЩЕНИЯ  (оригинальная + триггер живого отчёта)
# ============================================================
class CityTopicFilter(BaseFilter):
    def __init__(self, topic_kind):
        self.topic_kind = topic_kind

    async def __call__(self, message: Message):
        city = get_city_by_group(message.chat.id)
        if not city:
            return False
        thread_id = message.message_thread_id
        if self.topic_kind == "reports":
            matches = thread_id == city["topic_reports"]
        else:
            # Сохраняем рабочий контрак бота: слушать все темы группы
            # города, кроме «ОТЧЁТОВ». NPB всё ещё определяется отдельно.
            matches = thread_id != city["topic_reports"]
        return {"city": city} if matches else False


async def process_work_message(message: Message, city, npb=False, edited=False):
    text = message.text or message.caption or ""
    if not message.from_user or getattr(message.from_user, "is_bot", False):
        return

    uid = message.from_user.id
    linked_shift_id = await get_work_message_shift(uid, message.message_id, city["id"])
    existing_shift_ids = await get_action_shift_ids(uid, message.message_id, city["id"])
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
                if message_date.tzinfo is None:
                    message_date = message_date.replace(tzinfo=timezone.utc)
                message_date = message_date.astimezone(_city_tz(city))
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
        return

    message_date = getattr(message, "date", None)
    event_date = getattr(message, "edit_date", None) if edited else message_date
    event_date = event_date or datetime.now(timezone.utc)
    if event_date.tzinfo is None:
        event_date = event_date.replace(tzinfo=timezone.utc)
    event_version = event_date.timestamp()
    if edited or existing_shift_ids:
        await link_work_message(
            uid, message.message_id, city["id"], shift["id"],
            message_date.isoformat() if message_date else None,
        )

    if not text or text.startswith('/') or re.match(r'^\d{1,2}:\d{2}\s*', text):
        # Фото/стикер без подписи может получить корректную подпись уже после
        # закрытия смены, поэтому пустое рабочее сообщение тоже привязываем.
        if not text:
            await link_work_message(
                uid, message.message_id, city["id"], shift["id"],
                message_date.isoformat() if message_date else None,
            )
        removed_shift_ids, applied = await replace_message_actions(
            uid, message.message_id, city["id"], shift["id"], [], event_version
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
        message_date.isoformat() if message_date else None,
    )

    # === НОВОЕ: в теме NPB считаем голые номера как замены АКБ ===
    actions = parse_npb_message(text) if npb else parse_message(text)
    logger.info(f"Распаршено (msg={message.message_id}, npb={npb}): {actions}")

    removed_shift_ids, applied = await replace_message_actions(
        uid, message.message_id, city["id"], shift["id"], actions, event_version
    )
    if not applied:
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

# ============================================================
# ЧАТ 1 (и остальные темы, кроме ОТЧЕТОВ) — НОВЫЕ СООБЩЕНИЯ
# ============================================================
@work_router.message(CityTopicFilter("work"))
async def work_chat(message: Message, city):
    # === НОВОЕ: /topicid — узнать ID темы (для настройки конфига) ===
    if (message.text or "") == "/topicid":
        msg = await message.answer(
            f"chat_id: {message.chat.id}\nmessage_thread_id: {message.message_thread_id}"
        )
        asyncio.create_task(auto_delete(msg))
        return

    # === НОВОЕ: /app — закрепить кнопку приложения в этой теме ===
    if (message.text or "").strip() == "/app":
        await post_app_button(message)
        return

    # === НОВОЕ: тема NPB обрабатывается своим парсером ===
    npb = (message.message_thread_id == city["topic_npb"])
    await process_work_message(message, city, npb=npb)

# ============================================================
# РЕДАКТИРОВАННЫЕ РАБОЧИЕ СООБЩЕНИЯ  (оригинал + NPB)
# ============================================================
@work_router.edited_message(CityTopicFilter("work"))
async def work_chat_edit(message: Message, city):
    logger.info(f"СООБЩЕНИЕ ОТРЕДАКТИРОВАНО: {message.message_id}")
    npb = (message.message_thread_id == city["topic_npb"])
    await process_work_message(message, city, npb=npb, edited=True)

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
            "Исправить последний отчёт (5 цифр: перем. поправ. рем. в_СЦ из_СЦ, "
            "6-я необязательная — АКБ):\n"
            "/fix 11 5 1 2 0 Комментарий\n"
            "/fix 11 5 1 2 0 40 Комментарий\n\n"
            "Статус: /status\n"
            "ID темы: /topicid"
        )
        asyncio.create_task(auto_delete(msg))
        return

    # === НОВОЕ: /topicid и в теме отчётов ===
    if text == "/topicid":
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

    # /fix [move] [fix] [repair] [to_sc] [from_sc] [battery] Комментарий
    if text.startswith("/fix"):
        try:
            await message.delete()
        except:
            pass

        shift = await get_active_shift(user_id, city["id"])
        if shift:
            msg = await message.answer("У вас активная смена. Завершите её сначала.")
            asyncio.create_task(auto_delete(msg))
            return

        last_shift = await get_last_shift(user_id, city["id"])
        if not last_shift:
            msg = await message.answer("Нет завершённых смен.")
            asyncio.create_task(auto_delete(msg))
            return

        parts = text.split(maxsplit=1)
        args = parts[1].split() if len(parts) > 1 else []

        try:
            new_move = int(args[0]) if len(args) > 0 else 0
            new_fix = int(args[1]) if len(args) > 1 else 0
            new_repair = int(args[2]) if len(args) > 2 else 0
            new_to_sc = int(args[3]) if len(args) > 3 else 0
            new_from_sc = int(args[4]) if len(args) > 4 else 0
        except ValueError:
            msg = await message.answer("Ошибка: первые 5 аргументов должны быть числами.\nПример: /fix 11 5 1 2 0 Комментарий")
            asyncio.create_task(auto_delete(msg))
            return

        # === НОВОЕ: необязательное 6-е число — АКБ; без него АКБ сохраняется как было ===
        old_stats = await get_stats(last_shift['id'])
        if len(args) > 5 and args[5].isdigit():
            new_battery = int(args[5])
            new_comment = " ".join(args[6:]) if len(args) > 6 else last_shift.get('comment', '')
        else:
            new_battery = old_stats['battery']
            new_comment = " ".join(args[5:]) if len(args) > 5 else last_shift.get('comment', '')

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM actions WHERE shift_id = ?", (last_shift['id'],))

            if new_move > 0:
                await db.execute(
                    "INSERT INTO actions (user_id, shift_id, message_id, action_type, bike_codes, quantity, city_id) VALUES (?, ?, 0, 'move', '', ?, ?)",
                    (user_id, last_shift['id'], new_move, city["id"])
                )
            if new_fix > 0:
                await db.execute(
                    "INSERT INTO actions (user_id, shift_id, message_id, action_type, bike_codes, quantity, city_id) VALUES (?, ?, 0, 'fix', '', ?, ?)",
                    (user_id, last_shift['id'], new_fix, city["id"])
                )
            if new_repair > 0:
                await db.execute(
                    "INSERT INTO actions (user_id, shift_id, message_id, action_type, bike_codes, quantity, city_id) VALUES (?, ?, 0, 'repair', '', ?, ?)",
                    (user_id, last_shift['id'], new_repair, city["id"])
                )
            if new_to_sc > 0:
                await db.execute(
                    "INSERT INTO actions (user_id, shift_id, message_id, action_type, bike_codes, quantity, city_id) VALUES (?, ?, 0, 'to_sc', '', ?, ?)",
                    (user_id, last_shift['id'], new_to_sc, city["id"])
                )
            if new_from_sc > 0:
                await db.execute(
                    "INSERT INTO actions (user_id, shift_id, message_id, action_type, bike_codes, quantity, city_id) VALUES (?, ?, 0, 'from_sc', '', ?, ?)",
                    (user_id, last_shift['id'], new_from_sc, city["id"])
                )
            # === НОВОЕ: перезаписываем АКБ ===
            if new_battery > 0:
                await db.execute(
                    "INSERT INTO actions (user_id, shift_id, message_id, action_type, bike_codes, quantity, city_id) VALUES (?, ?, 0, 'battery', '', ?, ?)",
                    (user_id, last_shift['id'], new_battery, city["id"])
                )

            await db.execute("UPDATE shifts SET comment = ? WHERE id = ?", (new_comment, last_shift['id']))
            await db.commit()

        # === НОВОЕ: /fix удаляет старое сообщение отчёта и присылает новое ===
        await freeze_earned(last_shift['id'])
        await safe_flush_report_update(last_shift['id'], force_new=True)
        logger.info(f"Отчёт полностью пересчитан: {full_name}")
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
        await capture_manual_report(message, city)

# ============================================================
# === НОВОЕ: HTTP API ДЛЯ МИНИ-ПРИЛОЖЕНИЯ ===================
# ============================================================
MAX_LEVEL = 100

# === НОВОЕ: сколько XP даёт одно действие каждого типа.
# Раньше любое действие = 1 XP. Теперь вес зависит от сложности:
#   перемещение и работа с СЦ — самое частое/трудозатратное = 10,
#   ремонт и замена АКБ = 5, поправка (самое лёгкое) = 3.
# Неизвестный тип (на всякий случай) = 1. ===
XP_WEIGHTS = {
    "move": 10,
    "to_sc": 10,
    "from_sc": 10,
    "repair": 5,
    "battery": 5,
    "fix": 3,
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
    # Нелинейная прогрессия: на уровень L нужно 60 + 12·L + 0.35·L² опыта.
    # Уровень 1→2: ~72 XP, 10→11: ~215, 50→51: ~1535, 99→100: ~4680.
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


async def _is_city_member(uid, city_id):
    """Не даёт открыть смену в чужой закрытой группе через Mini App."""
    city = get_city(city_id)
    if not city:
        return False
    now_ts = datetime.now(timezone.utc).timestamp()
    cached = _city_membership_cache.get((uid, city_id))
    if cached and cached[1] > now_ts:
        return cached[0]
    allowed = False
    try:
        member = await bot.get_chat_member(city["group_id"], uid)
        status = getattr(member.status, "value", str(member.status)).lower().split(".")[-1]
        if status == "restricted":
            allowed = bool(getattr(member, "is_member", False))
        else:
            allowed = status in {"creator", "administrator", "member"}
    except Exception as exc:
        logger.warning(
            f"Не удалось проверить участие uid={uid} в группе города {city_id}: {exc}"
        )
    ttl = max(30, CITY_MEMBERSHIP_TTL_SEC if allowed else min(60, CITY_MEMBERSHIP_TTL_SEC))
    _city_membership_cache[(uid, city_id)] = (allowed, now_ts + ttl)
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
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp

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

    # === НОВОЕ: доход за текущий месяц (с 1 числа, по замороженным суммам) ===
    month_prefix = datetime.now(_city_tz(city)).strftime("%Y-%m")
    async with aiosqlite.connect(DB_PATH) as db:
        c = await db.execute(
            "SELECT COALESCE(SUM(earned), 0) FROM shifts "
            "WHERE user_id = ? AND city_id = ? AND is_active = 0 AND start_at LIKE ?",
            (uid, selected_city_id, month_prefix + "%")
        )
        month_earned = (await c.fetchone())[0]

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
    })

async def api_settings(request):
    tg_user = await _auth_user(request)
    if not tg_user:
        return web.json_response({"error": "auth"}, status=401)
    uid = tg_user["id"]

    body = await _request_json_object(request)
    if body is None:
        return web.json_response({"error": "json", "message": "Ожидается JSON-объект."}, status=400)

    city_id = body.get("city_id")
    if city_id is not None:
        if not isinstance(city_id, int) or not get_city(city_id):
            return web.json_response({"error": "city_id", "message": "Неизвестный город."}, status=400)
        if not await _is_city_member(uid, city_id):
            return web.json_response(
                {"error": "city_membership", "message": "Вы не состоите в рабочей группе этого города."},
                status=403)
        await set_user_city(uid, city_id)
    else:
        current_user = await get_user(uid)
        city_id = (current_user or {}).get("city_id") or (get_default_city() or {}).get("id")

    # === НОВОЕ: оплату сохраняем ТОЛЬКО если она реально передана.
    # Экран регистрации шлёт лишь имя+роль — тогда pay не трогаем, и у нового
    # сотрудника остаются DEFAULT'ы колонок (hourly / 350), а не обнуление. ===
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
        await set_user_pay(uid, pay_type, pay_amount)

    # === НОВОЕ: тумблер «Режим редактирования» — сохраняем, если передан ===
    if "edit_mode" in body:
        await set_user_edit_mode(uid, bool(body.get("edit_mode")))

    # Имя и роль — необязательно (можно зарегистрироваться прямо в приложении)
    name = (body.get("name") or "").strip()
    role = (body.get("role") or "").strip().lower()
    role_map = {"скаут": "Скаут", "водитель": "Водитель", "чарджер": "Чарджер"}
    if name and role in role_map:
        await add_user(uid, name, role_map[role], city_id)

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
    if not await _is_city_member(uid, city_id):
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

    try:
        sid = await start_shift(
            uid, user["full_name"], user.get("role") or "", time_str, district, city_id
        )
    except ActiveShiftExists:
        return web.json_response(
            {"error": "already_active", "message": "Смена уже открыта."}, status=400)
    await set_user_city(uid, city_id)
    report_ok = await safe_flush_report_update(sid)
    logger.info(f"Смена начата (из приложения): {user['full_name']}, {time_str}, {district or '—'}")
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

# === НОВОЕ: изменить любой из 6 счётчиков на ±1 из приложения (режим редактирования) ===
# Разрешённые типы действий, которые можно править из приложения.
EDITABLE_ACTIONS = ("move", "fix", "repair", "battery", "to_sc", "from_sc")

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
    city = get_city(shift.get("city_id"))
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
    items = []
    for s in rows:
        worked = _duration_shift(s) if s.get("end_time") else "—"
        city = get_city(s.get("city_id")) or {}
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
        })
    return web.json_response({"items": items})


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


def _admin_key():
    return hashlib.sha256((BOT_TOKEN + "\0" + ADMIN_PASSWORD).encode()).digest()


def _issue_admin_token(uid):
    expires = int(datetime.now(timezone.utc).timestamp()) + ADMIN_SESSION_TTL_SEC
    payload = json.dumps({"uid": uid, "exp": expires}, separators=(",", ":")).encode()
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
        return payload.get("uid") == uid and int(payload.get("exp", 0)) >= int(
            datetime.now(timezone.utc).timestamp()
        )
    except Exception:
        return False


_admin_login_failures = {}


async def _admin_user(request):
    tg_user = await _auth_user(request)
    if not tg_user:
        return None
    uid = tg_user["id"]
    if not _verify_admin_token(request.headers.get("X-Admin-Token", ""), uid):
        return None
    return tg_user


async def _admin_city(uid, bind_if_missing=False):
    """Город администратора хранится отдельно и не меняется через настройки."""
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute(
            "SELECT city_id FROM admin_city_access WHERE user_id = ?", (uid,)
        )).fetchone()
        if row:
            return get_city(row[0])
        if not bind_if_missing:
            return None
        user = await (await db.execute(
            "SELECT city_id FROM users WHERE user_id = ?", (uid,)
        )).fetchone()
        city = get_city(user[0] if user else None)
        if not city:
            return None
        await db.execute(
            "INSERT OR IGNORE INTO admin_city_access (user_id, city_id, created_at) "
            "VALUES (?, ?, ?)",
            (uid, city["id"], datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
        bound = await (await db.execute(
            "SELECT city_id FROM admin_city_access WHERE user_id = ?", (uid,)
        )).fetchone()
        return get_city(bound[0] if bound else None)


async def _admin_context(request):
    """Возвращает администратора и его серверно закреплённый город.

    Город никогда не берётся из параметров запроса: так подмена city_id в
    браузере не открывает данные другого филиала.
    """
    tg_user = await _admin_user(request)
    if not tg_user:
        return None
    user = await get_user(tg_user["id"])
    city = await _admin_city(tg_user["id"])
    return {"telegram_user": tg_user, "user": user or {}, "city": city}


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
    city = await _admin_city(uid, bind_if_missing=True)
    if not city:
        return web.json_response(
            {
                "error": "admin_city",
                "message": "Сначала зарегистрируйтесь и выберите свой город в настройках.",
            },
            status=409,
        )
    _admin_login_failures.pop(uid, None)
    token, expires = _issue_admin_token(uid)
    return web.json_response({
        "ok": True,
        "token": token,
        "expires_at": expires,
        "city": {"id": city["id"], "name": city["name"]},
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
                "is_active=0, created_at=?, start_at=?, end_at=?, earned=?, "
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
    return shift_id


async def api_admin_manual_approve(request):
    context = await _admin_context(request)
    if not context:
        return web.json_response(
            {"error": "admin_auth", "message": "Нужен вход в админку."}, status=401
        )
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
    actions = max(0, sum(_action_units(row) for row in action_rows))
    battery_count = max(0, sum(
        _action_units(row) for row in action_rows if row["action_type"] == "battery"
    ))
    worked = _shift_worked_min(shift, now)
    if shift.get("is_active"):
        status = "scheduled" if _shift_is_scheduled(shift, now) else "active"
        user = await (await db.execute(
            "SELECT pay_type, pay_amount FROM users WHERE user_id = ?",
            (shift["user_id"],),
        )).fetchone()
        pay_type = (user["pay_type"] if user else None) or DEFAULT_PAY_TYPE
        amount = user["pay_amount"] if user else None
        amount = DEFAULT_PAY_AMOUNT if amount is None else amount
        earned = 0 if status == "scheduled" else compute_earned(
            pay_type, amount, worked, battery_count
        )
    else:
        status = "closed"
        earned = shift.get("earned") or 0
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
        "worked_minutes": worked,
        "actions": actions,
        "efficiency": round(actions * 60 / worked, 2) if worked else None,
        "earned": earned,
    }


async def api_admin_dashboard(request):
    context = await _admin_context(request)
    if not context:
        return web.json_response(
            {"error": "admin_auth", "message": "Нужен вход в админку."}, status=401
        )
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
        pending = await (await db.execute(
            "SELECT id, message_id, user_id, sender_name, raw_text, parse_error, "
            "created_at, updated_at FROM manual_reports "
            "WHERE city_id = ? AND status = 'needs_review' ORDER BY id DESC LIMIT 50", (city_id,)
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
    return web.json_response({
        "city": {"id": city["id"], "name": city["name"]},
        "generated_at": now.isoformat(),
        "kpi_updated_at": latest[0] if latest else None,
        "open": open_items,
        "closed_today": closed_today_items,
        # Оставлено для совместимости со старой версией Mini App.
        "today": today_items,
        "employees": employee_items,
        "month": [{
            "user_id": row["user_id"], "name": row["full_name"], "role": row["role"],
            "shifts": row["shifts_count"], "worked_minutes": row["worked_minutes"],
            "actions": row["actions_count"], "earned": row["earned"]
        } for row in monthly],
        "kpi": [{
            "user_id": row["user_id"], "name": row["full_name"], "role": row["role"],
            "snapshot_hour": row["snapshot_hour"], "actions": row["actions_count"],
            "worked_minutes": row["worked_minutes"], "efficiency": row["efficiency"]
        } for row in kpi_rows],
        "manual_needs_review": [dict(row) for row in pending],
    })


async def api_admin_history(request):
    context = await _admin_context(request)
    if not context:
        return web.json_response(
            {"error": "admin_auth", "message": "Нужен вход в админку."}, status=401
        )
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
        rows = await (await db.execute(
            "SELECT * FROM shifts WHERE user_id = ? AND city_id = ? AND is_active = 0 "
            "ORDER BY COALESCE(start_at, created_at) DESC, id DESC LIMIT ? OFFSET ?",
            (user_id, city_id, limit + 1, offset),
        )).fetchall()
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
                   "to_sc": 0, "from_sc": 0}
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
            "earned": shift.get("earned") or 0,
            "pay_type": shift.get("pay_type_snap") or DEFAULT_PAY_TYPE,
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

async def serve_index(request):
    # Отдаём саму страницу мини-приложения с того же адреса, что и API —
    # тогда не нужен ни GitHub Pages, ни CORS.
    if os.path.exists(INDEX_PATH):
        return web.FileResponse(INDEX_PATH)
    return web.Response(text="BibiBike API ok")

async def start_api_server():
    try:
        app = web.Application(middlewares=[cors_mw])
        app.router.add_get("/api/state", api_state)
        app.router.add_post("/api/settings", api_settings)
        app.router.add_post("/api/shift/start", api_shift_start)
        app.router.add_post("/api/shift/stop", api_shift_stop)
        app.router.add_post("/api/shift/delete", api_shift_delete)
        app.router.add_post("/api/action/add", api_action_add)
        app.router.add_get("/api/history", api_history)
        app.router.add_post("/api/admin/login", api_admin_login)
        app.router.add_get("/api/admin/dashboard", api_admin_dashboard)
        app.router.add_get("/api/admin/history", api_admin_history)
        app.router.add_post("/api/admin/manual/approve", api_admin_manual_approve)
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

# ============================================================
# ЗАПУСК БОТА
# ============================================================
async def main():
    await init_db()
    await rebuild_monthly_aggregates()
    await start_api_server()   # === НОВОЕ: поднимаем API рядом с ботом ===
    kpi_task = asyncio.create_task(kpi_background_worker())
    scheduled_report_task = asyncio.create_task(scheduled_report_status_worker())
    dp = Dispatcher()
    dp.include_router(cmd_router)
    dp.include_router(work_router)

    logger.info("=" * 50)
    logger.info("BibiBike Bot запущен! (живое сообщение + NPB + роль Чарджер)")
    for city in CITIES_BY_ID.values():
        logger.info(
            f"Город {city['name']}: группа {city['group_id']}, "
            f"задачи {city['topic_tasks']}, NPB {city['topic_npb']}, отчёты {city['topic_reports']}"
        )
    logger.info("=" * 50)

    try:
        await dp.start_polling(bot)
    finally:
        kpi_task.cancel()
        scheduled_report_task.cancel()
        try:
            await asyncio.gather(kpi_task, scheduled_report_task)
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
