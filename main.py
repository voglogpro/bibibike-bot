import asyncio
import os
import logging
import re
import aiosqlite
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message
from aiogram.filters import Command

# ============================================================
# КОНФИГУРАЦИЯ
# ============================================================
BOT_TOKEN = "8897464834:AAGMgpcYbto51407Rxgz7NE5DllYam5-s-I"
GROUP_ID = -1003895375312
CHAT1_THREAD_ID = 1
CHAT2_THREAD_ID = 2

DISTRICTS = ["красная", "фмр", "юмр", "восточка", "ставрополька", "гмр"]

# ============================================================
# ЛОГИРОВАНИЕ
# ============================================================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================
# БАЗА ДАННЫХ
# ============================================================
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bibibike_work.db")

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT,
                role TEXT
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
                bike_codes TEXT,
                quantity INTEGER DEFAULT 0
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

async def add_action(uid, sid, atype, codes=None, qty=0):
    cstr = ",".join(codes) if codes else ""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO actions (user_id, shift_id, action_type, bike_codes, quantity) VALUES (?, ?, ?, ?, ?)",
            (uid, sid, atype, cstr, qty)
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
        s = {'move': 0, 'fix': 0, 'repair': 0, 'to_sc': 0, 'from_sc': 0}
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
# ПАРСИНГ
# ============================================================
def parse_message(text):
    text = text.lower().strip()
    all_codes = re.findall(r'\b(\d{4})\b', text)
    lines = text.split('\n')
    
    # Коды из строк с ремонтом
    repair_codes = []
    for line in lines:
        if any(kw in line for kw in ['ремонт', 'поломк', 'сломан']):
            repair_codes.extend(re.findall(r'\b(\d{4})\b', line))
    
    # Ищем ключевые слова
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
    
def get_action_type(kw):
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

# ============================================================
# РОУТЕРЫ
# ============================================================
work_router = Router()
cmd_router = Router()

# ============================================================
# ЧАТ 1
# ============================================================
@work_router.message(F.chat.id == GROUP_ID)
async def work_chat(message: Message):
    if message.message_thread_id == CHAT2_THREAD_ID:
        return

    text = message.text or message.caption or ""
    if not text:
        return
    if text.startswith('/'):
        return
    if re.match(r'^\d{1,2}:\d{2}\s*', text):
        return

    logger.info(f"ЧАТ 1: от {message.from_user.id} | текст: '{text}' | тред: {message.message_thread_id}")

    user = await get_user(message.from_user.id)
    if not user:
        logger.info("Пропущено: не зарегистрирован")
        return

    shift = await get_active_shift(message.from_user.id)
    if not shift:
        logger.info("Пропущено: нет активной смены")
        return

    actions = parse_message(text)
    logger.info(f"Распаршено: {actions}")

    for action in actions:
        await add_action(message.from_user.id, shift['id'], action['action_type'],
                         action.get('bike_codes', []), action.get('quantity', 0))
        logger.info(f"Записано: {user['full_name']} — {action}")

# ============================================================
# ЧАТ 2
# ============================================================
@cmd_router.message(F.chat.id == GROUP_ID, F.message_thread_id == CHAT2_THREAD_ID)
async def cmd_chat(message: Message):
    user_id = message.from_user.id
    full_name = message.from_user.full_name
    text = (message.text or message.caption or "").strip()

    if text == "/help":
        await message.answer(
            "BibiBike - команды:\n\n"
            "Начать смену:\n09:00 фмр\n\n"
            "Закончить смену:\n18:00\n18:00 Комментарий\n\n"
            "Статус: /status\n"
            f"Районы: {', '.join(DISTRICTS)}"
        )
        return

    if text == "/status":
        shift = await get_active_shift(user_id)
        if shift:
            await message.answer(
                f"{full_name}\n"
                f"Активная смена с {shift['start_time']}\n"
                f"Район: {shift['district']}"
            )
        else:
            await message.answer("Нет активной смены.")
        return

    active_shift = await get_active_shift(user_id)
    time_match = re.match(r'(\d{1,2}:\d{2})\s*(.*)', text)

    if time_match:
        time_str = time_match.group(1)
        extra = time_match.group(2).strip()

        if not active_shift:
            # Начало смены
            district = extra.split()[0].lower() if extra else ""
            if district and district in DISTRICTS:
                await start_shift(user_id, full_name, "", time_str, district)
                try:
                    await message.delete()
                except:
                    pass
                await message.answer(
                    f"Смена начата.\n\n{full_name}\nНачал: {time_str}\nРайон: {district}"
                )
                logger.info(f"Смена начата: {full_name}, {time_str}, {district}")
                return
            else:
                await message.answer(
                    f"Укажите район.\nФормат: 09:00 фмр\nДоступны: {', '.join(DISTRICTS)}"
                )
                return

        else:
            # Конец смены
            comment = extra if extra else ""
            sid = await end_shift(user_id, time_str, comment)
            if not sid:
                await message.answer("Ошибка завершения смены.")
                return

            stats = await get_stats(sid)
            sp = active_shift['start_time'].split(':')
            ep = time_str.split(':')
            sm = int(sp[0]) * 60 + int(sp[1])
            em = int(ep[0]) * 60 + int(ep[1])
            if em < sm:
                em += 24 * 60
            diff = em - sm
            duration = f"{diff // 60} ч. {diff % 60} мин."

            report = (
                f"{full_name}\n"
                f"Начал: {active_shift['start_time']}\n"
                f"Закончил: {time_str}\n"
                f"Отработано: {duration}\n"
                f"Район: {active_shift['district']}\n\n"
                f"Статистика за смену:\n"
            )

            # Только те строки, где есть действия
            if stats['move'] > 0:
                report += f"Перемещено: {stats['move']}\n"
            if stats['fix'] > 0:
                report += f"Поправлено: {stats['fix']}\n"
            if stats['repair'] > 0:
                report += f"Ремонт: {stats['repair']}\n"
            if stats['to_sc'] > 0:
                report += f"Привез на СЦ: {stats['to_sc']}\n"
            if stats['from_sc'] > 0:
                report += f"Вывез из СЦ: {stats['from_sc']}\n"

            if comment:
                report += f"\nКомментарий: {comment}"

            try:
                await message.delete()
            except:
                pass

            await message.answer(report)
            logger.info(f"Смена завершена: {full_name}, {duration}")
            return

    await message.answer(
        "Неизвестный формат.\n\n"
        "Начать смену: 09:00 фмр\n"
        "Закончить: 18:00 или 18:00 Комментарий\n"
        "Помощь: /help"
    )
# ============================================================
# ЗАПУСК
# ============================================================
async def main():
    await init_db()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(cmd_router)
    dp.include_router(work_router)

    logger.info("=" * 50)
    logger.info("BibiBike Bot запущен!")
    logger.info(f"Группа: {GROUP_ID}")
    logger.info(f"Чат 1 (рабочий): тред {CHAT1_THREAD_ID}")
    logger.info(f"Чат 2 (отчеты): тред {CHAT2_THREAD_ID}")
    logger.info("=" * 50)

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
