# Stream Control for Home Assistant

Watch and **listen to** the screens of several Android devices from a Home Assistant dashboard, over Wi-Fi. Each
device's screen and sound become an on-demand stream (H.264 video, Opus and AAC audio) that only runs while someone
watches it.

It is the audio+video companion of [ADB Control](https://github.com/mauriporte/ha-adb-control): ADB Control gives you
control (touch, keys) and video; Stream Control gives you video and sound, with no control. Turn Stream Control off
and the devices go back to ADB Control. It is meant for your own devices on your home network.

## What you get

- **Stream Control** switch: starts or stops the streaming service.
- Per device, two groups of switches; turning one on turns the others of its group off, so there is only one stream:
  - **Pantalla / Cámara frontal / Cámara trasera:** the screen or one of the cameras (Android 12+); all off = the
    device leaves the viewer;
  - **Sonido / Micrófono:** the device's sound or its microphone (Android 11+); both off = silence, instantly;
  - **Horizontal:** ADB Control's own switch; the stream (screen or camera) is locked to that shape.
  - Changing the source or the shape restarts the stream by itself in about 3 s.
- **Viewer:** ADB Control's viewer in stream mode, so the screens follow exactly the same layout rules, each one
  played over WebRTC with a sound button (browsers only autoplay muted video).
- From outside your home: through ADB Control's https address (see its README, *Remote access*), the player uses
  WebRTC when it can and otherwise MSE over a WebSocket, which goes through the tunnel.
- Optionally, a Home Assistant camera per device.
- The sound keeps playing on the device itself too (Android 13+).

## How it works

```
Home Assistant ──REST──▶ master.py :8005 ──▶ go2rtc :1984/:8554/:8555
viewer (ADB Control /view?mode=stream) ──▶ master.py /play ──WebRTC──▶ go2rtc
go2rtc (on demand) ──▶ stream.py ──adb──▶ scrcpy server on the device (raw H.264 + raw PCM)
                       stream.py ──▶ ffmpeg ──RTSP──▶ go2rtc
```

One container runs its own adb server, go2rtc, `master.py` (the switches' API, the player page and a WebRTC
signalling proxy) and, per watched device, `stream.py`:

- it starts the [scrcpy](https://github.com/Genymobile/scrcpy) 5.0.1 server on the device and reads its raw video and
  audio sockets;
- audio is filled with silence while the device plays nothing (otherwise ffmpeg would wait forever);
- if the encoder sends nothing while the screen is still (some Xiaomi devices), it asks for a new keyframe after 1 s;
- a keyframe with SPS/PPS every 2 s, so viewers that join a running stream get a picture quickly;
- ffmpeg probes as little as possible, so a stream publishes in about 3.5 s (Home Assistant gives up after 5 s);
- audio is published twice: AAC first (Home Assistant's HLS only takes AAC or MP3), Opus second (for WebRTC).

The API needs an `X-Token` header. The player page and the WebRTC proxy have **no authentication** (like ADB Control's
viewer), so keep ports 8005 and go2rtc's on your LAN only.

## Install

1. Prepare a data folder with `go2rtc.yaml` (start from [`go2rtc.example.yaml`](go2rtc.example.yaml): a password and
   one line per device).
2. Build and run the container (host network, so adb can reach the devices and WebRTC works on the LAN):

   ```sh
   docker build -t stream-control .
   docker run -d --name stream-control --restart unless-stopped --network host \
     -e ADB_CONTROL_TOKEN=<the same token as ADB Control> -e MASTER_PORT=8005 \
     -e ADB_CONTROL_URL=http://192.168.1.10:8002 \
     -v /path/to/stream-data:/data -v /path/to/stream-adb:/root/.android \
     stream-control
   ```

   - `ADB_CONTROL_TOKEN` must match ADB Control's: its viewer uses it to list the streams.
   - `ADB_CONTROL_URL` (optional; or a `/data/adb_control_url` file) is where the Horizontal switches are read.
   - `/root/.android` keeps this container's adb key: accept it once on each device, or reuse a key the devices
     already trust (`adbkey`/`adbkey.pub`).
3. ADB Control: put this container's address in its data folder as `stream_url` (e.g. `http://192.168.1.20:8005`).
4. Home Assistant:
   - add `stream_control_token` to `secrets.yaml`;
   - merge [`examples/home-assistant.yaml`](examples/home-assistant.yaml) into your configuration and restart;
   - add the view in [`examples/dashboard.yaml`](examples/dashboard.yaml) to a dashboard.

Devices are prepared exactly as for ADB Control (ADB over Wi-Fi on port 5555; on Xiaomi also *USB debugging
(Security settings)*). Both containers can be connected to the same device at once.

## API

| Request | Effect |
|---|---|
| `GET/POST /master` | Stream Control on/off (starts or stops go2rtc; off also disconnects this container's adb) |
| `GET/POST /screen`, `/camera`, `/camera_back`, `/sound`, `/mic` | per device: `?ip=` to read, `{"ip", "on"}` to set (each group exclusive) |
| `GET/POST /video`, `/audio` | older on/off switches: any video / any sound |
| `GET /streams` | streams by device IP and their Vídeo switch, for ADB Control's viewer |
| `GET /play?src=` | one stream's player page (no token) |
| `POST /webrtc?src=` | WebRTC offer, passed on to go2rtc with its login (no token) |
| `GET /ws?src=` | WebSocket: go2rtc's MSE stream, the player's fallback (no token) |

## Limits

- While the camera or the microphone is in use, Android shows its privacy indicator, and an app on the device that
  is already using the camera keeps the stream from opening it (and the other way round).
- Audio needs Android 11+; on older devices (and devices that accept only one adb host) leave them to ADB Control.
- On its own the viewer is plain http, which an https Home Assistant page refuses: for remote use, open it through
  ADB Control's https address. WebRTC's media has no route in from outside, so remotely the player falls back to MSE
  (a little more delay).
- The Horizontal switch fixes the picture's shape; if the device shows the other orientation, the picture is sideways
  (as in ADB Control's frame).
- Through Home Assistant's own go2rtc, WebRTC viewers get the AAC track and Home Assistant converts it to Opus with
  one extra ffmpeg while someone watches.
