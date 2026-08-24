"""Уведомления об изменениях графика и ответы автору задачи в личку.

Проверяет две вещи:
  * правка плановой смены шлёт сотруднику адресное уведомление о том, что
    именно изменилось (район, время, перенос, отмена), а не весь график;
  * правки одной смены за короткое окно склеиваются в одно сообщение;
  * бот отвечает автору задачи из рабочего чата в личку, а не в чат.
"""
import asyncio
import importlib.util
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory(prefix="bibibike-notify-")
os.environ["DATA_DIR"] = TMP.name
os.environ["TOKEN"] = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
spec = importlib.util.spec_from_file_location("bibibike_notify_test", ROOT / "main.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)

ADMIN_ID = 970001
WORKER_ID = 970002


class Request:
    def __init__(self, body, plan_id):
        self.body = body
        self.match_info = {"plan_id": str(plan_id)}
        self.query = {}
        self.headers = {}

    async def json(self):
        return self.body


async def outbox(kind="shift_plan_changed"):
    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        db.row_factory = bot.aiosqlite.Row
        rows = await (await db.execute(
            "SELECT * FROM crm_notification_outbox WHERE kind=? ORDER BY id", (kind,)
        )).fetchall()
    return [dict(row) for row in rows]


async def run():
    await bot.init_db()
    city = bot.get_default_city()
    now = datetime.now(timezone.utc)
    work_date = (datetime.now(bot._city_tz(city)) + timedelta(days=1)).date().isoformat()

    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        await db.executemany(
            "INSERT INTO users (user_id,full_name,role,city_id,telegram_username) VALUES (?,?,?,?,?)",
            [(ADMIN_ID, "Руководитель Т.Т.", "Скаут", city["id"], "chief_user"),
             (WORKER_ID, "Работник И.И.", "Скаут", city["id"], "worker_user")],
        )
        await db.execute(
            "INSERT INTO admin_accounts (user_id,role,role_scope,is_active,session_version,"
            "created_at,updated_at) VALUES (?,'network_admin',NULL,1,1,?,?)",
            (ADMIN_ID, now.isoformat(), now.isoformat()),
        )
        cur = await db.execute(
            "INSERT INTO crm_planned_shifts (city_id,work_date,start_time,end_time,user_id,role,"
            "district,note,work_kind,status,created_by,created_at,updated_by,updated_at) "
            "VALUES (?,?,?,?,?,NULL,?,'','regular','scheduled',?,?,?,?)",
            (city["id"], work_date, "09:00", "18:00", WORKER_ID, "Центральный",
             ADMIN_ID, now.isoformat(), ADMIN_ID, now.isoformat()),
        )
        plan_id = cur.lastrowid
        await db.commit()

    context = {
        "telegram_user": {"id": ADMIN_ID},
        "user": {"full_name": "Руководитель Т.Т."},
        "admin": {"role": "network_admin", "role_scope": None},
        "city": city,
        "allowed_city_ids": sorted(bot.CITIES_BY_ID),
    }

    async def update(body):
        with patch.object(bot, "_crm_admin", AsyncMock(return_value=(context, None))), \
                patch.object(bot, "safe_flush_report_update", AsyncMock(return_value=True)):
            response = await bot.api_crm_planned_shift_update(Request(body, plan_id))
        assert response.status == 200, response.text
        return response

    # 1. Смена района — сотруднику уходит адресное уведомление.
    await update({"district": "Фестивальный"})
    rows = await outbox()
    assert len(rows) == 1, rows
    payload = json.loads(rows[0]["payload_json"])
    assert payload["changes"] == ["district"], payload
    assert rows[0]["user_id"] == WORKER_ID
    text = bot._crm_notification_text("shift_plan_changed", payload)
    assert "Изменён район" in text, text
    assert "Фестивальный" in text, text
    # Полный график в сообщение не попадает.
    assert "График обновлён" not in text

    # 2. Правка времени в том же окне склеивается с первой: одно сообщение.
    await update({"start_time": "10:00", "end_time": "19:00"})
    rows = await outbox()
    assert len(rows) == 1, f"правки должны склеиться, а не плодить сообщения: {rows}"
    payload = json.loads(rows[0]["payload_json"])
    assert payload["changes"] == ["district", "time"], payload
    text = bot._crm_notification_text("shift_plan_changed", payload)
    assert "Смена изменена" in text, text
    assert "10:00–19:00" in text, text
    assert "Фестивальный" in text, text

    # 3. Уведомление уже отправлено — новая правка снова ставит его в очередь.
    # Схема допускает одну строку на смену (UNIQUE user_id+kind+entity_id),
    # поэтому запись переиспользуется, а не дублируется.
    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        await db.execute("UPDATE crm_notification_outbox SET status='sent',sent_at=?",
                         (datetime.now(timezone.utc).isoformat(),))
        await db.commit()
    await update({"district": "Центральный"})
    rows = await outbox()
    assert len(rows) == 1, rows
    assert rows[0]["status"] == "pending", rows[0]
    assert rows[0]["sent_at"] is None, rows[0]
    payload = json.loads(rows[0]["payload_json"])
    assert payload["changes"] == ["district"], payload
    assert payload["district"] == "Центральный", payload

    # 4. Отмена смены — отдельный текст, без предложения выходить.
    await update({"status": "cancelled"})
    rows = await outbox()
    payload = json.loads(rows[-1]["payload_json"])
    assert "cancelled" in payload["changes"], payload
    text = bot._crm_notification_text("shift_plan_changed", payload)
    assert "Смена отменена" in text, text
    assert "Выходить на эту смену не нужно" in text, text

    # 5. Правка без изменений не шлёт ничего.
    before = len(await outbox())
    await update({"district": "Центральный"})
    assert len(await outbox()) == before, "пустая правка не должна слать уведомление"

    # 6. Бот отвечает автору задачи в личку, а не в рабочий чат.
    class Sender:
        id = ADMIN_ID
    with patch.object(bot.bot, "send_message", AsyncMock()) as send_message:
        await bot._task_chat_notify_author(Sender(), "Задача создана")
    send_message.assert_awaited_once()
    assert send_message.await_args.args[0] == ADMIN_ID

    # Если автор не открывал бота, ошибка не должна ронять создание задачи.
    with patch.object(bot.bot, "send_message", AsyncMock(side_effect=RuntimeError("blocked"))):
        await bot._task_chat_notify_author(Sender(), "Задача создана")

    print("PASS schedule notify: targeted change alerts, merge window, author DM")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    finally:
        asyncio.run(bot.bot.session.close())
        TMP.cleanup()
