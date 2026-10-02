"""Проверки рассылки руководителя: адресаты, права, очередь и текст.

Запуск: python tests/broadcast_check.py
База временная, прод не затрагивается.
"""
import asyncio
import importlib.util
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory(prefix="bibibike-broadcast-")
os.environ["DATA_DIR"] = TMP.name
os.environ["TOKEN"] = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
spec = importlib.util.spec_from_file_location("bibibike_broadcast_test", ROOT / "main.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)

MANAGER = 970001
NETWORK = 970002
VIEWER = 970003
SCOUT_A = 970010
SCOUT_B = 970011
DRIVER = 970012
FIRED = 970013
OTHER_CITY_SCOUT = 970014


class Request:
    def __init__(self, body=None, query=None, method="POST"):
        self.body = body or {}
        self.query = query or {}
        self.method = method
        self.headers = {}
        self.match_info = {}

    async def json(self):
        return self.body


async def rows(sql, params=()):
    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        db.row_factory = bot.aiosqlite.Row
        result = await (await db.execute(sql, params)).fetchall()
    return [dict(row) for row in result]


def admin_context(user_id, city, role="city_manager", role_scope=None):
    return {
        "telegram_user": {"id": user_id, "first_name": "Руководитель"},
        "user": {"full_name": f"Администратор {user_id}"},
        "admin": {"role": role, "role_scope": role_scope},
        "city": city,
        "allowed_city_ids": (sorted(bot.CITIES_BY_ID) if role == "network_admin"
                             else [city["id"]]),
    }


def as_admin(context):
    return patch.object(bot, "_admin_context", AsyncMock(return_value=context))


async def run():
    await bot.init_db()
    city = bot.get_default_city()
    other_city = next(item for item in bot.CITIES_BY_ID.values()
                      if item["id"] != city["id"])
    now_iso = datetime.now(timezone.utc).isoformat()

    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        await db.executemany(
            "INSERT INTO users (user_id,full_name,role,city_id,statistics_visible) "
            "VALUES (?,?,?,?,?)",
            [
                (MANAGER, "Руководитель Города", "Скаут", city["id"], 1),
                (NETWORK, "Админ Сети", "Скаут", city["id"], 1),
                (VIEWER, "Наблюдатель", "Скаут", city["id"], 1),
                (SCOUT_A, "Скаут Первый", "Скаут", city["id"], 1),
                (SCOUT_B, "Скаут Второй", "Скаут", city["id"], 1),
                (DRIVER, "Водитель Первый", "Водитель", city["id"], 1),
                (FIRED, "Уволенный Скаут", "Скаут", city["id"], 0),
                (OTHER_CITY_SCOUT, "Скаут Чужого", "Скаут", other_city["id"], 1),
            ],
        )
        await db.commit()

    city_admin = admin_context(MANAGER, city)
    network_admin = admin_context(NETWORK, city, role="network_admin")
    viewer = admin_context(VIEWER, city, role="city_viewer")

    # 1. Пустой текст не создаёт рассылку.
    with as_admin(city_admin):
        response = await bot.api_crm_broadcast_create(Request({"body": "  "}))
    assert response.status == 400, response.text
    assert json.loads(response.text)["error"] == "body", response.text

    # 2. Доступ только на просмотр — отправка запрещена.
    with as_admin(viewer):
        response = await bot.api_crm_broadcast_create(Request({"body": "Проверка связи"}))
    assert response.status == 403, response.text

    # 3. Рассылка на весь город: уволенные и чужой город не попадают.
    with as_admin(city_admin):
        response = await bot.api_crm_broadcast_create(Request({
            "title": "Смены на выходные",
            "body": "Завтра выходим по обычному графику.",
        }))
    assert response.status == 200, response.text
    created = json.loads(response.text)
    assert created["recipients_total"] == 6, created
    broadcast_id = created["broadcast_id"]

    queued = await rows(
        "SELECT user_id,payload_json,status FROM crm_notification_outbox "
        "WHERE kind='broadcast' AND entity_id=? ORDER BY user_id", (broadcast_id,)
    )
    expected = sorted([MANAGER, NETWORK, VIEWER, SCOUT_A, SCOUT_B, DRIVER])
    assert [item["user_id"] for item in queued] == expected, queued
    recipients = {item["user_id"] for item in queued}
    assert FIRED not in recipients, "уволенный сотрудник не должен получать рассылку"
    assert OTHER_CITY_SCOUT not in recipients, "чужой город не должен получать рассылку"
    assert all(item["status"] == "pending" for item in queued), queued

    # 4. Повторная постановка той же рассылки не задваивает сообщения.
    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        payload = json.loads(queued[0]["payload_json"])
        for user_id in recipients:
            await bot._enqueue_crm_notification(
                db, city["id"], user_id, "broadcast", broadcast_id, payload
            )
        await db.commit()
    again = await rows(
        "SELECT id FROM crm_notification_outbox WHERE kind='broadcast' AND entity_id=?",
        (broadcast_id,)
    )
    assert len(again) == len(queued), (len(again), len(queued))

    # 5. Рассылка по роли уходит только этой роли.
    with as_admin(network_admin):
        response = await bot.api_crm_broadcast_create(Request({
            "body": "Водители, проверьте прицепы.", "role": "Водитель",
        }))
    assert response.status == 200, response.text
    drivers_id = json.loads(response.text)["broadcast_id"]
    drivers = await rows(
        "SELECT user_id FROM crm_notification_outbox WHERE kind='broadcast' AND entity_id=?",
        (drivers_id,)
    )
    assert [item["user_id"] for item in drivers] == [DRIVER], drivers

    # 6. Ограничение по роли у аккаунта не даёт разослать другой роли.
    scoped = admin_context(MANAGER, city, role_scope="Скаут")
    with as_admin(scoped):
        response = await bot.api_crm_broadcast_create(Request({
            "body": "Не должно уйти.", "role": "Водитель",
        }))
    assert response.status == 403, response.text

    # 7. Слишком длинный текст отклоняется до записи в очередь.
    with as_admin(city_admin):
        response = await bot.api_crm_broadcast_create(Request({
            "body": "я" * (bot.CRM_BROADCAST_BODY_LIMIT + 1),
        }))
    assert response.status == 400, response.text
    assert json.loads(response.text)["error"] == "body_too_long", response.text

    # 8. Текст сообщения содержит заголовок, тело и подпись автора.
    text = bot._crm_notification_text("broadcast", {
        "title": "Смены на выходные", "body": "Завтра выходим.",
        "author_name": "Кирилл",
    })
    assert "📣 Смены на выходные" in text, text
    assert "Завтра выходим." in text, text
    assert "— Кирилл" in text, text
    without_title = bot._crm_notification_text("broadcast", {"body": "Только текст"})
    assert without_title.startswith("📣 Сообщение от руководителя"), without_title

    # 9. Сводка показывает адресатов, роли и историю с доставкой.
    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        await db.execute(
            "UPDATE crm_notification_outbox SET status='sent' "
            "WHERE kind='broadcast' AND entity_id=? AND user_id=?",
            (broadcast_id, SCOUT_A),
        )
        await db.execute(
            "UPDATE crm_notification_outbox SET status='failed' "
            "WHERE kind='broadcast' AND entity_id=? AND user_id=?",
            (broadcast_id, SCOUT_B),
        )
        await db.commit()
    with as_admin(city_admin):
        response = await bot.api_crm_broadcasts(Request(query={}, method="GET"))
    assert response.status == 200, response.text
    summary = json.loads(response.text)
    assert summary["can_send"] is True, summary
    assert summary["recipients"]["total"] == 6, summary["recipients"]
    assert {item["role"] for item in summary["roles"]} == {"Скаут", "Водитель"}, summary["roles"]
    history = {item["id"]: item for item in summary["history"]}
    assert history[broadcast_id]["recipients_total"] == 6, history[broadcast_id]
    assert history[broadcast_id]["delivery"]["sent"] == 1, history[broadcast_id]
    assert history[broadcast_id]["delivery"]["failed"] == 1, history[broadcast_id]
    assert history[broadcast_id]["delivery"]["pending"] == 4, history[broadcast_id]

    # 10. Наблюдатель видит сводку, но без права отправки.
    with as_admin(viewer):
        response = await bot.api_crm_broadcasts(Request(query={}, method="GET"))
    assert response.status == 200, response.text
    assert json.loads(response.text)["can_send"] is False, response.text

    # 11. Доставка действительно отправляет текст рассылки в Telegram.
    sent_calls = []

    async def fake_send(chat_id, text, **kwargs):
        sent_calls.append((chat_id, text))

        class Sent:
            message_id = len(sent_calls)

        return Sent()

    with patch.object(bot.bot, "send_message", AsyncMock(side_effect=fake_send)):
        await bot.deliver_crm_notifications_once(limit=100)
    broadcast_texts = [text for _, text in sent_calls if "📣" in text]
    assert broadcast_texts, sent_calls
    assert any("Завтра выходим по обычному графику." in text for text in broadcast_texts), \
        broadcast_texts

    print("PASS рассылка: адресаты, права, роль, очередь, текст и доставка")


if __name__ == "__main__":
    asyncio.run(run())
