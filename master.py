# Master switch for the audio+video streams: starts or stops go2rtc (which in turn starts each phone's stream only
# while someone watches it). Home Assistant REST switch: GET /master -> {"is_on"}, POST /master {"on": true|false}.
# Per device: GET /screen?ip=, /camera (front), /camera_back, /sound, /mic (and the older /video, /audio) -> {"is_on"};
# POST the same paths {"ip", "on"}.
# GET /streams -> {"on", "streams": {ip: {"src", "video"}}} for the adb-control viewer (/view?mode=stream).
# No token (LAN only, like the ADB viewer): GET /play?src= is one stream's player page, POST /webrtc?src= its
# WebRTC offer, passed on to go2rtc with go2rtc's own login.
# GET /ws?src= (WebSocket) is go2rtc's MSE stream, the player's fallback when WebRTC cannot connect.
# Requests need the X-Token header (env ADB_CONTROL_TOKEN, the same key as the adb-control service).
import base64
import json
import os
import re
import socket
import subprocess
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TOKEN = os.environ["ADB_CONTROL_TOKEN"]
STATE = "/data/master_on"  # exists = on (kept across container restarts)
CONFIG = "/data/go2rtc.yaml"
go2rtc: dict = {"proc": None}
# Per device, one video source (screen | camera | camera_back | off) and one audio source (output | mic | off). Each switch is one
# value, so turning one on turns the other of its pair off; both off = no video (left out of the viewer) or silence.
# /video and /audio are the older on/off switches (on = the source in use, or screen/output if it was off).
PREFS = "/data/prefs.json"
SWITCHES = {"/screen": ("video", "screen"), "/camera": ("video", "camera"), "/camera_back": ("video", "camera_back"),
            "/sound": ("audio", "output"), "/mic": ("audio", "mic"),
            "/video": ("video", None), "/audio": ("audio", None)}
DEFAULT = {"video": "screen", "audio": "output"}
PREF_KINDS = tuple(SWITCHES)
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
// nested in Home Assistant's iframe card, autoplay alone may not start: keep asking until the video plays
setInterval(()=>{if(v.paused && (v.srcObject||v.src)) v.play().catch(()=>{});},1000);
// Paths are relative, so the page also works behind ADB Control's /stream/ proxy (remote access over https).
// WebRTC first (lowest delay). If it never connects (from outside there is no route for its media), MSE over a
// WebSocket, which goes through the https tunnel too.
function webrtc(){
 const pc=new RTCPeerConnection(); let ok=false, done=false;
 const fail=()=>{if(done)return; done=true; clearTimeout(t); pc.close(); if(ok) setTimeout(webrtc,2000); else mse();};
 const t=setTimeout(()=>{if(!ok) fail();},8000);
 pc.addTransceiver("video",{direction:"recvonly"}); pc.addTransceiver("audio",{direction:"recvonly"});
 const ms=new MediaStream(); pc.ontrack=e=>{ms.addTrack(e.track); v.srcObject=ms; v.play().catch(()=>{});};
 pc.onconnectionstatechange=()=>{
  if(pc.connectionState==="connected"){ok=true; clearTimeout(t);}
  else if(["failed","disconnected"].includes(pc.connectionState)) fail();};
 (async()=>{
  await pc.setLocalDescription(await pc.createOffer());
  await new Promise(r=>{if(pc.iceGatheringState==="complete")r(); pc.onicegatheringstatechange=()=>pc.iceGatheringState==="complete"&&r(); setTimeout(r,2000);});
  const a=await fetch("webrtc?src="+encodeURIComponent(src),{method:"POST",body:JSON.stringify({type:"offer",sdp:pc.localDescription.sdp})});
  if(!a.ok) throw 0;
  await pc.setRemoteDescription(await a.json());
 })().catch(fail);
}
const CODECS=["avc1.640029","avc1.64002A","avc1.640033","avc1.42E01E","mp4a.40.2","mp4a.40.5","opus"];
function mse(){  // go2rtc's MSE protocol: ask with the codecs this browser plays, get the mime type, then fMP4 chunks
 v.srcObject=null; const ms=new MediaSource(); v.src=URL.createObjectURL(ms);
 ms.addEventListener("sourceopen",()=>{
  const ws=new WebSocket(location.href.split("#")[0].replace(/^http/,"ws").replace("/play?","/ws?"));
  ws.binaryType="arraybuffer";
  let sb=null; const q=[];
  const feed=()=>{if(sb && !sb.updating && q.length) sb.appendBuffer(q.shift());};
  const trim=()=>{  // stay at the live edge and keep the buffer short
   const b=v.buffered;
   if(b.length){const end=b.end(b.length-1); if(end-v.currentTime>1.5) v.currentTime=end-0.2;
    if(end-b.start(0)>30){sb.remove(b.start(0),end-10); return;}}
   feed();};
  ws.onopen=()=>ws.send(JSON.stringify({type:"mse",value:CODECS.filter(c=>MediaSource.isTypeSupported(`video/mp4; codecs="${c}"`)).join()}));
  ws.onmessage=e=>{
   if(typeof e.data==="string"){const m=JSON.parse(e.data); if(m.type==="mse" && !sb){sb=ms.addSourceBuffer(m.value); sb.mode="segments"; sb.onupdateend=trim;} return;}
   q.push(e.data); feed();};
  ws.onclose=()=>setTimeout(()=>location.reload(),3000);
 },{once:true});
}
webrtc();
</script></body></html>"""


def load_prefs() -> dict:
    prefs = json.load(open(PREFS)) if os.path.exists(PREFS) else {}
    return {"audio": prefs.get("audio", {}), "video": prefs.get("video", {})}


def source(prefs: dict, kind: str, ip: str) -> str:
    value = prefs[kind].get(ip, DEFAULT[kind])
    if isinstance(value, bool):  # saved by the older on/off switches
        return DEFAULT[kind] if value else "off"
    return value


def switch_state(prefs: dict, path: str, ip: str) -> bool:
    kind, value = SWITCHES[path]
    current = source(prefs, kind, ip)
    return current != "off" if value is None else current == value


def set_switch(prefs: dict, path: str, ip: str, on: bool) -> None:
    kind, value = SWITCHES[path]
    current = source(prefs, kind, ip)
    if value is None:  # older switch: on keeps the source in use (or the default), off silences / hides
        prefs[kind][ip] = (current if current != "off" else DEFAULT[kind]) if on else "off"
    elif on:
        prefs[kind][ip] = value
    elif current == value:
        prefs[kind][ip] = "off"


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

    def _splice_ws(self, path: str) -> None:
        """Forward this WebSocket upgrade to go2rtc, then copy bytes both ways until either side closes."""
        head = [f"GET {path} HTTP/1.1", "Host: 127.0.0.1:1984", "Connection: Upgrade", f"Authorization: {go2rtc_auth()}"]
        head += [f"{k}: {v}" for k, v in self.headers.items()
                 if k.lower() not in ("host", "connection", "origin", "authorization", "cookie")]  # go2rtc checks Origin
        try:
            up = socket.create_connection(("127.0.0.1", 1984), timeout=10)
        except OSError:
            return self._reply(502, {"error": "go2rtc unreachable"})
        up.sendall(("\r\n".join(head) + "\r\n\r\n").encode())
        up.settimeout(None)

        def pump(src: socket.socket, dst: socket.socket) -> None:
            try:
                while data := src.recv(65536):
                    dst.sendall(data)
            except OSError:
                pass
            for sock in (src, dst):
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        threading.Thread(target=pump, args=(self.connection, up), daemon=True).start()
        pump(up, self.connection)
        up.close()
        self.close_connection = True

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/play":
            data = PLAY.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        if url.path == "/ws":  # the player's MSE fallback: go2rtc's WebSocket API, with go2rtc's login added
            src = (parse_qs(url.query).get("src") or [""])[0]
            if src not in streams().values() or not running():
                return self._reply(404, {"error": "no such stream"})
            return self._splice_ws(f"/api/ws?src={src}")
        if self.headers.get("X-Token") == TOKEN and url.path == "/streams":
            prefs = load_prefs()
            return self._reply(200, {"on": running(), "streams": {
                ip: {"src": name, "video": source(prefs, "video", ip) != "off"} for ip, name in streams().items()}})
        if self.headers.get("X-Token") != TOKEN or url.path not in ("/master", *PREF_KINDS):
            return self._reply(403, {"error": "forbidden"})
        if url.path == "/master":
            return self._reply(200, {"is_on": running()})
        ip = (parse_qs(url.query).get("ip") or [""])[0]
        self._reply(200, {"is_on": switch_state(load_prefs(), url.path, ip)})

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
        set_switch(prefs, self.path, ip, bool(body.get("on")))
        with open(PREFS + ".tmp", "w") as f:
            json.dump(prefs, f)
        os.replace(PREFS + ".tmp", PREFS)  # stream.py reads it while running: never let it see half a file
        self._reply(200, {"is_on": switch_state(prefs, self.path, ip)})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    apply(os.path.exists(STATE))
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("MASTER_PORT", "8004"))), Handler).serve_forever()
