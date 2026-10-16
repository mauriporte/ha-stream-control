# One Android screen + audio as an RTSP stream for go2rtc (started on demand by go2rtc's exec source).
#   python3 stream.py <device ip> <rtsp output url>
# Runs the scrcpy server on the phone (through this container's own adb server),
# reads its raw H.264 video and raw PCM audio sockets, and has ffmpeg publish them (audio as Opus, for WebRTC).
import json
import os
import random
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request

SCRCPY_VERSION = "5.0.1"
JAR_LOCAL = "/opt/scrcpy/scrcpy-server.jar"
JAR_REMOTE = "/data/local/tmp/scrcpy-server-stream.jar"


def adb(*args: str, timeout: int = 30) -> str:
    return subprocess.run(["adb", "-s", SERIAL, *args], capture_output=True, text=True, timeout=timeout).stdout.strip()


def log(msg: str) -> None:
    print(f"[stream {SERIAL}] {msg}", file=sys.stderr, flush=True)


def connect(port: int, dummy: bool) -> socket.socket:
    """adb accepts the TCP connection even before the server listens; the dummy byte proves the server is there."""
    for _ in range(100):
        s = socket.create_connection(("127.0.0.1", port), timeout=10)
        if not dummy:
            return s
        try:
            if s.recv(1):
                return s
        except OSError:
            pass
        s.close()
        time.sleep(0.2)
    raise RuntimeError("scrcpy server did not start")


CONTROL_FILE = "/data/adb_control_url"  # or the env var: the adb-control service, e.g. http://192.168.1.10:8002 (optional)
CONTROL_URL = os.environ.get("ADB_CONTROL_URL") or (open(CONTROL_FILE).read().strip() if os.path.exists(CONTROL_FILE) else "")
PREFS = "/data/prefs.json"  # written by master.py


def want_landscape() -> bool:
    """The device's Horizontal switch, kept by the adb-control service (the same one its ADB view uses)."""
    req = urllib.request.Request(f"{CONTROL_URL}/orientation?ip={IP}", headers={"X-Token": os.environ["ADB_CONTROL_TOKEN"]})
    with urllib.request.urlopen(req, timeout=5) as r:
        return bool(json.load(r)["is_on"])


def audio_on() -> bool:
    try:
        return json.load(open(PREFS)).get("audio", {}).get(IP, True)
    except (OSError, ValueError):
        return True


def capture_orientation(landscape: bool) -> str:
    """scrcpy capture_orientation that locks the picture to the wanted shape: the current orientation if it already
    matches, else 0 or 90 degrees from the natural one (the content is then sideways, as in the ADB view's frame)."""
    m = re.search(r"mOverrideDisplayInfo.*?real (\d+) x (\d+).*?rotation (\d)", adb("shell", "dumpsys", "display"))
    if not m:
        return "@"
    w, h, rot = map(int, m.groups())
    if (w > h) == landscape:
        return "@"  # lock the initial orientation
    natural_landscape = (w > h) == (rot % 2 == 0)
    return "@0" if natural_landscape == landscape else "@90"


IP, OUTPUT = sys.argv[1], sys.argv[2]
SERIAL = f"{IP}:5555"
subprocess.run(["adb", "connect", SERIAL], capture_output=True, timeout=20)  # this container has its own adb server
subprocess.run(["adb", "-s", SERIAL, "wait-for-device"], capture_output=True, timeout=20)  # a fresh connection needs a moment
sdk = int(adb("shell", "getprop", "ro.build.version.sdk") or 0)
audio = sdk >= 30  # scrcpy captures audio on Android 11+ only
if adb("shell", "ls", "-l", JAR_REMOTE).split()[4:5] != [str(os.path.getsize(JAR_LOCAL))]:
    adb("push", JAR_LOCAL, JAR_REMOTE, timeout=120)
scid = f"{random.randrange(1, 0x7FFFFFFF):08x}"
port = int(adb("forward", "tcp:0", f"localabstract:scrcpy_{scid}"))
try:
    landscape = want_landscape() if CONTROL_URL else None
except (OSError, ValueError, KeyError):
    landscape = None  # no adb-control (or unreachable): follow the device's own rotation
# control=true only so this script can ask for keyframes (RESET_VIDEO); nothing else is ever sent on that socket
params = [f"scid={scid}", "log_level=warn", "tunnel_forward=true", "control=true", "cleanup=true",
          "raw_stream=true", "send_dummy_byte=true", f"audio={str(audio).lower()}", "audio_codec=raw",
          "max_size=1024", "video_bit_rate=4000000", "max_fps=30",
          # viewers that join a running stream need a keyframe with SPS/PPS soon (scrcpy's default is every 10 s)
          "video_codec_options=i-frame-interval:int=2,prepend-sps-pps-to-idr-frames:int=1"]
if sdk >= 33:
    params.append("audio_dup=true")  # keep the sound playing on the phone too (Android 13+)
if landscape is not None:
    params.append(f"capture_orientation={capture_orientation(landscape)}")
server = subprocess.Popen(["adb", "-s", SERIAL, "shell",
                           f"CLASSPATH={JAR_REMOTE} app_process / com.genymobile.scrcpy.Server {SCRCPY_VERSION} "
                           + " ".join(params)])
procs = [server]


def stop(*_):
    for p in procs:
        if p.poll() is None:
            p.terminate()
    adb("forward", "--remove", f"tcp:{port}")
    sys.exit(0)


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
BYTES_PER_SEC = 48000 * 2 * 2  # raw audio from scrcpy: 48 kHz, stereo, 16-bit


def audio_pump(sock: socket.socket, out_fd: int) -> None:
    """Pass the phone's PCM on to ffmpeg, filling gaps with silence in real time. With audio_dup the phone sends
    nothing while nothing plays, and ffmpeg would wait for the first sample forever before publishing."""
    sock.settimeout(0.02)
    start, written = time.monotonic(), 0
    on, checked = audio_on(), time.monotonic()
    try:
        while True:
            try:
                data = sock.recv(65536)
                if not data:
                    return
            except socket.timeout:
                data = b""
            if time.monotonic() - checked > 1:  # the Audio switch applies live: muted means silence of the same length
                on, checked = audio_on(), time.monotonic()
            if data and not on:
                data = bytes(len(data))
            if data:
                os.write(out_fd, data)
                written += len(data)
            behind = int((time.monotonic() - start) * BYTES_PER_SEC) - written
            if behind >= 3840:  # more than 20 ms of nothing: send that much silence (whole 4-byte frames)
                gap = behind - behind % 4
                os.write(out_fd, bytes(gap))
                written += gap
    except OSError:
        return


RESET_VIDEO = bytes([17])  # scrcpy control message: restart the encoder, which emits a fresh keyframe
last_video = [None]  # time of the last video data; None until the first frame


def video_pump(sock: socket.socket, out_fd: int) -> None:
    """Pass the H.264 on to ffmpeg and remember when data last arrived (for the keyframe keep-alive)."""
    try:
        while data := sock.recv(65536):
            last_video[0] = time.monotonic()
            os.write(out_fd, data)
    except OSError:
        return


def keyframe_keepalive(ctrl: socket.socket) -> None:
    """Some encoders (Xiaomi) ignore scrcpy's repeat-previous-frame setting and send nothing while the screen is
    still; ffmpeg then waits for video and stalls everything. Ask for a new keyframe after 1 s of silence."""
    try:
        while True:
            time.sleep(0.25)
            # only once video has started: a reset before the capture exists crashes the server (slow devices)
            if last_video[0] is not None and time.monotonic() - last_video[0] > 1.0:
                ctrl.sendall(RESET_VIDEO)
                last_video[0] = time.monotonic()  # give the encoder a second before asking again
    except OSError:
        return


def orientation_watch() -> None:
    """The picture shape is fixed when scrcpy starts, so a change of the Horizontal switch ends this stream;
    go2rtc starts a new one, with the new shape, when the camera is opened again."""
    while True:
        time.sleep(3)
        try:
            if want_landscape() != landscape:
                log("Horizontal switch changed: restarting")
                os.kill(os.getpid(), signal.SIGTERM)
                return
        except (OSError, ValueError, KeyError):
            pass


def drain(sock: socket.socket) -> None:
    """The device writes its own messages (clipboard etc.) on the control socket; read and drop them."""
    try:
        while sock.recv(4096):
            pass
    except OSError:
        return


def start(target, *args) -> None:
    threading.Thread(target=target, args=args, daemon=True).start()


try:
    # sockets are opened in this order: video, audio (if enabled), control
    video = connect(port, dummy=True)
    audio_sock = connect(port, dummy=False) if audio else None
    ctrl = connect(port, dummy=False)
    vrd, vwr = os.pipe()
    start(video_pump, video, vwr)
    start(keyframe_keepalive, ctrl)
    start(drain, ctrl)
    if landscape is not None:
        start(orientation_watch)
    log(f"sdk {sdk}, audio {audio}, publishing to go2rtc")
    # minimal probing: the first packet carries SPS/PPS, and Home Assistant gives up if publishing takes over 5 s
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-fflags", "nobuffer", "-probesize", "32",
           "-analyzeduration", "0", "-thread_queue_size", "512", "-use_wallclock_as_timestamps", "1",
           "-f", "h264", "-i", f"pipe:{vrd}"]
    fds = [vrd]
    if audio:
        ard, awr = os.pipe()
        start(audio_pump, audio_sock, awr)
        fds.append(ard)
        # raw PCM gets its timing from the sample count; wall-clock stamps on its chunks go backwards
        cmd += ["-thread_queue_size", "512", "-f", "s16le", "-ar", "48000", "-ac", "2",
                "-i", f"pipe:{ard}", "-map", "0:v", "-map", "1:a", "-map", "1:a",
                # AAC first: Home Assistant's HLS (used remotely) takes the first audio track and only accepts AAC/MP3;
                # Opus second for WebRTC
                "-c:a:0", "aac", "-b:a:0", "128k",
                "-c:a:1", "libopus", "-b:a:1", "96k", "-application", "lowdelay"]
    cmd += ["-c:v", "copy", "-max_interleave_delta", "100000", "-f", "rtsp", "-rtsp_transport", "tcp", OUTPUT]
    ff = subprocess.Popen(cmd, pass_fds=fds)
    procs.insert(0, ff)
    ff.wait()
finally:
    stop()
