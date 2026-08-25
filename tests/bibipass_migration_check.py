"""Проверяет миграцию БибиПасса на копии существующей рабочей базы."""
import asyncio
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "bibibike_work.db"
CORE_TABLES = ("users", "shifts", "actions", "crm_tasks", "crm_task_assignees")
NEW_TABLES = ("bibipass_participants", "bibipass_reward_grants", "bibipass_task_grants")


def file_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def table_snapshot(path, tables):
    connection = sqlite3.connect(path)
    try:
        snapshots = {}
        for table in tables:
            columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
            rows = connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            snapshots[table] = json.dumps(
                {"columns": columns, "rows": rows}, ensure_ascii=False, default=str,
                separators=(",", ":"),
            )
        return snapshots
    finally:
        connection.close()


async def run():
    if not SOURCE.exists():
        raise SystemExit(f"Database not found: {SOURCE}")
    original_hash = file_hash(SOURCE)
    with tempfile.TemporaryDirectory(prefix="bibipass-migration-") as temp:
        copied = Path(temp) / "bibibike_work.db"
        shutil.copy2(SOURCE, copied)
        before = table_snapshot(copied, CORE_TABLES)
        os.environ["DATA_DIR"] = temp
        os.environ["BOT_TOKEN"] = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
        spec = importlib.util.spec_from_file_location("bibipass_migration_target", ROOT / "main.py")
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)
        await app.init_db()
        after = table_snapshot(copied, CORE_TABLES)
        if before != after:
            changed = [table for table in CORE_TABLES if before[table] != after[table]]
            raise AssertionError(f"Core data changed during migration: {changed}")
        connection = sqlite3.connect(copied)
        try:
            new_counts = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in NEW_TABLES
            }
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            connection.close()
        assert new_counts == {table: 0 for table in NEW_TABLES}, new_counts
        assert integrity == "ok", integrity
    assert file_hash(SOURCE) == original_hash, "Original database changed"
    print(
        "PASS BibiPass migration: core rows unchanged, new tables empty, "
        "integrity ok, source untouched"
    )


if __name__ == "__main__":
    asyncio.run(run())
