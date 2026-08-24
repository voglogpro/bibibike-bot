"""Regression checks for manager shift/lunch notifications and report dates."""
import asyncio
import importlib.util
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory(prefix="bibibike-lifecycle-")
os.environ["DATA_DIR"] = TMP.name
os.environ["TOKEN"] = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
spec = importlib.util.spec_from_file_location("bibibike_lifecycle_test", ROOT / "main.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)

EMPLOYEE = 980001
SCOUT_MANAGER = 980002
NETWORK_ADMIN = 980003
NO_CITY_MANAGER = 980004
VIEWER = 980005
DRIVER_MANAGER = 980006


class Request:
    def __init__(self, body=None, method="POST"):
        self.body = body or {}
        self.method = method
        self.query = {}
        self.headers = {}
        self.match_info = {}

    async def json(self):
        return self.body


async def rows(sql, params=()):
    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        db.row_factory = bot.aiosqlite.Row
        result = await (await db.execute(sql, params)).fetchall()
    return [dict(row) for row in result]


def admin_context(user_id, city, role="city_manager", role_scope="Скаут"):
    return {
        "telegram_user": {"id": user_id},
        "user": {"full_name": f"Администратор {user_id}"},
        "admin": {"role": role, "role_scope": role_scope},
        "city": city,
        "allowed_city_ids": sorted(bot.CITIES_BY_ID) if role == "network_admin" else [city["id"]],
    }


async def run():
    await bot.init_db()
    city = bot.get_default_city()
    tz = bot._city_tz(city)
    start_at = datetime(2026, 8, 24, 9, 0, tzinfo=tz)
    now_iso = start_at.isoformat()

    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        await db.executemany(
            "INSERT INTO users (user_id,full_name,role,city_id,telegram_username) "
            "VALUES (?,?,?,?,?)",
            [
                (EMPLOYEE, "Скаут Тестовый", "Скаут", city["id"], "test_scout"),
                (SCOUT_MANAGER, "Старший Скаут", "Скаут", city["id"], "scout_manager"),
                (NETWORK_ADMIN, "Админ Сети", "Скаут", city["id"], "network_admin"),
                (NO_CITY_MANAGER, "Чужой Город", "Скаут", city["id"], "no_city"),
                (VIEWER, "Наблюдатель", "Скаут", city["id"], "viewer"),
                (DRIVER_MANAGER, "Старший Водителей", "Водитель", city["id"], "driver_manager"),
            ],
        )
        accounts = [
            (SCOUT_MANAGER, "city_manager", "Скаут"),
            (NETWORK_ADMIN, "network_admin", None),
            (NO_CITY_MANAGER, "city_manager", "Скаут"),
            (VIEWER, "city_viewer", "Скаут"),
            (DRIVER_MANAGER, "city_manager", "Водитель"),
        ]
        await db.executemany(
            "INSERT INTO admin_accounts (user_id,role,role_scope,is_active,session_version,"
            "created_at,updated_at) VALUES (?,?,?,1,1,?,?)",
            [(uid, role, scope, now_iso, now_iso) for uid, role, scope in accounts],
        )
        await db.executemany(
            "INSERT INTO admin_city_permissions (user_id,city_id) VALUES (?,?)",
            [(SCOUT_MANAGER, city["id"]), (VIEWER, city["id"]),
             (DRIVER_MANAGER, city["id"])],
        )
        # The network administrator opts out only from lunch-start alerts.
        await db.execute(
            "INSERT INTO admin_notification_settings "
            "(user_id,notify_shift_end,notify_lunch_start,notify_lunch_end,updated_at) "
            "VALUES (?,1,0,1,?)",
            (NETWORK_ADMIN, now_iso),
        )
        cursor = await db.execute(
            "INSERT INTO shifts (user_id,full_name,role,start_time,start_at,created_at,"
            "is_active,on_lunch,city_id,district,source) VALUES (?,?,?,?,?,?,1,0,?,?,?)",
            (EMPLOYEE, "Скаут Тестовый", "Скаут", "09:00", start_at.isoformat(),
             start_at.isoformat(), city["id"], "Центральный", "mini_app"),
        )
        shift_id = cursor.lastrowid
        await db.commit()

    # Lunch start reaches only the matching city/role manager. The network
    # admin has disabled this exact event; viewers never receive lifecycle DMs.
    with patch.object(bot, "_auth_user", AsyncMock(return_value={"id": EMPLOYEE})), \
            patch.object(bot, "safe_flush_report_update", AsyncMock(return_value=True)):
        response = await bot.api_shift_lunch(Request({"active": True}))
        assert response.status == 200, response.text
        duplicate = await bot.api_shift_lunch(Request({"active": True}))
        assert duplicate.status == 200 and json.loads(duplicate.text)["unchanged"], duplicate.text

    lunch_started = await rows(
        "SELECT user_id,kind,entity_id,payload_json FROM crm_notification_outbox "
        "WHERE kind='admin_lunch_started' ORDER BY user_id"
    )
    assert [row["user_id"] for row in lunch_started] == [SCOUT_MANAGER], lunch_started
    assert len(await rows(
        "SELECT id FROM admin_lifecycle_events WHERE event_type='lunch_started'"
    )) == 1, "a repeated lunch status must not create another event"

    # Lunch end is enabled for the matching manager and the network admin.
    with patch.object(bot, "_auth_user", AsyncMock(return_value={"id": EMPLOYEE})), \
            patch.object(bot, "safe_flush_report_update", AsyncMock(return_value=True)):
        response = await bot.api_shift_lunch(Request({"active": False}))
        assert response.status == 200, response.text
    lunch_ended = await rows(
        "SELECT user_id FROM crm_notification_outbox WHERE kind='admin_lunch_ended' ORDER BY user_id"
    )
    assert [row["user_id"] for row in lunch_ended] == [SCOUT_MANAGER, NETWORK_ADMIN], lunch_ended

    # The city manager disables shift-end alerts through their own CRM settings.
    context = admin_context(SCOUT_MANAGER, city)
    with patch.object(bot, "_crm_admin", AsyncMock(return_value=(context, None))):
        response = await bot.api_crm_notification_settings(
            Request({"notify_shift_end": False}, method="PATCH")
        )
    assert response.status == 200, response.text
    settings = await bot._admin_notification_settings(SCOUT_MANAGER)
    assert settings["notify_shift_end"] is False
    assert settings["notify_lunch_start"] is True

    end_at = datetime(2026, 8, 24, 18, 15, tzinfo=tz)
    closed_id = await bot.end_shift(
        EMPLOYEE, "18:15", city_id=city["id"], now=end_at, end_at_override=end_at
    )
    assert closed_id == shift_id
    shift_ended = await rows(
        "SELECT user_id,payload_json FROM crm_notification_outbox "
        "WHERE kind='admin_shift_ended' ORDER BY user_id"
    )
    assert [row["user_id"] for row in shift_ended] == [NETWORK_ADMIN], shift_ended
    payload = json.loads(shift_ended[0]["payload_json"])
    text = bot._crm_notification_text("admin_shift_ended", payload)
    assert "СОТРУДНИК ЗАВЕРШИЛ СМЕНУ" in text
    assert "24.08.2026" in text and "09:00–18:15" in text, text

    closed_shift = await bot.get_shift_by_id(shift_id)
    report = bot.build_report_text(closed_shift, {
        "move": 0, "fix": 0, "repair": 0, "battery": 0,
        "sticker": 0, "to_sc": 0, "from_sc": 0,
    })
    assert "Дата завершения: 24.08.2026" in report, report

    print("PASS lifecycle notify: scopes, preferences, dedupe, report completion date")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    finally:
        asyncio.run(bot.bot.session.close())
        TMP.cleanup()
