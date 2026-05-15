import asyncio
import logging
import re
import aiosqlite
import pytz
from datetime import datetime
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import Command

# ============================================================
# КОНФИГУРАЦИЯ
# ============================================================
BOT_TOKEN = "8674884867:AAG_PMl3U8IMc7MQD3Vn26PBMLpEB18_wD8"
GROUP_ID = -1003818447487
CHAT1_THREAD_ID = 1   # Рабочий чат (только читаем)
CHAT2_THREAD_ID = 2   # Отчеты (панель управления)

DISTRICTS = ["Красная", "ФМР", "ЮМР", "Восточка", "Ставрополька", "ГМР"]

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
# ПАРСИНГ
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
def get_role_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Скаут")],
            [KeyboardButton(text="🚛 Водитель")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )

def get_panel_keyboard(user_id: int):
    """Главная панель управления."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Начать смену", callback_data=f"panel_start_{user_id}")],
        [InlineKeyboardButton(text="📊 Моя смена", callback_data=f"panel_status_{user_id}")],
    ])

def get_time_inline_keyboard(user_id: int, action: str):
    buttons = []
    row = []
    for i, time in enumerate(TIME_GRID):
        row.append(InlineKeyboardButton(text=time, callback_data=f"{action}_{time}_{user_id}"))
        if (i + 1) % 4 == 0:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_district_inline_keyboard(user_id: int):
    buttons = []
    for district in DISTRICTS:
        buttons.append([InlineKeyboardButton(text=district, callback_data=f"district_{district}_{user_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_active_shift_keyboard(user_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔴 Закончить смену", callback_data=f"end_shift_{user_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"panel_back_{user_id}")],
    ])

# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================
moscow_tz = pytz.timezone("Europe/Moscow")

def parse_time_to_minutes(time_str: str) -> int:
    parts = time_str.strip().split(':')
    return int(parts[0]) * 60 + int(parts[1])

def calc_duration(start: str, end: str) -> str:
    start_min = parse_time_to_minutes(start)
    end_min = parse_time_to_minutes(end)
    if end_min < start_min:
        end_min += 24 * 60
    diff = end_min - start_min
    return f"{diff // 60} ч. {diff % 60} мин."

def extract_user_id_from_callback(data: str) -> int:
    return int(data.split('_')[-1])

# ============================================================
# FSM
# ============================================================
class RegisterStates(StatesGroup):
    waiting_name = State()
    waiting_role = State()

# ============================================================
# РОУТЕРЫ
# ============================================================
register_router = Router()
chat1_router = Router()
chat2_router = Router()
callback_router = Router()

# ============================================================
# РЕГИСТРАЦИЯ В ЛС
# ============================================================
@register_router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if user:
        role_text = "Скаут" if user['role'] == 'scout' else 'Водитель'
        await message.answer(f"✅ Вы уже зарегистрированы!\n\n👤 Имя: {user['full_name']}\n🔧 Роль: {role_text}")
        return

    await message.answer("👋 Добро пожаловать в BibiBike!\n\nВведите ваше ФИО (например: Понамарев К.А.):")
    await state.set_state(RegisterStates.waiting_name)

@register_router.message(RegisterStates.waiting_name, F.chat.type == "private")
async def process_name(message: Message, state: FSMContext):
    full_name = message.text.strip()
    if len(full_name) < 5:
        await message.answer("❌ Слишком короткое имя. Введите ФИО полностью:")
        return
    await state.update_data(full_name=full_name)
    await message.answer("Выберите вашу роль:", reply_markup=get_role_keyboard())
    await state.set_state(RegisterStates.waiting_role)

@register_router.message(RegisterStates.waiting_role, F.chat.type == "private")
async def process_role(message: Message, state: FSMContext):
    if message.text == "🔍 Скаут": role = "scout"
    elif message.text == "🚛 Водитель": role = "driver"
    else:
        await message.answer("❌ Пожалуйста, выберите роль кнопкой:")
        return

    data = await state.get_data()
    await add_user(message.from_user.id, data['full_name'], role)
    role_text = "Скаут" if role == 'scout' else 'Водитель'
    await message.answer(f"✅ Регистрация завершена!\n\n👤 {data['full_name']}\n🔧 {role_text}", reply_markup=None)
    await state.clear()
    logger.info(f"Зарегистрирован: {data['full_name']} ({role_text})")

# ============================================================
# ПАРСИНГ ЧАТА 1
# ============================================================
@chat1_router.message(F.chat.id == GROUP_ID, F.message_thread_id == CHAT1_THREAD_ID, F.text)
async def handle_work_message(message: Message):
    user = await get_user(message.from_user.id)
    if not user: return
    shift = await get_active_shift(message.from_user.id)
    if not shift: return

    for action in parse_message(message.text):
        await add_action(message.from_user.id, shift['id'], action['action_type'], action['bike_codes'] if action['bike_codes'] else None)
        logger.info(f"Действие: {user['full_name']}, {action['action_type']}")

# ============================================================
# ЧАТ 2 — КОМАНДА /panel
# ============================================================
@chat2_router.message(F.chat.id == GROUP_ID, F.message_thread_id == CHAT2_THREAD_ID, Command("panel"))
async def cmd_panel(message: Message):
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer("❌ Вы не зарегистрированы. Напишите /start в ЛС бота.")
        return

    await message.answer(
        "🏍 **BibiBike — Панель управления**\n\nВыберите действие:",
        reply_markup=get_panel_keyboard(message.from_user.id)
    )

# ============================================================
# CALLBACK-ХЕНДЛЕРЫ
# ============================================================

# --- Главная панель: Начать смену ---
@callback_router.callback_query(F.data.startswith("panel_start_"))
async def panel_start(callback: CallbackQuery):
    if callback.from_user.id != extract_user_id_from_callback(callback.data):
        await callback.answer("❌ Это не ваша панель!", show_alert=True)
        return

    await callback.message.edit_text(
        "🏍 **Новая смена**\n\n⏰ Выберите время начала:",
        reply_markup=get_time_inline_keyboard(callback.from_user.id, "start_time")
    )
    await callback.answer()

# --- Главная панель: Статус смены ---
@callback_router.callback_query(F.data.startswith("panel_status_"))
async def panel_status(callback: CallbackQuery):
    if callback.from_user.id != extract_user_id_from_callback(callback.data):
        await callback.answer("❌ Это не ваша панель!", show_alert=True)
        return

    user = await get_user(callback.from_user.id)
    shift = await get_active_shift(callback.from_user.id)

    if shift:
        role_text = "Скаут" if user['role'] == 'scout' else 'Водитель'
        await callback.message.edit_text(
            f"👤 {user['full_name']} | {role_text}\n"
            f"🟢 Смену начал: {shift['start_time']}\n"
            f"📍 Район: {shift['district']}\n\n"
            f"Смена активна.",
            reply_markup=get_active_shift_keyboard(callback.from_user.id)
        )
    else:
        await callback.message.edit_text(
            f"👤 {user['full_name']}\n\n❌ Нет активной смены.\nНажмите «Начать смену» чтобы начать.",
            reply_markup=get_panel_keyboard(callback.from_user.id)
        )
    await callback.answer()

# --- Назад к панели ---
@callback_router.callback_query(F.data.startswith("panel_back_"))
async def panel_back(callback: CallbackQuery):
    if callback.from_user.id != extract_user_id_from_callback(callback.data):
        await callback.answer("❌ Это не ваша панель!", show_alert=True)
        return

    await callback.message.edit_text(
        "🏍 **BibiBike — Панель управления**\n\nВыберите действие:",
        reply_markup=get_panel_keyboard(callback.from_user.id)
    )
    await callback.answer()

# --- Выбор времени начала ---
@callback_router.callback_query(F.data.startswith("start_time_"))
async def callback_start_time(callback: CallbackQuery):
    if callback.from_user.id != extract_user_id_from_callback(callback.data):
        await callback.answer("❌ Это не ваша смена!", show_alert=True)
        return

    start_time = callback.data.replace(f"start_time_", "").replace(f"_{callback.from_user.id}", "")
    await callback.message.edit_text(
        f"🏍 **Новая смена**\n⏰ Время: {start_time}\n\n📍 Выберите район:",
        reply_markup=get_district_inline_keyboard(callback.from_user.id)
    )
    await callback.answer()

# --- Выбор района ---
@callback_router.callback_query(F.data.startswith("district_"))
async def callback_district(callback: CallbackQuery):
    if callback.from_user.id != extract_user_id_from_callback(callback.data):
        await callback.answer("❌ Это не ваша смена!", show_alert=True)
        return

    user = await get_user(callback.from_user.id)
    district = callback.data.split('_')[1]

    # Извлекаем время из текста сообщения
    start_time = "??:??"
    for line in callback.message.text.split('\n'):
        if 'Время:' in line:
            start_time = line.split('Время:')[1].strip()
            break

    await start_shift(user['user_id'], user['full_name'], user['role'], start_time, district)
    role_text = "Скаут" if user['role'] == 'scout' else 'Водитель'

    await callback.message.edit_text(
        f"👤 {user['full_name']} | {role_text}\n"
        f"🟢 Смену начал: {start_time}\n"
        f"📍 Район: {district}\n\n"
        f"✅ Смена активна!",
        reply_markup=get_active_shift_keyboard(callback.from_user.id)
    )
    await callback.answer()
    logger.info(f"Смена начата: {user['full_name']}, {start_time}, {district}")

# --- Завершить смену ---
@callback_router.callback_query(F.data.startswith("end_shift_"))
async def callback_end_shift(callback: CallbackQuery):
    if callback.from_user.id != extract_user_id_from_callback(callback.data):
        await callback.answer("❌ Это не ваша смена!", show_alert=True)
        return

    await callback.message.edit_text(
        callback.message.text + "\n\n⏰ Выберите время окончания:",
        reply_markup=get_time_inline_keyboard(callback.from_user.id, "end_time")
    )
    await callback.answer()

# --- Выбор времени окончания ---
@callback_router.callback_query(F.data.startswith("end_time_"))
async def callback_end_time(callback: CallbackQuery):
    if callback.from_user.id != extract_user_id_from_callback(callback.data):
        await callback.answer("❌ Это не ваша смена!", show_alert=True)
        return

    user = await get_user(callback.from_user.id)
    end_time = callback.data.replace(f"end_time_", "").replace(f"_{callback.from_user.id}", "")

    # Извлекаем данные из текста сообщения
    start_time = ""
    district = ""
    for line in callback.message.text.split('\n'):
        if 'Смену начал:' in line: start_time = line.split('Смену начал:')[1].strip()
        if 'Район:' in line: district = line.split('Район:')[1].strip()

    shift_id = await end_shift(user['user_id'], end_time)
    if not shift_id:
        await callback.answer("❌ Ошибка!", show_alert=True)
        return

    stats = await get_shift_stats(shift_id)
    duration = calc_duration(start_time, end_time)
    role_text = "Скаут" if user['role'] == 'scout' else 'Водитель'

    report = (
        f"👤 {user['full_name']} | {role_text}\n"
        f"🟢 Смену начал: {start_time}\n"
        f"🔴 Смену закончил: {end_time}\n"
        f"⏱ Отработано: {duration}\n"
        f"📍 Район: {district}\n\n"
        f"📊 Статистика за смену:\n"
    )

    if user['role'] == 'scout':
        report += f"🚲 Перемещено: {stats['move']}\n✅ Поправлено: {stats['fix']}\n🛠 Ремонт: {stats['repair']}\n"
    else:
        report += f"📦 Привез на СЦ: {stats['to_sc']}\n📤 Вывез из СЦ: {stats['from_sc']}\n🚲 Перемещено: {stats['move']}\n✅ Поправлено: {stats['fix']}\n🛠 Ремонт: {stats['repair']}\n"

    await callback.message.edit_text(report)
    await callback.answer("✅ Смена завершена!")
    logger.info(f"Смена завершена: {user['full_name']}, {duration}")

# ============================================================
# ЗАПУСК
# ============================================================
async def main():
    await init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(callback_router)
    dp.include_router(register_router)
    dp.include_router(chat1_router)
    dp.include_router(chat2_router)

    logger.info("=" * 50)
    logger.info("🏍 BibiBike Bot запущен!")
    logger.info(f"Группа: {GROUP_ID}")
    logger.info("=" * 50)

    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
    except Exception as e:
        logger.error(f"Ошибка: {e}")
