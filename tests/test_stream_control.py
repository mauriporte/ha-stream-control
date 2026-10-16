import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).parents[1]
os.environ.setdefault("ADB_CONTROL_TOKEN", "test")
_spec = importlib.util.spec_from_file_location("master", ROOT / "master.py")
master = importlib.util.module_from_spec(_spec)
sys.modules["master"] = master
_spec.loader.exec_module(master)


def stream_functions(dumpsys: str) -> dict:
    """stream.py runs as a script; take its helper functions (everything before it reads its arguments)."""
    src = (ROOT / "stream.py").read_text(encoding="utf8").split("IP, OUTPUT = sys.argv")[0]
    ns: dict = {"__name__": "stream"}
    exec(compile(src, "stream.py", "exec"), ns)
    ns["adb"] = lambda *args, **kw: dumpsys
    return ns


def display(w: int, h: int, rotation: int) -> str:
    return f'mOverrideDisplayInfo=DisplayInfo{{"Built-in", displayId 0, real {w} x {h}, rotation {rotation}, x}}'


def test_capture_orientation_locks_the_wanted_shape():
    tablet_turned = stream_functions(display(1920, 1200, 1))  # portrait-native tablet, now in landscape
    assert tablet_turned["capture_orientation"](True) == "@"  # already landscape: lock as it is
    assert tablet_turned["capture_orientation"](False) == "@0"  # its natural orientation is portrait
    phone = stream_functions(display(1080, 1920, 0))
    assert phone["capture_orientation"](False) == "@"
    assert phone["capture_orientation"](True) == "@90"
    assert stream_functions("no display info")["capture_orientation"](True) == "@"


def test_audio_switch_defaults_on(tmp_path):
    ns = stream_functions("")
    ns["IP"] = "192.168.1.21"
    ns["PREFS"] = str(tmp_path / "missing.json")
    assert ns["audio_on"]() is True
    prefs = tmp_path / "prefs.json"
    prefs.write_text('{"audio": {"192.168.1.21": false}}')
    ns["PREFS"] = str(prefs)
    assert ns["audio_on"]() is False


def test_streams_and_login_come_from_the_go2rtc_config(tmp_path, monkeypatch):
    cfg = tmp_path / "go2rtc.yaml"
    cfg.write_text((ROOT / "go2rtc.example.yaml").read_text(encoding="utf8"))
    monkeypatch.setattr(master, "CONFIG", str(cfg))
    assert master.streams() == {"192.168.1.21": "phone_a", "192.168.1.23": "tablet"}
    assert master.go2rtc_auth() == "Basic c3RyZWFtOmNoYW5nZS1tZQ=="  # stream:change-me


def test_prefs_default_to_on(tmp_path, monkeypatch):
    monkeypatch.setattr(master, "PREFS", str(tmp_path / "prefs.json"))
    assert master.load_prefs() == {"audio": {}, "video": {}}


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_player_page_javascript_parses(tmp_path):
    js = master.PLAY.split("<script>")[1].split("</script>")[0]
    f = tmp_path / "play.js"
    f.write_text(js, encoding="utf8")
    assert subprocess.run(["node", "--check", str(f)]).returncode == 0


def test_switch_pairs_turn_each_other_off():
    prefs = {"audio": {}, "video": {}}
    ip = "192.168.1.21"
    state = lambda: {k: master.switch_state(prefs, "/" + k, ip) for k in ("screen", "camera", "sound", "mic")}
    assert state() == {"screen": True, "camera": False, "sound": True, "mic": False}  # defaults
    master.set_switch(prefs, "/camera", ip, True)
    master.set_switch(prefs, "/mic", ip, True)
    assert state() == {"screen": False, "camera": True, "sound": False, "mic": True}
    master.set_switch(prefs, "/screen", ip, False)  # turning off the one not in use changes nothing
    assert state()["camera"] is True
    master.set_switch(prefs, "/camera", ip, False)  # both off: no video
    assert master.source(prefs, "video", ip) == "off" and not master.switch_state(prefs, "/video", ip)
    prefs["audio"][ip] = False  # written by the older on/off switch
    assert master.source(prefs, "audio", ip) == "off"


def test_camera_follows_the_wanted_shape():
    cams = (" android.lens.facing (80005): byte[1]\n [BACK ]\n android.sensor.orientation (e000e): int32[1]\n [90 ]\n"
            " android.lens.facing (80005): byte[1]\n [FRONT ]\n android.sensor.orientation (e000e): int32[1]\n [270 ]\n")
    ns = stream_functions("")
    ns["adb"] = lambda *args, **kw: cams if "media.camera" in args else display(1080, 1920, 0)
    assert ns["camera_orientation"](False) == "270"  # upright, vertical
    assert ns["camera_orientation"](True) == "180"  # horizontal


def test_sources_read_old_and_new_values(tmp_path):
    ns = stream_functions("")
    ns["IP"] = "192.168.1.21"
    prefs = tmp_path / "prefs.json"
    prefs.write_text('{"video": {"192.168.1.21": "camera"}, "audio": {"192.168.1.21": true}}')
    ns["PREFS"] = str(prefs)
    assert ns["sources"]() == ("camera", "output")
    assert ns["capture"]() == (True, False)
