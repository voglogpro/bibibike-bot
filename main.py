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
from aiogram.utils.keyboard import ReplyKeyboardBuilder

# ============================================================
# КОНФИГУРАЦИЯ
# ============================================================
BOT_TOKEN = "8674884867:AAEhqfJ2mEDAxwdiD7_Kntfdm1xDad86ZCQ"
GROUP_ID = -1003818447487
CHAT1_THREAD_ID = 1   # Рабочий чат (только читаем)
CHAT2_THREAD_ID = 2   # Отчеты (пишем)

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
def get_start_reply_keyboard():
    """Reply-кнопка Начать смену (исчезает после нажатия)."""
    builder = ReplyKeyboardBuilder()
    builder.add(KeyboardButton(text="🟢 Начать смену"))
    return builder.as_markup(resize_keyboard=True, one_time_keyboard=True)

def get_role_keyboard():
    """Reply-кнопки выбора роли."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔍 Скаут")],
            [KeyboardButton(text="🚛 Водитель")]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )

def get_time_inline_keyboard(user_id: int, action: str):
    """Inline-кнопки выбора времени."""
    buttons = []
    row = []
    for i, time in enumerate(TIME_GRID):
        row.append(InlineKeyboardButton(
            text=time,
            callback_data=f"{action}_{time}_{user_id}"
        ))
        if (i + 1) % 4 == 0:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_district_inline_keyboard(user_id: int):
    """Inline-кнопки выбора района."""
    buttons = []
    for district in DISTRICTS:
        buttons.append([InlineKeyboardButton(
            text=district,
            callback_data=f"district_{district}_{user_id}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_end_shift_inline_keyboard(user_id: int):
    """Inline-кнопка завершения смены."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="🔴 Закончить смену",
            callback_data=f"end_shift_{user_id}"
        )
    ]])

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

def extract_user_id_from_callback(callback_data: str) -> int:
    """Извлекает user_id из callback_data."""
    parts = callback_data.split('_')
    return int(parts[-1])

# ============================================================
# FSM — СОСТОЯНИЯ
# ============================================================
class ShiftStates(StatesGroup):
    on_shift = State()

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
# ХЕНДЛЕРЫ: РЕГИСТРАЦИЯ В ЛС БОТА
# ============================================================
@register_router.message(Command("start"), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if user:
        role_text = "Скаут" if user['role'] == 'scout' else 'Водитель'
        await message.answer(
            f"✅ Вы уже зарегистрированы!\n\n"
            f"👤 Имя: {user['full_name']}\n"
            f"🔧 Роль: {role_text}"
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
        f"🔧 {role_text}",
        reply_markup=None
    )
    await state.clear()
    logger.info(f"Зарегистрирован: {full_name} ({role_text}), ID: {message.from_user.id}")

# ============================================================
# ХЕНДЛЕРЫ: ПАРСИНГ РАБОЧЕГО ЧАТА (ЧАТ 1)
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
# ХЕНДЛЕРЫ: УПРАВЛЕНИЕ СМЕНОЙ (ЧАТ 2 — ОТЧЕТЫ)
# ============================================================
# ============================================================
# ХЕНДЛЕРЫ: УПРАВЛЕНИЕ СМЕНОЙ (ЧАТ 2 — ОТЧЕТЫ)
# ============================================================
@chat2_router.message(F.chat.id == GROUP_ID)
async def handle_chat2(message: Message, state: FSMContext):
    # ЛОГИРУЕМ ВСЁ для отладки
    logger.info(f"=== СООБЩЕНИЕ В ЧАТЕ 2 ===")
    logger.info(f"От кого: {message.from_user.id} ({message.from_user.full_name})")
    logger.info(f"Текст: {message.text}")
    logger.info(f"Тред: {message.message_thread_id}")
    logger.info(f"============================")

    # Игнорируем сообщения из Чата 1
    if message.message_thread_id == CHAT1_THREAD_ID:
        logger.info("Пропущено: это Чат 1")
        return

    user_id = message.from_user.id
    user = await get_user(user_id)

    if not user:
        logger.info(f"Пользователь {user_id} не зарегистрирован")
        await message.answer(
            "❌ Вы не зарегистрированы.\n"
            "Напишите боту в личные сообщения /start для регистрации."
        )
        return

    active_shift = await get_active_shift(user_id)
    logger.info(f"Активная смена: {active_shift is not None}")

    # Нажата кнопка "Начать смену"
    if message.text and "Начать смену" in message.text:
        logger.info(f"Кнопка Начать смену нажата пользователем {user_id}")
        if not active_shift:
            logger.info("Создаю новую смену...")
            # Отправляем сообщение с инлайн-кнопками
            await message.answer(
                "📋 **Новая смена**\nВыберите время начала:",
                reply_markup=get_time_inline_keyboard(user_id, "start_time")
            )
            # Убираем reply-клавиатуру
            await message.answer(
                "✅ Смена создана. Выберите время нажатием на кнопку выше.",
                reply_markup=ReplyKeyboardMarkup(keyboard=[], resize_keyboard=True)
            )
        else:
            logger.info("Смена уже активна")
            await message.answer(
                "🟢 У вас уже есть активная смена!",
                reply_markup=ReplyKeyboardMarkup(keyboard=[], resize_keyboard=True)
            )
    else:
        logger.info(f"Не кнопка: {message.text}")

# ============================================================
# CALLBACK-ХЕНДЛЕРЫ (ИНЛАЙН-КНОПКИ)
# ============================================================

@callback_router.callback_query(F.data.startswith("start_time_"))
async def callback_start_time(callback: CallbackQuery):
    user_id = callback.from_user.id
    callback_user_id = extract_user_id_from_callback(callback.data)

    # Защита от чужих нажатий
    if user_id != callback_user_id:
        await callback.answer("❌ Это не ваша смена!", show_alert=True)
        return

    user = await get_user(user_id)
    if not user:
        await callback.answer("❌ Вы не зарегистрированы!", show_alert=True)
        return

    start_time = callback.data.replace(f"start_time_", "").replace(f"_{user_id}", "")

    # Обновляем сообщение: теперь выбор района
    await callback.message.edit_text(
        f"📋 **Новая смена**\n⏰ Время начала: {start_time}\n\n📍 Выберите район:",
        reply_markup=get_district_inline_keyboard(user_id)
    )
    await callback.answer()

@callback_router.callback_query(F.data.startswith("district_"))
async def callback_district(callback: CallbackQuery):
    user_id = callback.from_user.id
    callback_user_id = extract_user_id_from_callback(callback.data)

    # Защита от чужих нажатий
    if user_id != callback_user_id:
        await callback.answer("❌ Это не ваша смена!", show_alert=True)
        return

    user = await get_user(user_id)
    if not user:
        await callback.answer("❌ Вы не зарегистрированы!", show_alert=True)
        return

    # Извлекаем район из callback_data
    # Формат: district_РАЙОН_USERID
    district = callback.data.split('_')[1]

    # Получаем время начала из текста сообщения
    message_text = callback.message.text
    start_time = "??:??"
    for line in message_text.split('\n'):
        if 'Время начала:' in line:
            start_time = line.split('Время начала:')[1].strip()
            break

    # Запускаем смену в БД
    shift_id = await start_shift(
        user_id=user['user_id'],
        full_name=user['full_name'],
        role=user['role'],
        start_time=start_time,
        district=district
    )

    role_text = "Скаут" if user['role'] == 'scout' else 'Водитель'

    # Обновляем сообщение: смена активна
    await callback.message.edit_text(
        f"👤 {user['full_name']} | {role_text}\n"
        f"🟢 Смену начал: {start_time}\n"
        f"📍 Район: {district}\n\n"
        f"✅ Смена активна!",
        reply_markup=get_end_shift_inline_keyboard(user_id)
    )
    await callback.answer()
    logger.info(f"Смена начата: {user['full_name']}, {start_time}, {district}")

@callback_router.callback_query(F.data.startswith("end_shift_"))
async def callback_end_shift(callback: CallbackQuery):
    user_id = callback.from_user.id
    callback_user_id = extract_user_id_from_callback(callback.data)

    # Защита от чужих нажатий
    if user_id != callback_user_id:
        await callback.answer("❌ Это не ваша смена!", show_alert=True)
        return

    user = await get_user(user_id)
    if not user:
        await callback.answer("❌ Вы не зарегистрированы!", show_alert=True)
        return

    # Показываем кнопки выбора времени окончания
    await callback.message.edit_text(
        callback.message.text + "\n\n⏰ Выберите время окончания:",
        reply_markup=get_time_inline_keyboard(user_id, "end_time")
    )
    await callback.answer()

@callback_router.callback_query(F.data.startswith("end_time_"))
async def callback_end_time(callback: CallbackQuery):
    user_id = callback.from_user.id
    callback_user_id = extract_user_id_from_callback(callback.data)

    # Защита от чужих нажатий
    if user_id != callback_user_id:
        await callback.answer("❌ Это не ваша смена!", show_alert=True)
        return

    user = await get_user(user_id)
    if not user:
        await callback.answer("❌ Вы не зарегистрированы!", show_alert=True)
        return

    end_time = callback.data.replace(f"end_time_", "").replace(f"_{user_id}", "")

    # Извлекаем данные из текста сообщения
    message_text = callback.message.text
    start_time = ""
    district = ""
    for line in message_text.split('\n'):
        if 'Смену начал:' in line:
            start_time = line.split('Смену начал:')[1].strip()
        if 'Район:' in line:
            district = line.split('Район:')[1].strip()

    shift_id = await end_shift(user['user_id'], end_time)

    if not shift_id:
        await callback.answer("❌ Ошибка: не найдена активная смена.", show_alert=True)
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
        report += f"🚲 Перемещено: {stats['move']}\n"
        report += f"✅ Поправлено: {stats['fix']}\n"
        report += f"🛠 Ремонт: {stats['repair']}\n"
    else:
        report += f"📦 Привез на СЦ: {stats['to_sc']}\n"
        report += f"📤 Вывез из СЦ: {stats['from_sc']}\n"
        report += f"🚲 Перемещено: {stats['move']}\n"
        report += f"✅ Поправлено: {stats['fix']}\n"
        report += f"🛠 Ремонт: {stats['repair']}\n"

    # Финальное сообщение без кнопок
    await callback.message.edit_text(report)
    await callback.answer("✅ Смена завершена!")
    logger.info(f"Смена завершена: {user['full_name']}, {duration}")

# ============================================================
# ЗАПУСК БОТА
# ============================================================
async def main():
    await init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(callback_router)  # Важно: первым!
    dp.include_router(register_router)
    dp.include_router(chat1_router)
    dp.include_router(chat2_router)

    logger.info("=" * 50)
    logger.info("BibiBike Bot запущен!")
    logger.info(f"Группа: {GROUP_ID}")
    logger.info(f"Чат 1 (чтение): тред {CHAT1_THREAD_ID}")
    logger.info(f"Чат 2 (отчеты): тред {CHAT2_THREAD_ID}")
    logger.info("=" * 50)

    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
    except Exception as e:
        logger.error(f"Ошибка: {e}")
