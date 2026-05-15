import asyncio
import logging
import re
import aiosqlite
import pytz
from datetime import datetime
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import Command
from aiohttp_socks import ProxyConnector
from aiogram.client.session.aiohttp import AiohttpSession

# ============================================================
# КОНФИГУРАЦИЯ (ЗАМЕНИ BOT_TOKEN НА РЕАЛЬНЫЙ)
# ============================================================
PROXY_URL = "socks5://DT5Rdn:ff2A2C@195.216.134.245:8000"
BOT_TOKEN = "8674884867:AAG_PMl3U8IMc7MQD3Vn26PBMLpEB18_wD8"  # ← ЗАМЕНИ НА СВОЙ ТОКЕН
GROUP_ID = -1003818447487
CHAT1_THREAD_ID = 1   # Рабочий чат (сотрудники пишут действия)
CHAT2_THREAD_ID = 2   # Отчеты (бот пишет сводки и кнопки)

DISTRICTS = ["Красная", "ФМР", "ЮМР", "Восточка", "Ставрополька", "ГМР"]

# Временная сетка (шаг 30 минут, с 6:00 до 23:30)
TIME_GRID = []
for h in range(6, 24):
    TIME_GRID.append(f"{h:02d}:00")
    TIME_GRID.append(f"{h:02d}:30")

# ============================================================
# ЛОГИРОВАНИЕ
# ============================================================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================
# БАЗА ДАННЫХ
# ============================================================
DB_PATH = "bibibike_work.db"

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL,
                role TEXT NOT NULL,
                registered_at TEXT DEFAULT (datetime('now', '+3 hours'))
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS shifts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                full_name TEXT NOT NULL,
                role TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT,
                district TEXT,
                is_active INTEGER DEFAULT 1,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                shift_id INTEGER NOT NULL,
                action_type TEXT NOT NULL,
                bike_codes TEXT,
                created_at TEXT DEFAULT (datetime('now', '+3 hours')),
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (shift_id) REFERENCES shifts(id)
            )
        """)
        await db.commit()
    logger.info("База данных инициализирована")

async def add_user(user_id: int, full_name: str, role: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO users (user_id, full_name, role) VALUES (?, ?, ?)",
            (user_id, full_name, role)
        )
        await db.commit()

async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

async def start_shift(user_id: int, full_name: str, role: str, start_time: str, district: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE shifts SET is_active = 0, end_time = datetime('now', '+3 hours') WHERE user_id = ? AND is_active = 1",
            (user_id,)
        )
        cursor = await db.execute(
            "INSERT INTO shifts (user_id, full_name, role, start_time, district, is_active) VALUES (?, ?, ?, ?, ?, 1)",
            (user_id, full_name, role, start_time, district)
        )
        await db.commit()
        return cursor.lastrowid

async def end_shift(user_id: int, end_time: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE shifts SET is_active = 0, end_time = ? WHERE user_id = ? AND is_active = 1",
            (end_time, user_id)
        )
        await db.commit()
        cursor = await db.execute(
            "SELECT id FROM shifts WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None

async def get_active_shift(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM shifts WHERE user_id = ? AND is_active = 1",
            (user_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

async def add_action(user_id: int, shift_id: int, action_type: str, bike_codes: list = None):
    codes_str = ",".join(bike_codes) if bike_codes else ""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO actions (user_id, shift_id, action_type, bike_codes) VALUES (?, ?, ?, ?)",
            (user_id, shift_id, action_type, codes_str)
        )
        await db.commit()

async def get_shift_stats(shift_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT action_type, COUNT(*) as count FROM actions WHERE shift_id = ? GROUP BY action_type",
            (shift_id,)
        )
        rows = await cursor.fetchall()
        stats = {'move': 0, 'fix': 0, 'repair': 0, 'to_sc': 0, 'from_sc': 0}
        for row in rows:
            action = row['action_type']
            if action in stats:
                stats[action] = row['count']
        return stats

# ============================================================
# ПАРСИНГ СООБЩЕНИЙ
# ============================================================
def extract_bike_codes(text: str) -> list:
    return re.findall(r'\b\d{4}\b', text)

def parse_message(text: str) -> list:
    text_lower = text.lower().strip()
    results = []
    processed_text = text_lower

    rules = [
        ('to_sc', ['привез на сц', 'привёз на сц', 'на сц привез', 'привез в сц', 'на сц'], ['вывез', 'вывёз']),
        ('from_sc', ['вывез из сц', 'вывёз из сц', 'из сц вывез', 'вывез с сц', 'из сц'], []),
        ('repair', ['ремонт', 'в ремонте', 'поломк', 'сломан'], []),
        ('move', ['переместил', 'перенес', 'перенёс', 'переставил', 'перемещ'], []),
        ('fix', ['поправил', 'выровнял', 'чист', 'поправ'], []),
    ]

    for action_type, keywords, exclude_keywords in rules:
        found_kw = None
        for kw in keywords:
            if kw in processed_text:
                found_kw = kw
                break

        if found_kw:
            if exclude_keywords:
                if any(ek in processed_text for ek in exclude_keywords):
                    continue

            codes = extract_bike_codes(processed_text)
            results.append({
                'action_type': action_type,
                'bike_codes': codes if codes else []
            })
            processed_text = processed_text.replace(found_kw, ' ', 1)

    return results

# ============================================================
# КЛАВИАТУРЫ
# ============================================================
def get_start_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🟢 Начать смену")]],
        resize_keyboard=True,
        one_time_keyboard=False
    )

def get_end_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🔴 Закончить смену")]],
        resize_keyboard=True,
        one_time_keyboard=False
    )

def get_time_keyboard():
    buttons = []
    row = []
    for i, time in enumerate(TIME_GRID):
        row.append(KeyboardButton(text=time))
        if (i + 1) % 4 == 0:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True, one_time_keyboard=True)

def get_district_keyboard():
    buttons = [[KeyboardButton(text=d)] for d in DISTRICTS]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True, one_time_keyboard=True)

def get_role_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Скаут")],
            [KeyboardButton(text="🚛 Водитель")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )

# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================
moscow_tz = pytz.timezone("Europe/Moscow")

def parse_time_to_minutes(time_str: str) -> int:
    time_str = time_str.strip()
    parts = time_str.split(':')
    h = int(parts[0])
    m = int(parts[1])
    return h * 60 + m

def calc_duration(start: str, end: str) -> str:
    start_min = parse_time_to_minutes(start)
    end_min = parse_time_to_minutes(end)
    if end_min < start_min:
        end_min += 24 * 60
    diff = end_min - start_min
    hours = diff // 60
    mins = diff % 60
    return f"{hours} ч. {mins} мин."

# ============================================================
# FSM — СОСТОЯНИЯ
# ============================================================
class ShiftStates(StatesGroup):
    waiting_start_time = State()
    waiting_district = State()
    on_shift = State()
    waiting_end_time = State()

class RegisterStates(StatesGroup):
    waiting_name = State()
    waiting_role = State()

# ============================================================
# РОУТЕРЫ
# ============================================================
register_router = Router()
chat1_router = Router()
chat2_router = Router()

# ============================================================
# ХЕНДЛЕРЫ: РЕГИСТРАЦИЯ В ЛС
# ============================================================
@register_router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if user:
        role_text = "Скаут" if user['role'] == 'scout' else 'Водитель'
        await message.answer(
            f"✅ Вы уже зарегистрированы!\n\n"
            f"👤 Имя: {user['full_name']}\n"
            f"🔧 Роль: {role_text}\n\n"
            f"Теперь вы можете работать в чате BibiBike."
        )
        return

    await message.answer(
        "👋 Добро пожаловать в BibiBike!\n\n"
        "Введите ваше ФИО (например: Понамарев К.А.):"
    )
    await state.set_state(RegisterStates.waiting_name)

@register_router.message(RegisterStates.waiting_name, F.chat.type == "private")
async def process_name(message: Message, state: FSMContext):
    full_name = message.text.strip()
    if len(full_name) < 5:
        await message.answer("❌ Слишком короткое имя. Введите ФИО полностью (например: Иванов И.И.):")
        return

    await state.update_data(full_name=full_name)
    await message.answer("Выберите вашу роль:", reply_markup=get_role_keyboard())
    await state.set_state(RegisterStates.waiting_role)

@register_router.message(RegisterStates.waiting_role, F.chat.type == "private")
async def process_role(message: Message, state: FSMContext):
    if message.text == "🔍 Скаут":
        role = "scout"
    elif message.text == "🚛 Водитель":
        role = "driver"
    else:
        await message.answer("❌ Пожалуйста, выберите роль кнопкой:")
        return

    data = await state.get_data()
    full_name = data['full_name']

    await add_user(message.from_user.id, full_name, role)

    role_text = "Скаут" if role == 'scout' else 'Водитель'
    await message.answer(
        f"✅ Регистрация завершена!\n\n"
        f"👤 {full_name}\n"
        f"🔧 {role_text}\n\n"
        f"Теперь вы можете работать в чате BibiBike.",
        reply_markup=None
    )
    await state.clear()
    logger.info(f"Зарегистрирован: {full_name} ({role_text}), ID: {message.from_user.id}")

# ============================================================
# ХЕНДЛЕРЫ: ПАРСИНГ ЧАТА 1
# ============================================================
@chat1_router.message(
    F.chat.id == GROUP_ID,
    F.message_thread_id == CHAT1_THREAD_ID,
    F.text
)
async def handle_work_message(message: Message):
    user_id = message.from_user.id

    user = await get_user(user_id)
    if not user:
        return

    shift = await get_active_shift(user_id)
    if not shift:
        return

    actions = parse_message(message.text)

    for action in actions:
        await add_action(
            user_id=user_id,
            shift_id=shift['id'],
            action_type=action['action_type'],
            bike_codes=action['bike_codes'] if action['bike_codes'] else None
        )
        logger.info(f"Действие: {user['full_name']}, {action['action_type']}, коды: {action['bike_codes']}")

# ============================================================
# ХЕНДЛЕРЫ: УПРАВЛЕНИЕ СМЕНОЙ (ЧАТ 2)
# ============================================================
@chat2_router.message(
    F.chat.id == GROUP_ID,
    F.message_thread_id == CHAT2_THREAD_ID
)
async def handle_chat2(message: Message, state: FSMContext):
    user_id = message.from_user.id
    user = await get_user(user_id)

    if not user:
        await message.answer(
            "❌ Вы не зарегистрированы.\n"
            "Напишите боту в личные сообщения /start для регистрации."
        )
        return

    active_shift = await get_active_shift(user_id)

    if message.text == "🟢 Начать смену" and not active_shift:
        await message.answer("⏰ Выберите время начала смены:", reply_markup=get_time_keyboard())
        await state.set_state(ShiftStates.waiting_start_time)

    elif message.text == "🔴 Закончить смену" and active_shift:
        await message.answer("⏰ Выберите время окончания смены:", reply_markup=get_time_keyboard())
        await state.set_state(ShiftStates.waiting_end_time)

    elif not active_shift:
        await message.answer("📋 Смена не активна:", reply_markup=get_start_keyboard())
    else:
        await message.answer("🟢 Смена активна:", reply_markup=get_end_keyboard())

@chat2_router.message(ShiftStates.waiting_start_time, F.text)
async def process_start_time(message: Message, state: FSMContext):
    if ':' not in message.text:
        await message.answer("❌ Пожалуйста, выберите время кнопкой:")
        return

    start_time = message.text.strip()
    await state.update_data(start_time=start_time)
    await message.answer("📍 Выберите район:", reply_markup=get_district_keyboard())
    await state.set_state(ShiftStates.waiting_district)

@chat2_router.message(ShiftStates.waiting_district, F.text)
async def process_district(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    district = message.text.strip()

    if district not in DISTRICTS:
        await message.answer("❌ Пожалуйста, выберите район кнопкой из списка:")
        return

    data = await state.get_data()
    start_time = data['start_time']

    shift_id = await start_shift(
        user_id=user['user_id'],
        full_name=user['full_name'],
        role=user['role'],
        start_time=start_time,
        district=district
    )

    role_text = "Скаут" if user['role'] == 'scout' else 'Водитель'

    await message.answer(
        f"👤 {user['full_name']} | {role_text}\n"
        f"🟢 Смену начал: {start_time}\n"
        f"📍 Район: {district}\n\n"
        f"✅ Смена активна! Можете приступать к работе.",
        reply_markup=get_end_keyboard()
    )

    await state.set_state(ShiftStates.on_shift)
    await state.update_data(shift_id=shift_id, district=district)
    logger.info(f"Смена начата: {user['full_name']}, {start_time}, {district}")

@chat2_router.message(ShiftStates.waiting_end_time, F.text)
async def process_end_time(message: Message, state: FSMContext):
    if ':' not in message.text:
        await message.answer("❌ Пожалуйста, выберите время кнопкой:")
        return

    user = await get_user(message.from_user.id)
    end_time = message.text.strip()

    data = await state.get_data()
    start_time = data.get('start_time', '')
    district = data.get('district', '')

    shift_id = await end_shift(user['user_id'], end_time)

    if not shift_id:
        await message.answer("❌ Ошибка: не найдена активная смена.")
        await state.clear()
        return

    stats = await get_shift_stats(shift_id)
    duration = calc_duration(start_time, end_time)
    role_text = "Скаут" if user['role'] == 'scout' else 'Водитель'

    report = (
        f"👤 {user['full_name']} | {role_text}\n"
        f"🔴 Смену закончил: {end_time}\n"
        f"⏱ Отработано: {duration}\n"
        f"📍 Район: {district}\n\n"
        f"📊 Статистика за смену:\n"
    )

    if user['role'] == 'scout':
        report += f"🚲 Перемещено: {stats['move']}\n"
        report += f"✅ Поправлено: {stats['fix']}\n"
        report += f"🛠 Ремонт: {stats['repair']}\n"
    else:
        report += f"📦 Привез на СЦ: {stats['to_sc']}\n"
        report += f"📤 Вывез из СЦ: {stats['from_sc']}\n"
        report += f"🚲 Перемещено: {stats['move']}\n"
        report += f"✅ Поправлено: {stats['fix']}\n"
        report += f"🛠 Ремонт: {stats['repair']}\n"

    await message.answer(report, reply_markup=get_start_keyboard())
    await state.clear()
    logger.info(f"Смена завершена: {user['full_name']}, {duration}, {stats}")

# ============================================================
# ЗАПУСК БОТА
# ============================================================
async def main():
    await init_db()

    # Создаём прокси-коннектор
    connector = ProxyConnector.from_url(PROXY_URL)

    # Создаём бота с прокси (правильный способ для aiogram 3.x)
    bot = Bot(
        token=BOT_TOKEN,
        session=AiohttpSession(proxy=PROXY_URL)
    )

    dp = Dispatcher()

    dp.include_router(register_router)
    dp.include_router(chat1_router)
    dp.include_router(chat2_router)

    logger.info("=" * 50)
    logger.info("BibiBike Bot запущен!")
    logger.info(f"Прокси: {PROXY_URL}")
    logger.info(f"Группа: {GROUP_ID}")
    logger.info("=" * 50)

    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен вручную")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")