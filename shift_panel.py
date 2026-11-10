"""Личная карточка смены и ввод времени/района прямо в переписке с ботом."""
import asyncio
import base64
import html
import json
import re
import uuid
import zlib
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
from aiogram.exceptions import TelegramBadRequest, TelegramNotFound
from aiogram.methods import TelegramMethod

ASSETS = Path(__file__).resolve().parent / "assets" / "shift-panel"
COMMAND = re.compile(r"sw:([a-f0-9]{20}):(\d{1,10}):(refresh|basic|open|close|now|commit|cancel|d\d)(?::(\d{1,12}))?")
DRAFT_TTL = 15 * 60


class SendWidget(TelegramMethod[Any]):
    __api_method__ = "sendWidget"
    __returning__ = Any
    chat_id: int
    html: str
    chat_data: str
    allow_queries: bool = True


class EditWidget(TelegramMethod[bool]):
    __api_method__ = "editMessageWidgetData"
    __returning__ = bool
    chat_id: int
    message_id: int
    chat_data: str


class AnswerWidget(TelegramMethod[bool]):
    __api_method__ = "answerWidgetQuery"
    __returning__ = bool
    widget_query_id: str
    data: str


class Namespace:
    def __init__(self, values):
        self.values = values

    def __getattr__(self, key):
        return self.values[key]


class ShiftPanels:
    def __init__(self, namespace):
        self.a = Namespace(namespace)
        self._locks = {}

    @asynccontextmanager
    async def guard(self, key):
        entry = self._locks.setdefault(key, {"lock": asyncio.Lock(), "users": 0})
        entry["users"] += 1
        try:
            async with entry["lock"]:
                yield
        finally:
            entry["users"] -= 1
            if not entry["users"]:
                self._locks.pop(key, None)

    async def init_schema(self, db):
        await db.execute("""CREATE TABLE IF NOT EXISTS shift_widget_sessions (
            token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, city_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL DEFAULT 0, kind TEXT NOT NULL DEFAULT 'widget',
            revision INTEGER NOT NULL DEFAULT 0, district TEXT NOT NULL DEFAULT '',
            last_shift_id INTEGER, last_data TEXT, draft_step TEXT, draft_time TEXT,
            draft_shift_id INTEGER, draft_expires_at REAL, prompt_message_id INTEGER,
            last_input_message_id INTEGER NOT NULL DEFAULT 0, ui_version INTEGER NOT NULL DEFAULT 0,
            UNIQUE(user_id,city_id))""")
        # Совместимость с ранним локальным прототипом: только добавление колонок.
        columns = {r[1] for r in await (await db.execute("PRAGMA table_info(shift_widget_sessions)")).fetchall()}
        for name, declaration in {"draft_step": "TEXT", "draft_time": "TEXT", "draft_shift_id": "INTEGER",
                "draft_expires_at": "REAL", "prompt_message_id": "INTEGER",
                "last_input_message_id": "INTEGER NOT NULL DEFAULT 0",
                "ui_version": "INTEGER NOT NULL DEFAULT 0"}.items():
            if name not in columns:
                await db.execute(f"ALTER TABLE shift_widget_sessions ADD COLUMN {name} {declaration}")

    async def session(self, token=None, uid=None, city_id=None, pending=False):
        async with self.a.db_connect() as db:
            db.row_factory = aiosqlite.Row
            if token is not None:
                sql, args = "token=?", (token,)
            elif pending:
                sql, args = "user_id=? AND draft_step IS NOT NULL", (uid,)
            else:
                sql, args = "user_id=? AND city_id=?", (uid, city_id)
            row = await (await db.execute("SELECT * FROM shift_widget_sessions WHERE " + sql
                                         + " ORDER BY rowid DESC LIMIT 1", args)).fetchone()
        return dict(row) if row else None

    async def patch(self, token, **values):
        async with self.a.db_connect() as db:
            await db.execute("UPDATE shift_widget_sessions SET " + ",".join(f"{k}=?" for k in values)
                             + " WHERE token=?", (*values.values(), token))
            await db.commit()

    async def clear_draft(self, token):
        await self.patch(token, draft_step=None, draft_time=None, draft_shift_id=None,
                         draft_expires_at=None, prompt_message_id=None)

    def callback_data(self, row, action, sid=0):
        return f"sw:{row['token']}:{row['revision']}:{action}:{sid}"

    def build_html(self):
        code = (ASSETS / "widget.html").read_text(encoding="utf-8")
        image = "data:image/webp;base64," + base64.b64encode((ASSETS / "brand.webp").read_bytes()).decode("ascii")
        code = code.replace("__APP_URL__", json.dumps(self.a._crm_miniapp_link()))
        code = code.replace("__BRAND_IMAGE__", image)
        if len(code.encode()) > 32768 or len(zlib.compress(code.encode(), 9)) > 4096:
            raise ValueError("Размер HTML карточки превышает лимит Telegram")
        return code

    async def retire_legacy_controls(self, uid):
        """Убирает старые внешние клавиатуры нашей карточки при новом /start."""
        async with self.a.db_connect() as db:
            rows = await (await db.execute("SELECT message_id,prompt_message_id FROM shift_widget_sessions "
                "WHERE user_id=? AND ui_version=0", (uid,))).fetchall()
        for mid in {mid for row in rows for mid in row if mid}:
            try:
                await self.a.bot.edit_message_reply_markup(chat_id=uid, message_id=mid, reply_markup=None)
            except (TelegramBadRequest, TelegramNotFound):
                pass

    async def open_private(self, message, payload=None):
        if message.chat.type != "private" or not message.from_user:
            return
        a, uid = self.a, message.from_user.id
        user = await a.get_user(uid) or {}
        active = await a.get_active_shift(uid)
        city_id = (active or {}).get("city_id") or user.get("city_id")
        if payload:
            match = re.fullmatch(r"shift_(\d+)_(open|close|status)", payload)
            if not match:
                await message.answer("Откройте свою карточку командой /start.")
                return
            city_id = int(match[1])
        city = a.get_city(city_id)
        if not city or not user.get("full_name") or not user.get("role"):
            await message.answer("Для первого входа укажите имя, город и роль в приложении. "
                                 "После регистрации сменой можно управлять здесь.\n" + a._crm_miniapp_link())
            return
        if not user.get("statistics_visible", 1):
            await message.answer("Профиль скрыт руководителем. Обратитесь к администратору.")
            return
        if not await a._is_city_role_member(uid, city_id, user["role"]):
            await message.answer("Нет доступа к рабочей группе вашего города и роли.")
            return
        if active and active["city_id"] != city_id:
            await message.answer("У вас открыта смена в другом городе. Отправьте /start.")
            return
        await a.remember_telegram_username(uid, message.from_user.username)
        async with self.guard(uid):
            await self.retire_legacy_controls(uid)
            plan = await a._employee_planned_shift(uid, city, active)
            token = uuid.uuid4().hex[:20]
            district = (active or plan or {}).get("district") or ""
            async with a.db_connect() as db:
                await db.execute("UPDATE shift_widget_sessions SET draft_step=NULL,prompt_message_id=NULL WHERE user_id=?", (uid,))
                await db.execute("INSERT INTO shift_widget_sessions(token,user_id,city_id,district,last_shift_id,last_input_message_id,ui_version) "
                    "VALUES (?,?,?,?,?,?,1) ON CONFLICT(user_id,city_id) DO UPDATE SET token=excluded.token,"
                    "message_id=0,kind='widget',revision=0,district=excluded.district,last_shift_id=excluded.last_shift_id,"
                    "last_data=NULL,draft_step=NULL,draft_time=NULL,draft_shift_id=NULL,draft_expires_at=NULL,"
                    "prompt_message_id=NULL,ui_version=1,last_input_message_id=MAX(shift_widget_sessions.last_input_message_id,excluded.last_input_message_id)",
                    (token, uid, city_id, district, (active or {}).get("id"), message.message_id))
                await db.commit()
            row = await self.session(token)
            state = await self.state(row)
            code = self.build_html()
            try:
                sent = await a.bot(SendWidget(chat_id=uid, html=code, chat_data=self.encode(state)))
                mid = sent["message_id"]
            except (TelegramBadRequest, TelegramNotFound):
                row["kind"] = "text"
                sent = await a.bot.send_message(uid, self.text(state) + "\n\nКарточка недоступна. Откройте приложение:\n"
                                               + a._crm_miniapp_link(), parse_mode="HTML")
                mid = sent.message_id
            await self.patch(token, message_id=mid, kind=row["kind"])

    @staticmethod
    def encode(state):
        data = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        if len(data.encode()) > 1024:
            raise ValueError("Состояние карточки превышает лимит Telegram")
        return data

    async def state(self, row, notice=""):
        a = self.a
        user = await a.get_user(row["user_id"]) or {}
        city = a.get_city(row["city_id"])
        active = await a.get_active_shift(row["user_id"])
        if active and active["city_id"] != row["city_id"]:
            active = None
            notice = "Смена открыта в другом городе. Отправьте /start."
        last = await a.get_shift_by_id(row["last_shift_id"]) if row.get("last_shift_id") else None
        shift = active or (last if last and not last["is_active"] else None)
        now = datetime.now(a._city_tz(city))
        step = row.get("draft_step") or ""
        if step and (row.get("draft_expires_at") or 0) <= now.timestamp():
            step = ""
        status = ("waiting" if a._shift_is_scheduled(active) else "active") if active else "closed" if shift else "idle"
        start = a._parse_datetime((shift or {}).get("start_at"))
        end = a._parse_datetime((shift or {}).get("end_at"))
        state = {"k": row["token"], "r": row["revision"], "n": user.get("full_name", "")[:36],
                 "c": city["name"][:30], "role": (user.get("role") or "")[:16], "s": status,
                 "sid": (active or {}).get("id", 0), "d": (row["district"] if step.startswith("start_")
                 else (shift or {}).get("district") or row["district"])[:80], "step": step,
                 "time": row.get("draft_time") or "", "opts": a.CITY_DISTRICTS.get(city["name"].casefold(), [])[:10]
                 if step == "start_district" else [], "msg": notice[:100],
                 "start": start.astimezone(a._city_tz(city)).strftime("%H:%M") if start else "",
                 "end": end.astimezone(a._city_tz(city)).strftime("%H:%M") if end else "",
                 "date": (end or start or now).astimezone(a._city_tz(city)).strftime("%d.%m.%Y")}
        while len(json.dumps(state, ensure_ascii=False).encode()) > 1000 and state["opts"]:
            state["opts"].pop()
        return state

    @staticmethod
    def text(state):
        titles = {"idle": "Смена не открыта", "active": "На смене", "waiting": "Ожидает начала", "closed": "Смена закрыта"}
        instructions = {"start_time": "Напишите время начала, например 09:00, или нажмите «Сейчас».",
            "start_district": "Напишите район сообщением или выберите из списка.",
            "start_confirm": "Проверьте время и район, затем нажмите «Открыть смену».",
            "stop_time": "Напишите время окончания или нажмите «Сейчас».",
            "stop_confirm": "Проверьте время и нажмите «Закрыть смену»."}
        lines = ["<b>бибибайк · Моя смена</b>", html.escape(state["n"]),
                 html.escape(f"{state['c']} · {state['role']}"), f"<b>{titles[state['s']]}</b>", state["date"]]
        if state["d"]:
            lines.append("Район: " + html.escape(state["d"]))
        if state["start"]:
            lines.append("Начало: " + state["start"])
        if state["end"]:
            lines.append("Окончание: " + state["end"])
        if state["step"]:
            lines += ["", instructions[state["step"]]]
            if state["time"]:
                lines.append("Выбрано время: " + state["time"])
        if state["msg"]:
            lines += ["", html.escape(state["msg"])]
        return "\n".join(lines)

    async def render(self, row, notice=""):
        async with self.guard(("render", row["token"])):
            fresh = await self.session(row["token"])
            if not fresh or not fresh["message_id"]:
                return
            state = await self.state(fresh, notice)
            data = self.encode(state)
            if data == fresh.get("last_data"):
                return
            try:
                if fresh["kind"] == "widget":
                    await self.a.bot(EditWidget(chat_id=fresh["user_id"], message_id=fresh["message_id"], chat_data=data))
                else:
                    await self.a.bot.edit_message_text(self.text(state) + "\n\nОткройте приложение:\n"
                        + self.a._crm_miniapp_link(), chat_id=fresh["user_id"],
                        message_id=fresh["message_id"], parse_mode="HTML", reply_markup=None)
            except TelegramBadRequest as exc:
                if "not modified" not in str(exc):
                    raise
            await self.patch(fresh["token"], last_data=data)

    async def sync_shift_safely(self, shift_id):
        try:
            shift = await self.a.get_shift_by_id(shift_id)
            if not shift:
                return
            row = await self.session(uid=shift["user_id"], city_id=shift["city_id"])
            if row:
                if row["last_shift_id"] is None or row["last_shift_id"] < shift_id:
                    async with self.a.db_connect() as db:
                        await db.execute("UPDATE shift_widget_sessions SET last_shift_id=? WHERE token=? "
                            "AND (last_shift_id IS NULL OR last_shift_id<?)", (shift_id, row["token"], shift_id))
                        await db.commit()
                await asyncio.wait_for(self.render(row), timeout=5)
        except Exception as exc:
            self.a.logger.warning("Карточка смены %s не обновлена: %s", shift_id, type(exc).__name__)


    async def accept_input(self, message):
        return bool(message.chat.type == "private" and message.from_user and message.text
                    and not message.text.startswith("/") and await self.session(uid=message.from_user.id, pending=True))

    async def receive_input(self, message):
        a, uid = self.a, message.from_user.id
        async with self.guard(uid):
            row = await self.session(uid=uid, pending=True)
            if not row or message.chat.id != uid or message.message_id <= row["last_input_message_id"]:
                return
            if (row.get("draft_expires_at") or 0) <= datetime.now(timezone.utc).timestamp():
                await self.clear_draft(row["token"])
                await self.render(row, "Ввод истёк. Нажмите кнопку открытия или закрытия заново.")
                return
            text = message.text.strip()
            step = row["draft_step"]
            if step in {"start_time", "stop_time"}:
                value = a._valid_time(text)
                if not value:
                    await self.render(row, "Введите время в формате ЧЧ:ММ, например 09:00.")
                    return
                values = {"draft_time": value, "draft_step": "start_district" if step == "start_time" else "stop_confirm"}
            elif step == "start_district":
                if not text or len(text) > 160 or a._valid_time(text):
                    await self.render(row, "Напишите название района (до 160 символов), например ФМР, ЮМР.")
                    return
                values = {"district": text, "draft_step": "start_confirm"}
            else:
                await self.render(row, "Проверьте данные и подтвердите действие внутри карточки.")
                return
            async with a.db_connect() as db:
                cur = await db.execute("UPDATE shift_widget_sessions SET " + ",".join(f"{k}=?" for k in values)
                    + ",revision=revision+1,last_input_message_id=? WHERE token=? AND revision=? "
                    "AND last_input_message_id<?", (*values.values(), message.message_id, row["token"],
                                                    row["revision"], message.message_id))
                await db.commit()
            if cur.rowcount == 1:
                fresh = await self.session(row["token"])
                await self.render(fresh)

    async def cancel_input(self, message):
        row = await self.session(uid=message.from_user.id, pending=True)
        if row:
            async with self.guard(message.from_user.id):
                await self.patch(row["token"], revision=row["revision"] + 1)
                await self.clear_draft(row["token"])
                await self.render(row, "Ввод отменён. Смена не изменена.")
        else:
            await message.answer("Чтобы открыть карточку смены, отправьте /start.")

    async def perform(self, uid, chat_id, message_id, data):
        match = COMMAND.fullmatch(data)
        if not match:
            return "Неверная команда карточки."
        token, revision, action, expected_sid = match.groups()
        async with self.guard(uid):
            row = await self.session(token)
            if not row or row["user_id"] != uid or chat_id != uid or message_id != row["message_id"]:
                return "Карточка устарела. Отправьте /start."
            a, notice = self.a, ""
            async with a.db_connect() as db:
                sql = "UPDATE shift_widget_sessions SET revision=revision+1 WHERE token=?"
                args = (token,)
                if action not in {"refresh", "basic"}:
                    sql += " AND revision=?"
                    args += (int(revision),)
                cur = await db.execute(sql, args)
                await db.commit()
            if cur.rowcount != 1:
                await self.render(row, "Данные изменились. Проверьте карточку и повторите.")
                return "Данные изменились."
            row = await self.session(token)
            try:
                user = await a.get_user(uid) or {}
                city = a.get_city(row["city_id"])
                if not city or not user.get("statistics_visible", 1):
                    raise ValueError("Доступ к смене отключён. Обратитесь к руководителю.")
                active = await a.get_active_shift(uid)
                if active and active["city_id"] != row["city_id"]:
                    raise ValueError("Смена открыта в другом городе. Отправьте /start.")
                if action not in {"refresh", "basic", "cancel"} and not await a._is_city_role_member(uid, row["city_id"], user.get("role")):
                    raise ValueError("Нет доступа к рабочей группе вашего города и роли.")
                now = datetime.now(a._city_tz(city))
                if action in {"open", "close"}:
                    if action == "open" and active:
                        raise ValueError("Смена уже открыта.")
                    if action == "close" and (not active or expected_sid is None or int(expected_sid) != active["id"]):
                        raise ValueError("Смена изменилась. Обновите карточку.")
                    async with a.db_connect() as db:
                        await db.execute("UPDATE shift_widget_sessions SET draft_step=NULL,prompt_message_id=NULL WHERE user_id=? AND token<>?", (uid, token))
                        await db.commit()
                    await self.patch(token, draft_step="start_time" if action == "open" else "stop_time",
                        draft_time=None, draft_shift_id=(active or {}).get("id"), draft_expires_at=now.timestamp()+DRAFT_TTL)
                elif action in {"now", "commit"} or action.startswith("d"):
                    if (row.get("draft_expires_at") or 0) <= now.timestamp():
                        await self.clear_draft(token)
                        raise ValueError("Ввод истёк. Начните заново.")
                    step = row.get("draft_step")
                    if action == "now":
                        if step not in {"start_time", "stop_time"}:
                            raise ValueError("Время уже выбрано. Проверьте карточку.")
                        await self.patch(token, draft_time=now.strftime("%H:%M"),
                            draft_step="start_district" if step == "start_time" else "stop_confirm")
                    elif action.startswith("d"):
                        options = (await self.state(row))["opts"]
                        index = int(action[1:])
                        if step != "start_district" or index >= len(options):
                            raise ValueError("Выберите район в текущей карточке.")
                        await self.patch(token, district=options[index], draft_step="start_confirm")
                    else:
                        if step == "start_confirm":
                            if active:
                                await self.clear_draft(token)
                                raise ValueError("Смена уже открыта — возможно, автоматически по календарю.")
                            response = await a._employee_shift_start(uid, {"city_id": row["city_id"],
                                "time": row["draft_time"], "district": row["district"],
                                "auto_close": bool(user.get("auto_close")), "auto_close_hours": user.get("auto_close_hours")})
                        elif step == "stop_confirm":
                            if not active or active["id"] != row["draft_shift_id"] or int(expected_sid or 0) != active["id"]:
                                await self.clear_draft(token)
                                raise ValueError("Смена изменилась. Подтвердите закрытие заново.")
                            response = await a._employee_shift_stop(uid, {"time": row["draft_time"]}, expected_shift_id=row["draft_shift_id"])
                        else:
                            raise ValueError("Сначала выберите время и район.")
                        if response.status != 200:
                            raise ValueError(json.loads(response.text).get("message") or "Не удалось сохранить смену.")
                        if step == "start_confirm":
                            saved = await a.get_active_shift(uid)
                            await self.patch(token, last_shift_id=saved["id"])
                        else:
                            await self.patch(token, last_shift_id=row["draft_shift_id"])
                        await self.clear_draft(token)
                        notice = "Смена открыта." if step == "start_confirm" else "Смена закрыта. Отчёт сохранён."
                elif action == "cancel":
                    await self.clear_draft(token)
                    notice = "Ввод отменён. Смена не изменена."
                elif action == "basic":
                    notice = "Все действия теперь доступны внутри карточки. Для новой карточки отправьте /start."
            except (ValueError, a.CitySubscriptionRequired) as exc:
                notice = str(exc)
            except Exception:
                a.logger.exception("Ошибка карточки смены uid=%s", uid)
                notice = "Не удалось обновить карточку. Нажмите «Обновить» и проверьте смену."
            fresh = await self.session(token)
            if fresh:
                await self.render(fresh, notice)
            return notice

    async def callback(self, callback):
        # Обработчик нужен только для очистки кнопок прежней версии.
        await callback.answer("Управление перенесено внутрь карточки. Отправьте /start.")
        if callback.message and callback.message.chat.id == callback.from_user.id:
            try:
                await self.a.bot.edit_message_reply_markup(chat_id=callback.message.chat.id,
                    message_id=callback.message.message_id, reply_markup=None)
            except (TelegramBadRequest, TelegramNotFound):
                pass

    async def middleware(self, handler, event, data):
        raw = (getattr(event, "model_extra", None) or {}).get("widget_query")
        if raw is None:
            return await handler(event, data)
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            return
        actor, chat = raw.get("from") or {}, raw.get("chat") or {}
        match = COMMAND.fullmatch(str(raw.get("data") or ""))
        row = await self.session(match[1]) if match else None
        if chat.get("type") != "private" or not row or row["user_id"] != actor.get("id") or row["user_id"] != chat.get("id") or row["message_id"] != raw.get("message_id"):
            await self.a.bot(AnswerWidget(widget_query_id=raw["id"], data=json.dumps(
                {"error": "Карточка устарела. Отправьте /start."}, ensure_ascii=False)))
            return
        await self.a.bot(AnswerWidget(widget_query_id=raw["id"], data='{"pending":true}'))
        await self.perform(actor["id"], chat["id"], raw["message_id"], raw["data"])
