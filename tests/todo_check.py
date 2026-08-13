"""Focused backend checks for employee-created To-Do tasks."""
import asyncio
import hashlib
import hmac
import importlib.util
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode


ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory(prefix="bibibike-todo-")
os.environ["DATA_DIR"] = TMP.name
os.environ["TOKEN"] = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
spec = importlib.util.spec_from_file_location("bibibike_todo_test", ROOT / "main.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)


def init_data(uid):
    values = {
        "auth_date": str(int(datetime.now(timezone.utc).timestamp())),
        "query_id": f"todo-{uid}",
        "user": json.dumps({"id": uid, "first_name": f"User {uid}"}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", bot.BOT_TOKEN.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


class Request:
    def __init__(self, uid, query=None, body=None, match=None, method="GET"):
        self.method = method
        self.query = query or {}
        self._body = body
        self.match_info = match or {}
        self.headers = {"Authorization": "tma " + init_data(uid)}

    async def json(self):
        return self._body


def payload(response):
    return json.loads(response.text)


async def run():
    await bot.init_db()
    city = bot.get_default_city()
    other = next(item for item in bot.CITIES_BY_ID.values() if item["id"] != city["id"])
    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        await db.executemany(
            "INSERT INTO users (user_id,full_name,role,city_id) VALUES (?,?,?,?)",
            [(111, "Task Author", "Скаут", city["id"]),
             (222, "Task Worker", "Водитель", city["id"]),
             (333, "Other City", "Скаут", other["id"]),
             (444, "Сотрудник #444", "Скаут", city["id"])],
        )
        await db.commit()

    directory = payload(await bot.api_employee_task_directory(Request(111)))
    assert {item["user_id"] for item in directory["items"]} == {111, 222}

    today = datetime.now(bot._city_tz(city)).date().isoformat()
    body = {"title": "Check parking", "description": "Take a photo", "priority": "high",
            "due_date": today, "due_time": "18:30", "assignee_user_ids": [222],
            "client_request_id": "todo-one"}
    created_response = await bot.api_employee_task_create(Request(111, body=body, method="POST"))
    created = payload(created_response)
    assert created_response.status == 201, created
    task_id = created["task"]["task_id"]
    assert created["task"]["due_time"] == "18:30"
    assert created["task"]["created_by"] == 111

    replay = payload(await bot.api_employee_task_create(Request(111, body=body, method="POST")))
    assert replay["already_created"] is True and replay["task"]["task_id"] == task_id
    conflict_body = dict(body, title="Changed")
    conflict = await bot.api_employee_task_create(Request(111, body=conflict_body, method="POST"))
    assert conflict.status == 409 and payload(conflict)["error"] == "idempotency_conflict"
    foreign = await bot.api_employee_task_create(Request(
        111, body=dict(body, assignee_user_ids=[333], client_request_id="todo-foreign"), method="POST"))
    assert foreign.status == 403

    inbox = payload(await bot.api_employee_tasks_mine(Request(222, query={"scope": "inbox"})))
    outbox = payload(await bot.api_employee_tasks_mine(Request(111, query={"scope": "outbox"})))
    assert inbox["items"][0]["creator_name"] == "Task Author"
    assert outbox["items"][0]["is_creator"] is True

    await bot.api_employee_task_progress(Request(
        222, body={"status": "in_progress"}, match={"task_id": str(task_id)}, method="POST"))
    await bot.api_employee_task_progress(Request(
        222, body={"status": "submitted", "comment": "Done"},
        match={"task_id": str(task_id)}, method="POST"))
    denied_review = await bot.api_employee_task_review(Request(
        222, body={"status": "accepted"},
        match={"task_id": str(task_id), "user_id": "222"}, method="POST"))
    assert denied_review.status == 404
    reviewed = await bot.api_employee_task_review(Request(
        111, body={"status": "accepted"},
        match={"task_id": str(task_id), "user_id": "222"}, method="POST"))
    assert reviewed.status == 200

    cancel_body = dict(body, title="Cancel me", client_request_id="todo-cancel")
    cancel_created = payload(await bot.api_employee_task_create(
        Request(111, body=cancel_body, method="POST")))
    cancel_id = cancel_created["task"]["task_id"]
    denied_cancel = await bot.api_employee_task_cancel(Request(
        222, match={"task_id": str(cancel_id)}, method="POST"))
    assert denied_cancel.status == 404
    cancelled = await bot.api_employee_task_cancel(Request(
        111, body={"reason": "Больше не актуально"},
        match={"task_id": str(cancel_id)}, method="POST"))
    assert cancelled.status == 200

    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        tasks = (await (await db.execute("SELECT COUNT(*) FROM crm_tasks")).fetchone())[0]
        kinds = {row[0] for row in await (await db.execute(
            "SELECT kind FROM crm_notification_outbox")).fetchall()}
    assert tasks == 2
    assert {"task_assigned", "task_submitted", "task_reviewed", "task_cancelled"} <= kinds
    print("PASS TODO: directory, same-city scope, create/idempotency, inbox/outbox, review, cancel")


try:
    asyncio.run(run())
finally:
    asyncio.run(bot.bot.session.close())
    TMP.cleanup()
