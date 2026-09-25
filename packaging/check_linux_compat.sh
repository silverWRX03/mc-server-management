#!/usr/bin/env bash
# Prove a Linux executable runs on an old distribution: CentOS 7 has glibc 2.17,
# the oldest mcsm supports. Runs the full start -> web UI -> stop cycle in a container.
#
#   packaging/check_linux_compat.sh dist/mcsm [image]
set -euo pipefail
exe="$(cd "$(dirname "$1")" && pwd)/$(basename "$1")"
image="${2:-centos:7}"

docker run --rm -v "$exe":/app/mcsm:ro "$image" sh -ec '
  echo "== $(ldd --version 2>&1 | head -1)"
  cd /tmp && mkdir srv && cd srv
  /app/mcsm --version
  XDG_CONFIG_HOME=/tmp/a /app/mcsm --accept-notice init >/dev/null
  test -f mcsm.toml
  /app/mcsm licenses --full | grep -q "PYTHON SOFTWARE FOUNDATION LICENSE"
  export XDG_CONFIG_HOME=/tmp/b   # notice not accepted here, so no server is downloaded
  /app/mcsm run --web --web-port 8765 >run.log 2>&1 &
  pid=$!
  for i in $(seq 1 60); do
    if curl -sf -o /dev/null http://127.0.0.1:8765/; then echo "web UI served"; break; fi
    kill -0 $pid 2>/dev/null || { cat run.log; exit 1; }
    sleep 0.5
  done
  curl -sf http://127.0.0.1:8765/app.js | grep -q "use strict"
  /app/mcsm stop
  for i in $(seq 1 60); do
    kill -0 $pid 2>/dev/null || { echo "stopped cleanly"; exit 0; }
    sleep 0.5
  done
  echo "did not stop"; cat run.log; exit 1
'
echo "compatible with $image"
