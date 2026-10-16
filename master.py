# Master switch for the audio+video streams: starts or stops go2rtc (which in turn starts each phone's stream only
# while someone watches it). Home Assistant REST switch: GET /master -> {"is_on"}, POST /master {"on": true|false}.
# Per device: GET /audio?ip=, /video?ip= -> {"is_on"}; POST /audio, /video {"ip", "on"}.
# GET /streams -> {"on", "streams": {ip: {"src", "video"}}} for the adb-control viewer (/view?mode=stream).
# No token (LAN only, like the ADB viewer): GET /play?src= is one stream's player page, POST /webrtc?src= its
# WebRTC offer, passed on to go2rtc with go2rtc's own login.
# Requests need the X-Token header (env ADB_CONTROL_TOKEN, the same key as the adb-control service).
import base64
import json
import os
import re
import subprocess
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TOKEN = os.environ["ADB_CONTROL_TOKEN"]
STATE = "/data/master_on"  # exists = on (kept across container restarts)
CONFIG = "/data/go2rtc.yaml"
go2rtc: dict = {"proc": None}
# per device, default on: "audio" (stream.py sends silence while off) and "video" (shown in the stream viewer, /view?mode=stream on adb-control)
PREFS = "/data/prefs.json"
PREF_KINDS = ("/audio", "/video")
IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def streams() -> dict:
    """ip -> go2rtc stream name, from the go2rtc config (lines 'name: exec:python3 /opt/stream.py <ip> ...')."""
    text = open(CONFIG).read()
    return {ip: name for name, ip in re.findall(r"^\s+(\w+): exec:python3 /opt/stream.py (\S+)", text, re.M)}


def go2rtc_auth() -> str:
    text = open(CONFIG).read()  # the api: block comes first, so the first username/password are the API's
    user = re.search(r"username: *(\S+)", text).group(1)
    pw = re.search(r"password: *(\S+)", text).group(1).strip("'\"")
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


PLAY = """<!doctype html><html><head><meta charset="utf-8"><title>Stream</title><style>
html,body{margin:0;height:100%;background:#000;overflow:hidden}video{width:100%;height:100%;object-fit:contain;display:block}
#snd{position:fixed;right:8px;bottom:8px;border:0;border-radius:50%;width:40px;height:40px;font-size:20px;
background:#000a;color:#fff;cursor:pointer}</style></head><body>
<video id="v" autoplay muted playsinline></video><button id="snd" title="Sonido">&#128263;</button><script>
// browsers only autoplay muted video: the button (a user gesture) turns the sound on and off
const v=document.getElementById("v"), snd=document.getElementById("snd"), src=new URLSearchParams(location.search).get("src");
snd.onclick=()=>{v.muted=!v.muted; snd.innerHTML=v.muted?"&#128263;":"&#128266;"; v.play();};
async function start(){
 const pc=new RTCPeerConnection();
 pc.addTransceiver("video",{direction:"recvonly"}); pc.addTransceiver("audio",{direction:"recvonly"});
 const ms=new MediaStream(); pc.ontrack=e=>{ms.addTrack(e.track); v.srcObject=ms; v.play().catch(()=>{});};
 // nested in Home Assistant's iframe card, autoplay alone may not start: retry until the video plays
 const kick=setInterval(()=>{if(v.paused) v.play().catch(()=>{}); else clearInterval(kick);},1000);
 pc.onconnectionstatechange=()=>{if(["failed","disconnected","closed"].includes(pc.connectionState)){pc.close();setTimeout(start,2000);}};
 await pc.setLocalDescription(await pc.createOffer());
 await new Promise(r=>{if(pc.iceGatheringState==="complete")r(); pc.onicegatheringstatechange=()=>pc.iceGatheringState==="complete"&&r(); setTimeout(r,2000);});
 try{
  const a=await fetch("/webrtc?src="+encodeURIComponent(src),{method:"POST",body:JSON.stringify({type:"offer",sdp:pc.localDescription.sdp})});
  if(!a.ok) throw 0;
  await pc.setRemoteDescription(await a.json());
 }catch(e){pc.close(); setTimeout(start,3000);}
}
start();
</script></body></html>"""


def load_prefs() -> dict:
    prefs = json.load(open(PREFS)) if os.path.exists(PREFS) else {}
    return {"audio": prefs.get("audio", {}), "video": prefs.get("video", {})}


def running() -> bool:
    return go2rtc["proc"] is not None and go2rtc["proc"].poll() is None


def apply(on: bool) -> None:
    if on:
        open(STATE, "w").close()
        if not running():
            go2rtc["proc"] = subprocess.Popen(["go2rtc", "-config", CONFIG])
    else:
        if os.path.exists(STATE):
            os.remove(STATE)
        if running():
            go2rtc["proc"].terminate()  # go2rtc stops its exec sources (the phone streams) on the way out
            try:
                go2rtc["proc"].wait(timeout=15)
            except subprocess.TimeoutExpired:
                go2rtc["proc"].kill()
        # release the devices; Home Assistant then hands them back to ADB Control (automation on this switch)
        subprocess.run(["adb", "disconnect"], capture_output=True, timeout=20)


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/play":
            data = PLAY.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        if self.headers.get("X-Token") == TOKEN and url.path == "/streams":
            video = load_prefs()["video"]
            return self._reply(200, {"on": running(), "streams": {
                ip: {"src": name, "video": video.get(ip, True)} for ip, name in streams().items()}})
        if self.headers.get("X-Token") != TOKEN or url.path not in ("/master", *PREF_KINDS):
            return self._reply(403, {"error": "forbidden"})
        if url.path == "/master":
            return self._reply(200, {"is_on": running()})
        ip = (parse_qs(url.query).get("ip") or [""])[0]
        self._reply(200, {"is_on": load_prefs()[url.path[1:]].get(ip, True)})

    def do_POST(self):
        url = urlparse(self.path)
        if url.path == "/webrtc":
            src = (parse_qs(url.query).get("src") or [""])[0]
            if src not in streams().values() or not running():
                return self._reply(404, {"error": "no such stream"})
            offer = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            req = urllib.request.Request(f"http://127.0.0.1:1984/api/webrtc?src={src}", data=offer, method="POST",
                                         headers={"Authorization": go2rtc_auth(), "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return self._reply(200, json.load(r))
            except (OSError, ValueError) as e:
                return self._reply(502, {"error": str(e)})
        if self.headers.get("X-Token") != TOKEN or self.path not in ("/master", *PREF_KINDS):
            return self._reply(403, {"error": "forbidden"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            body = {}
        if self.path == "/master":
            apply(bool(body.get("on")))
            return self._reply(200, {"is_on": running()})
        ip = str(body.get("ip") or "")
        if not IP_RE.match(ip):
            return self._reply(400, {"error": "ip must be an IPv4 address"})
        prefs = load_prefs()
        prefs[self.path[1:]][ip] = bool(body.get("on"))
        with open(PREFS + ".tmp", "w") as f:
            json.dump(prefs, f)
        os.replace(PREFS + ".tmp", PREFS)  # stream.py reads it while running: never let it see half a file
        self._reply(200, {"is_on": prefs[self.path[1:]][ip]})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    apply(os.path.exists(STATE))
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("MASTER_PORT", "8004"))), Handler).serve_forever()
