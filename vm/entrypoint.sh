#!/bin/sh
# Starts as root only to prepare /data (a volume) and the callers' tokens, then runs everything as
# `worker`: the worker itself, or the command given (the installer's browser check).
set -eu
umask 077
install -d -o worker -g worker -m 700 /data
# The Mac keeps the client registry and tokens (its callers read their token files there); the worker
# wants them inside its private data directory.
if [ -d /auth ]; then
  rm -rf /data/tokens /data/clients.json
  cp -R /auth/tokens /data/tokens
  cp /auth/clients.json /data/clients.json
  chown -R worker:worker /data/tokens /data/clients.json
fi
export HOME=/home/worker
exec setpriv --reuid=worker --regid=worker --init-groups /bin/sh -c '
  # The virtual display Chromium windows open on.
  Xvfb "$DISPLAY" -screen 0 1920x1080x24 -nolisten tcp >/dev/null 2>&1 &
  i=0
  until [ -S "/tmp/.X11-unix/X${DISPLAY#:}" ]; do
    i=$((i + 1)); [ "$i" -le 100 ] || { echo "Xvfb did not start" >&2; exit 1; }
    sleep 0.1
  done
  # A silent audio output: sites see a device, and nothing plays anywhere.
  pulseaudio --start --exit-idle-time=-1 --load=module-null-sink >/dev/null 2>&1 \
    || echo "PulseAudio did not start; the browser has no audio device" >&2
  [ "$#" -eq 0 ] || exec "$@"
  exec /opt/browser-worker/.venv/bin/python /opt/browser-worker/mcp-browser-worker/http_server.py
' sh "$@"
