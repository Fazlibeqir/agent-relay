"""Turn `claude -p --output-format stream-json --verbose` output back into the plain final text.

relay.sh streams each agent's output to relay-live-<agent>.log so relay-status.ps1 can show what it is doing
right now; the rest of the relay still expects the plain final message, so this prints the last `result` event.
If the stream has no result event (crash, timeout), the last lines are printed raw so limit/error text survives."""
import json
import sys

sys.stdin.reconfigure(encoding="utf-8", errors="replace")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
lines = sys.stdin.read().splitlines()
final = None
for line in lines:
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        event = json.loads(line)
    except ValueError:
        continue
    if event.get("type") == "result":
        final = event
if final is not None:
    print(final.get("result") or "")
else:
    print("\n".join(lines[-40:]))
