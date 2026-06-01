import asyncio
import logging
import re
import os
import sys
import aiosqlite
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message

# ============================================================
# КОНФИГУРАЦИЯ
# ============================================================
# Бот безопасно берет токен из настроек окружения BotHost
BOT_TOKEN = os.getenv("BOT_TOKEN")

GROUP_ID = -1003431950710
CHAT1_THREAD_ID = 1
CHAT2_THREAD_ID = 3

DISTRICTS = ["красная", "фмр", "юмр", "восточка", "ставрополька", "гмр"]

# ИНИЦИАЛИЗАЦИЯ РОУТЕРОВ
work_router = Router()
cmd_router = Router()

# ============================================================
# ЛОГИРОВАНИЕ
# ============================================================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Проверка наличия токена перед запуском
if not BOT_TOKEN:
    logger.error("КРИТИЧЕСКАЯ ОШИБКА: Переменная окружения BOT_TOKEN не задана в панели BotHost!")
    sys.exit(1)

# ============================================================
# БАЗА ДАННЫХ
# ============================================================
DB_PATH = "bibibike_work.db"

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
                message_id INTEGER,
                action_type TEXT,
                bike_codes TEXT,
                quantity INTEGER DEFAULT 0
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

async def get_last_shift(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute(
            "SELECT * FROM shifts WHERE user_id = ? AND is_active = 0 ORDER BY id DESC LIMIT 1",
            (uid,)
        )
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

async def add_action(uid, sid, mid, atype, codes=None, qty=0):
    cstr = ",".join(codes) if codes else ""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO actions (user_id, shift_id, message_id, action_type, bike_codes, quantity) VALUES (?, ?, ?, ?, ?, ?)",
            (uid, sid, mid, atype, cstr, qty)
        )
        await db.commit()

async def delete_actions_by_message(uid, mid):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM actions WHERE user_id = ? AND message_id = ?",
            (uid, mid)
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
# ПАРСИНГ ТЕКСТА О ТЕКУЩЕЙ РАБОТЕ
# ============================================================
def parse_message(text):
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
# ФУНКЦИЯ АВТОУДАЛЕНИЯ КОМАНД
# ============================================================
async def auto_delete(msg: Message, delay: int = 60):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except:
        pass

# ============================================================
# ОБРАБОТКА РАБОЧЕГО СООБЩЕНИЯ
# ============================================================
async def process_work_message(message: Message):
    text = message.text or message.caption or ""
    if not text:
        return
    if text.startswith('/'):
        return
    if re.match(r'^\d{1,2}:\d{2}\s*', text):
        return

    shift = await get_active_shift(message.from_user.id)
    if not shift:
        return

    await delete_actions_by_message(message.from_user.id, message.message_id)

    actions = parse_message(text)
    logger.info(f"Распаршено (msg={message.message_id}): {actions}")

    for action in actions:
        await add_action(
            message.from_user.id,
            shift['id'],
            message.message_id,
            action['action_type'],
            action.get('bike_codes', []),
            action.get('quantity', 0)
        )
        logger.info(f"Записано: {shift['full_name']} — {action}")

# ============================================================
# ЧАТ 1 — НОВЫЕ РАБОЧИЕ СООБЩЕНИЯ
# ============================================================
@work_router.message(F.chat.id == GROUP_ID)
async def work_chat(message: Message):
    if message.message_thread_id == CHAT2_THREAD_ID:
        return
    await process_work_message(message)

# ============================================================
# ЧАТ 1 — РЕДАКТИРОВАННЫЕ РАБОЧИЕ СООБЩЕНИЯ
# ============================================================
@work_router.edited_message(F.chat.id == GROUP_ID)
async def work_chat_edit(message: Message):
    if message.message_thread_id == CHAT2_THREAD_ID:
        return
    logger.info(f"СООБЩЕНИЕ ОТРЕДАКТИРОВАНО: {message.message_id}")
    await process_work_message(message)

# ============================================================
# ЧАТ 2 — УПРАВЛЕНИЕ СМЕНАМИ И ОТЧЕТАМИ
# ============================================================
@cmd_router.message(F.chat.id == GROUP_ID, F.message_thread_id == CHAT2_THREAD_ID)
async def cmd_chat(message: Message):
    user_id = message.from_user.id
    user = await get_user(user_id)
    full_name = user['full_name'] if user else message.from_user.full_name
    role = user['role'] if user else ""
    text = (message.text or message.caption or "").strip()

    # Игнорируем сообщения чарджеров
    if "чарджер" in text.lower():
        return

    # /help
    if text == "/help":
        try:
            await message.delete()
        except:
            pass
        msg = await message.answer(
            "BibiBike - команды:\n\n"
            "Начать смену:\n/09:00 фмр\n\n"
            "Закончить смену:\n/18:00\n/18:00 Комментарий\n\n"
            "Установить имя и роль:\n/setname Фамилия И.О. скаут\n\n"
            "Исправить последний отчёт (5 цифр: перем. поправ. рем. в_СЦ из_СЦ):\n"
            "/fix 11 5 1 2 0 Комментарий\n\n"
            "Статус: /status"
        )
        asyncio.create_task(auto_delete(msg))
        return

    # /status
    if text == "/status":
        try:
            await message.delete()
        except:
            pass
        shift = await get_active_shift(user_id)
        if shift:
            role_emoji = ""
            if shift.get('role') == "Скаут":
                role_emoji = " 🚶"
            elif shift.get('role') == "Водитель":
                role_emoji = " 🚚"
            role_text = f" | {shift['role']}{role_emoji}" if shift.get('role') else ""

            msg = await message.answer(
                f"{full_name}{role_text}\n"
                f"Активная смена с {shift['start_time']}\n"
                f"Район: {shift['district'].upper()}"
            )
        else:
            msg = await message.answer("Нет активной смены.")
        asyncio.create_task(auto_delete(msg))
        return

    # /fix [move] [fix] [repair] [to_sc] [from_sc] Комментарий
    if text.startswith("/fix"):
        try:
            await message.delete()
        except:
            pass

        shift = await get_active_shift(user_id)
        if shift:
            msg = await message.answer("У вас активная смена. Завершите её сначала.")
            asyncio.create_task(auto_delete(msg))
            return

        last_shift = await get_last_shift(user_id)
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

        new_comment = " ".join(args[5:]) if len(args) > 5 else last_shift.get('comment', '')

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM actions WHERE shift_id = ?", (last_shift['id'],))

            if new_move > 0:
                await db.execute(
                    "INSERT INTO actions (user_id, shift_id, message_id, action_type, bike_codes, quantity) VALUES (?, ?, 0, 'move', '', ?)",
                    (user_id, last_shift['id'], new_move)
                )
            if new_fix > 0:
                await db.execute(
                    "INSERT INTO actions (user_id, shift_id, message_id, action_type, bike_codes, quantity) VALUES (?, ?, 0, 'fix', '', ?)",
                    (user_id, last_shift['id'], new_fix)
                )
            if new_repair > 0:
                await db.execute(
                    "INSERT INTO actions (user_id, shift_id, message_id, action_type, bike_codes, quantity) VALUES (?, ?, 0, 'repair', '', ?)",
                    (user_id, last_shift['id'], new_repair)
                )
            if new_to_sc > 0:
                await db.execute(
                    "INSERT INTO actions (user_id, shift_id, message_id, action_type, bike_codes, quantity) VALUES (?, ?, 0, 'to_sc', '', ?)",
                    (user_id, last_shift['id'], new_to_sc)
                )
            if new_from_sc > 0:
                await db.execute(
                    "INSERT INTO actions (user_id, shift_id, message_id, action_type, bike_codes, quantity) VALUES (?, ?, 0, 'from_sc', '', ?)",
                    (user_id, last_shift['id'], new_from_sc)
                )

            await db.execute("UPDATE shifts SET comment = ? WHERE id = ?", (new_comment, last_shift['id']))
            await db.commit()

        stats = await get_stats(last_shift['id'])

        sp = last_shift['start_time'].split(':')
        ep = last_shift['end_time'].split(':')
        sm = int(sp[0]) * 60 + int(sp[1])
        em = int(ep[0]) * 60 + int(ep[1])
        if em < sm:
            em += 24 * 60
        diff = em - sm
        duration = f"{diff // 60} ч. {diff % 60} мин."

        role_emoji = ""
        if last_shift.get('role') == "Скаут":
            role_emoji = " 🚶"
        elif last_shift.get('role') == "Водитель":
            role_emoji = " 🚚"
        role_text = f" | {last_shift['role']}{role_emoji}" if last_shift.get('role') else ""

        report = (
            f"{full_name}{role_text}\n"
            f"Начал: {last_shift['start_time']}\n"
            f"Закончил: {last_shift['end_time']}\n"
            f"Отработано: {duration}\n"
            f"Район: {last_shift['district'].upper()}\n\n"
            f"Статистика за смену (ОБНОВЛЕНА):\n"
        )

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

        if new_comment:
            report += f"\nКомментарий: {new_comment}"

        await message.answer(report)
        logger.info(f"Отчёт полностью пересчитан: {full_name}")
        return

    # /setname ...
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
                else:
                    msg = await message.answer("Укажите роль: скаут или водитель\nПример: /setname Иванов И.И. скаут")
                    asyncio.create_task(auto_delete(msg))
                    return
                new_name = " ".join(args[:-1])
                await add_user(user_id, new_name, new_role)
                msg = await message.answer(f"Сохранено: {new_name} | {new_role}")
            else:
                msg = await message.answer("Формат: /setname Фамилия И.О. роль\nПример: /setname Иванов И.И. скаут")
        else:
            msg = await message.answer("Формат: /setname Фамилия И.О. роль\nПример: /setname Иванов И.И. скаут")
        asyncio.create_task(auto_delete(msg))
        return

    # Обработка команд времени (Начало / Конец смены)
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

        time_str = time_match.group(1)
        extra = time_match.group(2).strip()

        if not active_shift:
            # НАЧАЛО СМЕНЫ
            district = extra.split()[0].lower() if extra else ""
            if district and district in DISTRICTS:
                role_for_shift = role if role else ""
                await start_shift(user_id, full_name, role_for_shift, time_str, district)

                role_emoji = ""
                if role == "Скаут":
                    role_emoji = " 🚶"
                elif role == "Водитель":
                    role_emoji = " 🚚"

                role_text = f" | {role}{role_emoji}" if role else ""

                # Изменено форматирование: только фраза "Смена начата" и ФИО будут жирными
                msg = await message.answer(
                    f"🟢 <b>Смена начата: {full_name}</b>{role_text}\n"
                    f"Начал: {time_str}\n"
                    f"Район: {district.upper()}",
                    parse_mode="HTML"
                )
                logger.info(f"Смена начата: {full_name}, {time_str}, {district}")
                return
            else:
                msg = await message.answer(
                    f"Укажите район.\nФормат: /09:00 фмр\nДоступны: {', '.join(DISTRICTS)}"
                )
                asyncio.create_task(auto_delete(msg))
                return

        else:
            # КОНЕЦ СМЕНЫ
            comment = extra if extra else ""
            sid = await end_shift(user_id, time_str, comment)
            if not sid:
                msg = await message.answer("Ошибка завершения смены.")
                asyncio.create_task(auto_delete(msg))
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

            role_emoji = ""
            if active_shift.get('role') == "Скаут":
                role_emoji = " 🚶"
            elif active_shift.get('role') == "Водитель":
                role_emoji = " 🚚"
            role_text = f" | {active_shift['role']}{role_emoji}" if active_shift.get('role') else ""

            report = (
                f"{full_name}{role_text}\n"
                f"Начал: {active_shift['start_time']}\n"
                f"Закончил: {time_str}\n"
                f"Отработано: {duration}\n"
                f"Район: {active_shift['district'].upper()}\n\n"
                f"Статистика за смену:\n"
            )

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

            await message.answer(report)
            logger.info(f"Смена завершена: {full_name}, {duration}")
            return

    return

# ============================================================
# ЗАПУСК БОТА
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
