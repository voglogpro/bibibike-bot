"""Focused checks for creating shared To-Do tasks from Telegram task chats."""
import asyncio
import importlib.util
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
                 media_group_id=None):
        self.message_id = message_id
        self.from_user = SimpleNamespace(id=sender_id, is_bot=False)
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
    ids = {"author": 981001, "first": 981002, "second": 981003}
    async with app.aiosqlite.connect(app.DB_PATH) as db:
        await db.executemany(
            "INSERT INTO users (user_id,full_name,role,city_id,telegram_username) VALUES (?,?,?,?,?)",
            [
                (ids["author"], "Автор Задачи", "Скаут", city["id"], "task_author"),
                (ids["first"], "Первый Сотрудник", "Скаут", city["id"], "first_worker"),
                (ids["second"], "Второй Сотрудник", "Водитель", city["id"], "second_worker"),
            ],
        )
        await db.commit()

    route = {"city_key": app._city_key(city), "role": None}
    task_filter = app.TaskChatFilter()
    ordinary = Message(99, ids["author"], -1003431950710, text="Переместил 0001")
    command = Message(100, ids["author"], -1003431950710, text="/ @first_worker Проверить")
    assert await task_filter(ordinary) is False
    assert (await task_filter(command))["task_route"]["city_key"] == app.DEFAULT_CITY_KEY
    album = [
        Message(101, ids["author"], -1003431950710,
                caption="/ @first_worker @second_worker\nПереставить велосипеды\nПо отмеченной зоне",
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

    # Текст без фотографии всё равно создаёт и отправляет задачу.
    text_task = Message(
        104, ids["author"], -1003431950710,
        text="/ @first_worker Проверить парковку",
    )
    await app._create_task_from_chat([text_task], route, text_task.text)
    assert await scalar("SELECT COUNT(*) FROM crm_tasks WHERE created_via='telegram_chat'") == 2
    assert await scalar("SELECT COUNT(*) FROM crm_task_attachments") == 3

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
    print("PASS TODO chat: slash command, multiple assignees, photo album and text-only task")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    finally:
        asyncio.run(app.bot.session.close())
        TMP.cleanup()
