"""Интеграционная проверка CRM без запуска Telegram polling."""
import asyncio
import hashlib
import hmac
import importlib.util
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode


ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory(prefix="bibibike-crm-")
os.environ["DATA_DIR"] = TMP.name
os.environ["TOKEN"] = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
os.environ["ADMIN_PASSWORD"] = "test-admin-password"
os.environ["NETWORK_ADMIN_USER_IDS"] = "900001"

spec = importlib.util.spec_from_file_location("bibibike_crm_test", ROOT / "main.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)


def init_data(uid):
    values = {
        "auth_date": str(int(datetime.now(timezone.utc).timestamp())),
        "query_id": f"test-{uid}",
        "user": json.dumps({"id": uid, "first_name": f"User {uid}"}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", bot.BOT_TOKEN.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


class FileField:
    name = "file"

    def __init__(self, data, filename="result.jpg"):
        self.data = data
        self.filename = filename
        self.offset = 0

    async def read_chunk(self, size=65536):
        chunk = self.data[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk


class Multipart:
    def __init__(self, fields):
        self.fields = list(fields)

    async def next(self):
        return self.fields.pop(0) if self.fields else None


class Request:
    def __init__(self, uid, query=None, body=None, match=None, admin_token=None, files=None):
        self.query = query or {}
        self._body = body
        self.match_info = match or {}
        self.headers = {"Authorization": "tma " + init_data(uid)}
        if admin_token:
            self.headers["X-Admin-Token"] = admin_token
        self.content_type = "multipart/form-data" if files is not None else "application/json"
        self._multipart = Multipart([FileField(data, name) for name, data in (files or [])])

    async def json(self):
        return self._body

    async def multipart(self):
        return self._multipart


def payload(response):
    return json.loads(response.text)


async def counts():
    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        result = []
        for table in ("users", "shifts", "actions"):
            result.append((await (await db.execute(
                f"SELECT COUNT(*) FROM {table}"
            )).fetchone())[0])
        return tuple(result)


async def run():
    await bot.init_db()
    city = bot.get_default_city()
    other_city = next(value for value in bot.CITIES_BY_ID.values() if value["id"] != city["id"])
    today = datetime.now(bot._city_tz(city)).date().isoformat()
    start_at = today + "T08:00:00+03:00"
    end_at = today + "T16:00:00+03:00"
    now_iso = datetime.now(timezone.utc).isoformat()
    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        await db.executemany(
            "INSERT INTO users (user_id,full_name,role,city_id) VALUES (?,?,?,?)",
            [(900001, "Владелец", "Скаут", city["id"]),
             (900002, "Старший скаут", "Скаут", city["id"]),
             (910001, "Скаут Один", "Скаут", city["id"]),
             (910002, "Водитель Один", "Водитель", city["id"])],
        )
        await db.execute(
            "INSERT INTO admin_accounts (user_id,role,role_scope,is_active,session_version,"
            "created_at,updated_at) VALUES (900002,'city_manager','Скаут',1,1,?,?)",
            (now_iso, now_iso),
        )
        await db.execute(
            "INSERT INTO admin_city_permissions (user_id,city_id) VALUES (?,?)", (900002, city["id"])
        )
        cur = await db.execute(
            "INSERT INTO shifts (user_id,full_name,role,start_time,end_time,is_active,created_at,"
            "city_id,start_at,end_at,source) VALUES (?,?,?,?,?,0,?,?,?,?, 'bot')",
            (910001, "Скаут Один", "Скаут", "08:00", "16:00", start_at,
             city["id"], start_at, end_at),
        )
        shift_id = cur.lastrowid
        await db.execute(
            "INSERT INTO actions (user_id,shift_id,message_id,action_type,bike_codes,quantity,city_id) "
            "VALUES (?,?,?,?,?,?,?)", (910001, shift_id, 1, "move", "0001,0002", 0, city["id"]),
        )
        await db.commit()

    base_counts = await counts()
    network_token, _ = bot._issue_admin_token(900001, 1)
    scout_token, _ = bot._issue_admin_token(900002, 1)

    # Login/context используют явный account, а не старую admin_city_access.
    login = await bot.api_admin_login(Request(900001, body={"password": "test-admin-password"}))
    assert login.status == 200 and payload(login)["role"] == "network_admin"
    context = await bot.api_crm_context(Request(900002, admin_token=scout_token))
    assert context.status == 200 and payload(context)["role_scope"] == "Скаут"

    overview = await bot.api_crm_overview(Request(
        900002, query={"city_id": str(city["id"]), "from": today, "to": today},
        admin_token=scout_token,
    ))
    assert overview.status == 200 and payload(overview)["totals"]["actions"] == 2
    denied_city = await bot.api_crm_overview(Request(
        900002, query={"city_id": str(other_city["id"])}, admin_token=scout_token,
    ))
    assert denied_city.status == 403

    # Старший скаут не может планировать водителя, но может скаута.
    denied_plan = await bot.api_crm_planned_shift_create(Request(
        900002, body={"city_id": city["id"], "work_date": today, "start_time": "08:00",
                      "end_time": "16:00", "user_id": 910002}, admin_token=scout_token,
    ))
    assert denied_plan.status == 400
    plan = await bot.api_crm_planned_shift_create(Request(
        900002, body={"city_id": city["id"], "work_date": today, "start_time": "08:00",
                      "end_time": "16:00", "user_id": 910001}, admin_token=scout_token,
    ))
    assert plan.status == 201
    calendar = await bot.api_crm_calendar(Request(
        900002, query={"city_id": str(city["id"]), "from": today, "to": today},
        admin_token=scout_token,
    ))
    assert payload(calendar)["planned"][0]["match_status"] == "вышел"

    # Публикация role-task фиксирует snapshot получателей.
    task_response = await bot.api_crm_task_create(Request(
        900002, body={"city_id": city["id"], "work_date": today, "title": "Проверка зоны",
                      "description": "Фото после выполнения", "priority": "high",
                      "target_type": "role", "target_role": "Скаут", "publish": True,
                      "requires_photo": True},
        admin_token=scout_token,
    ))
    assert task_response.status == 201
    task = payload(task_response)["task"]
    task_id = task["task_id"]
    assignee_ids = {item["user_id"] for item in task["assignees"]}
    assert 910001 in assignee_ids and 910002 not in assignee_ids
    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        await db.execute("INSERT INTO users (user_id,full_name,role,city_id) VALUES (?,?,?,?)",
                         (910003, "Новый скаут", "Скаут", city["id"]))
        await db.commit()
        frozen = (await (await db.execute(
            "SELECT COUNT(*) FROM crm_task_assignees WHERE task_id=?", (task_id,)
        )).fetchone())[0]
    assert frozen == len(assignee_ids)

    # Строгие переходы и обязательное result photo.
    started = await bot.api_employee_task_progress(Request(
        910001, body={"status": "in_progress"}, match={"task_id": str(task_id)}
    ))
    assert started.status == 200
    no_photo = await bot.api_employee_task_progress(Request(
        910001, body={"status": "submitted"}, match={"task_id": str(task_id)}
    ))
    assert no_photo.status == 409 and payload(no_photo)["error"] == "result_photo_required"
    foreign_upload = await bot.api_employee_task_upload(Request(
        910002, match={"task_id": str(task_id)}, files=[("foreign.jpg", b"\xff\xd8\xffx")]
    ))
    assert foreign_upload.status == 403
    own_upload = await bot.api_employee_task_upload(Request(
        910001, match={"task_id": str(task_id)}, files=[("result.jpg", b"\xff\xd8\xffresult")]
    ))
    assert own_upload.status == 201 and payload(own_upload)["items"][0]["kind"] == "result"
    mine = await bot.api_employee_tasks_mine(Request(
        910001, query={"from": today, "to": today}
    ))
    assert len(payload(mine)["items"][0]["result_attachments"]) == 1
    submitted = await bot.api_employee_task_progress(Request(
        910001, body={"status": "submitted", "comment": "Готово"}, match={"task_id": str(task_id)}
    ))
    assert submitted.status == 200
    accepted = await bot.api_crm_task_assignee_status(Request(
        900002, body={"status": "accepted", "comment": "Принято"},
        match={"task_id": str(task_id), "user_id": "910001"}, admin_token=scout_token,
    ))
    assert accepted.status == 200 and payload(accepted)["assignee"]["status"] == "accepted"

    # Entity lookup тоже закрыт role_scope, не только списки.
    driver_task_response = await bot.api_crm_task_create(Request(
        900001, body={"city_id": city["id"], "work_date": today, "title": "Водительское",
                      "target_type": "user", "target_user_id": 910002, "publish": True},
        admin_token=network_token,
    ))
    driver_task_id = payload(driver_task_response)["task"]["task_id"]
    denied_detail = await bot.api_crm_task_detail(Request(
        900002, match={"task_id": str(driver_task_id)}, admin_token=scout_token,
    ))
    denied_upload = await bot.api_crm_task_upload(Request(
        900002, match={"task_id": str(driver_task_id)}, admin_token=scout_token,
        files=[("brief.jpg", b"\xff\xd8\xffbrief")],
    ))
    denied_review = await bot.api_crm_task_assignee_status(Request(
        900002, body={"status": "accepted"},
        match={"task_id": str(driver_task_id), "user_id": "910002"},
        admin_token=scout_token,
    ))
    assert (denied_detail.status, denied_upload.status, denied_review.status) == (403, 403, 403)
    driver_started = await bot.api_employee_task_progress(Request(
        910002, body={"status": "in_progress"}, match={"task_id": str(driver_task_id)}
    ))
    driver_submitted = await bot.api_employee_task_progress(Request(
        910002, body={"status": "submitted"}, match={"task_id": str(driver_task_id)}
    ))
    return_without_reason = await bot.api_crm_task_assignee_status(Request(
        900001, body={"status": "in_progress"},
        match={"task_id": str(driver_task_id), "user_id": "910002"}, admin_token=network_token,
    ))
    returned = await bot.api_crm_task_assignee_status(Request(
        900001, body={"status": "in_progress", "comment": "Нужно другое фото"},
        match={"task_id": str(driver_task_id), "user_id": "910002"}, admin_token=network_token,
    ))
    assert driver_started.status == 200 and driver_submitted.status == 200
    assert return_without_reason.status == 400 and returned.status == 200

    # Фото: проверка типов и orphan cleanup без публичной раздачи.
    assert bot._crm_image_type(b"\xff\xd8\xffrest")[0] == "image/jpeg"
    assert bot._crm_image_type(b"not-image")[0] is None
    os.makedirs(bot.CRM_UPLOAD_DIR, exist_ok=True)
    orphan = Path(bot.CRM_UPLOAD_DIR) / "orphan.jpg"
    orphan.write_bytes(b"\xff\xd8\xff")
    os.utime(orphan, (time.time() - 90000, time.time() - 90000))
    await bot.cleanup_crm_uploads()
    assert not orphan.exists()

    # CRM не изменила существующие смены/действия; добавился только fixture user.
    final_counts = await counts()
    assert final_counts[1:] == base_counts[1:]
    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        audit_count = (await (await db.execute("SELECT COUNT(*) FROM admin_audit_log")).fetchone())[0]
        event_count = (await (await db.execute("SELECT COUNT(*) FROM crm_task_events")).fetchone())[0]
    assert audit_count >= 3 and event_count >= 4
    print("PASS CRM: RBAC, role scope, analytics, calendar, task snapshot/review, audit, storage")


try:
    asyncio.run(run())
finally:
    asyncio.run(bot.bot.session.close())
    TMP.cleanup()
