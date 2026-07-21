"""Small BotHost launcher that reports failures before importing the bot."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import traceback


APP_DIR = Path(__file__).resolve().parent
MAIN_PATH = APP_DIR / "main.py"
INDEX_PATH = APP_DIR / "index.html"
DATA_DIR = Path(os.getenv("DATA_DIR") or "/app/data")


def _token_flag() -> str:
    names = ("BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "API_TOKEN", "TOKEN")
    return "SET" if any(os.getenv(name) for name in names) else "EMPTY"


def main() -> None:
    print("== BibiBike launcher: container started ==", flush=True)
    print(
        "Runtime: "
        f"python={sys.version.split()[0]} "
        f"app_dir={APP_DIR} "
        f"main={MAIN_PATH.is_file()} "
        f"index={INDEX_PATH.is_file()} "
        f"TOKEN_ANY={_token_flag()} "
        f"PORT={os.getenv('PORT') or 'EMPTY'} "
        f"DATA_DIR={DATA_DIR}",
        flush=True,
    )

    if not MAIN_PATH.is_file():
        raise FileNotFoundError(f"Main module is missing: {MAIN_PATH}")
    if not INDEX_PATH.is_file():
        raise FileNotFoundError(f"Mini App is missing: {INDEX_PATH}")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    probe = DATA_DIR / ".bibibike-write-test"
    probe.write_text("ok", encoding="ascii")
    probe.unlink()
    print("Runtime storage check: OK", flush=True)

    os.execv(
        sys.executable,
        [sys.executable, "-u", str(MAIN_PATH)],
    )


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        print("BibiBike launcher failed before main.py:", flush=True)
        traceback.print_exc()
        raise
