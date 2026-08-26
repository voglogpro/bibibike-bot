# -*- coding: utf-8 -*-
"""Контракты типов меток и безопасной геометрии свободного рисунка."""

import importlib.util
import os
import shutil
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMP_DIR = Path(tempfile.mkdtemp(prefix="bibibike-map-geometry-"))
os.environ["BOT_TOKEN"] = "123456789:" + ("A" * 35)
os.environ["DATA_DIR"] = str(TEMP_DIR)

spec = importlib.util.spec_from_file_location("bibibike_map_geometry", ROOT / "main.py")
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)


try:
    assert bot._crm_map_annotation_variant("marker", "attention") == "attention"
    assert bot._crm_map_annotation_variant("marker", "empty_parking") == "empty_parking"
    assert bot._crm_map_annotation_variant("marker", "unknown") is None
    assert bot._crm_map_annotation_variant("arrow", "drawing") == "drawing"
    assert bot._crm_map_annotation_variant("arrow", None) == "direction"

    point = {"type": "Point", "coordinates": [38.976, 45.035]}
    assert bot._crm_map_annotation_geometry("marker", point, "empty_parking") == point

    direction = {
        "type": "LineString",
        "coordinates": [[38.97, 45.03], [38.99, 45.04]],
    }
    assert bot._crm_map_annotation_geometry("arrow", direction, "direction") == direction
    assert bot._crm_map_annotation_geometry(
        "arrow", {"type": "LineString", "coordinates": [[38.97, 45.03]]}, "direction"
    ) is None
    assert bot._crm_map_annotation_geometry(
        "arrow", {"type": "LineString", "coordinates": [[38.97, 45.03]] * 2}, "direction"
    ) is None

    drawing_points = [[38.97 + index / 10000, 45.03] for index in range(250)]
    drawing = {"type": "LineString", "coordinates": drawing_points}
    normalized = bot._crm_map_annotation_geometry("arrow", drawing, "drawing")
    assert normalized and len(normalized["coordinates"]) == 250
    too_long = {"type": "LineString", "coordinates": drawing_points + [[39.2, 45.03]]}
    assert bot._crm_map_annotation_geometry("arrow", too_long, "drawing") is None
    duplicate_only = {
        "type": "LineString", "coordinates": [[38.97, 45.03], [38.97, 45.03]],
    }
    assert bot._crm_map_annotation_geometry("arrow", duplicate_only, "drawing") is None

    print("PASS map geometry: marker variants, arrows and freehand limits")
finally:
    shutil.rmtree(TEMP_DIR, ignore_errors=True)
