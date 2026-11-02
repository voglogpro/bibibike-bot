# -*- coding: utf-8 -*-
"""Регрессионная проверка бибибайк без обращения к Telegram и BotHost.

Запуск:
    python tests/regression_check.py main-4.py
    python tests/regression_check.py main.py
"""

import asyncio
import importlib.util
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
TARGET = (ROOT / (sys.argv[1] if len(sys.argv) > 1 else "main.py")).resolve()
TEMP_DIR = Path(tempfile.mkdtemp(prefix="bibibike-regression-"))

os.environ["BOT_TOKEN"] = "123456789:" + ("A" * 35)
os.environ["DATA_DIR"] = str(TEMP_DIR)
os.environ.pop("CITIES_CONFIG_JSON", None)

spec = importlib.util.spec_from_file_location("bibibike_regression_target", TARGET)
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)


class FakeMessage:
    def __init__(self, uid, chat_id, topic, message_id, text, *, date=None, edit_date=None,
                 media_group_id=None):
        self.text = text
        self.caption = None
        self.from_user = SimpleNamespace(
            id=uid, is_bot=False, full_name=f"Тестовый сотрудник {uid}"
        )
        self.chat = SimpleNamespace(id=chat_id, title="Регрессионный тест")
        self.message_thread_id = topic
        self.message_id = message_id
        self.date = date or datetime.now(timezone.utc)
        self.edit_date = edit_date
        self.media_group_id = media_group_id
        self.replies = []

    async def reply(self, text):
        self.replies.append(text)


def assert_action(actions, action_type, codes=None, quantity=None):
    item = next((entry for entry in actions if entry["action_type"] == action_type), None)
    assert item is not None, (action_type, actions)
    if codes is not None:
        assert item["bike_codes"] == codes, (codes, item)
    if quantity is not None:
        assert item["quantity"] == quantity, (quantity, item)


def check_parsers():
    photo_text = bot._photo_result_text([
        {"action_type": "move", "bike_codes": ["0915", "0103"], "quantity": 0},
        {"action_type": "repair", "bike_codes": ["0915"], "quantity": 0},
    ])
    assert photo_text == "0915 — переместил, требует ремонта\n0103 — переместил", photo_text
    assert photo_text.count("0915") == 1, photo_text
    assert "0103" in photo_text, photo_text
    assert bot._photo_result_text([]) == ""

    assert_action(bot.parse_message("Переместил 4821 4907 5512"), "move",
                  ["4821", "4907", "5512"], 0)
    assert_action(bot.parse_message("Поправил 4630"), "fix", ["4630"], 0)
    assert_action(bot.parse_message("4444 ремонт, колесо"), "repair", ["4444"], 0)
    assert_action(bot.parse_message("Привёз на СЦ 4901"), "to_sc", ["4901"], 0)
    assert_action(bot.parse_message("Вывез с СЦ 4902"), "from_sc", ["4902"], 0)

    assert bot.parse_message("переместил 13") == []
    assert bot.parse_message("ремонт 5") == []
    assert_action(bot.parse_message("поправил 13"), "fix", [], 13)
    assert_action(bot.parse_message("паправил 7"), "fix", [], 7)

    assert_action(bot.parse_npb_message("0001\n0002"), "battery", ["0001", "0002"], 0)
    mass_codes = [f"{index:04d}" for index in range(120)]
    assert_action(
        bot.parse_npb_message("08:05\n" + "\n".join(mass_codes)),
        "battery", mass_codes, 0,
    )
    assert_action(bot.parse_moves_message("0010\n0020"), "move", ["0010", "0020"], 0)
    assert_action(bot.parse_bare_repair_message("0011 не заводится"), "repair", ["0011"], 0)
    assert_action(bot.parse_sticker_message("Оклейка 0012"), "sticker", ["0012"], 0)

    polyana = bot.parse_polyana_message(
        "+3\n0000\n0001\n0002\nпоправил\n0999\n0888"
    )
    assert_action(polyana, "move", ["0000", "0001", "0002"], 0)
    assert_action(polyana, "fix", ["0999", "0888"], 0)


async def insert_shift(uid, city, role=None):
    role = role if role is not None else city.get("role_group", "")
    now = datetime.now(timezone.utc).isoformat()
    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO shifts "
            "(user_id, full_name, role, start_time, district, is_active, created_at, "
            "city_id, start_at, source) VALUES (?, ?, ?, '10:00', '', 1, ?, ?, ?, 'test')",
            (uid, f"Тестовый сотрудник {uid}", role, now, city["id"], now),
        )
        await db.commit()
        return cursor.lastrowid


async def check_configuration_and_routing():
    driver = bot.get_city_by_group(bot.KHIMKI_DRIVERS_GROUP_ID)
    charger = bot.get_city_by_group(bot.KHIMKI_CHARGERS_GROUP_ID)
    scout = bot.get_city_by_group(bot.KHIMKI_SCOUTS_GROUP_ID)

    assert driver and driver["role_group"] == "Водитель"
    assert charger and charger["role_group"] == "Чарджер"
    assert scout and scout["role_group"] == "Скаут"
    assert charger["topic_reports"] == 3
    assert charger["topic_npb"] == 4
    assert bot.city_for_role(charger["id"], "Чарджер")["group_id"] == bot.KHIMKI_CHARGERS_GROUP_ID

    reports_filter = bot.CityTopicFilter("reports")
    work_filter = bot.CityTopicFilter("work")
    charger_report = FakeMessage(1, bot.KHIMKI_CHARGERS_GROUP_ID, 3, 1, "/10:00")
    charger_battery = FakeMessage(1, bot.KHIMKI_CHARGERS_GROUP_ID, 4, 2, "0001")
    charger_other = FakeMessage(1, bot.KHIMKI_CHARGERS_GROUP_ID, 5, 3, "0002")

    assert await reports_filter(charger_report)
    assert not await work_filter(charger_report)
    assert not await reports_filter(charger_battery)
    assert await work_filter(charger_battery)
    assert not await reports_filter(charger_other)
    assert not await work_filter(charger_other)
    assert bot.topic_parser_kind(charger, 4) == "npb"

    stavropol = next(
        city for city in bot.CITIES_BY_ID.values() if city["city_key"] == "stavropol"
    )
    stavropol_scout = bot.get_city_by_group(bot.STAVROPOL_SCOUTS_GROUP_ID)
    stavropol_transport = bot.get_city_by_group(bot.STAVROPOL_TRANSPORT_GROUP_ID)
    assert stavropol_scout and stavropol_scout["role_group"] == "Скаут"
    assert stavropol_transport and set(stavropol_transport["role_groups"]) == {
        "Водитель", "Чарджер",
    }
    assert bot.city_for_role(stavropol["id"], "Водитель")["group_id"] == \
        bot.STAVROPOL_TRANSPORT_GROUP_ID
    assert bot.city_for_role(stavropol["id"], "Чарджер")["group_id"] == \
        bot.STAVROPOL_TRANSPORT_GROUP_ID
    assert bot.report_city_for_role(stavropol["id"], "Скаут")["group_id"] == \
        bot.STAVROPOL_GROUP_ID
    assert set(bot.city_supported_roles(stavropol["id"])) == {
        "Скаут", "Водитель", "Чарджер",
    }

    stavropol_report = FakeMessage(2, bot.STAVROPOL_GROUP_ID, None, 10, "/10:00")
    stavropol_scout_work = FakeMessage(
        2, bot.STAVROPOL_SCOUTS_GROUP_ID, bot.STAVROPOL_SCOUTS_TOPIC_WORK,
        11, "переместил 0001",
    )
    stavropol_driver_work = FakeMessage(
        2, bot.STAVROPOL_TRANSPORT_GROUP_ID, bot.STAVROPOL_DRIVERS_TOPIC_WORK,
        12, "переместил 0002",
    )
    stavropol_battery = FakeMessage(
        2, bot.STAVROPOL_TRANSPORT_GROUP_ID, bot.STAVROPOL_CHARGERS_TOPIC_BATTERY,
        13, "0003 0004",
    )
    stavropol_other = FakeMessage(2, bot.STAVROPOL_TRANSPORT_GROUP_ID, 99, 14, "0005")
    assert await reports_filter(stavropol_report)
    assert not await work_filter(stavropol_report)
    assert await work_filter(stavropol_scout_work)
    assert await work_filter(stavropol_driver_work)
    assert await work_filter(stavropol_battery)
    assert not await work_filter(stavropol_other)
    assert bot.topic_parser_kind(stavropol_scout, bot.STAVROPOL_SCOUTS_TOPIC_WORK) == "tasks"
    assert bot.topic_parser_kind(stavropol_transport, bot.STAVROPOL_DRIVERS_TOPIC_WORK) == "tasks"
    assert bot.topic_parser_kind(
        stavropol_transport, bot.STAVROPOL_CHARGERS_TOPIC_BATTERY,
    ) == "npb"


async def check_database_and_message_burst():
    driver = bot.get_city_by_group(bot.KHIMKI_DRIVERS_GROUP_ID)
    uid = 920001
    shift_id = await insert_shift(uid, driver)
    bot.schedule_report_update = lambda _shift_id: None

    messages = [
        FakeMessage(
            uid,
            bot.KHIMKI_DRIVERS_GROUP_ID,
            bot.KHIMKI_DRIVERS_TOPIC_MOVES,
            1000 + index,
            "\n".join(f"{3000 + index * 4 + offset:04d}" for offset in range(4)),
        )
        for index in range(25)
    ]
    await asyncio.gather(*(
        bot.process_work_message(message, driver, moves=True) for message in messages
    ))
    stats = await bot.get_stats(shift_id)
    assert stats["move"] == 100, stats

    original_date = datetime.now(timezone.utc)
    original = FakeMessage(
        uid, bot.KHIMKI_DRIVERS_GROUP_ID, bot.KHIMKI_DRIVERS_TOPIC_MOVES,
        5000, "4100", date=original_date,
    )
    await bot.process_work_message(original, driver, moves=True)
    edited = FakeMessage(
        uid, bot.KHIMKI_DRIVERS_GROUP_ID, bot.KHIMKI_DRIVERS_TOPIC_MOVES,
        5000, "4101 4102", date=original_date,
        edit_date=original_date + timedelta(seconds=5),
    )
    await bot.process_work_message(edited, driver, moves=True, edited=True)
    stats = await bot.get_stats(shift_id)
    assert stats["move"] == 102, stats

    charger = bot.get_city_by_group(bot.KHIMKI_CHARGERS_GROUP_ID)
    mismatched = FakeMessage(
        uid, bot.KHIMKI_CHARGERS_GROUP_ID, 4, 6000, "4200 4201"
    )
    await bot.process_work_message(mismatched, charger, npb=True)
    stats = await bot.get_stats(shift_id)
    assert stats["battery"] == 0, stats
    assert not bot._work_ingest_locks, bot._work_ingest_locks

    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        tables = {
            row[0] for row in await (
                await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
        assert {"cities", "users", "shifts", "actions", "work_message_links"} <= tables
        action_rows = (
            await (await db.execute(
                "SELECT COUNT(*) FROM actions WHERE shift_id = ?", (shift_id,)
            )).fetchone()
        )[0]
        assert action_rows == 26, action_rows


async def check_stavropol_processing():
    transport = bot.get_city_by_group(bot.STAVROPOL_TRANSPORT_GROUP_ID)
    bot.schedule_report_update = lambda _shift_id: None

    driver_uid = 930001
    driver_shift = await insert_shift(driver_uid, transport, "Водитель")
    driver_message = FakeMessage(
        driver_uid, bot.STAVROPOL_TRANSPORT_GROUP_ID,
        bot.STAVROPOL_DRIVERS_TOPIC_WORK, 7001, "переместил 5100 5101",
    )
    await bot.process_work_message(driver_message, transport)
    driver_battery = FakeMessage(
        driver_uid, bot.STAVROPOL_TRANSPORT_GROUP_ID,
        bot.STAVROPOL_CHARGERS_TOPIC_BATTERY, 7002, "5102 5103",
    )
    await bot.process_work_message(driver_battery, transport, npb=True)
    driver_stats = await bot.get_stats(driver_shift)
    assert driver_stats["move"] == 2 and driver_stats["battery"] == 2, driver_stats

    charger_uid = 930002
    charger_shift = await insert_shift(charger_uid, transport, "Чарджер")
    charger_message = FakeMessage(
        charger_uid, bot.STAVROPOL_TRANSPORT_GROUP_ID,
        bot.STAVROPOL_CHARGERS_TOPIC_BATTERY, 7003, "5200 5201",
    )
    await bot.process_work_message(charger_message, transport, npb=True)
    charger_stats = await bot.get_stats(charger_shift)
    assert charger_stats["battery"] == 2, charger_stats

    scout_uid = 930003
    scout_shift = await insert_shift(scout_uid, transport, "Скаут")
    scout_message = FakeMessage(
        scout_uid, bot.STAVROPOL_TRANSPORT_GROUP_ID,
        bot.STAVROPOL_CHARGERS_TOPIC_BATTERY, 7004, "5300 5301 5302",
    )
    await bot.process_work_message(scout_message, transport, npb=True)
    scout_stats = await bot.get_stats(scout_shift)
    assert scout_stats["battery"] == 3, scout_stats


async def check_charger_close_race():
    """Закрытие не обгоняет очередь, а запоздавший update сверяется по времени."""
    city = bot.get_city_by_group(bot.GROUP_ID)
    bot.schedule_report_update = lambda _shift_id: None

    # Сначала воспроизводим настоящий конфликт: обработчик действия уже держит
    # очередь, а закрытие запускается параллельно и обязано дождаться записи.
    uid = 940001
    shift_id = await insert_shift(uid, city, "Чарджер")
    event_at = datetime.now(timezone.utc)
    started = asyncio.Event()
    release = asyncio.Event()
    original_handler = bot._process_work_message_locked

    async def slow_handler(*args, **kwargs):
        started.set()
        await release.wait()
        return await original_handler(*args, **kwargs)

    message = FakeMessage(
        uid, bot.GROUP_ID, bot.NPB_THREAD_ID, 8001, "0101", date=event_at,
    )
    with patch.object(bot, "_process_work_message_locked", side_effect=slow_handler):
        action_task = asyncio.create_task(bot.process_work_message(message, city, npb=True))
        await started.wait()
        close_task = asyncio.create_task(bot.end_shift(
            uid, event_at.astimezone(bot._city_tz(city)).strftime("%H:%M"),
            city_id=city["id"], now=event_at,
            end_at_override=event_at + timedelta(seconds=1),
        ))
        await asyncio.sleep(0)
        assert not close_task.done(), "закрытие обогнало действие в очереди"
        release.set()
        await asyncio.gather(action_task, close_task)
    assert (await bot.get_stats(shift_id))["battery"] == 1

    # Даже если handler начал работу только после закрытия, Telegram timestamp
    # до end_at безопасно возвращает действие в правильную закрытую смену.
    delayed_uid = 940002
    delayed_shift_id = await insert_shift(delayed_uid, city, "Чарджер")
    sent_at = datetime.now(timezone.utc)
    closed_at = sent_at + timedelta(seconds=2)
    await bot.end_shift(
        delayed_uid, closed_at.astimezone(bot._city_tz(city)).strftime("%H:%M"),
        city_id=city["id"], now=closed_at, end_at_override=closed_at,
    )
    delayed = FakeMessage(
        delayed_uid, bot.GROUP_ID, bot.NPB_THREAD_ID, 8002,
        "\n".join(f"{2000 + index:04d}" for index in range(20)), date=sent_at,
    )
    await bot.process_work_message(delayed, city, npb=True)
    assert (await bot.get_stats(delayed_shift_id))["battery"] == 20

    too_late = FakeMessage(
        delayed_uid, bot.GROUP_ID, bot.NPB_THREAD_ID, 8003, "2999",
        date=closed_at + timedelta(seconds=1),
    )
    await bot.process_work_message(too_late, city, npb=True)
    assert (await bot.get_stats(delayed_shift_id))["battery"] == 20
    assert not bot._work_ingest_locks, bot._work_ingest_locks


async def check_report_rendering():
    city = bot.get_city_by_group(bot.KHIMKI_CHARGERS_GROUP_ID)
    shift = {
        "id": 999,
        "user_id": 1,
        "full_name": "Тест Чарджер",
        "role": "Чарджер",
        "city_id": city["id"],
        "start_time": "10:00",
        "end_time": None,
        "start_at": datetime.now(timezone.utc).isoformat(),
        "end_at": None,
        "is_active": 1,
        "district": "",
        "comment": "",
        "on_lunch": 1,
    }
    stats = {"move": 0, "fix": 0, "repair": 0, "battery": 2,
             "sticker": 0, "to_sc": 0, "from_sc": 0}
    text = bot.build_report_text(shift, stats)
    assert "Сейчас на обеде" in text
    assert "Поменял АКБ: 2" in text


async def check_photo_result_reply():
    # По умолчанию бот в рабочий чат не отвечает (только лог).
    silent = FakeMessage(77, -100, None, 14, "")
    await bot._send_photo_result(silent, [
        {"action_type": "move", "bike_codes": ["0915"], "quantity": 0}
    ])
    assert silent.replies == []
    with patch.object(bot, "PHOTO_GROUP_REPLIES", True):
        await _check_photo_result_reply_enabled()


async def _check_photo_result_reply_enabled():
    message = FakeMessage(77, -100, None, 15, "")
    await bot._reply_with_photo_result(message, [
        {"action_type": "move", "bike_codes": ["0915"], "quantity": 0}
    ])
    assert message.replies == ["0915 — переместил"]

    first = FakeMessage(77, -100, None, 16, "", media_group_id="album-1")
    second = FakeMessage(77, -100, None, 17, "", media_group_id="album-1")
    with patch.object(bot, "PHOTO_MEDIA_GROUP_REPLY_DELAY_SEC", 0.01):
        await bot._reply_with_photo_result(first, [
            {"action_type": "move", "bike_codes": ["0103"], "quantity": 0},
        ])
        await bot._reply_with_photo_result(second, [
            {"action_type": "move", "bike_codes": ["0915"], "quantity": 0},
            {"action_type": "repair", "bike_codes": ["0915"], "quantity": 0},
        ])
        await asyncio.sleep(0.03)
    assert first.replies == ["0103 — переместил\n0915 — переместил, требует ремонта"]
    assert second.replies == []


def _photo_message(uid, message_id, *, caption=None, media_group_id=None, date=None):
    message = FakeMessage(
        uid, bot.STAVROPOL_TRANSPORT_GROUP_ID,
        bot.STAVROPOL_DRIVERS_TOPIC_WORK, message_id, None,
        media_group_id=media_group_id, date=date,
    )
    message.caption = caption
    message.photo = [SimpleNamespace(file_id=f"photo-{message_id}")]
    return message


def _photo_analysis(kinds):
    """Подмена скачивания и разбора: message_id -> результат анализа фото."""
    async def analyze(message):
        return kinds[message.message_id]
    return AsyncMock(side_effect=analyze)


def _screen(*codes, action="move"):
    return {"kind": "screenshot", "screen": "card",
            "items": [(action, code) for code in codes], "codes": list(codes)}


STREET = {"kind": "street"}


async def check_photo_parking_rule():
    """Скриншот засчитывается перемещением только вместе с фото парковки."""
    city = bot.get_city_by_group(bot.STAVROPOL_TRANSPORT_GROUP_ID)
    uid = 930004
    await bot.add_user(uid, "Фото Тест", "Водитель", city["id"])
    await bot.set_user_photo_parse(uid, True)
    shift_id = await insert_shift(uid, city, "Водитель")
    dm = AsyncMock()
    patches = [
        patch.object(bot.bot, "send_message", dm),
        patch.object(bot, "PHOTO_PARKING_REMINDER_SEC", 0.05),
        patch.object(bot, "PHOTO_REPOST_ENABLED", False),
    ]
    for item in patches:
        item.start()
    try:
        await _check_photo_parking_rule(uid, city, shift_id, dm)
    finally:
        for item in patches:
            item.stop()
    await check_photo_repost(city)


async def check_photo_repost(city):
    """Пачка «скриншоты + парковка» заменяется одним аккуратным постом."""
    uid = 930005
    await bot.add_user(uid, "Иван Петров", "Водитель", city["id"])
    await bot.set_user_photo_parse(uid, True)
    shift_id = await insert_shift(uid, city, "Водитель")
    album = [
        _photo_message(uid, 7201, media_group_id="post-album"),
        _photo_message(uid, 7202, media_group_id="post-album"),
        _photo_message(uid, 7203, media_group_id="post-album", caption="поправил 3"),
    ]
    kinds = {
        7201: {"kind": "screenshot", "screen": "card", "codes": ["0537"],
               "items": [("move", "0537")]},
        7202: {"kind": "screenshot", "screen": "card", "codes": ["0612"],
               "items": [("to_sc", "0612")]},
        7203: STREET,
    }
    sent = []
    markups = []
    for item in album:
        item.from_user.username = "ivan_petrov"

    async def send_photo(chat_id, photo, caption=None, message_thread_id=None, parse_mode=None,
                         reply_markup=None):
        assert parse_mode == "HTML"
        sent.append((chat_id, photo, caption, message_thread_id))
        markups.append(reply_markup)
        return SimpleNamespace(message_id=9001)

    delete = AsyncMock()
    with patch.object(bot, "analyze_photo_message", _photo_analysis(kinds)), \
            patch.object(bot, "PHOTO_REPOST_DELAY_SEC", 0.02), \
            patch.object(bot.bot, "send_photo", AsyncMock(side_effect=send_photo)), \
            patch.object(bot.bot, "delete_message", delete):
        for item in album:
            await bot.process_work_message(item, city)
        await asyncio.sleep(0.15)
    stats = await bot.get_stats(shift_id)
    assert stats["move"] == 1 and stats["to_sc"] == 1 and stats["fix"] == 3, stats
    assert len(sent) == 1, sent
    chat_id, photo, caption, thread = sent[0]
    assert photo == "photo-7203" and thread == bot.STAVROPOL_DRIVERS_TOPIC_WORK
    assert caption.splitlines()[0] == (
        '<a href="https://t.me/ivan_petrov"><b>Иван Петров</b></a> · <code>@ivan_petrov</code>'
    ), caption
    button = markups[0].inline_keyboard[0][0].model_dump(exclude_none=True)
    assert button == {"text": "📋 Скопировать @ivan_petrov",
                      "copy_text": {"text": "@ivan_petrov"}}, button
    assert caption.splitlines()[1].startswith("🕒 "), caption
    assert "<code>0537</code> · перемещён" in caption, caption
    assert "<code>0612</code> · на складе" in caption, caption
    assert "🛠 Поправлено: 3" in caption and "💬 <i>поправил 3</i>" in caption, caption
    deleted = sorted(call.args[1] for call in delete.await_args_list)
    assert deleted == [7201, 7202, 7203], deleted
    assert bot._photo_packets == {}

    # Только фото парковки с подписью — без скриншота пост не делается.
    lonely = _photo_message(uid, 7204, caption="поправил 2")
    send_photo_mock = AsyncMock()
    with patch.object(bot, "analyze_photo_message", _photo_analysis({7204: STREET})), \
            patch.object(bot, "PHOTO_REPOST_DELAY_SEC", 0.02), \
            patch.object(bot.bot, "send_photo", send_photo_mock), \
            patch.object(bot.bot, "delete_message", AsyncMock()) as lonely_delete:
        await bot.process_work_message(lonely, city)
        await asyncio.sleep(0.1)
    assert send_photo_mock.await_count == 0 and lonely_delete.await_count == 0
    stats = await bot.get_stats(shift_id)
    assert stats["fix"] == 5, stats


async def _check_photo_parking_rule(uid, city, shift_id, dm):
    # 1. Скриншот без парковки: перемещения нет, подпись считается как текст.
    screen = _photo_message(uid, 7101, caption="0001 требует ремонта")
    with patch.object(bot, "analyze_photo_message", _photo_analysis({7101: _screen("0915")})):
        await bot.process_work_message(screen, city)
    stats = await bot.get_stats(shift_id)
    assert stats["move"] == 0 and stats["repair"] == 1, stats
    assert screen.replies == [], screen.replies

    # 2. Следом фото парковки: ожидавший скриншот записывается перемещением,
    #    ремонт из подписи сохраняется, ответ приходит под скриншотом.
    street = _photo_message(uid, 7102)
    with patch.object(bot, "analyze_photo_message", _photo_analysis({7102: STREET})), \
            patch.object(bot, "PHOTO_MEDIA_GROUP_REPLY_DELAY_SEC", 0.01):
        await bot.process_work_message(street, city)
    stats = await bot.get_stats(shift_id)
    assert stats["move"] == 1 and stats["repair"] == 1, stats
    # В рабочий чат бот не пишет; напоминание о парковке не ушло — успели.
    assert screen.replies == [] and street.replies == [], (screen.replies, street.replies)
    await asyncio.sleep(0.1)
    assert dm.await_count == 0, dm.await_args_list

    # 3. Правка подписи подтверждённого скриншота не теряет перемещение.
    edited = _photo_message(uid, 7101, caption="0001 требует ремонта, 0002 требует ремонта")
    edited.edit_date = datetime.now(timezone.utc) + timedelta(seconds=5)
    with patch.object(bot, "analyze_photo_message", _photo_analysis({7101: _screen("0915")})):
        await bot.process_work_message(edited, city, edited=True)
    stats = await bot.get_stats(shift_id)
    assert stats["move"] == 1 and stats["repair"] == 2, stats

    # 4. Альбом: парковка и два скриншота (карточка + список) — один ответ.
    album = [
        _photo_message(uid, 7103, media_group_id="work-album-1"),
        _photo_message(uid, 7104, media_group_id="work-album-1"),
        _photo_message(uid, 7105, media_group_id="work-album-1"),
    ]
    kinds = {
        7103: _screen("0103"),
        7104: STREET,
        7105: {"kind": "screenshot", "screen": "list", "codes": ["0201", "0202", "0103"],
               "items": [("move", "0201"), ("move", "0202"), ("move", "0103")]},
    }
    with patch.object(bot, "analyze_photo_message", _photo_analysis(kinds)), \
            patch.object(bot, "PHOTO_MEDIA_GROUP_REPLY_DELAY_SEC", 0.01):
        for item in album:
            await bot.process_work_message(item, city)
        await asyncio.sleep(0.05)
    stats = await bot.get_stats(shift_id)
    # 0103 есть и на карточке, и в списке — одно перемещение, не два.
    assert stats["move"] == 4, stats
    assert all(item.replies == [] for item in album)

    # 5. Подпись уже называет тот же номер — дубля перемещения нет.
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    same = _photo_message(uid, 7106, caption="Переместил 0300", date=old)
    same_street = _photo_message(uid, 7107, date=old + timedelta(seconds=30))
    with patch.object(bot, "analyze_photo_message",
                      _photo_analysis({7106: _screen("0300"), 7107: STREET})), \
            patch.object(bot, "PHOTO_MEDIA_GROUP_REPLY_DELAY_SEC", 0.01):
        await bot.process_work_message(same, city)
        await bot.process_work_message(same_street, city)
    stats = await bot.get_stats(shift_id)
    assert stats["move"] == 5, stats

    # 6. Парковка пришла слишком поздно — перемещение не записывается.
    late_screen = _photo_message(uid, 7108, date=old + timedelta(minutes=10))
    late_street = _photo_message(
        uid, 7109, date=old + timedelta(minutes=10, seconds=bot.PHOTO_PARKING_WINDOW_SEC + 60),
    )
    with patch.object(bot, "analyze_photo_message",
                      _photo_analysis({7108: _screen("0400"), 7109: STREET})):
        await bot.process_work_message(late_screen, city)
        await bot.process_work_message(late_street, city)
    stats = await bot.get_stats(shift_id)
    assert stats["move"] == 5, stats
    assert late_screen.replies == []
    # Скриншот без парковки: напоминание в личку, не в чат.
    await asyncio.sleep(0.1)
    assert dm.await_count == 1, dm.await_args_list
    assert dm.await_args.args[0] == uid and "нет фото парковки" in dm.await_args.args[1]

    # Номер на скриншоте не прочитан — сразу сообщение в личку.
    unreadable = _photo_message(uid, 7111)
    with patch.object(bot, "analyze_photo_message", _photo_analysis({7111: {
        "kind": "screenshot", "screen": "unknown", "items": [], "codes": [],
        "problem": "unrecognized",
    }})):
        await bot.process_work_message(unreadable, city)
    assert dm.await_count == 2 and "Не смог прочитать номер" in dm.await_args.args[1]
    assert unreadable.replies == []

    # 7. Без тумблера фото не разбирается вовсе.
    await bot.set_user_photo_parse(uid, False)
    analyze = _photo_analysis({7110: _screen("0500")})
    with patch.object(bot, "analyze_photo_message", analyze):
        await bot.process_work_message(_photo_message(uid, 7110), city)
    assert analyze.await_count == 0
    assert bot._photo_media_group_pending == {}


def check_screenshot_recognition():
    """Распознавание на настоящих пикселях техприложения (без имён сотрудников)."""
    fixtures = ROOT / "tests" / "fixtures"
    selection = (fixtures / "screen_selection.jpg").read_bytes()
    card = (fixtures / "screen_card_online.jpg").read_bytes()
    result = bot.analyze_bike_photo(selection)
    assert result == {"kind": "screenshot", "screen": "list", "codes": ["0537"],
                      "items": [("move", "0537")]}, result
    result = bot.analyze_bike_photo(card)
    assert result == {"kind": "screenshot", "screen": "card", "codes": ["0537"],
                      "items": [("move", "0537")]}, result

    from io import BytesIO
    from PIL import Image
    # Другие разрешения экрана — тот же результат.
    for size in ((720, 1600), (576, 1280)):
        for raw, screen in ((selection, "list"), (card, "card")):
            image = Image.open(BytesIO(raw)).convert("RGB")
            image = image.resize((size[0], int(image.height * size[0] / image.width)))
            buffer = BytesIO()
            image.save(buffer, "JPEG", quality=85)
            result = bot.analyze_bike_photo(buffer.getvalue())
            assert result["codes"] == ["0537"] and result["screen"] == screen, (size, result)

    # Карточка, где «На линии» выключен: номер виден, но перемещения нет.
    image = Image.open(BytesIO(card)).convert("RGB")
    width, height = image.size
    states = bot._photo_read_toggles_v2(image, bot_numpy())
    assert states == [False, False, True, False], states

    # Фото улицы (пёстрая картинка) — это парковка, а не скриншот.
    import random
    rng = random.Random(7)
    street = Image.new("RGB", (480, 640))
    street.putdata([(rng.randrange(40, 200), rng.randrange(40, 200), rng.randrange(40, 200))
                    for _ in range(480 * 640)])
    buffer = BytesIO()
    street.save(buffer, "JPEG", quality=80)
    assert bot.analyze_bike_photo(buffer.getvalue()) == {"kind": "street"}


def check_photo_post_caption():
    actions = [
        {"action_type": "move", "bike_codes": "0537", "quantity": 0},
        {"action_type": "repair", "bike_codes": "0537", "quantity": 0},
        {"action_type": "to_sc", "bike_codes": "0612", "quantity": 0},
        {"action_type": "fix", "bike_codes": "", "quantity": 2},
    ]
    caption = bot._photo_post_caption(
        "Анна <Тест>", "09:05", actions, ["коммент & ещё"],
        bot._photo_post_author_link(1, "@anna_scout"), "anna_scout",
    )
    assert caption == (
        '<a href="https://t.me/anna_scout"><b>Анна &lt;Тест&gt;</b></a>'
        ' · <code>@anna_scout</code>\n'
        "🕒 09:05\n\n"
        "<code>0537</code> · перемещён, требует ремонта\n"
        "<code>0612</code> · на складе\n"
        "🛠 Поправлено: 2\n"
        "💬 <i>коммент &amp; ещё</i>"
    ), caption
    many = [{"action_type": "move", "bike_codes": f"{index:04d}", "quantity": 0}
            for index in range(300)]
    long_caption = bot._photo_post_caption("Имя", "10:00", many, [], None)
    visible = __import__("re").sub(r"<[^>]+>", "", long_caption)
    assert len(visible) <= 1024 and "… и ещё" in long_caption, len(visible)


def bot_numpy():
    import numpy
    return numpy


async def main():
    try:
        await bot.init_db()
        check_parsers()
        await check_configuration_and_routing()
        await check_stavropol_processing()
        await check_database_and_message_burst()
        await check_charger_close_race()
        await check_report_rendering()
        await check_photo_result_reply()
        await check_photo_parking_rule()
        check_screenshot_recognition()
        check_photo_post_caption()
        print(f"PASS {TARGET.name}: parser, routing, database, burst, edit, roles, report, photo rules, screenshots")
    finally:
        await bot.bot.session.close()
        shutil.rmtree(TEMP_DIR, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
