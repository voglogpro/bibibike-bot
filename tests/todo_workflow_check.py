"""Focused operational checks for the employee To-Do workflow.

The test calls HTTP handlers directly against a temporary SQLite database.  It
is intentionally separate from the broader CRM suite so permissions and task
lifecycle regressions are quick to diagnose.
"""
import asyncio
import hashlib
import hmac
import importlib.util
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode


ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory(prefix="bibibike-todo-")
os.environ["DATA_DIR"] = TMP.name
os.environ["TOKEN"] = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
os.environ["ADMIN_PASSWORD"] = "test-admin-password"

spec = importlib.util.spec_from_file_location("bibibike_todo_test", ROOT / "main.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)


def init_data(uid):
    values = {
        "auth_date": str(int(datetime.now(timezone.utc).timestamp())),
        "query_id": f"todo-test-{uid}",
        "user": json.dumps({"id": uid, "first_name": f"User {uid}"}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", bot.BOT_TOKEN.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


class Request:
    def __init__(self, uid, *, query=None, body=None, match=None, method="GET"):
        self.method = method
        self.query = query or {}
        self._body = body
        self.match_info = match or {}
        self.headers = {"Authorization": "tma " + init_data(uid)}
        self.content_type = "application/json"

    async def json(self):
        return self._body


def payload(response):
    return json.loads(response.text)


def create_body(assignee_user_id, request_id, title="Проверить парковку"):
    return {
        "title": title,
        "description": "Проверить состояние велосипедов и оставить результат.",
        "assignee_user_id": assignee_user_id,
        "due_date": datetime.now().date().isoformat(),
        "due_time": "19:00",
        "priority": "normal",
        "client_request_id": request_id,
    }


async def scalar(sql, params=()):
    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        return (await (await db.execute(sql, params)).fetchone())[0]


async def run():
    await bot.init_db()
    city = bot.get_default_city()
    other_city = next(item for item in bot.CITIES_BY_ID.values() if item["id"] != city["id"])
    users = {
        "author": 970001,
        "colleague": 970002,
        "unnamed": 970003,
        "foreign": 970004,
        "observer": 970005,
    }
    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        await db.executemany(
            "INSERT INTO users (user_id,full_name,role,city_id) VALUES (?,?,?,?)",
            [
                (users["author"], "Анна Орлова", "Скаут", city["id"]),
                (users["colleague"], "Иван Петров", "Скаут", city["id"]),
                (users["unnamed"], "Сотрудник", "Скаут", city["id"]),
                (users["foreign"], "Мария Соколова", "Скаут", other_city["id"]),
                (users["observer"], "Олег Волков", "Скаут", city["id"]),
            ],
        )
        await db.commit()

    # Directory exposes only named profiles from the caller's city.
    directory = await bot.api_employee_task_directory(Request(users["author"]))
    assert directory.status == 200
    directory_ids = {item["user_id"] for item in payload(directory)["items"]}
    assert users["author"] in directory_ids and users["colleague"] in directory_ids
    assert users["unnamed"] not in directory_ids
    assert users["foreign"] not in directory_ids

    # Any employee can create a task for themselves or a named colleague.
    self_created = await bot.api_employee_task_create(Request(
        users["author"], method="POST",
        body=create_body(users["author"], "todo-self-1", "Проверить свою зону"),
    ))
    assert self_created.status == 201
    self_id = payload(self_created)["task"]["task_id"]
    self_started = await bot.api_employee_task_progress(Request(
        users["author"], method="POST", body={"status": "in_progress"},
        match={"task_id": str(self_id)},
    ))
    self_submitted = await bot.api_employee_task_progress(Request(
        users["author"], method="POST", body={"status": "submitted"},
        match={"task_id": str(self_id)},
    ))
    assert self_started.status == 200
    assert payload(self_submitted)["status"] == "accepted"

    colleague_body = create_body(users["colleague"], "todo-colleague-1")
    colleague_created = await bot.api_employee_task_create(Request(
        users["author"], method="POST", body=colleague_body,
    ))
    assert colleague_created.status == 201
    task_id = payload(colleague_created)["task"]["task_id"]

    # Retries are idempotent; reusing the key for different content is rejected.
    replay = await bot.api_employee_task_create(Request(
        users["author"], method="POST", body=colleague_body,
    ))
    assert replay.status == 200
    assert payload(replay)["already_created"] is True
    assert payload(replay)["task"]["task_id"] == task_id
    conflict = await bot.api_employee_task_create(Request(
        users["author"], method="POST",
        body={**colleague_body, "title": "Другое поручение"},
    ))
    assert conflict.status == 409
    assert await scalar("SELECT COUNT(*) FROM crm_tasks WHERE client_request_id=?",
                        ("todo-colleague-1",)) == 1
    assert await scalar(
        "SELECT COUNT(*) FROM crm_notification_outbox "
        "WHERE kind='task_assigned' AND entity_id=? AND user_id=?",
        (task_id, users["colleague"]),
    ) == 1

    # Cross-city and unnamed placeholder profiles can never be assignees.
    foreign = await bot.api_employee_task_create(Request(
        users["author"], method="POST",
        body=create_body(users["foreign"], "todo-foreign-1"),
    ))
    unnamed = await bot.api_employee_task_create(Request(
        users["author"], method="POST",
        body=create_body(users["unnamed"], "todo-unnamed-1"),
    ))
    assert foreign.status == 403 and unnamed.status == 403

    # The author sees the outbox, the assignee sees the inbox, bystanders see neither.
    author_outbox = await bot.api_employee_tasks_mine(Request(
        users["author"], query={"scope": "outbox"},
    ))
    assignee_inbox = await bot.api_employee_tasks_mine(Request(
        users["colleague"], query={"scope": "inbox"},
    ))
    observer_inbox = await bot.api_employee_tasks_mine(Request(
        users["observer"], query={"scope": "inbox"},
    ))
    assert task_id in {item["task_id"] for item in payload(author_outbox)["items"]}
    assert task_id in {item["task_id"] for item in payload(assignee_inbox)["items"]}
    assert task_id not in {item["task_id"] for item in payload(observer_inbox)["items"]}

    # Only the assignee may progress the task.
    unauthorized_progress = await bot.api_employee_task_progress(Request(
        users["observer"], method="POST", body={"status": "in_progress"},
        match={"task_id": str(task_id)},
    ))
    assert unauthorized_progress.status == 404
    started = await bot.api_employee_task_progress(Request(
        users["colleague"], method="POST", body={"status": "in_progress"},
        match={"task_id": str(task_id)},
    ))
    submitted = await bot.api_employee_task_progress(Request(
        users["colleague"], method="POST",
        body={"status": "submitted", "comment": "Готово"},
        match={"task_id": str(task_id)},
    ))
    assert started.status == 200 and submitted.status == 200
    blocked_created = await bot.api_employee_task_create(Request(
        users["author"], method="POST",
        body=create_body(users["observer"], "todo-blocked-1", "Задача с проблемой"),
    ))
    blocked_id = payload(blocked_created)["task"]["task_id"]
    await bot.api_employee_task_progress(Request(
        users["observer"], method="POST", body={"status": "in_progress"},
        match={"task_id": str(blocked_id)},
    ))
    blocked = await bot.api_employee_task_progress(Request(
        users["observer"], method="POST",
        body={"status": "blocked", "comment": "Нет доступа к парковке"},
        match={"task_id": str(blocked_id)},
    ))
    assert blocked.status == 200
    assert await scalar(
        "SELECT COUNT(*) FROM crm_notification_outbox WHERE kind='task_blocked' "
        "AND user_id=?", (users["author"],),
    ) == 1

    # Review belongs exclusively to the original author.
    unauthorized_review = await bot.api_employee_task_review(Request(
        users["observer"], method="POST", body={"status": "accepted"},
        match={"task_id": str(task_id), "user_id": str(users["colleague"])},
    ))
    assert unauthorized_review.status == 404
    accepted = await bot.api_employee_task_review(Request(
        users["author"], method="POST", body={"status": "accepted", "comment": "Принято"},
        match={"task_id": str(task_id), "user_id": str(users["colleague"])},
    ))
    assert accepted.status == 200
    completed_cancel = await bot.api_employee_task_cancel(Request(
        users["author"], method="POST", match={"task_id": str(task_id)},
    ))
    assert completed_cancel.status == 409 and payload(completed_cancel)["error"] == "task_completed"

    # Cancel is a soft, retry-safe author action; assignees cannot cancel it.
    cancel_created = await bot.api_employee_task_create(Request(
        users["author"], method="POST",
        body=create_body(users["colleague"], "todo-cancel-1", "Отменяемая задача"),
    ))
    cancel_id = payload(cancel_created)["task"]["task_id"]
    denied_cancel = await bot.api_employee_task_cancel(Request(
        users["colleague"], method="POST", match={"task_id": str(cancel_id)},
    ))
    cancelled = await bot.api_employee_task_cancel(Request(
        users["author"], method="POST", body={"reason": "Планы изменились"},
        match={"task_id": str(cancel_id)},
    ))
    cancel_replay = await bot.api_employee_task_cancel(Request(
        users["author"], method="POST", match={"task_id": str(cancel_id)},
    ))
    assert denied_cancel.status == 404
    assert cancelled.status == 200 and payload(cancelled)["already_cancelled"] is False
    assert cancel_replay.status == 200 and payload(cancel_replay)["already_cancelled"] is True
    assert await scalar("SELECT status FROM crm_tasks WHERE id=?", (cancel_id,)) == "cancelled"

    # Незавершённые задачи автоматически уходят из основной ленты через 48 часов,
    # но остаются доступными в истории и больше не могут менять состояние.
    expired_created = await bot.api_employee_task_create(Request(
        users["author"], method="POST",
        body=create_body(users["colleague"], "todo-expired-1", "Старая задача"),
    ))
    expired_id = payload(expired_created)["task"]["task_id"]
    old_iso = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        await db.execute(
            "UPDATE crm_tasks SET published_at=?,created_at=? WHERE id=?",
            (old_iso, old_iso, expired_id),
        )
        await db.commit()
    assert await bot.archive_expired_tasks_once() == 1
    assert await scalar(
        "SELECT COUNT(*) FROM crm_tasks WHERE id=? AND archived_at IS NOT NULL "
        "AND archive_reason='expired'", (expired_id,),
    ) == 1
    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        expired_notice = await (await db.execute(
            "SELECT status,last_error FROM crm_notification_outbox "
            "WHERE kind='task_assigned' AND entity_id=?", (expired_id,),
        )).fetchone()
    assert tuple(expired_notice) == ("failed", "task_expired")
    expired_progress = await bot.api_employee_task_progress(Request(
        users["colleague"], method="POST", body={"status": "in_progress"},
        match={"task_id": str(expired_id)},
    ))
    assert expired_progress.status == 404
    expired_comment = await bot.api_employee_task_comment(Request(
        users["colleague"], method="POST", body={"body": "Уже поздно"},
        match={"task_id": str(expired_id)},
    ))
    assert expired_comment.status == 404
    history = await bot.api_employee_tasks_mine(Request(
        users["colleague"], query={"scope": "inbox"},
    ))
    archived_item = next(item for item in payload(history)["items"] if item["task_id"] == expired_id)
    assert archived_item["archived_at"] and archived_item["archive_reason"] == "expired"

    # Lifecycle decisions remain auditable.
    event_types = set()
    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        rows = await (await db.execute(
            "SELECT event_type FROM crm_task_events WHERE task_id IN (?,?)", (task_id, cancel_id)
        )).fetchall()
        event_types = {row[0] for row in rows}
    assert {"task.created.employee", "assignee.progress", "assignee.review.employee",
            "task.cancelled.employee"}.issubset(event_types)

    print("PASS TODO: city scope, named directory, idempotency, outbox, lifecycle, review and cancel RBAC")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    finally:
        TMP.cleanup()
