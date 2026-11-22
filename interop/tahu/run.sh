#!/usr/bin/env bash
# Run tagbridge against the demo PLC and check its Sparkplug B output with the
# Eclipse Tahu reference decoder, including a PLC restart (DDEATH/DBIRTH) and a
# gateway crash (NDEATH via MQTT will) followed by a restart (new NBIRTH).
#
# Needs: an MQTT broker on localhost:1883, Java 17+, Maven, and tagbridge installed.
set -euo pipefail
cd "$(dirname "$0")"
CONFIG=../../examples/demo.yaml
TAGBRIDGE=${TAGBRIDGE:-tagbridge}

mvn -q -B compile
mvn -q -B exec:java -Dexec.args="tcp://localhost:1883 30 20" &
checker=$!
sleep 4

$TAGBRIDGE demo-server --opcua-port 4840 --modbus-port 5020 >/tmp/tahu-demo.log 2>&1 &
demo=$!
sleep 3
$TAGBRIDGE run "$CONFIG" >/tmp/tahu-gateway.log 2>&1 &
gateway=$!
sleep 6

echo "restarting the PLC"
kill "$demo"; sleep 4
$TAGBRIDGE demo-server >/tmp/tahu-demo.log 2>&1 &
demo=$!
sleep 6

echo "killing the gateway (no clean shutdown)"
kill -9 "$gateway"; sleep 2
$TAGBRIDGE run "$CONFIG" >>/tmp/tahu-gateway.log 2>&1 &
gateway=$!

status=0
wait "$checker" || status=$?
kill "$gateway" "$demo" 2>/dev/null || true
exit "$status"
