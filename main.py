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
    """
    Разбирает текст на действия.
    Коды привязываются к ближайшему ключевому слову ПЕРЕД ними.
    Если в строке и код, и ключевое слово — привязывается к этому слову.
    """
    text = text.lower().strip()
    lines = text.split('\n')
    
    results = []  # Итоговый список действий
    current_codes = []  # Коды, собранные перед ключевым словом
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # Ищем коды в строке
        codes_in_line = re.findall(r'\b(\d{4})\b', line)
        current_codes.extend(codes_in_line)
        
        # Ищем ключевые слова в строке
        keywords_in_line = []
        for kw in ['привез на сц', 'привёз на сц', 'на сц привез', 'на сц',
                   'вывез из сц', 'вывёз из сц', 'из сц вывез', 'вывез с сц', 'из сц',
                   'ремонт', 'поломк', 'сломан',
                   'переместил', 'перенес', 'перенёс', 'переставил', 'перемещ',
                   'поправил', 'выровнял', 'чист', 'поправ']:
            if kw in line:
                atype = get_action_type(kw)
                if atype and atype not in keywords_in_line:
                    keywords_in_line.append(atype)
        
        # Если есть ключевые слова — фиксируем действие
        for atype in keywords_in_line:
            # Ищем количество в этой строке
            qty = 0
            qty_match = re.search(r'(?<!\d)(\d{1,3})(?!\d)', line)
            if qty_match:
                num = int(qty_match.group(1))
                if not re.search(r'\b\d{4}\b', line):
                    qty = num
            
            if qty > 0:
                # Действие с количеством
                results.append({
                    'action_type': atype,
                    'bike_codes': [],
                    'quantity': qty
                })
            else:
                # Действие с кодами
                if current_codes:
                    results.append({
                        'action_type': atype,
                        'bike_codes': current_codes.copy(),
                        'quantity': 0
                    })
                else:
                    results.append({
                        'action_type': atype,
                        'bike_codes': [],
                        'quantity': 0
                    })
            
            # Если в строке были коды — сбрасываем их для следующего действия
            if codes_in_line:
                current_codes = []
    
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
    user = await get_user(message.from_user.id)
    text = (message.text or message.caption or "").strip()

    if text == "/help":
        await message.answer(
            "BibiBike - команды:\n\n"
            "Регистрация (1 раз):\n"
            "Фамилия И.О. скаут\n\n"
            "Начать смену:\n"
            "09:00 фмр\n\n"
            "Закончить смену:\n"
            "18:00\n"
            "18:00 Комментарий\n\n"
            "Статус: /status\n"
            f"Районы: {', '.join(DISTRICTS)}"
        )
        return

    if text == "/status":
        if not user:
            await message.answer("Вы не зарегистрированы.")
            return
        shift = await get_active_shift(user['user_id'])
        if shift:
            role_text = "Скаут" if user['role'] == 'scout' else 'Водитель'
            await message.answer(
                f"{user['full_name']} | {role_text}\n"
                f"Активная смена с {shift['start_time']}\n"
                f"Район: {shift['district']}"
            )
        else:
            await message.answer("Нет активной смены.")
        return

    if not user:
        parts = text.split()
        if len(parts) >= 2:
            role_word = parts[-1].lower()
            if role_word in ["скаут", "scout"]:
                role = "scout"
            elif role_word in ["водитель", "driver", "вод"]:
                role = "driver"
            else:
                await message.answer("Укажите роль: скаут или водитель\nПример: Понамарев К.А. скаут")
                return

            full_name = " ".join(parts[:-1])
            await add_user(message.from_user.id, full_name, role)

            try:
                await message.delete()
            except:
                pass

            role_text = "Скаут" if role == 'scout' else 'Водитель'
            await message.answer(
                f"Запомнил: {full_name} | {role_text}\n\n"
                f"Для начала смены напиши время и район.\n"
                f"Например: 09:00 фмр"
            )
            logger.info(f"Зарегистрирован: {full_name} ({role_text})")
            return
        else:
            await message.answer(
                "Вы не зарегистрированы.\n"
                "Напишите: Фамилия И.О. роль\n"
                "Например: Понамарев К.А. скаут"
            )
            return

    active_shift = await get_active_shift(user['user_id'])
    time_match = re.match(r'(\d{1,2}:\d{2})\s*(.*)', text)

    if time_match:
        time_str = time_match.group(1)
        extra = time_match.group(2).strip()

        if not active_shift:
            district = extra.split()[0].lower() if extra else ""
            if district and district in DISTRICTS:
                await start_shift(user['user_id'], user['full_name'], user['role'], time_str, district)

                try:
                    await message.delete()
                except:
                    pass

                role_text = "Скаут" if user['role'] == 'scout' else 'Водитель'
                await message.answer(
                    f"Смена начата.\n\n"
                    f"{user['full_name']} | {role_text}\n"
                    f"Начал: {time_str}\n"
                    f"Район: {district}"
                )
                logger.info(f"Смена начата: {user['full_name']}, {time_str}, {district}")
                return
            else:
                await message.answer(
                    f"Укажите район.\n"
                    f"Формат: 09:00 фмр\n"
                    f"Доступны: {', '.join(DISTRICTS)}"
                )
                return

        else:
            comment = extra if extra else ""
            sid = await end_shift(user['user_id'], time_str, comment)
            if not sid:
                await message.answer("Ошибка завершения смены.")
                return

            stats = await get_stats(sid)
            role_text = "Скаут" if user['role'] == 'scout' else 'Водитель'

            sp = active_shift['start_time'].split(':')
            ep = time_str.split(':')
            sm = int(sp[0]) * 60 + int(sp[1])
            em = int(ep[0]) * 60 + int(ep[1])
            if em < sm:
                em += 24 * 60
            diff = em - sm
            duration = f"{diff // 60} ч. {diff % 60} мин."

            report = (
                f"{user['full_name']} | {role_text}\n"
                f"Начал: {active_shift['start_time']}\n"
                f"Закончил: {time_str}\n"
                f"Отработано: {duration}\n"
                f"Район: {active_shift['district']}\n\n"
                f"Статистика за смену:\n"
            )

            if user['role'] == 'scout':
                report += f"Перемещено: {stats['move']}\n"
                report += f"Поправлено: {stats['fix']}\n"
                report += f"Ремонт: {stats['repair']}\n"
            else:
                report += f"Привез на СЦ: {stats['to_sc']}\n"
                report += f"Вывез из СЦ: {stats['from_sc']}\n"
                report += f"Перемещено: {stats['move']}\n"
                report += f"Поправлено: {stats['fix']}\n"
                report += f"Ремонт: {stats['repair']}\n"

            if comment:
                report += f"\nКомментарий: {comment}"

            try:
                await message.delete()
            except:
                pass

            await message.answer(report)
            logger.info(f"Смена завершена: {user['full_name']}, {duration}")
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
