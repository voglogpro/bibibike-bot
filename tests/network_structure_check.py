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


class WebRequest:
    def __init__(self, body=None, token=None):
        self._body = body or {}
        self.headers = {"X-Admin-Token": token} if token else {}
        self.remote = "127.0.0.42"

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
            "is_active": True, "web_password": "manager-site-password",
        }))
        assert admin_saved.status == 200, admin_saved.text
        admin_payload = json.loads(admin_saved.text)
        assert admin_payload["admin"]["user_id"] == 900003
        assert admin_payload["admin"]["telegram_username"] == "new_manager"
        assert admin_payload["admin"]["has_web_password"] is True
        assert admin_payload["admin"]["web_password"] == "manager-site-password"
        assert admin_payload["admin"]["web_password_recoverable"] is True
        assert admin_saved.headers["Cache-Control"] == "no-store"
        assert admin_payload["notification_queued"] is True
        async with bot.db_connect() as db:
            credential = await (await db.execute(
                "SELECT password_digest,password_ciphertext FROM crm_web_credentials WHERE user_id=?",
                (900003,),
            )).fetchone()
        assert credential and credential[0] != "manager-site-password"
        assert credential[1] != "manager-site-password"
        assert bot._crm_web_password_decrypt(credential[1]) == "manager-site-password"
        owner_after_password = await bot.api_crm_network_structure(Request(900001))
        owner_admin = next(
            item for item in json.loads(owner_after_password.text)["admins"]
            if item["user_id"] == 900003
        )
        assert owner_admin["web_password"] == "manager-site-password"
        assert owner_after_password.headers["Cache-Control"] == "no-store"
        viewer_after_password = await bot.api_crm_network_structure(Request(900002))
        viewer_admin = next(
            item for item in json.loads(viewer_after_password.text)["admins"]
            if item["user_id"] == 900003
        )
        assert "web_password" not in viewer_admin
        web_login = await bot.api_admin_login(WebRequest({
            "password": "manager-site-password",
        }))
        web_payload = json.loads(web_login.text)
        assert web_login.status == 200 and web_payload["role"] == "city_manager"
        assert [item["id"] for item in web_payload["cities"]] == [krasnodar["id"]]
        assert web_payload["remembered"] is True
        assert web_payload["expires_at"] is None
        remembered_payload = bot._verify_admin_token(web_payload["token"])
        assert remembered_payload["uid"] == 900003
        assert remembered_payload["remember"] is True and remembered_payload["exp"] == 0
        web_user = await bot._admin_user(WebRequest(token=web_payload["token"]))
        assert web_user["id"] == 900003 and web_user["web_login"] is True
        await bot.init_db()
        remembered_after_restart = await bot._admin_user(WebRequest(token=web_payload["token"]))
        assert remembered_after_restart["id"] == 900003
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
            assert "CRM бибибайк" in send_message.await_args.args[1]

        cleared = await bot.api_crm_admin_upsert(Request(900001, {
            "username": "@new_manager", "role": "city_manager",
            "city_ids": [krasnodar["id"]], "is_active": True,
            "clear_web_password": True,
        }))
        assert cleared.status == 200
        assert json.loads(cleared.text)["admin"]["has_web_password"] is False
        assert await bot._admin_user(WebRequest(token=web_payload["token"])) is None
        denied_web_login = await bot.api_admin_login(WebRequest({
            "password": "manager-site-password",
        }))
        assert denied_web_login.status == 403

        # A restart must keep CRM-owned configuration instead of restoring code/env.
        await bot.init_db()
        assert next(city for city in bot.CITIES_BY_ID.values() if city["city_key"] == "krasnodar")["name"] == "Краснодар CRM"

    print("PASS network structure: owner-only writes, viewer access, routes and restart persistence")


if __name__ == "__main__":
    asyncio.run(main())
