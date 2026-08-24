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
            await db.execute(
                "INSERT INTO crm_planned_shifts "
                "(city_id,work_date,start_time,end_time,user_id,role,district,note,work_kind,status,"
                "created_by,created_at,updated_by,updated_at) "
                "VALUES (?,?,?,?,?,NULL,'','','regular','scheduled',?,?,?,?)",
                (city["id"], today, "09:00", "19:00", 910002,
                 900001, now_iso, 900001, now_iso),
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
        assert [item["user_id"] for item in map_data["employees"]] == [910002]
        assert map_data["employees"][0]["start_time"] == "09:00"
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
        original_zone = map_data["zones"]["features"][0]
        edited_geometry = json.loads(json.dumps(original_zone["geometry"]))
        edited_geometry["coordinates"][0].insert(1, [38.96, 45.01])
        updated_zone = await bot.api_crm_map_zone_update(Request(
            body={"city_id": city["id"], "name": "Центр", "color": "#31cf42",
                  "geometry": edited_geometry},
            match={"zone_id": str(zone_id)},
        ))
        assert updated_zone.status == 200, updated_zone.text
        assert len(payload(updated_zone)["zone"]["geometry"]["coordinates"][0]) == 6
        created_zone = await bot.api_crm_map_zone_create(Request(body={
            "city_id": city["id"], "name": "Юг", "color": "#9b51e0",
            "geometry": {"type": "Polygon", "coordinates": [[
                [38.93, 44.99], [39.00, 44.99], [39.00, 45.02],
                [38.93, 45.02], [38.93, 44.99],
            ]]},
        }))
        assert created_zone.status == 201, created_zone.text
        south_zone_id = payload(created_zone)["zone"]["properties"]["id"]
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
        async with bot.db_connect() as db:
            district = (await (await db.execute(
                "SELECT district FROM crm_planned_shifts WHERE user_id=910002 AND work_date=?",
                (today,),
            )).fetchone())[0]
        assert district == "Центр"
        moved = await bot.api_crm_map_assignment_create(Request(body={
            "city_id": city["id"], "date": today, "zone_id": south_zone_id,
            "user_id": 910002,
        }))
        assert moved.status == 200, moved.text
        assignment_id = payload(moved)["assignment"]["id"]
        moved_map = payload(await bot.api_crm_map(Request(query={
            "city_id": str(city["id"]), "date": today,
        })))
        assert [(item["zone_id"], item["user_id"]) for item in moved_map["assignments"]] == [
            (south_zone_id, 910002)
        ]
        assert next(item for item in moved_map["employees"] if item["user_id"] == 910002)[
            "district"
        ] == "Юг"
        marker = await bot.api_crm_map_annotation_create(Request(body={
            "city_id": city["id"], "date": today, "kind": "marker",
            "geometry": {"type": "Point", "coordinates": [38.98, 45.03]},
            "note": "Проверить парковку", "assigned_user_id": 910002,
        }))
        assert marker.status == 201, marker.text
        marker_id = payload(marker)["annotation"]["id"]
        assert payload(marker)["annotation"]["task_id"]
        arrow = await bot.api_crm_map_annotation_create(Request(body={
            "city_id": city["id"], "date": today, "kind": "arrow",
            "geometry": {"type": "LineString", "coordinates": [
                [38.98, 45.03], [39.01, 45.04],
            ]}, "note": "Переместить сюда", "assigned_user_id": 910002,
        }))
        assert arrow.status == 201, arrow.text
        annotated = payload(await bot.api_crm_map(Request(query={
            "city_id": str(city["id"]), "date": today,
        })))
        assert {item["kind"] for item in annotated["annotations"]} == {"marker", "arrow"}
        async with bot.db_connect() as db:
            map_tasks = (await (await db.execute(
                "SELECT COUNT(*) FROM crm_tasks WHERE created_via='map' AND status='published'"
            )).fetchone())[0]
            map_alerts = (await (await db.execute(
                "SELECT COUNT(*) FROM crm_notification_outbox "
                "WHERE kind='task_assigned' AND user_id=910002"
            )).fetchone())[0]
        assert map_tasks == 2 and map_alerts == 2
        removed_marker = await bot.api_crm_map_annotation_delete(Request(
            match={"annotation_id": str(marker_id)}
        ))
        assert removed_marker.status == 200
        no_schedule = await bot.api_crm_map_assignment_create(Request(body={
            "city_id": city["id"], "date": today, "zone_id": zone_id,
            "user_id": 910003,
        }))
        assert no_schedule.status == 409
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
        async with bot.db_connect() as db:
            cleared_district = (await (await db.execute(
                "SELECT district FROM crm_planned_shifts WHERE user_id=910002 AND work_date=?",
                (today,),
            )).fetchone())[0]
        assert cleared_district == ""
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

    print("PASS map/archive: calendar roster, editable zones, drag assignment and annotations")


if __name__ == "__main__":
    asyncio.run(run())
