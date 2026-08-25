"""Контракт БибиПасса: прогрессия, награды, задачи, рейтинг и подписка."""
import asyncio
import importlib.util
import os
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory(prefix="bibibike-bibipass-")
os.environ["DATA_DIR"] = TMP.name
os.environ["TOKEN"] = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
os.environ["BIBIPASS_SEASON_ID"] = "test-quest-2026-08-26"
os.environ["BIBIPASS_SEASON_START"] = "2026-08-26T09:00:00+03:00"
os.environ["BIBIPASS_SEASON_END"] = "2026-09-15T09:00:00+03:00"

spec = importlib.util.spec_from_file_location("bibibike_bibipass_test", ROOT / "main.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)


async def run():
    await bot.init_db()
    season = bot._bibipass_season()
    rewards = bot.BIBIPASS_REWARDS

    before = bot._bibipass_season(datetime(2026, 8, 26, 8, 59, 59, tzinfo=bot.MSK))
    active = bot._bibipass_season(datetime(2026, 8, 26, 9, 0, 0, tzinfo=bot.MSK))
    ended = bot._bibipass_season(datetime(2026, 9, 15, 9, 0, 0, tzinfo=bot.MSK))
    assert before["status"] == "upcoming" and before["active"] is False
    assert active["status"] == "active" and active["active"] is True
    assert ended["status"] == "ended" and ended["active"] is False
    assert active["duration_days"] == 20
    assert active["start_at"] == "2026-08-26T09:00:00+03:00"
    assert active["end_at"] == "2026-09-15T09:00:00+03:00"

    assert len(rewards) == 20
    assert rewards[0]["points_needed"] == 20
    assert rewards[-1]["points_needed"] == 115
    assert rewards[-1]["cumulative_points"] == 1350
    assert sum(item["bibibonuses"] for item in rewards) == 150
    assert [(item["level"], item["subscription_months"]) for item in rewards
            if item["subscription_months"]] == [(10, 1), (20, 3)]
    assert sum(item["subscription_months"] for item in rewards) == 4
    assert bot._bibipass_level(19.5)["level"] == 0
    assert bot._bibipass_level(20)["level"] == 1
    assert bot._bibipass_level(45)["level"] == 2
    assert bot._bibipass_level(1350)["level"] == 20

    cities = list(bot.CITIES_BY_ID.values())
    first, second = cities[0], cities[1]
    stamp = "2026-08-30T10:00:00+03:00"
    async with bot.db_connect() as db:
        await db.executemany(
            "INSERT INTO users (user_id,full_name,role,city_id) VALUES (?,?,?,?)",
            [(701, "Первый участник", "Скаут", first["id"]),
             (702, "Второй участник", "Водитель", second["id"]),
             (703, "Новый участник", "Чарджер", first["id"])],
        )
        first_shift = (await db.execute(
            "INSERT INTO shifts (user_id,full_name,role,start_time,created_at,start_at,"
            "city_id,is_active) VALUES (?,?,?,?,?,?,?,0)",
            (701, "Первый участник", "Скаут", "10:00", stamp, stamp, first["id"]),
        )).lastrowid
        second_shift = (await db.execute(
            "INSERT INTO shifts (user_id,full_name,role,start_time,created_at,start_at,"
            "city_id,is_active) VALUES (?,?,?,?,?,?,?,0)",
            (702, "Второй участник", "Водитель", "10:00", stamp, stamp, second["id"]),
        )).lastrowid
        actions = [
            (701, first_shift, "move", ",".join(f"{1000+i}" for i in range(10)), 0,
             first["id"]),
            (701, first_shift, "fix", "2001,2002,2003,2004", 0, first["id"]),
            (701, first_shift, "to_sc", "3001,3002", 0, first["id"]),
            (701, first_shift, "from_sc", "3003", 0, first["id"]),
            (701, first_shift, "battery", "4001,4002,4003,4004,4005", 0,
             first["id"]),
            # Ремонт не участвует в БибиПассе.
            (701, first_shift, "repair", "9999", 0, first["id"]),
            (702, second_shift, "move", ",".join(f"{5000+i}" for i in range(25)), 0,
             second["id"]),
        ]
        await db.executemany(
            "INSERT INTO actions (user_id,shift_id,message_id,action_type,bike_codes,"
            "quantity,city_id) VALUES (?,?,1,?,?,?,?)", actions,
        )
        task_id = (await db.execute(
            "INSERT INTO crm_tasks (city_id,work_date,title,description,priority,status,"
            "created_by,created_at,updated_by,updated_at,published_at) "
            "VALUES (?,?,?,?,?,'published',?,?,?,?,?)",
            (first["id"], "2026-08-30", "Большая задача", "", "high", 900,
             stamp, 900, stamp, stamp),
        )).lastrowid
        await db.execute(
            "INSERT INTO crm_task_assignees "
            "(task_id,user_id,full_name_snap,role_snap,status,updated_at) "
            "VALUES (?,?,?,?, 'accepted', ?)",
            (task_id, 701, "Первый участник", "Скаут", stamp),
        )
        await db.executemany(
            "INSERT INTO bibipass_participants "
            "(season_id,user_id,intro_seen_at,joined_at,membership_status,membership_checked_at) "
            "VALUES (?,?,?,?, 'member', ?)",
            [(season["id"], 701, stamp, stamp, stamp),
             (season["id"], 702, stamp, stamp, stamp)],
        )
        await db.commit()

    score = await bot._bibipass_points(701, season)
    assert score["action_points"] == 20
    assert score["task_points"] == 10
    assert score["task_bibibonuses"] == 3
    assert score["total"] == 30

    payload = await bot._bibipass_payload(701, verify=False)
    assert payload["progress"]["level"] == 1
    assert payload["earned"]["level_bibibonuses"] == 5
    assert payload["earned"]["bibibonuses"] == 8
    assert payload["position"] == 1  # 30 баллов против 25, города объединены.
    assert {item["city"] for item in payload["ranking"]} == {first["name"], second["name"]}
    concurrent = await asyncio.gather(*[
        bot._bibipass_payload(701, verify=False) for _ in range(8)
    ])
    assert all(item["progress"]["points"] == 30 for item in concurrent)

    await bot._bibipass_sync_level_rewards(701, season, 20)
    async with bot.db_connect() as db:
        bonus = (await (await db.execute(
            "SELECT SUM(amount) FROM bibipass_reward_grants WHERE season_id=? "
            "AND user_id=701 AND reward_type='bibibonus'", (season["id"],)
        )).fetchone())[0]
        subscriptions = [row[0] for row in await (await db.execute(
            "SELECT amount FROM bibipass_reward_grants WHERE season_id=? AND user_id=701 "
            "AND reward_type='subscription' ORDER BY level", (season["id"],)
        )).fetchall()]
    assert bonus == 150
    assert subscriptions == [1, 3]

    with patch.object(bot.bot, "get_chat_member", AsyncMock(
            return_value=SimpleNamespace(status="member"))):
        allowed, error = await bot._bibipass_verify_membership(703, season, force=True)
    assert allowed is True and error is None
    participant = await bot._bibipass_participant(703, season, create=False)
    assert participant["membership_status"] == "member" and participant["joined_at"]

    print("PASS BibiPass: exact 20-day timer, levels, rewards, rating and membership")


if __name__ == "__main__":
    asyncio.run(run())
