"""Проверка CRM UI: синтаксис JavaScript и обязательные контракты crm.html.

Раньше эта проверка жила в tests/crm_ui_check.js. Единственный .js-файл в
репозитории заставлял автоопределение BotHost считать проект Node.js и
запускать тест вместо бота, поэтому проверка переписана на Python.

Синтаксис JS по-прежнему проверяется настоящим движком: если в системе есть
node, скрипт из crm.html компилируется через `new Function`. Без node
проверяются только текстовые контракты, и об этом печатается предупреждение.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRM_PATH = os.path.join(ROOT, "crm.html")

# Строки, без которых CRM ломается молча: маршрут карты, её очистка при уходе
# с экрана, управление архивом, подсказка календаря и продление смены.
REQUIRED = (
    'data-route="map"',
    'id="page-map"',
    'if(state.route==="map"&&route!=="map")destroyMap()',
    "/statistics-visibility",
    'data-calendar-guide',
    'id="extendShiftModal"',
    '/extend`,{method:\'PATCH\'',
)


def main():
    with open(CRM_PATH, encoding="utf-8") as handle:
        source = handle.read()

    match = re.search(r"<script>([\s\S]*?)</script>\s*</body>", source)
    if not match:
        raise SystemExit("CRM script block not found")

    for required in REQUIRED:
        if required not in source:
            raise SystemExit(f"CRM contract missing: {required}")

    node = shutil.which("node")
    if not node:
        print("WARN CRM UI: node не найден, проверен только контракт разметки")
    else:
        # Компилируем скрипт в отдельном процессе: синтаксическая ошибка в
        # crm.html должна валить проверку так же, как раньше в .js-версии.
        with tempfile.TemporaryDirectory() as tmp:
            script_path = os.path.join(tmp, "crm_script.js")
            with open(script_path, "w", encoding="utf-8") as handle:
                handle.write(match.group(1))
            runner = os.path.join(tmp, "runner.js")
            with open(runner, "w", encoding="utf-8") as handle:
                # Путь передаём аргументом, а не вставкой в код: под Windows
                # обратные слэши иначе ломают сам runner.
                handle.write(
                    "const fs=require('fs');"
                    "new Function(fs.readFileSync(process.argv[2],'utf8'));"
                )
            result = subprocess.run(
                [node, runner, script_path], capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                sys.stderr.write(result.stderr)
                raise SystemExit("CRM UI: ошибка синтаксиса JavaScript в crm.html")

    print("PASS CRM UI: syntax, map fallback, calendar guide and shift extension")


if __name__ == "__main__":
    main()
