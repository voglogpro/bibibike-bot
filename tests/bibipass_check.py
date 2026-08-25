"""Контракт БибиПасса: прогрессия только по действиям, награды и подписка."""
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
    assert [item["bibibonuses"] for item in rewards] == [
        2, 2, 2, 2, 4, 4, 4, 6, 6, 6,
        8, 8, 8, 10, 10, 10, 12, 12, 14, 20,
    ]
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
             (703, "Новый участник", "Чарджер", first["id"]),
             (704, "Не сотрудник", "Администратор", first["id"]),
             (705, "Архивный сотрудник", "Скаут", first["id"])],
        )
        await db.execute(
            "UPDATE users SET statistics_archived_at=? WHERE user_id=705", (stamp,)
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
        # Даже сохранённая запись старой механики заданий не должна менять
        # XP, рейтинг или награды: в конкурсе учитываются только действия.
        await db.execute(
            "INSERT INTO bibipass_task_grants "
            "(season_id,user_id,task_id,task_priority,points,bibibonus_amount,earned_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (season["id"], 701, task_id, "high", 10, 3, stamp),
        )
        await db.executemany(
            "INSERT INTO bibipass_participants "
            "(season_id,user_id,intro_seen_at,joined_at,membership_status,membership_checked_at) "
            "VALUES (?,?,?,?, 'member', ?)",
            [(season["id"], 701, stamp, stamp, stamp),
             (season["id"], 702, stamp, stamp, stamp)],
        )
        await db.commit()

    announcement_at = datetime(2026, 8, 25, 21, 0, tzinfo=bot.MSK)
    started_at = datetime(2026, 8, 26, 9, 0, tzinfo=bot.MSK)
    assert await bot._enqueue_bibipass_campaign_notifications(
        "bibipass_started", announcement_at,
    ) == 0
    assert await bot._enqueue_bibipass_campaign_notifications(
        "bibipass_announcement", announcement_at,
    ) == 3
    assert await bot._enqueue_bibipass_campaign_notifications(
        "bibipass_announcement", announcement_at,
    ) == 0
    assert await bot._enqueue_bibipass_campaign_notifications(
        "bibipass_started", started_at,
    ) == 3
    assert await bot._enqueue_bibipass_campaign_notifications(
        "bibipass_started", started_at,
    ) == 0
    async with bot.db_connect() as db:
        queued = await (await db.execute(
            "SELECT kind,user_id,status FROM crm_notification_outbox "
            "WHERE kind LIKE 'bibipass_%' ORDER BY kind,user_id"
        )).fetchall()
    assert len(queued) == 6
    assert {row[1] for row in queued} == {701, 702, 703}
    assert {row[0] for row in queued} == {"bibipass_announcement", "bibipass_started"}
    sender = AsyncMock(return_value=SimpleNamespace(message_id=777))
    with patch.object(bot.bot, "send_message", sender):
        assert await bot.deliver_crm_notifications_once(limit=20) == 6
    assert sender.await_count == 6
    first_message = sender.await_args_list[0]
    assert "квест сотрудников" in first_message.args[1]
    for kind in ("bibipass_announcement", "bibipass_started"):
        notification_text = bot._crm_notification_text(kind, {})
        assert "XP" in notification_text
        assert "балл" not in notification_text.lower()
    keyboard = first_message.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][0].text == "1. Открыть канал"
    assert keyboard.inline_keyboard[0][0].url == "https://t.me/bbbikefan"
    assert keyboard.inline_keyboard[1][0].text == "2. Открыть БибиПасс"
    assert "startapp=bibipass" in keyboard.inline_keyboard[1][0].url
    async with bot.db_connect() as db:
        statuses = await (await db.execute(
            "SELECT status,COUNT(*) FROM crm_notification_outbox "
            "WHERE kind LIKE 'bibipass_%' GROUP BY status"
        )).fetchall()
    assert statuses == [("sent", 6)]

    score = await bot._bibipass_points(701, season)
    assert score["action_points"] == 20
    assert score["total"] == 20
    assert set(score) == {"action_points", "total"}

    payload = await bot._bibipass_payload(701, verify=False)
    summary = await bot._bibipass_state_summary(701)
    assert summary["member"] is True and summary["intro_required"] is False
    assert payload["progress"]["level"] == 1
    assert payload["earned"]["level_bibibonuses"] == 2
    assert payload["earned"]["bibibonuses"] == 2
    assert "tasks" not in payload["rules"]
    assert payload["position"] == 2  # 20 XP против 25, города объединены.
    assert {item["city"] for item in payload["ranking"]} == {first["name"], second["name"]}
    concurrent = await asyncio.gather(*[
        bot._bibipass_payload(701, verify=False) for _ in range(8)
    ])
    assert all(item["progress"]["points"] == 20 for item in concurrent)

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

    # Временная ошибка Telegram не закрывает квест тому, кто уже подтверждён.
    with patch.object(bot.bot, "get_chat_member", AsyncMock(
            side_effect=RuntimeError("temporary Telegram failure"))):
        preserved = await bot._bibipass_payload(701, verify=True, force=True)
    assert preserved["member"] is True
    assert preserved["check_error"] == "check_failed"
    assert preserved["progress"]["level"] == 1

    print("PASS BibiPass: timer, rewards, rating and idempotent private notifications")


if __name__ == "__main__":
    asyncio.run(run())
