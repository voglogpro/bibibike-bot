"""Focused checks for creating shared To-Do tasks from Telegram task chats."""
import asyncio
import importlib.util
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory(prefix="bibibike-todo-chat-")
os.environ["DATA_DIR"] = TMP.name
os.environ["TOKEN"] = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
spec = importlib.util.spec_from_file_location("bibibike_todo_chat_test", ROOT / "main.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class Message:
    def __init__(self, message_id, sender_id, chat_id, *, text=None, caption=None, photos=0,
                 media_group_id=None, sender_username=None):
        self.message_id = message_id
        self.from_user = SimpleNamespace(
            id=sender_id, is_bot=False, username=sender_username or "KuBerCaMypAu"
        )
        self.chat = SimpleNamespace(id=chat_id)
        self.text = text
        self.caption = caption
        self.entities = []
        self.caption_entities = []
        self.photo = [SimpleNamespace(file_id=f"photo-{message_id}-{i}") for i in range(photos)]
        self.media_group_id = media_group_id
        self.message_thread_id = 1
        self.date = datetime.now(timezone.utc)
        self.reply = AsyncMock()


async def scalar(sql, params=()):
    async with app.aiosqlite.connect(app.DB_PATH) as db:
        return (await (await db.execute(sql, params)).fetchone())[0]


async def run():
    await app.init_db()
    city = app.get_default_city()
    ids = {"author": 981001, "first": 981002, "second": 981003, "ordinary": 981004}
    async with app.aiosqlite.connect(app.DB_PATH) as db:
        await db.executemany(
            "INSERT INTO users (user_id,full_name,role,city_id,telegram_username) VALUES (?,?,?,?,?)",
            [
                (ids["author"], "Автор Задачи", "Скаут", city["id"], "task_author"),
                (ids["first"], "Первый Сотрудник", "Скаут", city["id"], "first_worker"),
                (ids["second"], "Второй Сотрудник", "Водитель", city["id"], "second_worker"),
                (ids["ordinary"], "Обычный Сотрудник", "Скаут", city["id"], "ordinary_worker"),
            ],
        )
        now_iso = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT INTO admin_accounts (user_id,role,is_active,session_version,created_at,updated_at) "
            "VALUES (?,'city_manager',1,1,?,?)",
            (ids["author"], now_iso, now_iso),
        )
        await db.execute(
            "INSERT INTO admin_city_permissions (user_id,city_id) VALUES (?,?)",
            (ids["author"], city["id"]),
        )
        await db.commit()

    route = {
        "city_key": app._city_key(city), "role": None,
        "authors": ("KuBerCaMypAu", "Aleksandroll"),
    }
    task_filter = app.TaskChatFilter()
    ordinary = Message(99, ids["author"], -1003431950710, text="Переместил 0001")
    command = Message(100, ids["author"], -1003431950710, text="/ @first_worker Проверить")
    natural = Message(
        98, ids["author"], -1003431950710,
        caption="Навести порядок, разряд стараться кучковать @first_worker", photos=1,
    )
    assert await task_filter(ordinary) is False
    assert (await task_filter(command))["task_route"]["city_key"] == app.DEFAULT_CITY_KEY
    assert (await task_filter(natural))["task_route"]["city_key"] == app.DEFAULT_CITY_KEY
    question = Message(
        96, ids["author"], -1003431950710,
        caption="@first_worker ты сегодня выходишь на смену?", photos=1,
    )
    assert (await task_filter(question))["task_route"]["city_key"] == app.DEFAULT_CITY_KEY
    await app.task_chat_message(question, route)
    question.reply.assert_not_awaited()
    missing_target = Message(
        97, ids["author"], -1003431950710,
        caption="Саня приоритет к выполнению убрать байк с территории", photos=1,
    )
    assert (await task_filter(missing_target))["task_route"]["city_key"] == app.DEFAULT_CITY_KEY
    await app.task_chat_message(missing_target, route)
    missing_target.reply.assert_not_awaited()
    album = [
        Message(101, ids["author"], -1003431950710,
                caption="@first_worker @second_worker\nПереставить велосипеды\nПо отмеченной зоне",
                photos=1, media_group_id="album-1"),
        Message(102, ids["author"], -1003431950710, photos=1, media_group_id="album-1"),
        Message(103, ids["author"], -1003431950710, photos=1, media_group_id="album-1"),
    ]

    async def fake_download(_file_path, destination):
        Path(destination).write_bytes(b"test-photo")

    with patch.object(app.bot, "get_file", AsyncMock(return_value=SimpleNamespace(file_path="remote"))), \
            patch.object(app.bot, "download_file", side_effect=fake_download):
        await app._create_task_from_chat(album, route, album[0].caption)

    assert await scalar("SELECT COUNT(*) FROM crm_tasks WHERE created_via='telegram_chat'") == 1
    async with app.aiosqlite.connect(app.DB_PATH) as db:
        db.row_factory = app.aiosqlite.Row
        task = await (await db.execute(
            "SELECT * FROM crm_tasks WHERE created_via='telegram_chat'"
        )).fetchone()
        targets = await (await db.execute(
            "SELECT user_id FROM crm_task_targets WHERE task_id=? ORDER BY user_id", (task["id"],)
        )).fetchall()
    assert task["title"] == "Переставить велосипеды"
    assert task["description"] == "Переставить велосипеды\nПо отмеченной зоне"
    assert [row[0] for row in targets] == [ids["first"], ids["second"]]
    assert await scalar(
        "SELECT COUNT(*) FROM crm_task_attachments WHERE task_id=? AND kind='brief'", (task["id"],)
    ) == 3
    assert await scalar(
        "SELECT COUNT(*) FROM crm_notification_outbox WHERE entity_id=? AND kind='task_assigned'",
        (task["id"],),
    ) == 2

    # Задача сразу доступна исполнителю через API экрана «Задачи» Mini App.
    request = SimpleNamespace(query={"scope": "inbox"})
    with patch.object(app, "_auth_user", AsyncMock(return_value={"id": ids["first"]})):
        response = await app.api_employee_tasks_mine(request)
    mini_items = json.loads(response.text)["items"]
    assert task["id"] in {item["task_id"] for item in mini_items}

    # Текст без фотографии всё равно создаёт и отправляет задачу.
    text_task = Message(
        104, ids["author"], -1003431950710,
        text="/ @first_worker Проверить парковку",
    )
    await app._create_task_from_chat([text_task], route, text_task.text)
    assert await scalar("SELECT COUNT(*) FROM crm_tasks WHERE created_via='telegram_chat'") == 2
    assert await scalar("SELECT COUNT(*) FROM crm_task_attachments") == 3

    # Формулировка со скриншота автоматически требует фото результата.
    photo_report = Message(
        105, ids["author"], -1003431950710,
        caption=("@first_worker Саня приоритет к выполнению убрать байк с территории ЦГ\n"
                 "Сделать фотоотчет места через таймштамп"),
        photos=1,
    )
    with patch.object(app.bot, "get_file", AsyncMock(return_value=SimpleNamespace(file_path="remote"))), \
            patch.object(app.bot, "download_file", side_effect=fake_download):
        await app._create_task_from_chat([photo_report], route, photo_report.caption)
    async with app.aiosqlite.connect(app.DB_PATH) as db:
        db.row_factory = app.aiosqlite.Row
        photo_task = await (await db.execute(
            "SELECT * FROM crm_tasks WHERE client_request_id=?",
            (f"task-chat:{photo_report.chat.id}:{photo_report.message_id}",),
        )).fetchone()
    assert photo_task["priority"] == "urgent"
    assert photo_task["requires_photo"] == 1
    assert "@first_worker" not in photo_task["description"]

    # Обычный сотрудник не может превратить упоминание в задачу из руководящего чата.
    denied = Message(
        106, ids["ordinary"], -1003431950710,
        text="/ @first_worker Проверить парковку", sender_username="ordinary_worker",
    )
    await app._create_task_from_chat([denied], route, denied.text)
    assert "старший скаут" in denied.reply.await_args.args[0]
    assert await scalar("SELECT COUNT(*) FROM crm_tasks WHERE created_via='telegram_chat'") == 3

    # Даже CRM-руководитель не принимается в тестовой теме, если его нет в allowlist.
    blocked_author = Message(
        107, ids["author"], -1003431950710,
        text="/ @first_worker Проверить парковку", sender_username="another_manager",
    )
    await app._create_task_from_chat([blocked_author], route, blocked_author.text)
    assert "назначенных старших" in blocked_author.reply.await_args.args[0]
    assert await scalar("SELECT COUNT(*) FROM crm_tasks WHERE created_via='telegram_chat'") == 3

    # Все фото одной задачи доставляются одним альбомом, а ID сообщений
    # сохраняются для автоматического удаления до лимита Telegram в 48 часов.
    media_messages = [SimpleNamespace(message_id=201 + i) for i in range(3)]
    text_message = SimpleNamespace(message_id=204)
    with patch.object(app.bot, "send_media_group", AsyncMock(return_value=media_messages)) as send_album, \
            patch.object(app.bot, "send_message", AsyncMock(return_value=text_message)):
        assert await app.deliver_crm_notifications_once(limit=1) == 1
    send_album.assert_awaited_once()
    assert await scalar("SELECT COUNT(*) FROM crm_notification_messages") == 4
    async with app.aiosqlite.connect(app.DB_PATH) as db:
        await db.execute(
            "UPDATE crm_notification_messages SET delete_after=?",
            ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),),
        )
        await db.commit()
    with patch.object(app.bot, "delete_message", AsyncMock()) as delete_message:
        assert await app.cleanup_task_messages_once() == 4
    assert delete_message.await_count == 4
    assert await scalar(
        "SELECT COUNT(*) FROM crm_notification_messages WHERE deleted_at IS NOT NULL"
    ) == 4
    print("PASS TODO chat: natural senior message, Mini App, DM, photos and permissions")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    finally:
        asyncio.run(app.bot.session.close())
        TMP.cleanup()
