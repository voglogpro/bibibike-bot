"""Подписка: срок, расчёт, права владельца и реальные HTTP-ограничения.

Только временная БД, без Telegram и платежей. python tests/city_billing_check.py
"""
import asyncio
import importlib.util
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

ROOT = Path(__file__).resolve().parents[1]
TMP = tempfile.TemporaryDirectory(prefix="bibibike-city-billing-")
os.environ["DATA_DIR"] = TMP.name
os.environ["BOT_TOKEN"] = "123456789:" + "A" * 35
os.environ["CRM_OWNER_USER_ID"] = "990001"
spec = importlib.util.spec_from_file_location("city_billing_test_main", ROOT / "main.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)
billing = bot.city_billing
OWNER, ADMIN, EMPLOYEE = 990001, 990002, 990003


async def run():
    await bot.init_db()
    cities = sorted(bot.CITIES_BY_ID)
    first, second = cities[:2]
    now = datetime(2026, 10, 22, 10, tzinfo=timezone.utc)
    async with bot.db_connect() as db:
        await db.executemany(
            "INSERT INTO users (user_id,full_name,role,city_id,statistics_visible) VALUES (?,?,?,?,?)",
            [(OWNER, "Владелец", "Скаут", first, 1),
             (ADMIN, "Руководитель", "Скаут", first, 1),
             (EMPLOYEE, "Сотрудник", "Скаут", first, 1),
             (990004, "Уволенный", "Скаут", first, 0),
             (990005, "Другой город", "Скаут", second, 1)],
        )
        await db.commit()
        initial = await billing.status(db, first, now)
        assert initial["allowed"] and not initial["enabled"]
        assert initial["employees"] == 3 and initial["amount_rub"] == 1500
        await billing.launch(db, OWNER, now)
        await billing.launch(db, OWNER, now + timedelta(days=2))
        deadline = now + timedelta(days=7)
        assert (await billing.status(db, first, deadline - timedelta(microseconds=1)))["allowed"]
        expired = await billing.status(db, first, deadline)
        assert expired["state"] == "expired" and not expired["allowed"]
        assert expired["deadline_at"] == deadline.isoformat()
        until = await billing.grant(db, first, OWNER, 1, "same-request-id-001", "Оплата по счёту 1", deadline)
        assert until == datetime(2026, 11, 29, 10, tzinfo=timezone.utc).isoformat()
        repeated = await billing.grant(db, first, OWNER, 1, "same-request-id-001", "Оплата по счёту 1", deadline)
        assert until == repeated
        await db.rollback()
        assert not (await billing.status(db, second, deadline))["allowed"]
        assert (await billing.status(db, first, deadline))["allowed"]
        # Повторный запрос с подменённым городом не выдаёт второй доступ.
        try:
            await billing.grant(db, second, OWNER, 1, "same-request-id-001", "Оплата по счёту 1", deadline)
        except ValueError:
            await db.rollback()
        else:
            raise AssertionError("request_id нельзя использовать для другого города")
        # Изменение штата обновляет следующий расчёт, не отнимая выданный доступ.
        await db.execute("UPDATE users SET statistics_visible=0 WHERE user_id=?", (EMPLOYEE,))
        await db.commit()
        assert (await billing.status(db, first, deadline))["amount_rub"] == 1000
        ledger = await (await db.execute(
            "SELECT employees,amount_rub FROM city_billing_grants WHERE request_id='same-request-id-001'"
        )).fetchone()
        assert tuple(ledger) == (3, 1500)
    assert billing.add_months(datetime(2028, 1, 31, tzinfo=timezone.utc), 1).day == 29
    # Рестарт сохраняет дату отсечки и доступ, а не выдаёт новую неделю.
    await bot.init_db()
    async with bot.db_connect() as db:
        assert (await billing.status(db, first, deadline))["deadline_at"] == deadline.isoformat()
        # Фиксируем прошедшие даты для интеграционных проверок с реальным now().
        old = (billing.utcnow() - timedelta(days=1)).isoformat()
        await db.execute("UPDATE city_billing_campaign SET deadline_at=?", (old,))
        await db.execute("UPDATE city_billing_access SET access_until=?", (old,))
        await db.execute("UPDATE users SET statistics_visible=1 WHERE user_id=?", (EMPLOYEE,))
        await db.commit()

    async def admin_context(request):
        actor = int(request.headers.get("X-Test-Actor", ADMIN))
        return {"telegram_user": {"id": actor}, "user": {},
                "city": bot.get_city(first), "admin": {"role": "network_admin"},
                "allowed_city_ids": cities}

    async def employee_auth(request):
        return {"id": EMPLOYEE}

    async def ok(request):
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[bot.city_subscription_mw])
    app.router.add_get("/api/history", ok)
    app.router.add_post("/api/action/add", ok)
    app.router.add_post("/api/shift/start", ok)
    app.router.add_post("/api/shift/stop", ok)
    app.router.add_post("/api/crm/tasks/{task_id}/comment", ok)
    app.router.add_post("/api/admin/crm/subscriptions/launch", bot.api_city_subscription_launch)
    app.router.add_post("/api/admin/crm/subscriptions/grant", bot.api_city_subscription_grant)
    app.router.add_get("/api/admin/crm/subscriptions", bot.api_city_subscriptions)
    with patch.object(bot, "_admin_context", side_effect=admin_context), \
            patch.object(bot, "_auth_user", side_effect=employee_auth):
        async with TestClient(TestServer(app)) as client:
            payload = {"city_id": first, "months": 1, "reason": "Оплата руководителем",
                       "request_id": "owner-grant-request-001", "confirm": True}
            # Даже другой network_admin не может запускать платный режим и выдавать доступ.
            assert (await client.post("/api/admin/crm/subscriptions/launch", json={"confirm": True})).status == 403
            assert (await client.post("/api/admin/crm/subscriptions/grant", json=payload)).status == 403
            assert (await client.get("/api/history")).status == 200
            assert (await client.post("/api/action/add", json={})).status == 402
            assert (await client.post("/api/shift/start", json={"city_id": first})).status == 402
            assert (await client.post("/api/shift/stop", json={})).status == 200
            try:
                await bot.start_shift(EMPLOYEE, "Сотрудник", "Скаут", "10:00", "", first)
            except bot.CitySubscriptionRequired:
                pass
            else:
                raise AssertionError("прямой старт смены обходит подписку")
            headers = {"X-Test-Actor": str(OWNER)}
            invalid = await client.post("/api/admin/crm/subscriptions/grant", headers=headers,
                                        json={**payload, "months": True})
            assert invalid.status == 400
            assert (await client.post("/api/admin/crm/subscriptions/grant", headers=headers,
                                      json=payload)).status == 200
            response = await client.get("/api/admin/crm/subscriptions", headers=headers)
            data = await response.json()
            assert data["can_manage"] and len(data["items"]) == len(cities)
            assert (await client.post("/api/shift/start", json={"city_id": first})).status == 200
            assert (await client.post("/api/shift/start", json={"city_id": second})).status == 402
            # Подмена city_id оплаченного города не открывает задачу неоплаченного.
            async with bot.db_connect() as db:
                cur = await db.execute(
                    "INSERT INTO crm_tasks (city_id,title,status,work_date,created_by,updated_by,created_at,updated_at) "
                    "VALUES (?,'Проверка','published','2026-10-22',?,?,?,?)", (second, OWNER, OWNER, now.isoformat(), now.isoformat()),
                )
                task_id = cur.lastrowid
                await db.commit()
            response = await client.post(f"/api/crm/tasks/{task_id}/comment", json={"city_id": first})
            assert response.status == 402, await response.text()
            # Доступ общий на город: оба сотрудника получают один срок, не персональные лицензии.
            assert (await bot._city_subscription(first))["state"] == "paid"
            shift_id = await bot.start_shift(EMPLOYEE, "Сотрудник", "Скаут", "10:00", "", first)
            async with bot.db_connect() as db:
                await db.execute("UPDATE city_billing_access SET access_until=? WHERE city_id=?", (old, first))
                await db.commit()
            try:
                await bot.add_action(EMPLOYEE, shift_id, 1, "move", ["0001"], city_id=first)
            except bot.CitySubscriptionRequired:
                pass
            else:
                raise AssertionError("подсчёт действий должен остановиться")
            closed = await bot.end_shift(EMPLOYEE, datetime.now(bot._city_tz(bot.get_city(first))).strftime("%H:%M"), city_id=first)
            assert closed == shift_id
            shift = await bot.get_shift_by_id(shift_id)
            assert not shift["is_active"], shift
    print("PASS city billing: pricing, persistence, deadlines, renewal, owner-only grants, HTTP and city isolation")


if __name__ == "__main__":
    asyncio.run(run())
