# -*- coding: utf-8 -*-
"""Регрессионная проверка BibiBike без обращения к Telegram и BotHost.

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
    def __init__(self, uid, chat_id, topic, message_id, text, *, date=None, edit_date=None):
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


def assert_action(actions, action_type, codes=None, quantity=None):
    item = next((entry for entry in actions if entry["action_type"] == action_type), None)
    assert item is not None, (action_type, actions)
    if codes is not None:
        assert item["bike_codes"] == codes, (codes, item)
    if quantity is not None:
        assert item["quantity"] == quantity, (quantity, item)


def check_parsers():
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
    wrong_battery = FakeMessage(
        driver_uid, bot.STAVROPOL_TRANSPORT_GROUP_ID,
        bot.STAVROPOL_CHARGERS_TOPIC_BATTERY, 7002, "5102 5103",
    )
    await bot.process_work_message(wrong_battery, transport, npb=True)
    driver_stats = await bot.get_stats(driver_shift)
    assert driver_stats["move"] == 2 and driver_stats["battery"] == 0, driver_stats

    charger_uid = 930002
    charger_shift = await insert_shift(charger_uid, transport, "Чарджер")
    charger_message = FakeMessage(
        charger_uid, bot.STAVROPOL_TRANSPORT_GROUP_ID,
        bot.STAVROPOL_CHARGERS_TOPIC_BATTERY, 7003, "5200 5201",
    )
    await bot.process_work_message(charger_message, transport, npb=True)
    charger_stats = await bot.get_stats(charger_shift)
    assert charger_stats["battery"] == 2, charger_stats


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


async def main():
    try:
        await bot.init_db()
        check_parsers()
        await check_configuration_and_routing()
        await check_stavropol_processing()
        await check_database_and_message_burst()
        await check_report_rendering()
        print(f"PASS {TARGET.name}: parser, routing, database, burst, edit, roles, report")
    finally:
        await bot.bot.session.close()
        shutil.rmtree(TEMP_DIR, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
