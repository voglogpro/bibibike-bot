# -*- coding: utf-8 -*-
"""Проверка календарного месяца и декады в профиле сотрудника."""

import importlib.util
import os
import shutil
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMP_DIR = Path(tempfile.mkdtemp(prefix="bibibike-profile-payroll-"))
os.environ["BOT_TOKEN"] = "123456789:" + ("A" * 35)
os.environ["DATA_DIR"] = str(TEMP_DIR)
os.environ.pop("CITIES_CONFIG_JSON", None)

spec = importlib.util.spec_from_file_location("bibibike_profile_payroll", ROOT / "main.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)


def row(day, earned, *, month=8, year=2026, tz=True):
    stamp = datetime(year, month, day, 9, 0)
    if tz:
        stamp = stamp.replace(tzinfo=timezone(timedelta(hours=3)))
    return {"start_at": stamp.isoformat(), "created_at": stamp.isoformat(), "earned": earned}


try:
    city = {"timezone_offset": 3}
    now = datetime(2026, 8, 26, 12, tzinfo=timezone(timedelta(hours=3)))
    rows = [
        row(31, 400, month=7),
        row(5, 100),
        row(20, 150, tz=False),
        row(21, 200),
        row(26, 300),
        row(1, 500, month=9),
    ]

    metrics = bot._profile_pay_metrics(rows, city, now)
    assert metrics["month_earned"] == 750, metrics
    assert metrics["decade_earned"] == 500, metrics
    assert metrics["month_shifts"] == 4, metrics
    assert metrics["decade_start"].isoformat() == "2026-08-21T00:00:00+03:00"
    assert metrics["decade_end"].isoformat() == "2026-09-01T00:00:00+03:00"

    assert not bot._shift_in_calendar_decade(row(20, 150), city, now)
    assert bot._shift_in_calendar_decade(row(21, 200), city, now)
    assert bot._shift_in_calendar_decade(row(31, 200), city, now)
    assert not bot._shift_in_calendar_decade(row(1, 500, month=9), city, now)

    expected = {
        10: (1, 11),
        11: (11, 21),
        20: (11, 21),
        21: (21, 1),
    }
    for day, (start_day, end_day) in expected.items():
        _, _, start, end = bot._calendar_pay_bounds(
            city, datetime(2026, 8, day, 12, tzinfo=timezone(timedelta(hours=3)))
        )
        assert start.day == start_day, (day, start)
        assert end.day == end_day, (day, end)

    _, next_month, december_start, december_end = bot._calendar_pay_bounds(
        city, datetime(2026, 12, 31, 12, tzinfo=timezone(timedelta(hours=3)))
    )
    assert next_month.isoformat() == "2027-01-01T00:00:00+03:00"
    assert december_start.isoformat() == "2026-12-21T00:00:00+03:00"
    assert december_end == next_month

    print("OK: профиль использует календарный месяц и календарную декаду")
finally:
    shutil.rmtree(TEMP_DIR, ignore_errors=True)
