"""Read account allowance through the installed Copilot CLI without a model session."""
import asyncio
import json
import subprocess
import tempfile
import threading
import time

import appapi
import osops


_cache_lock = threading.Lock()
_cached = {}
_next_fetch = 0


async def _account_quota(cli, folder):
    # The Copilot SDK uses Content-Length framed JSON-RPC over CLI stdio.
    command = ["cmd.exe", "/c", cli] if osops.IS_WIN and cli.lower().endswith((".cmd", ".bat")) else [cli]
    child = await asyncio.create_subprocess_exec(
        *command, "--headless", "--stdio", "--no-auto-update", cwd=folder,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        creationflags=osops.NO_WINDOW,
    )

    async def request(request_id, method, params):
        data = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}).encode()
        child.stdin.write(f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
        await child.stdin.drain()
        while True:
            headers = await child.stdout.readuntil(b"\r\n\r\n")
            for line in headers.splitlines():
                name, _, value = line.partition(b":")
                if name.lower() == b"content-length":
                    size = int(value)
                    break
            else:
                raise ValueError("Missing Copilot response length")
            if not 0 < size <= 1_000_000:
                raise ValueError("Invalid Copilot response length")
            response = json.loads(await child.stdout.readexactly(size))
            if isinstance(response, dict) and response.get("id") == request_id:
                return response

    async def read():
        connection = await request(1, "connect", {"supportedTaskKinds": []})
        if "error" in connection and connection["error"].get("code") != -32601:
            return {}
        response = await request(2, "account.getQuota", {})
        return (response.get("result") or {}).get("quotaSnapshots") or {}

    try:
        return await asyncio.wait_for(read(), 8)
    finally:
        child.stdin.close()
        try:
            await asyncio.wait_for(child.communicate(), 2)
        except TimeoutError:
            if child.returncode is None:
                child.kill()
            await child.communicate()


def quota_windows(snapshots):
    labels = {"premium_interactions": "Premium", "ai_credits": "AI credits", "session": "Session",
              "weekly": "Weekly", "chat": "Chat", "completions": "Completions"}
    windows = []
    for key, label in labels.items():
        quota = snapshots.get(key, {})
        if not isinstance(quota, dict):
            continue
        entitlement, remaining = quota.get("entitlementRequests"), quota.get("remainingPercentage")
        if type(entitlement) not in (int, float) or not 0 < entitlement < float("inf"):
            continue
        if type(remaining) not in (int, float) or not 0 <= remaining <= 100:
            continue
        # Some CLI versions report the fetch time as resetDate.
        windows.append({"label": label, "usedPercent": round(100 - remaining, 1)})
    return windows


_background = {"running": False}
_background_lock = threading.Lock()


def cached():
    """The last answer without waiting; starts a refresh in the background when it is due. None until the first answer arrives."""
    if time.monotonic() >= _next_fetch:
        with _background_lock:
            if not _background["running"]:
                _background["running"] = True
                def refresh():
                    try: usage()
                    except Exception: pass
                    finally: _background["running"] = False
                threading.Thread(target=refresh, name="copilot-quota", daemon=True).start()
    return _cached or None


def usage():
    global _cached, _next_fetch
    with _cache_lock:
        if time.monotonic() < _next_fetch:
            return _cached
        cli = appapi.access_launcher("copilot")
        windows = []
        if cli:
            try:
                with tempfile.TemporaryDirectory() as folder:
                    snapshots = asyncio.run(_account_quota(cli, folder))
                windows = quota_windows(snapshots)
            except (OSError, TimeoutError, ValueError, AttributeError, EOFError, asyncio.LimitOverrunError):
                pass
        _cached = {"windows": windows, "installed": bool(cli)}
        _next_fetch = time.monotonic() + (300 if windows else 60)
        return _cached
