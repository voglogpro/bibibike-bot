import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class Request:
    def __init__(self, uid, body=None):
        self.uid = uid
        self._body = body or {}

    async def json(self):
        return self._body


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DATA_DIR"] = tmp
        os.environ["BOT_TOKEN"] = "123:test"
        os.environ["ADMIN_PASSWORD"] = "test-password"
        os.environ["CRM_OWNER_USER_ID"] = "900001"
        os.environ["NETWORK_ADMIN_USER_IDS"] = "900001"

        import main as bot

        await bot.init_db()

        async def fake_crm_admin(request, **_kwargs):
            uid = request.uid
            return ({
                "telegram_user": {"id": uid},
                "user": {"full_name": "Owner" if uid == 900001 else "Viewer"},
                "admin": {"role": "network_admin" if uid == 900001 else "city_viewer"},
                "allowed_city_ids": sorted(bot.CITIES_BY_ID),
                "city": bot.get_default_city(),
            }, None)

        bot._crm_admin = fake_crm_admin

        viewer = await bot.api_crm_network_structure(Request(900002))
        viewer_data = json.loads(viewer.text)
        assert viewer.status == 200 and viewer_data["can_manage"] is False
        denied = await bot.api_crm_network_structure_save(
            Request(900002, {"cities": viewer_data["cities"]})
        )
        assert denied.status == 403
        denied_admin = await bot.api_crm_admin_upsert(Request(900002, {
            "user_id": 900003, "role": "city_viewer", "city_ids": [viewer_data["cities"][0]["id"]],
        }))
        assert denied_admin.status == 403

        owner = await bot.api_crm_network_structure(Request(900001))
        owner_data = json.loads(owner.text)
        assert owner_data["can_manage"] is True
        missing_user = await bot.api_crm_admin_upsert(Request(900001, {
            "username": "unknown_person", "role": "city_viewer",
            "city_ids": [owner_data["cities"][0]["id"]],
        }))
        assert missing_user.status == 404
        cities = owner_data["cities"]
        krasnodar = next(city for city in cities if city["city_key"] == "krasnodar")
        krasnodar["name"] = "Краснодар CRM"
        krasnodar["task_routes"][0]["authors"] = ["KuBerCaMypAu", "Aleksandroll"]
        saved = await bot.api_crm_network_structure_save(Request(900001, {"cities": cities}))
        assert saved.status == 200, saved.text
        saved_data = json.loads(saved.text)
        assert next(city for city in saved_data["cities"] if city["city_key"] == "krasnodar")["name"] == "Краснодар CRM"
        assert bot.TASK_CHAT_ROUTES[(-1003431950710, 1)]["authors"] == (
            "Aleksandroll", "KuBerCaMypAu"
        )
        async with bot.db_connect() as db:
            await db.execute(
                "INSERT INTO users (user_id,full_name,role,city_id,telegram_username) VALUES (?,?,?,?,?)",
                (900003, "Новый старший", "Скаут", krasnodar["id"], "new_manager"),
            )
            await db.commit()
        admin_saved = await bot.api_crm_admin_upsert(Request(900001, {
            "username": "@new_manager", "role": "city_manager", "city_ids": [krasnodar["id"]],
            "is_active": True,
        }))
        assert admin_saved.status == 200, admin_saved.text
        admin_payload = json.loads(admin_saved.text)
        assert admin_payload["admin"]["user_id"] == 900003
        assert admin_payload["admin"]["telegram_username"] == "new_manager"
        assert admin_payload["notification_queued"] is True
        async with bot.db_connect() as db:
            notice = await (await db.execute(
                "SELECT kind,payload_json FROM crm_notification_outbox WHERE user_id=?",
                (900003,),
            )).fetchone()
        assert notice[0] == "admin_access_updated"
        notice_text = bot._crm_notification_text(notice[0], json.loads(notice[1]))
        assert "СТАРШ" in notice_text.upper() and "Краснодар CRM" in notice_text
        with patch.object(
            bot.bot, "send_message", AsyncMock(return_value=SimpleNamespace(message_id=501))
        ) as send_message:
            assert await bot.deliver_crm_notifications_once() == 1
            assert send_message.await_args.args[0] == 900003
            assert "BibiBike CRM" in send_message.await_args.args[1]

        # A restart must keep CRM-owned configuration instead of restoring code/env.
        await bot.init_db()
        assert next(city for city in bot.CITIES_BY_ID.values() if city["city_key"] == "krasnodar")["name"] == "Краснодар CRM"

    print("PASS network structure: owner-only writes, viewer access, routes and restart persistence")


if __name__ == "__main__":
    asyncio.run(main())
