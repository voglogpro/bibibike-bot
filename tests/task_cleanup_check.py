"""Очистка личных сообщений при отмене задачи и увольнение сотрудника.

Проверяет:
  * отмена задачи сотрудником убирает карточку с фото из личных сообщений;
  * отмена из CRM делает то же и уведомляет исполнителей;
  * недоступное для удаления сообщение не ломает отмену;
  * уволенный сотрудник скрывается, а история смен остаётся;
  * уволить сотрудника с незакрытой сменой нельзя.
"""
import asyncio
import importlib.util
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory(prefix="bibibike-cleanup-")
os.environ["DATA_DIR"] = TMP.name
os.environ["TOKEN"] = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
spec = importlib.util.spec_from_file_location("bibibike_cleanup_test", ROOT / "main.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)

ADMIN_ID = 960001
WORKER_ID = 960002


class Request:
    def __init__(self, body=None, match=None, query=None):
        self.body = body or {}
        self.match_info = match or {}
        self.query = query or {}
        self.headers = {}

    async def json(self):
        return self.body


async def scalar(sql, params=()):
    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        row = await (await db.execute(sql, params)).fetchone()
    return row[0] if row else None


async def seed_task(city, title="Проверить стойку"):
    """Публикует задачу и отмечает, что сотруднику ушло сообщение с фото."""
    now_iso = datetime.now(timezone.utc).isoformat()
    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO crm_tasks (city_id,work_date,title,description,priority,status,created_by,"
            "created_at,updated_by,updated_at,requires_photo,date_from,date_to,district,"
            "completion_mode,created_via) "
            "VALUES (?,?,?,'',?,'published',?,?,?,?,0,?,?,'','manual','crm')",
            (city["id"], "2026-08-24", title, "normal", ADMIN_ID, now_iso, ADMIN_ID, now_iso,
             "2026-08-24", "2026-08-24"),
        )
        task_id = cur.lastrowid
        await db.execute(
            "INSERT INTO crm_task_assignees "
            "(task_id,user_id,full_name_snap,role_snap,status,updated_at) "
            "VALUES (?,?,?,?,'assigned',?)",
            (task_id, WORKER_ID, "Работник И.И.", "Скаут", now_iso),
        )
        # Уведомление о назначении уже доставлено: два сообщения — фото и текст.
        cur = await db.execute(
            "INSERT INTO crm_notification_outbox (city_id,user_id,kind,entity_id,payload_json,"
            "status,attempt_count,next_attempt_at,created_at,sent_at) "
            "VALUES (?,?,'task_assigned',?,'{}','sent',1,?,?,?)",
            (city["id"], WORKER_ID, task_id, now_iso, now_iso, now_iso),
        )
        outbox_id = cur.lastrowid
        delete_after = (datetime.now(timezone.utc) + timedelta(hours=47)).isoformat()
        await db.executemany(
            "INSERT INTO crm_notification_messages "
            "(outbox_id,user_id,message_id,created_at,delete_after) VALUES (?,?,?,?,?)",
            [(outbox_id, WORKER_ID, 5000 + task_id * 10 + i, now_iso, delete_after)
             for i in range(2)],
        )
        await db.commit()
    return task_id


async def run():
    await bot.init_db()
    city = bot.get_default_city()
    now_iso = datetime.now(timezone.utc).isoformat()
    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        await db.executemany(
            "INSERT INTO users (user_id,full_name,role,city_id,telegram_username) "
            "VALUES (?,?,?,?,?)",
            [(ADMIN_ID, "Руководитель Т.Т.", "Скаут", city["id"], "chief_user"),
             (WORKER_ID, "Работник И.И.", "Скаут", city["id"], "worker_user")],
        )
        await db.execute(
            "INSERT INTO admin_accounts (user_id,role,role_scope,is_active,session_version,"
            "created_at,updated_at) VALUES (?,'network_admin',NULL,1,1,?,?)",
            (ADMIN_ID, now_iso, now_iso),
        )
        await db.commit()

    context = {
        "telegram_user": {"id": ADMIN_ID},
        "user": {"full_name": "Руководитель Т.Т."},
        "admin": {"role": "network_admin", "role_scope": None},
        "city": city,
        "allowed_city_ids": sorted(bot.CITIES_BY_ID),
    }

    # 1. Отмена сотрудником убирает сообщения задачи из личного чата.
    task_id = await seed_task(city)
    with patch.object(bot, "_auth_user", AsyncMock(return_value={"id": ADMIN_ID})), \
            patch.object(bot.bot, "delete_message", AsyncMock()) as delete_message:
        response = await bot.api_employee_task_cancel(
            Request({"reason": "Отпала необходимость"}, {"task_id": str(task_id)})
        )
    assert response.status == 200, response.text
    assert delete_message.await_count == 2, delete_message.await_args_list
    assert {call.args[0] for call in delete_message.await_args_list} == {WORKER_ID}
    assert await scalar(
        "SELECT COUNT(*) FROM crm_notification_messages WHERE deleted_at IS NULL"
    ) == 0
    assert await scalar("SELECT status FROM crm_tasks WHERE id=?", (task_id,)) == "cancelled"
    # Исполнителю ушло короткое уведомление об отмене.
    assert await scalar(
        "SELECT COUNT(*) FROM crm_notification_outbox WHERE kind='task_cancelled' AND user_id=?",
        (WORKER_ID,),
    ) == 1

    # 2. Отмена из CRM ведёт себя так же.
    crm_task_id = await seed_task(city, "Задача из CRM")
    with patch.object(bot, "_crm_admin", AsyncMock(return_value=(context, None))), \
            patch.object(bot.bot, "delete_message", AsyncMock()) as delete_message:
        response = await bot.api_crm_task_update(
            Request({"status": "cancelled"}, {"task_id": str(crm_task_id)})
        )
    assert response.status == 200, response.text
    assert delete_message.await_count == 2, delete_message.await_args_list
    assert await scalar("SELECT status FROM crm_tasks WHERE id=?", (crm_task_id,)) == "cancelled"
    assert await scalar(
        "SELECT COUNT(*) FROM crm_notification_outbox WHERE kind='task_cancelled'"
    ) >= 2

    # 3. Telegram отказал в удалении — отмена всё равно проходит.
    stubborn_id = await seed_task(city, "Старая задача")
    with patch.object(bot, "_crm_admin", AsyncMock(return_value=(context, None))), \
            patch.object(bot.bot, "delete_message",
                         AsyncMock(side_effect=RuntimeError("too old"))):
        response = await bot.api_crm_task_update(
            Request({"status": "cancelled"}, {"task_id": str(stubborn_id)})
        )
    assert response.status == 200, response.text
    assert await scalar("SELECT status FROM crm_tasks WHERE id=?", (stubborn_id,)) == "cancelled"

    # 4. Увольнение: сотрудник скрыт, история смен сохранена.
    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        await db.execute(
            "INSERT INTO shifts (user_id,city_id,start_at,end_at,is_active,created_at,district) "
            "VALUES (?,?,?,?,0,?,'Центр')",
            (WORKER_ID, city["id"], now_iso, now_iso, now_iso),
        )
        await db.commit()
    shifts_before = await scalar("SELECT COUNT(*) FROM shifts WHERE user_id=?", (WORKER_ID,))
    with patch.object(bot, "_crm_admin", AsyncMock(return_value=(context, None))):
        response = await bot.api_crm_employee_statistics_visibility(
            Request({"city_id": city["id"], "visible": False}, {"user_id": str(WORKER_ID)})
        )
    assert response.status == 200, response.text
    assert await scalar("SELECT statistics_visible FROM users WHERE user_id=?", (WORKER_ID,)) == 0
    assert await scalar("SELECT statistics_archived_at FROM users WHERE user_id=?", (WORKER_ID,))
    assert await scalar("SELECT COUNT(*) FROM shifts WHERE user_id=?", (WORKER_ID,)) == shifts_before

    # 5. Возврат в команду снимает отметку.
    with patch.object(bot, "_crm_admin", AsyncMock(return_value=(context, None))):
        response = await bot.api_crm_employee_statistics_visibility(
            Request({"city_id": city["id"], "visible": True}, {"user_id": str(WORKER_ID)})
        )
    assert response.status == 200
    assert await scalar("SELECT statistics_visible FROM users WHERE user_id=?", (WORKER_ID,)) == 1
    assert await scalar(
        "SELECT statistics_archived_at FROM users WHERE user_id=?", (WORKER_ID,)
    ) is None

    # 6. Сотрудника с открытой сменой уволить нельзя.
    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        await db.execute(
            "INSERT INTO shifts (user_id,city_id,start_at,is_active,created_at,district) "
            "VALUES (?,?,?,1,?,'Центр')",
            (WORKER_ID, city["id"], now_iso, now_iso),
        )
        await db.commit()
    with patch.object(bot, "_crm_admin", AsyncMock(return_value=(context, None))):
        response = await bot.api_crm_employee_statistics_visibility(
            Request({"city_id": city["id"], "visible": False}, {"user_id": str(WORKER_ID)})
        )
    assert response.status == 409, response.text
    assert await scalar("SELECT statistics_visible FROM users WHERE user_id=?", (WORKER_ID,)) == 1

    print("PASS task cleanup: cancel clears DMs, dismissal keeps history and guards open shifts")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    finally:
        asyncio.run(bot.bot.session.close())
        TMP.cleanup()
