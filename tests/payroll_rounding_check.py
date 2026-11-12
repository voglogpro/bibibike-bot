"""Расчётное окончание, ночные смены и однократная миграция ставок без потери действий."""
import asyncio
import importlib.util
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory(prefix="bibibike-payroll-rounding-")
os.environ["DATA_DIR"] = TMP.name
os.environ["TOKEN"] = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
os.environ["ADMIN_PASSWORD"] = "test-password"
spec = importlib.util.spec_from_file_location("payroll_rounding_host", ROOT / "main.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)


async def run():
    await bot.init_db()
    city = bot.get_default_city()
    cid, tz = city["id"], bot._city_tz(city)

    def at(day, hour, minute, month=11):
        return datetime(2026, month, day, hour, minute, tzinfo=tz)

    # Начало сохраняется; округляется только окончание, ровно :30 оставляется.
    naive = {"start_at": "2026-11-12T07:30:00", "end_at": "2026-11-12T16:59:00",
             "is_active": 0, "city_id": cid}
    assert bot._shift_payroll_minutes(naive) == 569
    assert bot._shift_payroll_minutes({**naive, "payroll_rule": bot.PAYROLL_CLOCK_END_RULE}) == 570
    cases = [(16, 59, 570), (17, 1, 570), (17, 0, 570), (17, 29, 570),
             (17, 30, 600), (17, 31, 630), (17, 59, 630)]
    with patch.object(bot, "_require_city_subscription", AsyncMock()):
        for index, (hour, minute, paid) in enumerate(cases):
            uid = 991000 + index
            await bot.add_user(uid, "Тестовый Скаут", "Скаут", cid)
            assert (await bot.get_user(uid))["pay_amount"] == 320
            sid = await bot.start_shift(uid, "Тестовый Скаут", "Скаут", "07:30", "ФМР", cid,
                                        now=at(12, 7, 30))
            # Все 115 действий остаются даже при округлении конца вниз.
            await bot.add_action(uid, sid, 7001, "battery", [f"{i:04}" for i in range(115)], city_id=cid)
            await bot.end_shift(uid, f"{hour:02}:{minute:02}", city_id=cid, now=at(12, hour, minute))
            shift = await bot.get_shift_by_id(sid)
            assert shift["start_time"] == "07:30"
            assert shift["end_time"] == f"{hour:02}:{minute:02}"
            assert bot._shift_worked_min(shift) == (hour * 60 + minute) - 450
            assert bot._shift_payroll_minutes(shift) == paid, shift
            assert shift["earned"] == round(320 * paid / 60, 2), shift
            assert (await bot.get_stats(sid))["battery"] == 115
            assert "К оплате:" in bot.build_report_text(shift, await bot.get_stats(sid))
            # Повторный /fix сохраняет расчёт и историческую ставку.
            await bot.set_user_pay(uid, "hourly", 400)
            await bot.freeze_earned(sid)
            assert (await bot.get_shift_by_id(sid))["earned"] == shift["earned"]

        # Начало в :10 не сдвигается; фактическая граница и полночь сохраняются.
        await bot.add_user(992000, "Начало :10", "Скаут", cid)
        sid = await bot.start_shift(992000, "Начало :10", "Скаут", "07:10", "ЮМР", cid, now=at(12, 7, 10))
        await bot.end_shift(992000, "17:01", city_id=cid, now=at(12, 17, 1))
        shift = await bot.get_shift_by_id(sid)
        assert bot._shift_payroll_minutes(shift) == 590
        assert shift["earned"] == round(320 * 590 / 60, 2)

        await bot.add_user(992001, "Полночь", "Скаут", cid)
        sid = await bot.start_shift(992001, "Полночь", "Скаут", "19:30", "ФМР", cid, now=at(12, 19, 30))
        await bot.end_shift(992001, "23:59", city_id=cid, now=at(12, 23, 59))
        shift = await bot.get_shift_by_id(sid)
        assert bot._shift_payroll_end_at(shift) == at(13, 0, 0)
        assert bot._shift_payroll_minutes(shift) == 270
        assert shift["end_at"] == at(12, 23, 59).isoformat()

        await bot.add_user(992002, "Ночная", "Скаут", cid)
        sid = await bot.start_shift(992002, "Ночная", "Скаут", "19:30", "ФМР", cid, now=at(12, 19, 30))
        await bot.end_shift(992002, "08:01", city_id=cid, now=at(13, 8, 1))
        shift = await bot.get_shift_by_id(sid)
        assert bot._shift_payroll_minutes(shift) == 750
        assert shift["end_at"] == at(13, 8, 1).isoformat()

        # Будущая отменённая смена и короткая смена не получают отрицательную оплату.
        await bot.add_user(992003, "Отмена", "Скаут", cid)
        sid = await bot.start_shift(992003, "Отмена", "Скаут", "20:50", "ФМР", cid, now=at(12, 19, 0))
        await bot.end_shift(992003, "19:01", city_id=cid, now=at(12, 19, 1))
        shift = await bot.get_shift_by_id(sid)
        assert shift["earned"] == 0 and bot._shift_payroll_minutes(shift) == 0
        assert bot._shift_payroll_minutes({"start_at": at(12, 7, 50).isoformat(),
            "end_at": at(12, 8, 10).isoformat(), "is_active": 0, "city_id": cid,
            "payroll_rule": bot.PAYROLL_CLOCK_END_RULE}) == 10

        # Оплата за АКБ и фикс за смену не зависят от округления времени.
        for uid, pay_type, amount, expected in [(992004, "piece", 15, 1725), (992005, "salary", 2000, 2000)]:
            await bot.add_user(uid, "Другая оплата", "Чарджер", cid)
            await bot.set_user_pay(uid, pay_type, amount)
            sid = await bot.start_shift(uid, "Другая оплата", "Чарджер", "07:30", "ФМР", cid, now=at(12, 7, 30))
            await bot.add_action(uid, sid, 8001, "battery", [f"{i:04}" for i in range(115)], city_id=cid)
            await bot.end_shift(uid, "08:01", city_id=cid, now=at(12, 8, 1))
            assert (await bot.get_shift_by_id(sid))["earned"] == expected

        # Автозакрытие уже открытой старой смены принимает новое правило.
        await bot.add_user(992006, "Автозакрытие", "Скаут", cid)
        sid = await bot.start_shift(992006, "Автозакрытие", "Скаут", "07:30", "ФМР", cid, now=at(12, 7, 30))
        async with bot.db_connect() as db:
            await db.execute("UPDATE shifts SET payroll_rule=NULL,auto_close_at=? WHERE id=?",
                (at(12, 17, 1).isoformat(), sid))
            await db.commit()
        with patch.object(bot, "safe_flush_report_update", AsyncMock()):
            await bot._auto_close_shift(await bot.get_shift_by_id(sid))
        assert (await bot.get_shift_by_id(sid))["earned"] == 3040

    # Ведомость CRM и история сотрудника возвращают те же деньги и 9,5 часа.
    request = SimpleNamespace(query={"city_id": str(cid), "month": "2026-11", "decade": "2"})
    with patch.object(bot, "_crm_admin", AsyncMock(return_value=({}, None))), \
         patch.object(bot, "_crm_city", return_value=(city, None)), \
         patch.object(bot, "_crm_scoped_role", return_value=(None, None)):
        response = await bot.api_crm_payroll(request)
    item = next(item for item in json.loads(response.text)["items"] if item["user_id"] == 991000)
    assert item["worked_minutes"] == 570 and item["earned"] == 3040, item
    assert item["shifts"][0]["actual_worked_minutes"] == 569
    assert item["shifts"][0]["payroll_end_at"].endswith("17:00:00+03:00")
    with patch.object(bot, "_auth_user", AsyncMock(return_value={"id": 991000})):
        response = await bot.api_history(request)
    history = json.loads(response.text)["items"][0]
    assert history["earned"] == 3040 and history["payroll_worked"] == "9 ч. 30 мин."
    assert history["worked"] == "9 ч. 29 мин."

    # Ручной отчёт, его правка и проверка администратором используют ту же оплату.
    for uid in (992010, 992011):
        await bot.add_user(uid, "Ручной отчёт", "Скаут", cid)
    report_message = SimpleNamespace(from_user=SimpleNamespace(id=992010, is_bot=False, full_name="Ручной отчёт"),
        text="Скаут. Начал смену 07:30. Закончил смену 16:59. Переместил 115", caption=None,
        message_id=9010, date=at(11, 17, 1), edit_date=None)
    parsed, error = bot.parse_manual_report(report_message.text)
    assert parsed and not error, error
    await bot.capture_manual_report(report_message, city)
    async with bot.db_connect() as db:
        rid, sid = await (await db.execute("SELECT id,shift_id FROM manual_reports WHERE user_id=992010")).fetchone()
    assert (await bot.get_shift_by_id(sid))["earned"] == 3040
    report_message.edit_date = at(11, 17, 2)
    await bot.capture_manual_report(report_message, city)
    assert (await bot.get_shift_by_id(sid))["earned"] == 3040
    async with bot.db_connect() as db:
        cur = await db.execute("INSERT INTO manual_reports(city_id,user_id,message_id,raw_text,status,"
            "pay_type_snap,pay_amount_snap,created_at,updated_at) VALUES (?,992011,9011,'Переместил 115',"
            "'needs_review','hourly',320,?,?)", (cid, at(11, 17, 1).isoformat(), at(11, 17, 1).isoformat()))
        rid = cur.lastrowid
        await db.commit()
    sid = await bot.approve_manual_report(rid, "07:30", "16:59", allowed_city_id=cid)
    assert (await bot.get_shift_by_id(sid))["earned"] == 3040

    migration = "scout_hourly_350_to_320_2026_11_v1"
    async with bot.db_connect() as db:
        # Воспроизводим старую базу до выпуска, в том числе необычный регистр роли.
        await db.executemany("INSERT INTO users(user_id,full_name,role,city_id,pay_type,pay_amount) VALUES (?,?,?,?,?,?)", [
            (993000, "Скаут старый", "Скаут", cid, "hourly", 350),
            (993001, "Своя ставка", "Скаут", cid, "hourly", 400),
            (993002, "Водитель", "Водитель", cid, "hourly", 350),
            (993003, "Фикс", "Скаут", cid, "salary", 350),
            (993004, "Нижний регистр", " сКаУт ", cid, "hourly", 350)])
        for sid, month, uid, role, rate, rule, earned in [
            (50001, 10, 993000, "Скаут", 350, None, 3325),
            (50002, 11, 993000, "Скаут", 350, None, 3325),
            (50003, 11, 993000, "Скаут", 350, bot.PAYROLL_CLOCK_END_RULE, 3325),
            (50004, 11, 993001, "Скаут", 400, None, 3800),
            (50005, 11, 993002, "Водитель", 350, None, 3325)]:
            await db.execute("INSERT INTO shifts(id,user_id,full_name,role,city_id,start_time,end_time,"
                "start_at,end_at,created_at,is_active,pay_type_snap,pay_amount_snap,payroll_rule,earned) "
                "VALUES (?,?,?,?,?,'07:30','17:00',?,?,?,0,'hourly',?,?,?)",
                (sid, uid, "История", role, cid, at(10, 7, 30, month).isoformat(),
                 at(10, 17, 0, month).isoformat(), at(10, 7, 30, month).isoformat(), rate, rule, earned))
        await db.execute("INSERT INTO manual_reports(city_id,user_id,message_id,raw_text,status,shift_id,"
            "pay_type_snap,pay_amount_snap,created_at,updated_at) VALUES (?,993000,9001,'отчёт','accepted',"
            "50002,'hourly',350,?,?)", (cid, at(10, 17, 0).isoformat(), at(10, 17, 0).isoformat()))
        await db.execute("INSERT INTO actions(user_id,shift_id,message_id,action_type,quantity,city_id) "
            "VALUES (993000,50002,9002,'move',115,?)", (cid,))
        await db.execute("INSERT INTO shifts(id,user_id,full_name,role,city_id,start_time,start_at,created_at,"
            "is_active,pay_type_snap,pay_amount_snap) VALUES (50006,993004,'Активный скаут','Скаут',?,"
            "'07:30',?,?,1,'hourly',350)", (cid, at(12, 7, 30).isoformat(), at(12, 7, 30).isoformat()))
        await db.execute("INSERT INTO manual_reports(city_id,user_id,message_id,raw_text,status,"
            "pay_type_snap,pay_amount_snap,created_at,updated_at) VALUES (?,993000,9003,'Нужна проверка',"
            "'needs_review','hourly',350,?,?)", (cid, at(10, 17, 0).isoformat(), at(10, 17, 0).isoformat()))
        await db.execute("DELETE FROM schema_migrations WHERE name=?", (migration,))
        await db.commit()
    await bot.refresh_monthly_aggregate(cid, 993000, "2026-11")
    # Сбой посередине миграции не оставляет частично изменённые выплаты.
    with patch.object(bot, "compute_earned", side_effect=RuntimeError("test interruption")):
        try:
            await bot._migrate_scout_rate_november_2026()
        except RuntimeError:
            pass
        else:
            raise AssertionError("Ожидался тестовый сбой миграции")
    assert (await bot.get_user(993000))["pay_amount"] == 350
    assert (await bot.get_shift_by_id(50002))["earned"] == 3325
    async with bot.db_connect() as db:
        assert not await (await db.execute("SELECT 1 FROM schema_migrations WHERE name=?", (migration,))).fetchone()
        assert not await (await db.execute("SELECT 1 FROM payroll_rate_adjustments WHERE migration=?", (migration,))).fetchone()
    await bot._migrate_scout_rate_november_2026()
    assert (await bot.get_user(993000))["pay_amount"] == 320
    assert (await bot.get_user(993004))["pay_amount"] == 320
    assert (await bot.get_user(993001))["pay_amount"] == 400
    assert (await bot.get_user(993002))["pay_amount"] == 350
    assert (await bot.get_user(993003))["pay_amount"] == 350
    assert (await bot.get_shift_by_id(50006))["pay_amount_snap"] == 320
    assert (await bot.get_shift_by_id(50006))["is_active"] == 1
    assert (await bot.get_shift_by_id(50001))["earned"] == 3325
    assert (await bot.get_shift_by_id(50001))["pay_amount_snap"] == 350
    for sid in (50002, 50003):
        shift = await bot.get_shift_by_id(sid)
        assert shift["earned"] == 3040 and shift["pay_amount_snap"] == 320, shift
    assert (await bot.get_stats(50002))["move"] == 115
    async with bot.db_connect() as db:
        assert (await (await db.execute("SELECT pay_amount_snap FROM manual_reports WHERE shift_id=50002")).fetchone())[0] == 320
        assert (await (await db.execute("SELECT earned FROM monthly_aggregates WHERE city_id=? AND user_id=993000 AND month='2026-11'", (cid,))).fetchone())[0] == 6080
        audits = await (await db.execute("SELECT old_data,new_data FROM payroll_rate_adjustments WHERE migration=?", (migration,))).fetchall()
        assert len(audits) == 6 and all(json.loads(row[0]) and json.loads(row[1]) for row in audits)
        assert (await (await db.execute("SELECT pay_amount_snap FROM manual_reports WHERE message_id=9003")).fetchone())[0] == 320
    # Резерв действительно содержит старые значения и те же действия.
    with closing(sqlite3.connect(str(bot.DB_PATH) + ".before-scout-rate-2026-11.bak")) as backup:
        assert backup.execute("SELECT pay_amount FROM users WHERE user_id=993000").fetchone()[0] == 350
        assert backup.execute("SELECT earned FROM shifts WHERE id=50002").fetchone()[0] == 3325
        assert backup.execute("SELECT quantity FROM actions WHERE shift_id=50002").fetchone()[0] == 115
    # Ставка, вручную изменённая после миграции, переживает регистрацию и перезапуск.
    await bot.set_user_pay(993000, "hourly", 350)
    await bot.add_user(993000, "Новое имя", "Скаут", cid)
    await bot.init_db()
    assert (await bot.get_user(993000))["pay_amount"] == 350
    assert (await bot.get_shift_by_id(50002))["earned"] == 3040
    await bot.freeze_earned(50001)
    assert (await bot.get_shift_by_id(50001))["earned"] == 3325
    # Регистрация после set_user_city получает правильный дефолт роли.
    await bot.set_user_city(993010, cid)
    await bot.add_user(993010, "Новый", "Скаут", cid)
    assert (await bot.get_user(993010))["pay_amount"] == 320
    print("PASS payroll: end-clock boundaries, :30, unchanged start, midnight/night, zero, 115 actions, piece/salary, November rate migration, backup/audit, history and restart")


if __name__ == "__main__":
    asyncio.run(run())
