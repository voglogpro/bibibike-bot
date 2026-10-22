"""Подписка города. Оплату по договорённости подтверждает только владелец.

Никаких списаний и ссылок на стороннюю кассу внутри Telegram. Тариф в рублях,
месяц календарный, состав города фиксируется при продлении без доплат внутри
оплаченного периода. История остаётся доступной после окончания подписки.
"""
import calendar
import math
from datetime import datetime, timedelta, timezone

PRICE_RUB = 500


def utcnow():
    return datetime.now(timezone.utc)


def parse(value):
    if not value:
        return None
    result = datetime.fromisoformat(value)
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result


def add_months(value, months):
    year, month = divmod(value.year * 12 + value.month - 1 + months, 12)
    month += 1
    return value.replace(year=year, month=month,
                         day=min(value.day, calendar.monthrange(year, month)[1]))


async def init_schema(db):
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS city_billing_campaign (
            id INTEGER PRIMARY KEY CHECK(id=1),
            launched_at TEXT NOT NULL,
            deadline_at TEXT NOT NULL,
            launched_by INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS city_billing_access (
            city_id INTEGER PRIMARY KEY,
            access_until TEXT NOT NULL,
            updated_by INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS city_billing_grants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL UNIQUE,
            city_id INTEGER NOT NULL,
            granted_by INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            access_until TEXT NOT NULL,
            months INTEGER NOT NULL,
            employees INTEGER NOT NULL,
            price_rub INTEGER NOT NULL,
            amount_rub INTEGER NOT NULL,
            reason TEXT NOT NULL
        );
    """)


async def status(db, city_id, now=None):
    now = now or utcnow()
    campaign = await (await db.execute(
        "SELECT deadline_at FROM city_billing_campaign WHERE id=1"
    )).fetchone()
    access = await (await db.execute(
        "SELECT access_until FROM city_billing_access WHERE city_id=?", (city_id,)
    )).fetchone()
    employees = (await (await db.execute(
        "SELECT COUNT(*) FROM users WHERE city_id=? AND COALESCE(statistics_visible,1)=1",
        (city_id,),
    )).fetchone())[0]
    deadline = parse(campaign[0]) if campaign else None
    access_until = parse(access[0]) if access else None
    paid = bool(access_until and access_until > now)
    allowed = not deadline or paid or now < deadline
    state = "paid" if paid else "disabled" if not deadline else "grace" if allowed else "expired"
    effective_until = max(filter(None, [deadline, access_until]), default=None)
    result = {
        "city_id": city_id, "state": state, "enabled": bool(campaign),
        "allowed": allowed, "employees": employees, "price_rub": PRICE_RUB,
        "amount_rub": employees * PRICE_RUB, "period": "calendar_month",
        "access_until": access_until.isoformat() if access_until else None,
        "deadline_at": deadline.isoformat() if deadline else None,
        "days_left": max(0, math.ceil((effective_until - now).total_seconds() / 86400))
                     if effective_until else None,
    }
    date_label = effective_until.astimezone(timezone(timedelta(hours=3))).strftime(
        "%d.%m.%Y %H:%M МСК") if effective_until else ""
    if state == "grace":
        message = (f"Бот переходит на подписку. Бесплатная работа до {date_label}. "
                   "Затем открытие новых смен и подсчёт новых действий будут приостановлены. ")
    elif state == "expired":
        message = "Подписка города закончилась. Новые смены и действия приостановлены. "
    elif state == "paid":
        message = f"Город оплачен до {date_label}. "
    else:
        message = "Переход на подписку ещё не запущен. "
    result["message"] = message + (
        f"Тариф: {PRICE_RUB} ₽ за сотрудника в месяц. В городе {employees} чел., "
        f"итого {employees * PRICE_RUB:,} ₽/мес. ".replace(",", " ") +
        "Подписку на город оформляет руководитель по договорённости с владельцем бота. "
        "Доступ подтверждает владелец. История смен и заработка сохранится; "
        "открытую смену можно завершить."
    )
    return result


async def launch(db, owner_id, now=None):
    """INSERT OR IGNORE: повторное нажатие и перезапуск не сдвигают неделю."""
    now = now or utcnow()
    await db.execute(
        "INSERT OR IGNORE INTO city_billing_campaign VALUES (1,?,?,?)",
        (now.isoformat(), (now + timedelta(days=7)).isoformat(), owner_id),
    )
    await db.commit()


async def grant(db, city_id, owner_id, months, request_id, reason, now=None):
    """Атомарное продление, идемпотентность защищает от повторного нажатия."""
    if type(months) is not int or not 1 <= months <= 12:
        raise ValueError("Укажите от 1 до 12 месяцев.")
    if not isinstance(reason, str) or not 3 <= len(reason.strip()) <= 500:
        raise ValueError("Укажите основание выдачи доступа (3–500 символов).")
    if not isinstance(request_id, str) or not 16 <= len(request_id) <= 80:
        raise ValueError("Некорректный идентификатор продления.")
    now = now or utcnow()
    await db.execute("BEGIN IMMEDIATE")
    previous = await (await db.execute(
        "SELECT city_id,months,reason,access_until FROM city_billing_grants WHERE request_id=?",
        (request_id,),
    )).fetchone()
    if previous:
        if (previous[0], previous[1], previous[2]) != (city_id, months, reason.strip()):
            raise ValueError("Этот запрос уже использован для другого продления.")
        await db.rollback()
        return previous[3]
    current = await status(db, city_id, now)
    until = add_months(max(now, parse(current["access_until"]) or now,
                          parse(current["deadline_at"]) or now), months)
    await db.execute(
        "INSERT INTO city_billing_access VALUES (?,?,?,?) ON CONFLICT(city_id) DO UPDATE SET "
        "access_until=excluded.access_until,updated_by=excluded.updated_by,updated_at=excluded.updated_at",
        (city_id, until.isoformat(), owner_id, now.isoformat()),
    )
    await db.execute(
        "INSERT INTO city_billing_grants "
        "(request_id,city_id,granted_by,created_at,access_until,months,employees,price_rub,amount_rub,reason) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (request_id, city_id, owner_id, now.isoformat(), until.isoformat(), months,
         current["employees"], PRICE_RUB, current["amount_rub"] * months, reason.strip()),
    )
    await db.commit()
    return until.isoformat()
