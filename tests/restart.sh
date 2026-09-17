#!/bin/bash
# Restart the test harness (app + mock graph + hostile reverse proxy).
cd "$(dirname "$0")/.."
[ -f /tmp/od_harness.pid ] && kill "$(cat /tmp/od_harness.pid)" 2>/dev/null
sleep 1
nohup python3 tests/harness.py "$@" > /tmp/harness.log 2>&1 &
echo $! > /tmp/od_harness.pid
for i in $(seq 1 40); do
  head -1 /tmp/harness.log | grep -q '^{' && break
  sleep 0.5
done
head -1 /tmp/harness.log
