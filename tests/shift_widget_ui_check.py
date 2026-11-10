"""Реальный браузер: компактная карточка смены и все кнопки её сценария."""
import html
import base64
import json
from pathlib import Path
import re
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from crm_website_check import browser_path

ROOT = Path(__file__).resolve().parents[1]


def document():
    state = {"k": "a" * 20, "r": 0, "n": "<img src=x onerror=alert(1)>",
             "c": "Краснодар", "role": "Чарджер", "s": "idle", "sid": 0,
             "d": "", "step": "", "time": "", "opts": [], "msg": "",
             "date": "10.11.2026", "start": "", "end": ""}
    stub = """<script>
window.testState=%s;window.requests=[];window.links=[];window.changed=null;
window.TelegramWidget={chatData:JSON.stringify(testState),onEvent(n,f){window.changed=f},ready(){},
openTelegramLink(u){links.push(u)},request(data){requests.push(data);const action=data.split(':')[3];testState.r++;
if(action==='open')testState.step='start_time';
if(action==='now'){testState.time='09:00';testState.step=testState.step==='start_time'?'start_district':'stop_confirm';testState.opts=['Восточка','Красная площадь','Западный обход','ФМР','ЮМР','ГМР','Ставрополька','Красная']}
if(action==='d0'){testState.d='Восточка';testState.step='start_confirm';testState.opts=[]}
if(action==='commit'){if(testState.step==='start_confirm'){testState.sid=42;testState.s='active';testState.start='09:00'}else{testState.sid=0;testState.s='closed';testState.end='09:00'}testState.step='';testState.time=''}
if(action==='close')testState.step='stop_time';
queueMicrotask(()=>{this.chatData=JSON.stringify(testState);changed()});return Promise.resolve({result:'{"pending":true}'})}};
</script>""" % json.dumps(state, ensure_ascii=False)
    source = (ROOT / "assets/shift-panel/widget.html").read_text(encoding="utf-8")
    source = source.replace("__APP_URL__", '"https://t.me/test_bot/app"')
    source = source.replace("__BRAND_IMAGE__", "data:image/webp;base64," + base64.b64encode(
        (ROOT / "assets/shift-panel/brand.webp").read_bytes()).decode("ascii"))
    source = source.replace("<body>", "<body><style>html,body{width:320px}</style>" + stub, 1)
    probe = """<script>
setTimeout(async()=>{const pause=()=>new Promise(r=>setTimeout(r,10));const result={};
const click=t=>{const b=Array.from(document.querySelectorAll('button')).find(x=>x.textContent===t);if(!b)throw new Error('Missing '+t);b.click()};
try{result.safe=document.querySelectorAll('input,video').length===0&&document.querySelectorAll('img').length===1;result.width=document.body.scrollWidth;result.logo=document.querySelector('.logo').src.startsWith('data:image/webp;base64,');
click('Открыть смену');await pause();result.timeStep=testState.step==='start_time';result.noEarlyStart=testState.sid===0;
click('Сейчас');await pause();result.districtHeight=document.body.scrollHeight;click('Восточка');await pause();result.district=testState.d;
click('Открыть смену');await pause();result.started=testState.sid===42;
click('Закрыть смену');await pause();click('Сейчас');await pause();result.needsConfirm=testState.sid===42&&testState.step==='stop_confirm';
click('Закрыть смену');await pause();result.closed=testState.s==='closed';click('Открыть приложение ↗');result.links=links;
result.noBusy=Array.from(document.querySelectorAll('button')).every(b=>!b.disabled);result.requests=requests;
testState.n='Кулаков Н.С.';testState.s='active';testState.sid=43;testState.start='19:25';testState.end='';testState.d='ФМР, ЮМР';TelegramWidget.chatData=JSON.stringify(testState);changed();result.height=document.body.scrollHeight;
result.bodyColor=getComputedStyle(document.body).backgroundColor;result.padding=getComputedStyle(document.body).padding;result.buttonColor=getComputedStyle(document.querySelector('.primary')).backgroundColor;
}catch(e){result.error=e.message}
const out=document.createElement('pre');out.id='widgetTestResult';out.style.display='none';out.textContent=JSON.stringify(result);document.body.appendChild(out);
},50);</script>"""
    return source.replace("</body>", probe + "</body>").encode("utf-8")


def run():
    chrome = browser_path()
    if not chrome:
        raise RuntimeError("Нужен Chromium для проверки карточки")
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(document())
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix="shift-widget-browser-") as profile:
            shots = ROOT / "tests/.chrome-tmp"
            shots.mkdir(exist_ok=True)
            response = subprocess.run([chrome, "--headless=new", "--disable-gpu", "--no-first-run",
                "--no-default-browser-check", f"--user-data-dir={profile}", "--window-size=360,600",
                "--virtual-time-budget=3500", f"--screenshot={shots / 'private-shift.png'}", "--dump-dom",
                f"http://127.0.0.1:{server.server_port}/"], capture_output=True, timeout=40)
            match = re.search(r'<pre id="widgetTestResult"[^>]*>(.*?)</pre>', response.stdout.decode("utf-8", errors="replace"), re.S)
            assert match, response.stderr.decode("utf-8", errors="replace")[-1000:]
            result = json.loads(html.unescape(match[1]))
            assert "error" not in result, result
            assert result["width"] <= 320 and result["height"] <= 590 and result["districtHeight"] <= 590, result
            assert all(result[k] for k in ("safe", "logo", "timeStep", "noEarlyStart", "started", "needsConfirm", "closed", "noBusy")), result
            assert result["bodyColor"] == "rgb(248, 251, 247)" and result["padding"] == "0px", result
            assert result["buttonColor"] == "rgb(53, 211, 58)", result
            assert result["district"] == "Восточка" and result["links"] == ["https://t.me/test_bot/app"]
            assert len(result["requests"]) == 8 and all(len(s.encode()) <= 64 for s in result["requests"]), result
    finally:
        server.shutdown()
        server.server_close()
    print("PASS private shift UI: 320px app styling, safe text, time/zone/confirmation, start/close, no polling")


if __name__ == "__main__":
    run()
