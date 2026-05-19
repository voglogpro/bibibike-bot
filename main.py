import asyncio
import logging
import re
import aiosqlite
from datetime import datetime
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import Command

# ============================================================
# КОНФИГУРАЦИЯ
# ============================================================
BOT_TOKEN = "8897464834:AAFGWmPH_hPYjasg72eKPVtoNG60GR6tRRk"
GROUP_ID = -1003895375312
CHAT1_THREAD_ID = 1   # Рабочий чат (только читаем)
CHAT2_THREAD_ID = 2   # Отчеты (команды и итоги)

DISTRICTS = ["Красная", "ФМР", "ЮМР", "Восточка", "Ставрополька", "ГМР"]

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
                role TEXT NOT NULL
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
                is_active INTEGER DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                shift_id INTEGER,
                action_type TEXT,
                bike_codes TEXT
            )
        """)
        await db.commit()
    logger.info("БД готова")

async def add_user(uid, name, role):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO users VALUES (?, ?, ?)", (uid, name, role))
        await db.commit()

async def get_user(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute("SELECT * FROM users WHERE user_id = ?", (uid,))
        r = await c.fetchone()
        return dict(r) if r else None

async def get_active_shift(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute("SELECT * FROM shifts WHERE user_id = ? AND is_active = 1", (uid,))
        r = await c.fetchone()
        return dict(r) if r else None

async def start_shift(uid, name, role, time, district):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE shifts SET is_active = 0 WHERE user_id = ? AND is_active = 1", (uid,))
        c = await db.execute(
            "INSERT INTO shifts (user_id, full_name, role, start_time, district, is_active) VALUES (?, ?, ?, ?, ?, 1)",
            (uid, name, role, time, district)
        )
        await db.commit()
        return c.lastrowid

async def end_shift(uid, time, comment=""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE shifts SET is_active = 0, end_time = ?, comment = ? WHERE user_id = ? AND is_active = 1",
            (time, comment, uid)
        )
        await db.commit()
        c = await db.execute("SELECT id FROM shifts WHERE user_id = ? ORDER BY id DESC LIMIT 1", (uid,))
        r = await c.fetchone()
        return r[0] if r else None

async def add_action(uid, sid, atype, codes):
    cstr = ",".join(codes) if codes else ""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO actions VALUES (NULL, ?, ?, ?, ?)", (uid, sid, atype, cstr))
        await db.commit()

async def get_stats(sid):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute("SELECT action_type, COUNT(*) as cnt FROM actions WHERE shift_id = ? GROUP BY action_type", (sid,))
        rows = await c.fetchall()
        s = {'move': 0, 'fix': 0, 'repair': 0, 'to_sc': 0, 'from_sc': 0}
        for r in rows:
            if r['action_type'] in s:
                s[r['action_type']] = r['cnt']
        return s

# ============================================================
# ПАРСИНГ (коды привязаны к ближайшему ключевому слову)
# ============================================================
def parse_message(text):
    """
    Разбирает текст на действия.
    Коды привязываются к ближайшему ключевому слову.
    """
    text = text.lower().strip()
    lines = text.split('\n')

    # Собираем все строки в один список (коды и ключевые слова)
    tokens = []
    for line in lines:
        line = line.strip().rstrip('.')
        if not line:
            continue
        # Проверяем, это код или ключевое слово
        if re.match(r'^\d{4}$', line):
            tokens.append(('code', line))
        else:
            # Ищем ключевые слова внутри строки
            found = False
            for kw in ['привез на сц', 'привёз на сц', 'на сц привез', 'на сц',
                       'вывез из сц', 'вывёз из сц', 'из сц вывез', 'вывез с сц', 'из сц',
                       'ремонт', 'в ремонте', 'поломк', 'сломан',
                       'переместил', 'перенес', 'перенёс', 'переставил', 'перемещ',
                       'поправил', 'выровнял', 'чист', 'поправ']:
                if kw in line:
                    tokens.append(('keyword', kw))
                    found = True
                    break
            if not found:
                # Ищем 4-значные коды внутри строки
                codes = re.findall(r'\b\d{4}\b', line)
                for code in codes:
                    tokens.append(('code', code))

    if not tokens:
        return []

    # Группируем коды с ближайшими ключевыми словами
    results = []
    current_keyword = None
    current_codes = []

    for token_type, token_value in tokens:
        if token_type == 'keyword':
            # Сохраняем предыдущее действие
            if current_keyword and current_codes:
                action_type = get_action_type(current_keyword)
                if action_type:
                    results.append({'action_type': action_type, 'bike_codes': current_codes.copy()})

            current_keyword = token_value
            current_codes = []
        elif token_type == 'code':
            current_codes.append(token_value)

    # Последнее действие
    if current_keyword:
        action_type = get_action_type(current_keyword)
        if action_type:
            # Собираем все коды (и те, что после ключевого слова)
            results.append({'action_type': action_type, 'bike_codes': current_codes.copy()})

    return results


def get_action_type(keyword):
    """Определяет тип действия по ключевому слову."""
    if keyword in ['привез на сц', 'привёз на сц', 'на сц привез', 'на сц']:
        return 'to_sc'
    if keyword in ['вывез из сц', 'вывёз из сц', 'из сц вывез', 'вывез с сц', 'из сц']:
        return 'from_sc'
    if keyword in ['ремонт', 'в ремонте', 'поломк', 'сломан']:
        return 'repair'
    if keyword in ['переместил', 'перенес', 'перенёс', 'переставил', 'перемещ']:
        return 'move'
    if keyword in ['поправил', 'выровнял', 'чист', 'поправ']:
        return 'fix'
    return None

# ============================================================
# РОУТЕРЫ
# ============================================================
reg_router = Router()
work_router = Router()
cmd_router = Router()

# ============================================================
# РЕГИСТРАЦИЯ В ЛС
# ============================================================
class RegStates(StatesGroup):
    name = State()
    role = State()

@reg_router.message(Command("start"), F.chat.type == "private")
async def start_reg(message: Message, state: FSMContext):
    user = await get_user(message.from_user.id)
    if user:
        role_text = "Скаут" if user['role'] == 'scout' else 'Водитель'
        await message.answer(f"✅ Вы уже зарегистрированы:\n👤 {user['full_name']}\n🔧 {role_text}")
        return
    await message.answer("👋 Добро пожаловать в BibiBike!\n\nВведите ваше ФИО (например: Иванов И.И.):")
    await state.set_state(RegStates.name)

@reg_router.message(RegStates.name, F.chat.type == "private")
async def reg_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if len(name) < 5:
        await message.answer("❌ Слишком коротко. Введите ФИО полностью:")
        return
    await state.update_data(name=name)
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🔍 Скаут"), KeyboardButton(text="🚛 Водитель")]],
        resize_keyboard=True, one_time_keyboard=True
    )
    await message.answer("Выберите вашу роль:", reply_markup=kb)
    await state.set_state(RegStates.role)

@reg_router.message(RegStates.role, F.chat.type == "private")
async def reg_role(message: Message, state: FSMContext):
    if message.text == "🔍 Скаут":
        role = "scout"
    elif message.text == "🚛 Водитель":
        role = "driver"
    else:
        await message.answer("❌ Пожалуйста, выберите роль кнопкой:")
        return

    data = await state.get_data()
    await add_user(message.from_user.id, data['name'], role)
    role_text = "Скаут" if role == 'scout' else 'Водитель'
    await message.answer(f"✅ Регистрация завершена!\n\n👤 {data['name']}\n🔧 {role_text}", reply_markup=None)
    await state.clear()
    logger.info(f"Зарегистрирован: {data['name']} ({role_text})")

# ============================================================
# ЧАТ 1 — ПАРСИНГ (ТОЛЬКО ЧИТАЕМ)
# ============================================================
@work_router.message(F.chat.id == GROUP_ID, F.message_thread_id == CHAT1_THREAD_ID)
async def work_chat(message: Message):
    user = await get_user(message.from_user.id)
    if not user:
        return

    shift = await get_active_shift(message.from_user.id)
    if not shift:
        return

    if not message.text:
        return

    actions = parse_message(message.text)
    for action in actions:
        await add_action(
            message.from_user.id,
            shift['id'],
            action['action_type'],
            action['bike_codes'] if action['bike_codes'] else None
        )
        logger.info(f"Записано: {user['full_name']} — {action['action_type']} {action['bike_codes']}")


# ============================================================
# ЧАТ 2 — КОМАНДЫ И ОТЧЁТЫ
# ============================================================
@cmd_router.message(F.chat.id == GROUP_ID, F.message_thread_id == CHAT2_THREAD_ID)
async def cmd_chat(message: Message):
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer("❌ Вы не зарегистрированы. Напишите /start в ЛС бота.")
        return

    text = message.text.strip() if message.text else ""

    # Помощь
    if text == "/help" or text == "/start":
        await message.answer(
            "📋 **BibiBike — команды:**\n\n"
            "**Начать смену:**\n"
            "`Фамилия И.О. 09:00 ФМР`\n\n"
            "**Закончить смену:**\n"
            "`18:00`\n"
            "`18:00 Комментарий`\n\n"
            "**Статус:** `/status`\n"
            f"**Районы:** {', '.join(DISTRICTS)}"
        )
        return

    # Статус
    if text == "/status":
        shift = await get_active_shift(user['user_id'])
        if shift:
            role_text = "Скаут" if user['role'] == 'scout' else 'Водитель'
            await message.answer(
                f"👤 {user['full_name']} | {role_text}\n"
                f"🟢 Активная смена с {shift['start_time']}\n"
                f"📍 {shift['district']}"
            )
        else:
            await message.answer("❌ Нет активной смены.")
        return

    # Проверяем, это начало смены или конец
    active_shift = await get_active_shift(user['user_id'])

    # Пробуем распарсить как время (ЧЧ:ММ)
    time_match = re.match(r'(\d{1,2}:\d{2})\s*(.*)', text)

    if time_match:
        time_str = time_match.group(1)
        extra = time_match.group(2).strip()

        if not active_shift:
            # === НАЧАЛО СМЕНЫ (если нет активной) ===
            # Для начала смены нужно: Фамилия И.О. 09:00 Район
            # Но пользователь уже идентифицирован по user_id
            # Проверяем, что extra — это район
            district = extra.rstrip('.')
            if district and district in DISTRICTS:
                await start_shift(user['user_id'], user['full_name'], user['role'], time_str, district)
                role_text = "Скаут" if user['role'] == 'scout' else 'Водитель'
                await message.answer(
                    f"✅ Смена начата!\n\n"
                    f"👤 {user['full_name']} | {role_text}\n"
                    f"🟢 Начал: {time_str}\n"
                    f"📍 Район: {district}"
                )
                logger.info(f"Смена начата: {user['full_name']}, {time_str}, {district}")
                return
            else:
                # Не район — значит, это попытка завершить, но смены нет
                await message.answer(
                    f"❌ Нет активной смены.\n"
                    f"Начните смену: `/start_smena 09:00 ФМР`\n"
                    f"Или напишите `/help`"
                )
                return

        else:
            # === КОНЕЦ СМЕНЫ ===
            comment = extra if extra else ""

            sid = await end_shift(user['user_id'], time_str, comment)
            if not sid:
                await message.answer("❌ Ошибка завершения смены!")
                return

            stats = await get_stats(sid)
            role_text = "Скаут" if user['role'] == 'scout' else 'Водитель'

            # Расчёт времени
            start_parts = active_shift['start_time'].split(':')
            end_parts = time_str.split(':')
            start_min = int(start_parts[0]) * 60 + int(start_parts[1])
            end_min = int(end_parts[0]) * 60 + int(end_parts[1])
            if end_min < start_min:
                end_min += 24 * 60
            diff = end_min - start_min
            duration = f"{diff // 60} ч. {diff % 60} мин."

            report = (
                f"👤 {user['full_name']} | {role_text}\n"
                f"🟢 Начал: {active_shift['start_time']}\n"
                f"🔴 Закончил: {time_str}\n"
                f"⏱ Отработано: {duration}\n"
                f"📍 Район: {active_shift['district']}\n\n"
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

            if comment:
                report += f"\n💬 Комментарий: {comment}"

            # Удаляем сообщение пользователя и отправляем отчёт
            try:
                await message.delete()
            except:
                pass

            await message.answer(report)
            logger.info(f"Смена завершена: {user['full_name']}, {duration}")
            return

    # Если не время — может быть командой /start_smena
    if text.startswith("/start_smena"):
        parts = text.split()
        if len(parts) >= 3:
            time_str = parts[1]
            district = parts[2]
            if district not in DISTRICTS:
                await message.answer(f"❌ Неизвестный район. Доступны: {', '.join(DISTRICTS)}")
                return
            if active_shift:
                await message.answer("❌ У вас уже есть активная смена!")
                return
            await start_shift(user['user_id'], user['full_name'], user['role'], time_str, district)
            role_text = "Скаут" if user['role'] == 'scout' else 'Водитель'
            await message.answer(
                f"✅ Смена начата!\n\n"
                f"👤 {user['full_name']} | {role_text}\n"
                f"🟢 Начал: {time_str}\n"
                f"📍 Район: {district}"
            )
        else:
            await message.answer("❌ Формат: `/start_smena 09:00 ФМР`")
        return

    # Если ничего не подошло
    await message.answer(
        f"❌ Неизвестная команда.\n\n"
        f"Начать смену: `/start_smena 09:00 ФМР`\n"
        f"Закончить: `18:00` или `18:00 Комментарий`\n"
        f"Помощь: `/help`"
    )

# ============================================================
# ЗАПУСК
# ============================================================
async def main():
    await init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(reg_router)
    dp.include_router(work_router)
    dp.include_router(cmd_router)

    logger.info("=" * 50)
    logger.info("🏍 BibiBike Bot запущен!")
    logger.info(f"Группа: {GROUP_ID}")
    logger.info(f"Чат 1 (рабочий): тред {CHAT1_THREAD_ID}")
    logger.info(f"Чат 2 (отчеты): тред {CHAT2_THREAD_ID}")
    logger.info("=" * 50)

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
