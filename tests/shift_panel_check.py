"""Время и свободный район в личке: транзакции, доступ, перезапуск и Bot API 10.4."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import zlib

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory(prefix="shift-widget-")
os.environ["DATA_DIR"] = TMP.name
os.environ["TOKEN"] = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
os.environ["ADMIN_PASSWORD"] = "test-password"
os.environ["NETWORK_ADMIN_USER_IDS"] = "990001"
spec = importlib.util.spec_from_file_location("shift_panel_host", ROOT / "main.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)
from shift_panel import SendWidget, EditWidget, AnswerWidget
from aiogram import Dispatcher
from aiogram.exceptions import TelegramNotFound
from aiogram.types import Update


class FakeBot:
    def __init__(self):
        self.mid, self.methods, self.unsupported = 1000, [], False
        self.send_message = AsyncMock(side_effect=self.message)
        self.edit_message_text = AsyncMock()
        self.edit_message_reply_markup = AsyncMock()

    async def message(self, *_args, **_kwargs):
        self.mid += 1
        return SimpleNamespace(message_id=self.mid)

    async def __call__(self, method):
        self.methods.append(method)
        if isinstance(method, SendWidget):
            if self.unsupported:
                raise TelegramNotFound(method=method, message="Unknown method")
            self.mid += 1
            return {"message_id": self.mid}
        return True


def message(uid, text="/start", mid=1):
    return SimpleNamespace(from_user=SimpleNamespace(id=uid, username=f"worker_{uid}"),
        chat=SimpleNamespace(id=uid, type="private"), text=text, message_id=mid, answer=AsyncMock())


async def run():
    await bot.init_db()
    city = next(c for c in bot.CITIES_BY_ID.values() if c["city_key"] == "krasnodar")
    cid, uid = city["id"], 990002
    async with bot.db_connect() as db:
        await db.executemany("INSERT INTO users(user_id,full_name,role,city_id) VALUES (?,?,?,?)", [
            (uid, "Тестовый Сотрудник", "Чарджер", cid), (990003, "Другой", "Скаут", cid)])
        await db.commit()
    fake, panels = FakeBot(), bot.shift_panels
    with patch.object(bot, "bot", fake), patch.object(bot, "_is_city_role_member", AsyncMock(return_value=True)), \
         patch.object(bot, "_is_city_member", AsyncMock(return_value=True)), \
         patch.object(bot, "safe_flush_report_update", AsyncMock(return_value=True)):
        # Тестируем именно /start, без дополнительных параметров и выбора города.
        await bot.private_subscription_start(message(uid))
        row = await panels.session(uid=uid, city_id=cid)
        assert row and row["kind"] == "widget"
        widget = next(m for m in fake.methods if isinstance(m, SendWidget))
        assert len(widget.html.encode()) <= 32768 and len(zlib.compress(widget.html.encode(), 9)) <= 4096
        assert len(widget.chat_data.encode()) <= 1024 and "fetch(" not in widget.html
        assert "<input" not in widget.html and "<video" not in widget.html
        assert "reply_markup" not in widget.model_dump() and "caption" not in widget.model_dump()
        assert "data:image/webp;base64," in widget.html
        if shutil.which("node"):
            script = re.search(r"<script>(.*?)</script>", widget.html, re.S)[1]
            result = subprocess.run(["node", "-e", "new Function(process.argv[1])", script], capture_output=True)
            assert result.returncode == 0, result.stderr

        async def action(name, revision=None, actor=uid, mid=None, sid=None, token=None):
            current = await panels.session(token or row["token"])
            active = await bot.get_active_shift(actor)
            sid = (active or {}).get("id", 0) if sid is None else sid
            return await panels.perform(actor, actor, mid or current["message_id"],
                f"sw:{current['token']}:{current['revision'] if revision is None else revision}:{name}:{sid}")

        assert "устарела" in await action("open", actor=990003)
        await action("open")
        assert not await bot.get_active_shift(uid)
        invalid = message(uid, "25:99", 2)
        assert await panels.accept_input(invalid)
        await panels.receive_input(invalid)
        assert invalid.answer.await_count == 0
        error_state = json.loads([m for m in fake.methods if isinstance(m, EditWidget)][-1].chat_data)
        assert "Введите время" in error_state["msg"]
        await panels.receive_input(message(uid, "09:00", 3))
        current = await panels.session(row["token"])
        assert current["draft_step"] == "start_district" and current["draft_time"] == "9:00", current
        # Повторная доставка сообщения со временем не превращается в район.
        await panels.receive_input(message(uid, "09:00", 3))
        assert (await panels.session(row["token"]))["draft_step"] == "start_district"
        await bot.init_db()
        await panels.receive_input(message(uid, "ФМР, ЮМР — свой район", 4))
        assert not await bot.get_active_shift(uid)
        current = await panels.session(row["token"])
        assert current["draft_step"] == "start_confirm"
        rev = current["revision"]
        await asyncio.gather(action("commit", rev), action("commit", rev))
        active = await bot.get_active_shift(uid)
        assert active and active["start_time"] == "9:00" and active["district"] == "фмр, юмр — свой район"
        async with bot.db_connect() as db:
            assert (await (await db.execute("SELECT COUNT(*) FROM shifts WHERE user_id=?", (uid,))).fetchone())[0] == 1
        await bot.add_action(uid, active["id"], 7001, "battery", [f"{i:04}" for i in range(115)], city_id=cid)
        await action("close")
        await panels.receive_input(message(uid, "10:30", 5))
        assert await bot.get_active_shift(uid)
        current = await panels.session(row["token"])
        await asyncio.gather(action("commit", current["revision"]), action("commit", current["revision"]))
        assert not await bot.get_active_shift(uid)
        closed = await bot.get_shift_by_id(active["id"])
        assert closed["end_time"] == "10:30" and (await bot.get_stats(active["id"]))["battery"] == 115

        # Отмена ничего не пишет в смены; обычный текст без черновика не перехватывается.
        await action("open")
        await panels.cancel_input(message(uid, "/cancel", 6))
        assert not await bot.get_active_shift(uid)
        assert not await panels.accept_input(message(uid, "обычный текст", 7))
        assert not await panels.accept_input(message(uid, "/tasks", 7))
        await action("open")
        await panels.patch(row["token"], draft_expires_at=1)
        await panels.receive_input(message(uid, "09:00", 8))
        assert (await panels.session(row["token"]))["draft_step"] is None
        await action("open")
        await action("now")
        await action("d0")
        with patch.object(bot, "_is_city_role_member", AsyncMock(return_value=False)):
            assert "Нет доступа" in await action("commit")
        with patch.object(bot, "_require_city_subscription", AsyncMock(side_effect=
            bot.CitySubscriptionRequired({"message": "Подписка закончилась"}))):
            assert "Подписка" in await action("commit")
        assert not await bot.get_active_shift(uid)
        await action("commit")
        newer = await bot.get_active_shift(uid)
        assert newer["id"] != active["id"]
        assert "изменилась" in await action("close", sid=active["id"])
        assert await bot.end_shift(uid, newer["start_time"], city_id=cid, expected_shift_id=active["id"]) is None

        # Подтверждение связано с ID смены, не с последней открытой сменой.
        await action("close")
        await action("now")
        await panels.patch(row["token"], draft_shift_id=active["id"])
        assert "изменилась" in await action("commit")
        assert (await bot.get_active_shift(uid))["id"] == newer["id"]
        await panels.sync_shift_safely(newer["id"])
        await panels.sync_shift_safely(active["id"])
        assert (await panels.session(row["token"]))["last_shift_id"] == newer["id"]

        current = await panels.session(row["token"])
        update = Update.model_validate({"update_id": 7001, "widget_query": {
            "id": "query-id", "from": {"id": uid, "is_bot": False, "first_name": "Test"},
            "chat": {"id": uid, "type": "private"}, "message_id": current["message_id"],
            "chat_instance": "test", "type": {"type": "data"},
            "data": f"sw:{row['token']}:{current['revision']}:refresh:0"}})
        dp = Dispatcher()
        dp.update.outer_middleware(panels.middleware)
        client = bot.Bot(token=os.environ["TOKEN"])
        try:
            await dp.feed_update(client, update)
        finally:
            await client.session.close()
        assert any(isinstance(m, AnswerWidget) for m in fake.methods)
        # Обычный сценарий целиком обновляет виджет, не создавая сообщений
        # с текстовыми клавиатурами или дублирующими подтверждениями.
        assert fake.send_message.await_count == 0

        # Старый сервер получает ссылку на приложение, но не новую клавиатуру.
        fake.unsupported = True
        await bot.private_subscription_start(message(990003, mid=10))
        fallback = await panels.session(uid=990003, city_id=cid)
        assert fallback["kind"] == "text"
        assert "reply_markup" not in fake.send_message.await_args.kwargs
        assert "https://t.me/" in fake.send_message.await_args.args[1]
        await panels.render(fallback, "Проверка ссылки")
        assert "https://t.me/" in fake.edit_message_text.await_args.args[0]
        assert fake.edit_message_text.await_args.kwargs["reply_markup"] is None
        with patch.object(bot, "_is_city_role_member", AsyncMock(return_value=False)):
            denied = message(990003, mid=11)
            await panels.open_private(denied)
            assert denied.answer.await_count == 1
        newcomer = message(999999, mid=12)
        await panels.open_private(newcomer)
        assert newcomer.answer.await_count == 1
        old = row["token"]
        fake.unsupported = False
        legacy = await panels.session(old)
        await panels.patch(old, ui_version=0, prompt_message_id=8888)
        await bot.private_subscription_start(message(uid, mid=20))
        assert await panels.session(old) is None
        cleared = {(call.kwargs["chat_id"], call.kwargs["message_id"]) for call in fake.edit_message_reply_markup.await_args_list}
        assert (uid, legacy["message_id"]) in cleared and (uid, 8888) in cleared
        assert all(call.kwargs["reply_markup"] is None for call in fake.edit_message_reply_markup.await_args_list)
        # Нажатие старой внешней кнопки только убирает её, не меняя смену.
        callback = SimpleNamespace(answer=AsyncMock(), from_user=SimpleNamespace(id=uid),
            message=SimpleNamespace(chat=SimpleNamespace(id=uid), message_id=8888),
            data=panels.callback_data(legacy, "close", 123))
        with patch.object(panels, "perform", AsyncMock()) as perform:
            await panels.callback(callback)
            perform.assert_not_awaited()
        callback.answer.assert_awaited_once()
        # Проверяем настоящую маршрутизацию /start и текстового ответа:
        # ввод времени не перехватывает команды или обработчик фотографии.
        dp.include_router(bot.private_router)
        def telegram_message(update_id, mid, text):
            return Update.model_validate({"update_id": update_id, "message": {
                "message_id": mid, "date": int(bot.time.time()),
                "chat": {"id": 990003, "type": "private"},
                "from": {"id": 990003, "is_bot": False, "first_name": "Test"}, "text": text}})
        client = bot.Bot(token=os.environ["TOKEN"])
        try:
            with patch.object(client.session, "make_request", AsyncMock(return_value=True)):
                await dp.feed_update(client, telegram_message(7010, 30, "/start"))
                current = await panels.session(uid=990003, city_id=cid)
                await panels.perform(990003, 990003, current["message_id"],
                    panels.callback_data(current, "open"))
                await dp.feed_update(client, telegram_message(7011, 31, "08:15"))
                current = await panels.session(current["token"])
                assert current["draft_step"] == "start_district" and current["draft_time"] == "8:15"
        finally:
            await client.session.close()
        assert not panels._locks
    print("PASS private shift: /start identity, custom time/district, replay protection, restart, 115 actions, cancel/expiry, stale close, membership/subscription, widget routing and fallback")


if __name__ == "__main__":
    asyncio.run(run())
