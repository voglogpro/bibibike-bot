"""Проверяет навигацию обычного сайта CRM в настоящем браузере.

Telegram SDK на публичной странице создаёт объект WebApp даже вне Mini App.
Тест воспроизводит это поведение и следит, чтобы сайт не открывал fullscreen,
не показывал блокирующее Telegram-окно и позволял нажать все разделы.
"""

import json
import os
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRM_PATH = os.path.join(ROOT, "crm.html")
CHROME_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def browser_path():
    for path in CHROME_PATHS:
        if os.path.isfile(path):
            return path
    return shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("microsoft-edge")


def test_html(init_data=""):
    with open(CRM_PATH, encoding="utf-8") as handle:
        source = handle.read()
    telegram_stub = """<script>
window.__fullscreenCalls=0;
window.Telegram={WebApp:{initData:%s,platform:'web',isFullscreen:false,
 ready(){},expand(){},onEvent(){},requestFullscreen(){window.__fullscreenCalls+=1}}};
</script>""" % json.dumps(init_data)
    source = source.replace(
        '<script src="https://telegram.org/js/telegram-web-app.js?63"></script>',
        telegram_stub,
        1,
    )
    source = source.replace(
        '<script>\n"use strict";',
        '<script>localStorage.setItem("bb_crm_admin_token","browser-test");'
        + ('localStorage.setItem("bb_crm_release_seen","2026-08-24-map-operations-v2");' if init_data else '')
        + '</script>\n<script>\n"use strict";',
        1,
    )
    probe = """<script>
setTimeout(async()=>{
  const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms)),results={};
  for(const route of ['map','calendar','tasks','team','more','analysis']){
    const button=document.querySelector(`.sidebar [data-route="${route}"]`),rect=button.getBoundingClientRect();
    const target=document.elementFromPoint(rect.left+rect.width/2,rect.top+rect.height/2);
    target.dispatchEvent(new MouseEvent('click',{bubbles:true,clientX:rect.left+rect.width/2,clientY:rect.top+rect.height/2}));
    await wait(80);results[route]=document.getElementById(`page-${route}`).classList.contains('active');
  }
  results.releaseBlocked=document.getElementById('releaseModal').classList.contains('open');
  results.fullscreenCalls=window.__fullscreenCalls;
  results.fullscreenControlsHidden=['fullscreenButton','sideFullscreenButton'].every(id=>document.getElementById(id).classList.contains('hidden'));
  results.telegramMiniApp=isTelegramMiniApp;
  const output=document.createElement('pre');output.id='browserTestResult';output.textContent=JSON.stringify(results);document.body.appendChild(output);
},900);
</script>"""
    return source.replace("</body>", probe + "\n</body>", 1).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    html = b""

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in {"/crm", "/crm/", "/crm.html"}:
            return self.reply(self.html, "text/html; charset=utf-8")
        if path == "/api/admin/crm/context":
            return self.reply_json({
                "role": "network_admin", "can_write": True, "can_manage_network": True,
                "default_city_id": 1, "cities": [{"id": 1, "name": "Краснодар", "timezone_offset": 3, "districts": []}],
                "user": {"name": "Проверка сайта"}, "calendar_presets": {},
            })
        if path == "/api/admin/crm/map":
            return self.reply_json({"supported": False, "message": "Тестовый режим"})
        if path.endswith("/overview"):
            return self.reply_json({"generated_at": "2026-09-02T12:00:00Z", "totals": {}, "current": {}})
        if path.endswith("/calendar"):
            return self.reply_json({"plans": [], "actual": [], "days": []})
        if path.endswith("/notification-settings"):
            return self.reply_json({"eligible": True, "settings": {}})
        if path.endswith("/operational-signals"):
            return self.reply_json({"items": [], "summary": {}})
        if path.endswith("/trends"):
            return self.reply_json({"series": []})
        return self.reply_json({"items": []})

    def reply_json(self, value):
        self.reply(json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def reply(self, body, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def run_browser(browser, server, init_data=""):
    Handler.html = test_html(init_data)
    with tempfile.TemporaryDirectory() as profile:
        result = subprocess.run(
            [browser, "--headless=new", "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
             "--hide-scrollbars", "--window-size=1440,1000", f"--user-data-dir={profile}",
             "--virtual-time-budget=3500", "--dump-dom", f"http://127.0.0.1:{server.server_port}/crm"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45,
        )
    marker = '<pre id="browserTestResult">'
    if result.returncode != 0 or marker not in result.stdout:
        raise SystemExit(f"CRM website: браузерная проверка не завершилась: {result.stderr[-1000:]}")
    payload = result.stdout.split(marker, 1)[1].split("</pre>", 1)[0]
    return json.loads(payload.replace("&quot;", '"'))


def assert_routes(data):
    for route in ("map", "calendar", "tasks", "team", "more", "analysis"):
        assert data.get(route) is True, f"кнопка раздела {route} не открыла страницу"


def main():
    browser = browser_path()
    if not browser:
        raise SystemExit("CRM website: Chrome/Edge не найден для проверки кликов")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        website = run_browser(browser, server)
        mini_app = run_browser(browser, server, "signed-telegram-test-data")
    finally:
        server.shutdown()
        server.server_close()
    assert_routes(website)
    assert website["telegramMiniApp"] is False
    assert website["releaseBlocked"] is False, "релизное окно перекрыло навигацию сайта"
    assert website["fullscreenCalls"] == 0, "обычный сайт вызвал Telegram fullscreen"
    assert website["fullscreenControlsHidden"] is True, "на сайте остались fullscreen-кнопки"
    assert_routes(mini_app)
    assert mini_app["telegramMiniApp"] is True
    assert mini_app["releaseBlocked"] is False
    assert mini_app["fullscreenCalls"] >= 1, "Mini App потерял свой Telegram fullscreen"
    assert mini_app["fullscreenControlsHidden"] is False, "в Mini App пропали Telegram-инструменты"
    print("PASS CRM website/Mini App: all navigation buttons, host modes, shared API session")


if __name__ == "__main__":
    main()
