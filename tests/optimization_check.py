"""Проверки оптимизации фоновых workers и одноразовой миграции SQLite."""
import asyncio
import importlib.util
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory(prefix="bibibike-optimization-")
os.environ["DATA_DIR"] = TMP.name
os.environ["TOKEN"] = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
spec = importlib.util.spec_from_file_location("bibibike_optimization_test", ROOT / "main.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)


async def check_index_and_migration():
    await bot.init_db()
    city = bot.get_default_city()
    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        columns = await (await db.execute(
            "PRAGMA index_info(idx_actions_message_lookup)"
        )).fetchall()
        assert [row[2] for row in columns] == [
            "city_id", "user_id", "message_id", "chat_id", "shift_id"
        ]

        await db.execute(
            "DELETE FROM schema_migrations WHERE name='work_message_links_from_actions_v1'"
        )
        await db.execute("DELETE FROM work_message_links")
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id,full_name,role,city_id) VALUES (?,?,?,?)",
            (970001, "Optimization Test", "Скаут", city["id"]),
        )
        shift_ids = []
        for created_at in ("2026-08-20T08:00:00+00:00", "2026-08-20T09:00:00+00:00"):
            cur = await db.execute(
                "INSERT INTO shifts (user_id,full_name,role,is_active,created_at,city_id) "
                "VALUES (?,?,?,0,?,?)",
                (970001, "Optimization Test", "Скаут", created_at, city["id"]),
            )
            shift_ids.append(cur.lastrowid)
        await db.executemany(
            "INSERT INTO actions (user_id,shift_id,message_id,action_type,bike_codes,quantity,city_id,chat_id) "
            "VALUES (?,?,?,'move','[]',0,?,?)",
            [
                (970001, shift_ids[0], 77, city["id"], -10001),
                (970001, shift_ids[1], 77, city["id"], -10002),
                (970001, shift_ids[1], 77, city["id"], -10001),
            ],
        )
        await db.commit()

    await bot.init_db()
    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        marker_count = (await (await db.execute(
            "SELECT COUNT(*) FROM schema_migrations "
            "WHERE name='work_message_links_from_actions_v1'"
        )).fetchone())[0]
        links = await (await db.execute(
            "SELECT chat_id,shift_id FROM work_message_links "
            "WHERE user_id=970001 ORDER BY chat_id"
        )).fetchall()
        assert marker_count == 1
        assert [tuple(row) for row in links] == [
            (-10002, shift_ids[1]), (-10001, shift_ids[1])
        ], "backfill должен сохранять чаты и выбирать последнее действие полного ключа"

        await db.execute(
            "INSERT INTO actions (user_id,shift_id,message_id,action_type,bike_codes,quantity,city_id,chat_id) "
            "VALUES (?,?,78,'move','[]',0,?,?)",
            (970001, shift_ids[0], city["id"], -10001),
        )
        await db.commit()

    await bot.init_db()
    async with bot.aiosqlite.connect(bot.DB_PATH) as db:
        links = (await (await db.execute(
            "SELECT COUNT(*) FROM work_message_links WHERE user_id=970001"
        )).fetchone())[0]
        assert links == 2, "одноразовая миграция не должна сканировать actions на каждом старте"


async def check_worker_lifecycle():
    started = set()
    stopped = set()
    gate = asyncio.Event()
    names = (
        "kpi_background_worker", "scheduled_report_status_worker", "auto_close_worker",
        "health_watchdog", "crm_planned_shift_worker", "bibipass_campaign_worker",
        "crm_notification_worker",
        "crm_shift_task_sync_worker", "todo_cleanup_worker",
    )

    def worker(label):
        async def run():
            started.add(label)
            try:
                await gate.wait()
            finally:
                stopped.add(label)
        return run

    patches = [patch.object(bot, name, worker(name)) for name in names]
    for item in patches:
        item.start()
    try:
        tasks = bot.start_background_workers()
        for _ in range(20):
            if len(started) == len(names):
                break
            await asyncio.sleep(0)
        assert started == set(names)
        assert {task.get_name() for task in tasks} == {
            "bibibike:kpi", "bibibike:scheduled-report", "bibibike:auto-close",
            "bibibike:health", "bibibike:planned-shift", "bibibike:bibipass-campaign",
            "bibibike:notification",
            "bibibike:shift-task-sync", "bibibike:todo-cleanup",
        }
        await bot.stop_background_workers(tasks)
        assert stopped == set(names)
        assert all(task.done() for task in tasks)
    finally:
        for item in reversed(patches):
            item.stop()


async def check_main_worker_wiring():
    """main обязан не только уметь создать workers, но и реально запустить/остановить их."""
    sentinels = [object()]

    class FakeDispatcher:
        def include_router(self, _router):
            pass

        async def start_polling(self, _bot):
            return None

    with patch.object(bot, "init_db", AsyncMock()), \
            patch.object(bot, "cleanup_crm_uploads", AsyncMock()), \
            patch.object(bot, "rebuild_monthly_aggregates", AsyncMock()), \
            patch.object(bot, "start_api_server", AsyncMock()), \
            patch.object(bot, "Dispatcher", FakeDispatcher), \
            patch.object(bot, "_photo_libs", return_value=(None, None, None)), \
            patch.object(bot, "start_background_workers", return_value=sentinels) as start, \
            patch.object(bot, "stop_background_workers", AsyncMock()) as stop:
        await bot.main()
        start.assert_called_once_with()
        stop.assert_awaited_once_with(sentinels)


async def check_real_idle_intervals():
    """Все DB-workers делают один recovery-pass и затем выбирают редкий idle timeout."""
    specs = (
        (bot.crm_notification_worker, "_notification_wakeup", 900.0),
        (bot.crm_planned_shift_worker, "_planned_shift_wakeup", 3600.0),
        (bot.crm_shift_task_sync_worker, "_shift_task_sync_wakeup", 3600.0),
        (bot.scheduled_report_status_worker, "_scheduled_report_wakeup", 900.0),
        (bot.auto_close_worker, "_auto_close_wakeup", 900.0),
    )
    for worker, event_name, expected_timeout in specs:
        bot._reset_worker_events()
        observed = []

        async def capture_wait(event, timeout):
            observed.append((event, timeout))
            raise asyncio.CancelledError

        with patch.object(bot, "_wait_worker", capture_wait):
            try:
                await worker()
            except asyncio.CancelledError:
                pass
        assert len(observed) == 1
        assert observed[0][0] is getattr(bot, event_name)
        assert observed[0][1] == expected_timeout, (worker.__name__, observed)


async def check_commit_before_wakeup():
    bot._reset_worker_events()
    release = asyncio.Event()

    class SlowCommit:
        async def commit(self):
            await release.wait()

    task = asyncio.create_task(
        bot._commit_and_wake(SlowCommit(), bot._notification_wakeup)
    )
    await asyncio.sleep(0)
    assert not bot._notification_wakeup.is_set()
    release.set()
    await task
    assert bot._notification_wakeup.is_set()


async def check_all_worker_lost_wakeups():
    """Каждый отдельный цикл сохраняет сигнал, пришедший во время его DB-pass."""
    simple_specs = (
        (bot.crm_planned_shift_worker, "_planned_shift_wakeup",
         "process_planned_shifts_once", "_next_planned_shift_wait_seconds"),
        (bot.crm_shift_task_sync_worker, "_shift_task_sync_wakeup",
         "sync_closed_shift_tasks_once", None),
    )
    for worker, event_name, once_name, next_name in simple_specs:
        bot._reset_worker_events()
        calls = 0
        second_pass = asyncio.Event()

        async def once():
            nonlocal calls
            calls += 1
            if calls == 1:
                getattr(bot, event_name).set()
            elif calls == 2:
                second_pass.set()
            return 0

        patches = [patch.object(bot, once_name, once)]
        if next_name:
            async def next_wait(_now=None):
                return 3600.0
            patches.append(patch.object(bot, next_name, next_wait))
        for item in patches:
            item.start()
        task = asyncio.create_task(worker())
        try:
            await asyncio.wait_for(second_pass.wait(), 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            for item in reversed(patches):
                item.stop()

    class EmptyCursor:
        async def fetchall(self):
            return []

    for worker, event_name in (
        (bot.scheduled_report_status_worker, "_scheduled_report_wakeup"),
        (bot.auto_close_worker, "_auto_close_wakeup"),
    ):
        bot._reset_worker_events()
        calls = 0
        second_pass = asyncio.Event()

        class WakeDB:
            row_factory = None

            async def execute(self, _sql, _params=()):
                nonlocal calls
                calls += 1
                if calls == 1:
                    getattr(bot, event_name).set()
                elif calls == 2:
                    second_pass.set()
                return EmptyCursor()

        class WakeContext:
            async def __aenter__(self):
                return WakeDB()

            async def __aexit__(self, *_exc):
                return False

        with patch.object(bot, "db_connect", return_value=WakeContext()):
            task = asyncio.create_task(worker())
            try:
                await asyncio.wait_for(second_pass.wait(), 1)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def check_notification_idle_and_wakeup():
    bot._reset_worker_events()
    calls = 0
    first_pass = asyncio.Event()
    second_pass = asyncio.Event()

    async def deliver_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            first_pass.set()
        elif calls == 2:
            second_pass.set()
        return 0

    async def long_idle(_now=None):
        return 3600.0

    with patch.object(bot, "deliver_crm_notifications_once", deliver_once), \
            patch.object(bot, "_next_crm_notification_wait_seconds", long_idle):
        task = asyncio.create_task(bot.crm_notification_worker())
        try:
            await asyncio.wait_for(first_pass.wait(), 1)
            for _ in range(20):
                await asyncio.sleep(0)
            assert calls == 1, "worker не должен вращаться или опрашивать БД в простое"
            bot._notification_wakeup.set()
            await asyncio.wait_for(second_pass.wait(), 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # Сигнал, пришедший во время обработки, тоже не должен потеряться.
    bot._reset_worker_events()
    calls = 0
    second_pass = asyncio.Event()

    async def deliver_with_concurrent_wakeup():
        nonlocal calls
        calls += 1
        if calls == 1:
            bot._notification_wakeup.set()
        elif calls == 2:
            second_pass.set()
        return 0

    with patch.object(bot, "deliver_crm_notifications_once", deliver_with_concurrent_wakeup), \
            patch.object(bot, "_next_crm_notification_wait_seconds", long_idle):
        task = asyncio.create_task(bot.crm_notification_worker())
        try:
            await asyncio.wait_for(second_pass.wait(), 1)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def run():
    await check_index_and_migration()
    await check_worker_lifecycle()
    await check_main_worker_wiring()
    await check_real_idle_intervals()
    await check_commit_before_wakeup()
    await check_all_worker_lost_wakeups()
    await check_notification_idle_and_wakeup()
    print("PASS optimization: index, migration, main lifecycle, idle, commit, wakeup")


if __name__ == "__main__":
    asyncio.run(run())
