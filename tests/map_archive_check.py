"""Regression checks for safe employee archiving and the Krasnodar map."""
import asyncio
import importlib.util
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class Request:
    def __init__(self, query=None, body=None, match=None, role_scope=None):
        self.query = query or {}
        self._body = body or {}
        self.match_info = match or {}
        self.role_scope = role_scope

    async def json(self):
        return self._body


def payload(response):
    return json.loads(response.text)


async def run():
    with tempfile.TemporaryDirectory(prefix="bibibike-map-") as tmp:
        os.environ["DATA_DIR"] = tmp
        os.environ["BOT_TOKEN"] = "123:test"
        os.environ["ADMIN_PASSWORD"] = "test-password"
        os.environ["CRM_OWNER_USER_ID"] = "900001"
        os.environ["NETWORK_ADMIN_USER_IDS"] = "900001"
        os.environ["MAPTILER_API_KEY"] = "test-map-key"
        spec = importlib.util.spec_from_file_location("bibibike_map_test", ROOT / "main.py")
        bot = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bot)
        await bot.init_db()
        city = bot.get_default_city()
        other = next(item for item in bot.CITIES_BY_ID.values() if item["id"] != city["id"])
        today = datetime.now(bot._city_tz(city)).date().isoformat()
        now_iso = datetime.now(timezone.utc).isoformat()
        async with bot.db_connect() as db:
            await db.executemany(
                "INSERT INTO users (user_id,full_name,role,city_id,telegram_username) "
                "VALUES (?,?,?,?,?)",
                [(910001, "Архивный Сотрудник", "Скаут", city["id"], "archive_me"),
                 (910002, "Сотрудник Карты", "Скаут", city["id"], "map_worker"),
                 (910003, "Активный Сотрудник", "Скаут", city["id"], "active_worker")],
            )
            closed = await db.execute(
                "INSERT INTO shifts (user_id,full_name,role,start_time,end_time,is_active,"
                "created_at,city_id,start_at,end_at,source) VALUES (?,?,?,?,?,0,?,?,?,?,?)",
                (910001, "Архивный Сотрудник", "Скаут", "08:00", "16:00", now_iso,
                 city["id"], now_iso, now_iso, "bot"),
            )
            await db.execute(
                "INSERT INTO actions (user_id,shift_id,message_id,action_type,bike_codes,quantity,city_id) "
                "VALUES (?,?,?,?,?,?,?)",
                (910001, closed.lastrowid, 1, "move", "0001", 0, city["id"]),
            )
            await db.execute(
                "INSERT INTO shifts (user_id,full_name,role,start_time,is_active,created_at,city_id,"
                "start_at,source) VALUES (?,?,?,?,1,?,?,?,?)",
                (910003, "Активный Сотрудник", "Скаут", "08:00", now_iso,
                 city["id"], now_iso, "bot"),
            )
            await db.execute("UPDATE users SET calendar_visible=0 WHERE user_id=910001")
            await db.commit()

        async def fake_admin(request, **_kwargs):
            return ({
                "telegram_user": {"id": 900001},
                "user": {"full_name": "Owner"},
                "admin": {"role": "city_manager" if request.role_scope else "network_admin",
                          "role_scope": request.role_scope},
                "allowed_city_ids": sorted(bot.CITIES_BY_ID),
                "city": city,
            }, None)

        bot._crm_admin = fake_admin
        hidden = await bot.api_crm_employee_statistics_visibility(Request(
            body={"city_id": city["id"], "visible": False}, match={"user_id": "910001"}
        ))
        assert hidden.status == 200, hidden.text
        async with bot.db_connect() as db:
            preserved = [
                (await (await db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE user_id=910001"
                )).fetchone())[0]
                for table in ("users", "shifts", "actions")
            ]
            archived = await (await db.execute(
                "SELECT statistics_visible,calendar_visible FROM users WHERE user_id=910001"
            )).fetchone()
        assert preserved == [1, 1, 1]
        assert tuple(archived) == (0, 0)

        visible_list = payload(await bot.api_crm_employees(Request(query={
            "city_id": str(city["id"]), "from": today, "to": today, "limit": "200",
        })))
        assert 910001 not in {item["user_id"] for item in visible_list["items"]}
        all_list = payload(await bot.api_crm_employees(Request(query={
            "city_id": str(city["id"]), "from": today, "to": today, "limit": "200",
            "include_hidden": "1",
        })))
        archived_item = next(item for item in all_list["items"] if item["user_id"] == 910001)
        assert archived_item["statistics_visible"] is False
        assert archived_item["shifts"] == 1 and archived_item["actions_total"] == 1
        detail = payload(await bot.api_crm_employee(Request(
            query={"city_id": str(city["id"]), "from": today, "to": today},
            match={"user_id": "910001"},
        )))
        assert len(detail["shifts"]) == 1 and detail["totals"]["actions"] == 1
        try:
            await bot.start_shift(910001, "Архивный Сотрудник", "Скаут", "09:00", "", city["id"])
            raise AssertionError("archived employee started a shift")
        except bot.EmployeeArchived:
            pass
        await bot.set_user_city(910001, other["id"])
        await bot.add_user(910001, "Архивный Сотрудник", "Скаут", other["id"])
        async with bot.db_connect() as db:
            archived_city = (await (await db.execute(
                "SELECT city_id FROM users WHERE user_id=910001"
            )).fetchone())[0]
        assert archived_city == city["id"]
        payroll = payload(await bot.api_crm_payroll(Request(query={
            "city_id": str(city["id"]), "month": today[:7],
            "decade": "1" if int(today[-2:]) <= 10 else "2" if int(today[-2:]) <= 20 else "3",
        })))
        assert 910001 in {item["user_id"] for item in payroll["items"]}

        blocked = await bot.api_crm_employee_statistics_visibility(Request(
            body={"city_id": city["id"], "visible": False}, match={"user_id": "910003"}
        ))
        assert blocked.status == 409

        map_data = payload(await bot.api_crm_map(Request(query={
            "city_id": str(city["id"]), "date": today,
        })))
        assert map_data["supported"] is True
        assert map_data["map"]["maptiler_api_key"] == "test-map-key"
        assert len(map_data["zones"]["features"]) >= 3
        assert map_data["bikes"] == {"type": "FeatureCollection", "features": []}
        assert 910001 not in {item["user_id"] for item in map_data["employees"]}
        async with bot.db_connect() as db:
            for task_id, title, role in ((501, "Задача скаута", "Скаут"),
                                         (502, "Задача водителя", "Водитель")):
                await db.execute(
                    "INSERT INTO crm_tasks (id,city_id,work_date,title,description,priority,status,"
                    "created_by,created_at,updated_by,updated_at,published_at,date_from,date_to) "
                    "VALUES (?,?,?,?,?,'normal','published',?,?,?,?,?,?,?)",
                    (task_id, city["id"], today, title, "", 900001, now_iso,
                     900001, now_iso, now_iso, today, today),
                )
                await db.execute(
                    "INSERT INTO crm_task_targets (task_id,target_type,user_id,role) "
                    "VALUES (?,'role',NULL,?)", (task_id, role),
                )
            await db.commit()
        scoped_map = payload(await bot.api_crm_map(Request(query={
            "city_id": str(city["id"]), "date": today,
        }, role_scope="Скаут")))
        assert [item["title"] for item in scoped_map["tasks"]] == ["Задача скаута"]
        zone_id = map_data["zones"]["features"][0]["properties"]["id"]
        assigned = await bot.api_crm_map_assignment_create(Request(body={
            "city_id": city["id"], "date": today, "zone_id": zone_id,
            "user_id": 910002, "note": "Проверить зону",
        }))
        assert assigned.status == 201, assigned.text
        assignment_id = payload(assigned)["assignment"]["id"]
        refreshed = payload(await bot.api_crm_map(Request(query={
            "city_id": str(city["id"]), "date": today,
        })))
        assert refreshed["assignments"][0]["note"] == "Проверить зону"
        restored_for_map = await bot.api_crm_employee_statistics_visibility(Request(
            body={"city_id": city["id"], "visible": True}, match={"user_id": "910001"}
        ))
        assert restored_for_map.status == 200
        map_blocked_archive = await bot.api_crm_employee_statistics_visibility(Request(
            body={"city_id": city["id"], "visible": False}, match={"user_id": "910002"}
        ))
        assert map_blocked_archive.status == 409
        denied_task = await bot.api_crm_map_assignment_create(Request(body={
            "city_id": city["id"], "date": today, "zone_id": zone_id,
            "user_id": 910002, "task_id": 502,
        }, role_scope="Скаут"))
        assert denied_task.status == 403
        removed = await bot.api_crm_map_assignment_delete(Request(
            match={"assignment_id": str(assignment_id)}
        ))
        assert removed.status == 200
        other_map = payload(await bot.api_crm_map(Request(query={
            "city_id": str(other["id"]), "date": today,
        })))
        assert other_map["supported"] is False and "скоро" in other_map["message"].lower()
        restored = await bot.api_crm_employee_statistics_visibility(Request(
            body={"city_id": city["id"], "visible": True}, match={"user_id": "910001"}
        ))
        assert restored.status == 200
        async with bot.db_connect() as db:
            restored_flags = await (await db.execute(
                "SELECT statistics_visible,calendar_visible FROM users WHERE user_id=910001"
            )).fetchone()
        assert tuple(restored_flags) == (1, 0)

    print("PASS map/archive: history preserved, active shift protected, map groundwork works")


if __name__ == "__main__":
    asyncio.run(run())
