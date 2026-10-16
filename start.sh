#!/bin/sh
# own adb server (keys in /root/.android, a volume); each stream connects its device when it starts
adb start-server
exec python3 /opt/master.py
