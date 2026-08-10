"""Проверка напоминания и идемпотентной автоматической смены CRM."""
import asyncio
import importlib.util
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory(prefix="bibibike-auto-")
os.environ["DATA_DIR"] = TMP.name
os.environ["TOKEN"] = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
spec = importlib.util.spec_from_file_location("bibibike_auto_test", ROOT / "main.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)


async def run():
    await bot.init_db()
    city = bot.get_default_city()
    now = datetime.now(bot._city_tz(city)).replace(second=0, microsecond=0)
    start = now - timedelta(minutes=1)
    end = now + timedelta(hours=2)
    reminder_start = now + timedelta(minutes=20)
    reminder_end = reminder_start + timedelta(hours=10)
    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        await db.execute(
            "INSERT INTO users (user_id,full_name,role,city_id,telegram_username) VALUES (?,?,?,?,?)",
            (990001, "Авто Тест", "Скаут", city["id"], "auto_test"),
        )
        cur = await db.execute(
            "INSERT INTO crm_planned_shifts (city_id,work_date,start_time,end_time,user_id,role,"
            "district,note,status,created_by,created_at,updated_by,updated_at) "
            "VALUES (?,?,?,?,?,NULL,?,?,'scheduled',?,?,?,?)",
            (city["id"], start.date().isoformat(), start.strftime("%H:%M"), end.strftime("%H:%M"),
             990001, "Центр", "Автотест", 1, now.isoformat(), 1, now.isoformat()),
        )
        auto_plan_id = cur.lastrowid
        cur = await db.execute(
            "INSERT INTO crm_planned_shifts (city_id,work_date,start_time,end_time,user_id,role,"
            "district,note,status,created_by,created_at,updated_by,updated_at) "
            "VALUES (?,?,?,?,?,NULL,?,?,'scheduled',?,?,?,?)",
            (city["id"], reminder_start.date().isoformat(), reminder_start.strftime("%H:%M"),
             reminder_end.strftime("%H:%M"), 990001, "ФМР", "Напоминание", 1,
             now.isoformat(), 1, now.isoformat()),
        )
        reminder_plan_id = cur.lastrowid
        await db.commit()

    with patch.object(bot, "safe_flush_report_update", AsyncMock(return_value=True)):
        await bot.process_planned_shifts_once()
        await bot.process_planned_shifts_once()

    class UpdateRequest:
        def __init__(self, body, plan_id=auto_plan_id):
            self.body = body
            self.match_info = {"plan_id": str(plan_id)}
        async def json(self):
            return self.body

    with patch.object(bot, "_auth_user", AsyncMock(return_value={"id": 990001})):
        extension = await bot.api_employee_planned_shift_update(UpdateRequest({
            "end_time": (now + timedelta(hours=3)).strftime("%H:%M")
        }))
    assert extension.status == 200

    with patch.object(bot, "_auth_user", AsyncMock(return_value={"id": 990001})):
        district_update = await bot.api_employee_planned_shift_update(
            UpdateRequest({"district": "Новый район"})
        )
    assert district_update.status == 400, district_update.text

    with patch.object(bot, "_auth_user", AsyncMock(return_value={"id": 990001})):
        settings_update = await bot.api_settings(UpdateRequest({
            "edit_mode": True, "photo_parse": True,
        }))
    assert settings_update.status == 200

    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        shift_count = (await (await db.execute(
            "SELECT COUNT(*) FROM shifts WHERE user_id=990001 AND source='crm_plan'"
        )).fetchone())[0]
        plan = await (await db.execute(
            "SELECT actual_shift_id,auto_started_at,district FROM crm_planned_shifts WHERE id=?",
            (auto_plan_id,),
        )).fetchone()
        reminder = await (await db.execute(
            "SELECT reminder_sent_at FROM crm_planned_shifts WHERE id=?", (reminder_plan_id,)
        )).fetchone()
        reminder_rows = (await (await db.execute(
            "SELECT COUNT(*) FROM crm_notification_outbox WHERE kind='shift_reminder' AND entity_id=?",
            (reminder_plan_id,),
        )).fetchone())[0]
        extended_deadline = await (await db.execute(
            "SELECT auto_close_at,district FROM shifts WHERE id=?", (plan[0],)
        )).fetchone()
        settings = await (await db.execute(
            "SELECT edit_mode,photo_parse FROM users WHERE user_id=990001"
        )).fetchone()
    assert shift_count == 1 and plan[0] and plan[1]
    assert plan[2] == "Центр" and extended_deadline[1] == "Центр"
    assert tuple(settings) == (1, 1)
    assert reminder[0] and reminder_rows == 1
    assert datetime.fromisoformat(extended_deadline[0]).astimezone(bot._city_tz(city)).strftime("%H:%M") == (now + timedelta(hours=3)).strftime("%H:%M")
    sample = bot._crm_notification_text("shift_reminder", {
        "work_date": reminder_start.date().isoformat(), "start_time": "09:00",
        "end_time": "19:00", "district": "ФМР", "description": "Стянуть байки по карте",
    })
    assert "30 МИНУТ" in sample and "📍 Район: ФМР" in sample
    assert "🚀 В 09:00" in sample and "🏁 В 19:00" in sample
    print("PASS CRM automation: reminder, auto-start, active settings and admin-only district")


try:
    asyncio.run(run())
finally:
    asyncio.run(bot.bot.session.close())
    TMP.cleanup()
