"""Контракты и JavaScript Mini App сотрудника."""
import os
import re
import shutil
import subprocess
import sys
import tempfile


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX_PATH = os.path.join(ROOT, "index.html")

REQUIRED = (
    'data-tab="shift"',
    'data-tab="profile"',
    'data-tab="more"',
    'id="tab-profile"',
    'id="profileTasksOpen"',
    'id="profileScheduleOpen"',
    'id="profileBibipassOpen"',
    'id="tab-bibipass"',
    'id="bibipassIntro"',
    'id="bibipassIntroTimer"',
    'function tickBibipassCountdowns()',
    'data-bibipass-countdown',
    "String(startParam)==='bibipass'",
    'function openBibipass()',
    "'/api/bibipass'",
    'id="profileNavBadge"',
    'id="todoProfileBack"',
    'id="myScheduleBack">← Профиль',
    'function openProfile()',
    'function openTasks(taskId=null)',
    'function openMySchedule()',
)

REMOVED_GAMIFICATION = (
    'id="badgeLvl"',
    'RANK_ICONS',
    'Топ недели',
    'data-tab="tasks"',
)


def main():
    with open(INDEX_PATH, encoding="utf-8") as handle:
        source = handle.read()

    for required in REQUIRED:
        if required not in source:
            raise SystemExit(f"Employee UI contract missing: {required}")
    for removed in REMOVED_GAMIFICATION:
        if removed in source:
            raise SystemExit(f"Removed employee UI element returned: {removed}")

    match = re.search(r"<script>([\s\S]*?)</script>\s*</body>", source)
    if not match:
        raise SystemExit("Employee UI script block not found")
    node = shutil.which("node")
    if node:
        with tempfile.TemporaryDirectory() as tmp:
            script_path = os.path.join(tmp, "employee_ui.js")
            with open(script_path, "w", encoding="utf-8") as handle:
                handle.write(match.group(1))
            result = subprocess.run(
                [node, "--check", script_path], capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                sys.stderr.write(result.stderr)
                raise SystemExit("Employee UI: ошибка синтаксиса JavaScript")
    else:
        print("WARN employee UI: node не найден, проверены только контракты разметки")

    print("PASS employee UI: profile navigation, tasks, schedule and BibiPass")


if __name__ == "__main__":
    main()
