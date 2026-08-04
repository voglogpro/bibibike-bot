"""Интеграционная проверка CRM без запуска Telegram polling."""
import asyncio
import hashlib
import hmac
import importlib.util
import json
import os
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
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
             (910002, "Водитель Один", "Водитель", city["id"]),
             (910004, "Скаут Для Закрытия", "Скаут", city["id"]),
             (920004, "Сотрудник Другого Города", "Скаут", other_city["id"])],
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
        active_start = datetime.now(bot._city_tz(city)) - timedelta(hours=2)
        cur = await db.execute(
            "INSERT INTO shifts (user_id,full_name,role,start_time,end_time,is_active,created_at,"
            "city_id,start_at,end_at,source) VALUES (?,?,?,?,?,1,?,?,?,?, 'bot')",
            (910004, "Скаут Для Закрытия", "Скаут", active_start.strftime("%H:%M"), None,
             active_start.isoformat(), city["id"], active_start.isoformat(), None),
        )
        active_shift_id = cur.lastrowid
        other_start = datetime.now(bot._city_tz(other_city)) - timedelta(hours=3)
        cur = await db.execute(
            "INSERT INTO shifts (user_id,full_name,role,start_time,end_time,is_active,created_at,"
            "city_id,start_at,end_at,source) VALUES (?,?,?,?,?,1,?,?,?,?, 'bot')",
            (920004, "Сотрудник Другого Города", "Скаут", other_start.strftime("%H:%M"), None,
             other_start.isoformat(), other_city["id"], other_start.isoformat(), None),
        )
        other_active_shift_id = cur.lastrowid
        await db.commit()

    base_counts = await counts()
    network_token, _ = bot._issue_admin_token(900001, 1)
    scout_token, _ = bot._issue_admin_token(900002, 1)

    # Руководитель может закрыть только активную смену доступного города и роли.
    no_confirm = await bot.api_crm_shift_close(Request(
        900002, body={"city_id": city["id"]},
        match={"shift_id": str(active_shift_id)}, admin_token=scout_token,
    ))
    assert no_confirm.status == 400
    wrong_city = await bot.api_crm_shift_close(Request(
        900001, body={"city_id": other_city["id"], "confirm": True, "duration_hours": 10},
        match={"shift_id": str(active_shift_id)}, admin_token=network_token,
    ))
    assert wrong_city.status == 404
    with patch.object(bot, "safe_flush_report_update", new=AsyncMock(return_value=True)):
        closed = await bot.api_crm_shift_close(Request(
            900002, body={"city_id": city["id"], "confirm": True, "duration_hours": 10,
                          "comment": "Комментарий руководителя"},
            match={"shift_id": str(active_shift_id)}, admin_token=scout_token,
        ))
    assert closed.status == 200 and payload(closed)["shift_id"] == active_shift_id
    assert payload(closed)["duration_hours"] == 10
    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        closed_row = await (await db.execute(
            "SELECT is_active,start_at,end_at,comment FROM shifts WHERE id=?", (active_shift_id,)
        )).fetchone()
    assert closed_row[0] == 0 and closed_row[2]
    assert datetime.fromisoformat(closed_row[2]) - datetime.fromisoformat(closed_row[1]) == timedelta(hours=10)
    assert closed_row[3] == "Комментарий руководителя"
    # Сетевой администратор выбирает другой город и закрывает там 12-часовую смену.
    with patch.object(bot, "safe_flush_report_update", new=AsyncMock(return_value=True)):
        other_closed = await bot.api_crm_shift_close(Request(
            900001, body={"city_id": other_city["id"], "confirm": True,
                          "duration_hours": 12, "comment": "Проверено директором"},
            match={"shift_id": str(other_active_shift_id)}, admin_token=network_token,
        ))
    assert other_closed.status == 200 and payload(other_closed)["duration_hours"] == 12
    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        other_row = await (await db.execute(
            "SELECT start_at,end_at,comment FROM shifts WHERE id=?", (other_active_shift_id,)
        )).fetchone()
    assert datetime.fromisoformat(other_row[1]) - datetime.fromisoformat(other_row[0]) == timedelta(hours=12)
    assert other_row[2] == "Проверено директором"

    # Missing or expired CRM sessions must return 401, never crash with
    # context=None and turn an authentication error into HTTP 500.
    unauthenticated_trends = await bot.api_crm_trends(Request(
        900002, query={"city_id": str(city["id"]), "from": today, "to": today},
    ))
    assert unauthenticated_trends.status == 401
    unauthenticated_quality = await bot.api_crm_data_quality(Request(
        900002, query={"city_id": str(city["id"]), "from": today, "to": today},
    ))
    assert unauthenticated_quality.status == 401

    # Login/context используют явный account, а не старую admin_city_access.
    login = await bot.api_admin_login(Request(900001, body={"password": "test-admin-password"}))
    assert login.status == 200 and payload(login)["role"] == "network_admin"
    password_login = await bot.api_admin_login(Request(
        910001, body={"password": "test-admin-password"},
    ))
    password_payload = payload(password_login)
    assert password_login.status == 200
    assert password_payload["role"] == "city_manager"
    assert [item["id"] for item in password_payload["cities"]] == [city["id"]]
    password_token = password_payload["token"]
    password_context = await bot.api_crm_context(Request(910001, admin_token=password_token))
    assert password_context.status == 200
    password_denied_city = await bot.api_crm_overview(Request(
        910001, query={"city_id": str(other_city["id"])}, admin_token=password_token,
    ))
    assert password_denied_city.status == 403
    unregistered_login = await bot.api_admin_login(Request(
        919999, body={"password": "test-admin-password"},
    ))
    assert unregistered_login.status == 409
    context = await bot.api_crm_context(Request(900002, admin_token=scout_token))
    assert context.status == 200 and payload(context)["role_scope"] == "Скаут"

    overview = await bot.api_crm_overview(Request(
        900002, query={"city_id": str(city["id"]), "from": today, "to": today},
        admin_token=scout_token,
    ))
    assert overview.status == 200 and payload(overview)["totals"]["actions"] == 2
    hourly = await bot.api_crm_trends(Request(
        900002, query={"city_id": str(city["id"]), "from": today, "to": today,
                       "bucket": "hour"}, admin_token=scout_token,
    ))
    hourly_payload = payload(hourly)
    assert hourly.status == 200 and len(hourly_payload["series"]) == 24
    assert hourly_payload["series"][8]["types"]["move"] == 2
    assert sum(item["actions"] for item in hourly_payload["series"]) == 2
    activity = await bot.api_crm_activity(Request(
        900002, query={"city_id": str(city["id"]), "bike_code": "0001", "limit": "20"},
        admin_token=scout_token,
    ))
    activity_payload = payload(activity)
    assert activity.status == 200 and activity_payload["items"][0]["bike_codes"] == ["0001", "0002"]
    assert payload(await bot.api_crm_activity(Request(
        900002, query={"city_id": str(city["id"]), "bike_code": "001"},
        admin_token=scout_token,
    )))["error"] == "bike_code"
    idle_start = datetime.now(bot._city_tz(city)) - timedelta(hours=2)
    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        idle_cur = await db.execute(
            "INSERT INTO shifts (user_id,full_name,role,start_time,is_active,on_lunch,created_at,"
            "city_id,start_at,source) VALUES (?,?,?,?,1,0,?,?,?,'bot')",
            (910001, "Скаут Один", "Скаут", idle_start.strftime("%H:%M"),
             idle_start.isoformat(), city["id"], idle_start.isoformat()),
        )
        lunch_cur = await db.execute(
            "INSERT INTO shifts (user_id,full_name,role,start_time,is_active,on_lunch,created_at,"
            "city_id,start_at,source) VALUES (?,?,?,?,1,1,?,?,?,'bot')",
            (910004, "Скаут На Обеде", "Скаут", idle_start.strftime("%H:%M"),
             idle_start.isoformat(), city["id"], idle_start.isoformat()),
        )
        await db.commit()
    signals = await bot.api_crm_operational_signals(Request(
        900002, query={"city_id": str(city["id"])}, admin_token=scout_token,
    ))
    signal_payload = payload(signals)
    assert signals.status == 200 and "actions_last_hour" in signal_payload["summary"]
    assert any(item["type"] == "no_activity" and item["user_id"] == 910001
               for item in signal_payload["items"])
    assert not any(item.get("user_id") == 910004 for item in signal_payload["items"])
    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        await db.execute("DELETE FROM shifts WHERE id IN (?,?)", (idle_cur.lastrowid, lunch_cur.lastrowid))
        await db.commit()
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
    tomorrow = (datetime.fromisoformat(today) + timedelta(days=1)).date().isoformat()
    extra_plan = await bot.api_crm_planned_shift_create(Request(
        900002, body={"city_id": city["id"], "work_date": tomorrow,
                      "start_time": "08:00", "end_time": "16:00", "user_id": 910001,
                      "work_kind": "extra"}, admin_token=scout_token,
    ))
    assert extra_plan.status == 201 and payload(extra_plan)["plan"]["work_kind"] == "extra"
    tomorrow_calendar = await bot.api_crm_calendar(Request(
        900002, query={"city_id": str(city["id"]), "from": tomorrow, "to": tomorrow},
        admin_token=scout_token,
    ))
    tomorrow_item = payload(tomorrow_calendar)["planned"][0]
    assert tomorrow_item["work_kind"] == "extra" and tomorrow_item["match_status"] == "ожидается"
    cancelled_extra = await bot.api_crm_planned_shift_update(Request(
        900002, body={"status": "cancelled"},
        match={"plan_id": str(payload(extra_plan)["plan"]["id"])}, admin_token=scout_token,
    ))
    assert cancelled_extra.status == 200 and payload(cancelled_extra)["plan"]["status"] == "cancelled"

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

    # Период, несколько адресатов и обратная совместимость work_date.
    range_from = (datetime.fromisoformat(today) + timedelta(days=10)).date()
    range_to = range_from + timedelta(days=2)
    range_task_response = await bot.api_crm_task_create(Request(
        900002, body={"city_id": city["id"], "date_from": range_from.isoformat(),
                      "date_to": range_to.isoformat(), "title": "Задание на период",
                      "district": "Центр", "completion_mode": "manual",
                      "target_user_ids": [910001, 910003], "publish": True},
        admin_token=scout_token,
    ))
    assert range_task_response.status == 201
    range_task = payload(range_task_response)["task"]
    assert range_task["date_from"] == range_from.isoformat()
    assert {item["user_id"] for item in range_task["assignees"]} == {910001, 910003}
    middle_mine = await bot.api_employee_tasks_mine(Request(
        910003, query={"from": (range_from + timedelta(days=1)).isoformat(),
                       "to": (range_from + timedelta(days=1)).isoformat()}
    ))
    assert any(item["task_id"] == range_task["task_id"] for item in payload(middle_mine)["items"])
    incompatible = await bot.api_crm_task_create(Request(
        900002, body={"city_id": city["id"], "work_date": today, "title": "Нельзя",
                      "target_type": "user", "target_user_id": 910001,
                      "completion_mode": "shift_end", "requires_photo": True},
        admin_token=scout_token,
    ))
    assert incompatible.status == 400

    # Пакет 2/1: дата начала — первый рабочий день, повтор ключа ничего не создаёт.
    batch_from = range_to + timedelta(days=10); batch_to = batch_from + timedelta(days=5)
    batch_body = {"city_id": city["id"], "user_ids": [910001, 910003],
                  "date_from": batch_from.isoformat(), "date_to": batch_to.isoformat(),
                  "work_days": 2, "rest_days": 1, "start_time": "08:00", "end_time": "16:00",
                  "district": "Север", "idempotency_key": "crm-test-batch-2-1"}
    batch = await bot.api_crm_planned_shifts_batch(Request(
        900002, body=batch_body, admin_token=scout_token,
    ))
    assert batch.status == 201 and payload(batch)["created"] == 8
    batch_replay = await bot.api_crm_planned_shifts_batch(Request(
        900002, body=batch_body, admin_token=scout_token,
    ))
    assert batch_replay.status == 200 and payload(batch_replay)["idempotent_replay"] is True
    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        batch_plan_count = (await (await db.execute(
            "SELECT COUNT(*) FROM crm_planned_shifts WHERE batch_id=?", (payload(batch)["batch_id"],)
        )).fetchone())[0]
        batch_notice_count = (await (await db.execute(
            "SELECT COUNT(*) FROM crm_notification_outbox WHERE kind='plan_batch' AND entity_id=?",
            (payload(batch)["batch_id"],)
        )).fetchone())[0]
    assert batch_plan_count == 8 and batch_notice_count == 2

    # Фото задания доставляется через outbox; повторная публикация не создаёт дубль.
    photo_task_response = await bot.api_crm_task_create(Request(
        900002, body={"city_id": city["id"], "work_date": today, "title": "Задание с фото",
                      "target_type": "user", "target_user_id": 910001}, admin_token=scout_token,
    ))
    photo_task_id = payload(photo_task_response)["task"]["task_id"]
    brief = await bot.api_crm_task_upload(Request(
        900002, match={"task_id": str(photo_task_id)}, admin_token=scout_token,
        files=[("brief.jpg", b"\xff\xd8\xffbrief")],
    ))
    assert brief.status == 201
    published_photo_task = await bot.api_crm_task_publish(Request(
        900002, match={"task_id": str(photo_task_id)}, admin_token=scout_token,
    ))
    republished_photo_task = await bot.api_crm_task_publish(Request(
        900002, match={"task_id": str(photo_task_id)}, admin_token=scout_token,
    ))
    assert published_photo_task.status == 200 and payload(republished_photo_task)["already_published"]
    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        photo_outbox_count = (await (await db.execute(
            "SELECT COUNT(*) FROM crm_notification_outbox WHERE kind='task_assigned' AND entity_id=?",
            (photo_task_id,),
        )).fetchone())[0]
    assert photo_outbox_count == 1
    notification_text = bot._crm_notification_text("task_assigned", {
        "title": "Задание с фото", "date_from": today, "date_to": today,
        "district": "Центр", "description": "Стянуть байки по отмеченной карте",
    })
    assert "Центр" in notification_text and "Стянуть байки" in notification_text
    send_message = AsyncMock(); send_photo = AsyncMock()
    with patch.object(bot.bot, "send_message", send_message), patch.object(bot.bot, "send_photo", send_photo):
        delivered = await bot.deliver_crm_notifications_once(limit=100)
    assert delivered > 0 and send_photo.await_count >= 1
    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        await db.execute(
            "UPDATE crm_notification_outbox SET status='retry',next_attempt_at=? "
            "WHERE kind='task_assigned' AND entity_id=?",
            (datetime.now(timezone.utc).isoformat(), photo_task_id),
        )
        await db.commit()
    fallback_message = AsyncMock(); failed_photo = AsyncMock(side_effect=RuntimeError("photo failure"))
    with patch.object(bot.bot, "send_message", fallback_message), patch.object(bot.bot, "send_photo", failed_photo):
        fallback_delivered = await bot.deliver_crm_notifications_once(limit=100)
    assert fallback_delivered == 1 and fallback_message.await_count == 1

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

    # shift_end автоматически и ровно один раз закрывает подходящее задание.
    auto_task_response = await bot.api_crm_task_create(Request(
        900002, body={"city_id": city["id"], "work_date": today, "title": "Авто по смене",
                      "district": "Автозона", "completion_mode": "shift_end",
                      "target_type": "user", "target_user_id": 910003, "publish": True},
        admin_token=scout_token,
    ))
    auto_task_id = payload(auto_task_response)["task"]["task_id"]
    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO shifts (user_id,full_name,role,start_time,end_time,district,is_active,created_at,"
            "city_id,start_at,end_at,source) VALUES (?,?,?,?,?,?,0,?,?,?,?, 'bot')",
            (910003, "Новый скаут", "Скаут", "09:00", "17:00", "  АВТОЗОНА ",
             start_at, city["id"], start_at, end_at),
        )
        auto_shift_id = cur.lastrowid
        await db.commit()
    await bot.sync_closed_shift_tasks_once(limit=100)
    await bot.sync_closed_shift_tasks_once(limit=100)
    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        auto_status = (await (await db.execute(
            "SELECT status FROM crm_task_assignees WHERE task_id=? AND user_id=910003",
            (auto_task_id,),
        )).fetchone())[0]
        auto_events = (await (await db.execute(
            "SELECT COUNT(*) FROM crm_task_events WHERE task_id=? AND event_type='auto_completed.shift_end'",
            (auto_task_id,),
        )).fetchone())[0]
        sync_rows = (await (await db.execute(
            "SELECT COUNT(*) FROM crm_shift_task_sync WHERE shift_id=?", (auto_shift_id,)
        )).fetchone())[0]
    assert auto_status == "accepted" and auto_events == 1 and sync_rows == 1

    # Фото: проверка типов и orphan cleanup без публичной раздачи.
    assert bot._crm_image_type(b"\xff\xd8\xffrest")[0] == "image/jpeg"
    assert bot._crm_image_type(b"not-image")[0] is None
    os.makedirs(bot.CRM_UPLOAD_DIR, exist_ok=True)
    orphan = Path(bot.CRM_UPLOAD_DIR) / "orphan.jpg"
    orphan.write_bytes(b"\xff\xd8\xff")
    os.utime(orphan, (time.time() - 90000, time.time() - 90000))
    await bot.cleanup_crm_uploads()
    assert not orphan.exists()

    # CRM не изменила существующие действия; добавилась только тестовая закрытая смена.
    final_counts = await counts()
    assert final_counts[1] == base_counts[1] + 1 and final_counts[2] == base_counts[2]
    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        audit_count = (await (await db.execute("SELECT COUNT(*) FROM admin_audit_log")).fetchone())[0]
        event_count = (await (await db.execute("SELECT COUNT(*) FROM crm_task_events")).fetchone())[0]
    assert audit_count >= 3 and event_count >= 4
    print("PASS CRM: RBAC, outbox, batch planning, ranges, shift sync, audit, storage")


try:
    asyncio.run(run())
finally:
    asyncio.run(bot.bot.session.close())
    TMP.cleanup()
