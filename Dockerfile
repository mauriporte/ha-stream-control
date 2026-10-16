# go2rtc + adb + ffmpeg + the scrcpy server: each Android device's screen and sound as an on-demand stream.
FROM debian:bookworm-slim
ARG GO2RTC_VERSION=1.9.14
ARG SCRCPY_VERSION=5.0.1
RUN apt-get update && apt-get install -y --no-install-recommends adb ffmpeg python3 ca-certificates curl \
 && curl -fsSL -o /usr/local/bin/go2rtc \
      "https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/go2rtc_linux_amd64" \
 && chmod +x /usr/local/bin/go2rtc \
 && mkdir -p /opt/scrcpy \
 && curl -fsSL -o /opt/scrcpy/scrcpy-server.jar \
      "https://github.com/Genymobile/scrcpy/releases/download/v${SCRCPY_VERSION}/scrcpy-server-v${SCRCPY_VERSION}" \
 && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*
COPY stream.py master.py start.sh /opt/
RUN chmod +x /opt/start.sh
CMD ["/opt/start.sh"]
