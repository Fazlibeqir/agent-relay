"""Phone access: pairing, per-device tokens, and an opt-in listener on the home network.

The main dashboard server only listens on this computer. When the owner turns on phone access, a second server listens on the
local network (a separate port) and accepts only requests that carry a device token, to a short list of endpoints: the status
summary, approving or denying requests, the review buttons, and start/stop. Setup, installs, project creation, settings and
files stay on the computer.

A phone gets its token by scanning a pairing code shown on the computer: the code is random, expires after a few minutes, and
works once. Tokens are stored only as SHA-256 hashes in phones.json (in the app's data folder), each device can be removed, and a
device is either "control" (can act) or "view" (can only look).
"""
import hashlib, hmac, ipaddress, json, os, re, secrets, shutil, socket, subprocess, threading, time

import appapi, osops

FILE = appapi.APPDATA / "phones.json"
DEFAULT_PORT = 8766
PAIR_SECONDS = 300
NAME_MAX = 40

# What a paired phone may reach. Everything else answers 403 on the phone listener.
PUBLIC_GET = {"/phone", "/phone.webmanifest"}
PUBLIC_POST = {"/api/phone/pair"}
VIEW_GET = {"/api/phone/summary", "/api/phone/me"}
CONTROL_POST = {"/api/relay/start", "/api/relay/stop", "/api/requests/decide",
                "/api/task/approve", "/api/task/changes", "/api/task/feedback", "/api/task/next", "/api/task/cancel"}

_lock = threading.Lock()
_pairing = {}            # code -> {"scope", "expires"}
_failures = []           # times of wrong pairing codes (all phones together)
_listener = {"server": None, "thread": None, "port": None, "error": ""}


# ---------------------------------------------------------------- stored devices
def _load():
    try: data = json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError): data = {}
    if not isinstance(data, dict): data = {}
    data.setdefault("enabled", False)
    data.setdefault("port", DEFAULT_PORT)
    if not isinstance(data.get("devices"), list): data["devices"] = []
    return data


def _save(data):
    FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(FILE)


def _hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def public_device(d):
    return {k: d.get(k) for k in ("id", "name", "scope", "created", "lastSeen", "lastIp")}


def devices():
    with _lock: return [public_device(d) for d in _load()["devices"]]


def device_for(token):
    """The paired device this token belongs to, or None. Compares every stored hash in constant time."""
    if not token or len(token) > 200: return None
    h = _hash(token)
    with _lock:
        data = _load()
        hit = None
        for d in data["devices"]:
            if hmac.compare_digest(str(d.get("tokenHash", "")), h): hit = d
        return dict(hit) if hit else None


def touch(device_id, ip):
    """Remember when a device was last seen; written at most once a minute per device."""
    with _lock:
        data = _load()
        for d in data["devices"]:
            if d["id"] == device_id and (time.time() - (d.get("lastSeen") or 0) > 60 or d.get("lastIp") != ip):
                d["lastSeen"] = time.time(); d["lastIp"] = ip
                _save(data)
                return


def revoke(device_id):
    with _lock:
        data = _load()
        left = [d for d in data["devices"] if d.get("id") != device_id]
        if len(left) == len(data["devices"]): return {"ok": False, "error": "That phone is not paired."}
        data["devices"] = left
        _save(data)
    return {"ok": True}


# ---------------------------------------------------------------- pairing
def start_pairing(scope):
    """A new one-time code (any earlier code stops working)."""
    if scope not in ("control", "view"): return {"ok": False, "error": "Choose what the phone may do."}
    code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(10))
    expires = time.time() + PAIR_SECONDS
    with _lock:
        _pairing.clear()
        _pairing[code] = {"scope": scope, "expires": expires}
    return {"ok": True, "code": code, "expiresAt": expires, "scope": scope}


def cancel_pairing():
    with _lock: _pairing.clear()
    return {"ok": True}


def pairing_state():
    now = time.time()
    with _lock:
        live = [(c, p) for c, p in _pairing.items() if p["expires"] > now]
    return {"active": bool(live), "expiresAt": live[0][1]["expires"] if live else None, "scope": live[0][1]["scope"] if live else None}


def redeem(code, name, ip):
    """Exchange a pairing code for a device token (returned once, never stored in plain form)."""
    now = time.time()
    code = "".join(ch for ch in str(code or "").upper() if ch.isalnum())[:20]
    name = " ".join(str(name or "").split())[:NAME_MAX] or "Phone"
    with _lock:
        _failures[:] = [t for t in _failures if now - t < 60]
        if len(_failures) >= 10:
            _pairing.clear()          # someone is guessing: the code stops working; pair again from the computer
            return {"ok": False, "error": "Too many wrong codes. Start pairing again on the computer."}
        hit = None
        for c, p in list(_pairing.items()):
            if p["expires"] <= now: _pairing.pop(c, None); continue
            if hmac.compare_digest(c, code): hit = (c, p)
        if not hit:
            _failures.append(now)
            return {"ok": False, "error": "That code is wrong or expired. Show a new code on the computer."}
        _pairing.pop(hit[0], None)
        token = secrets.token_urlsafe(32)
        dev = {"id": secrets.token_hex(6), "name": name, "scope": hit[1]["scope"], "tokenHash": _hash(token),
               "created": now, "lastSeen": now, "lastIp": ip}
        data = _load()
        data["devices"].append(dev)
        _save(data)
    return {"ok": True, "token": token, "device": public_device(dev)}


# ---------------------------------------------------------------- network
def lan_addresses():
    """This computer's addresses on the local network (private IPv4 only). Uses a UDP socket to read the route; nothing is sent."""
    found = []
    for probe in ("192.168.0.1", "10.0.0.1", "172.16.0.1"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect((probe, 9)); ip = s.getsockname()[0]
            finally: s.close()
        except OSError: continue
        if ip not in found: found.append(ip)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            if info[4][0] not in found: found.append(info[4][0])
    except OSError: pass
    out = []
    for ip in found:
        try: a = ipaddress.ip_address(ip)
        except ValueError: continue
        if a.is_private and not a.is_loopback and not a.is_link_local: out.append(ip)
    return out


def status():
    data = _load()
    port = _listener["port"] or data["port"]
    running = _listener["server"] is not None
    return {"enabled": data["enabled"], "running": running, "port": port, "error": _listener["error"],
            "addresses": lan_addresses() if running else [], "devices": devices(), "pairing": pairing_state(),
            "tailscale": tailscale_state(port) if running else None}


# ---------------------------------------------------------------- away from home: Tailscale
# `tailscale serve` publishes the phone listener (never the computer-only port) at https://<this-pc>.<tailnet>.ts.net, reachable only by
# devices signed in to the same Tailscale account. Funnel (the public internet) is never turned on by the app.
_TS_CACHE = {"at": 0, "port": None, "value": None}


def tailscale_bin():
    found = shutil.which("tailscale")
    if found: return found
    for p in (r"C:\Program Files\Tailscale\tailscale.exe", "/usr/bin/tailscale", "/usr/local/bin/tailscale", "/usr/sbin/tailscale"):
        if os.path.isfile(p): return p
    return ""


def _ts(args, timeout=10):
    """(exit code, output) of a tailscale command; output is whatever it printed even when it had to be stopped."""
    exe = tailscale_bin()
    if not exe: return 127, ""
    try:
        p = subprocess.Popen([exe] + list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             text=True, encoding="utf-8", errors="replace", creationflags=osops.NO_WINDOW)
    except OSError as e:
        return 126, str(e)
    try:
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, out or ""
    except subprocess.TimeoutExpired:
        # A command that waits (for example for HTTPS to be allowed) is stopped with everything it started, then its output is read.
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, creationflags=osops.NO_WINDOW)
        else:
            p.kill()
        try: out, _ = p.communicate(timeout=5)
        except subprocess.TimeoutExpired: out = ""
        return 124, out or ""


def _serve_config():
    rc, out = _ts(["serve", "status", "--json"])
    try: cfg = json.loads(out) if rc == 0 and out.strip().startswith("{") else {}
    except ValueError: cfg = {}
    return cfg if isinstance(cfg, dict) else {}


def tailscale_state(port, fresh=False):
    now = time.time()
    if not fresh and _TS_CACHE["value"] is not None and _TS_CACHE["port"] == port and now - _TS_CACHE["at"] < 8: return _TS_CACHE["value"]
    st = {"installed": bool(tailscale_bin()), "signedIn": False, "dnsName": "", "url": "", "serving": False, "servingOther": False,
          "funnel": False, "httpsCerts": False, "install": osops.install_line(TS_INSTALL)}
    if st["installed"]:
        rc, out = _ts(["status", "--json"])
        try: s = json.loads(out) if out.strip().startswith("{") else {}
        except ValueError: s = {}
        me = s.get("Self") or {}
        st["backend"] = s.get("BackendState", "")
        st["signedIn"] = st["backend"] == "Running"
        st["dnsName"] = str(me.get("DNSName") or "").rstrip(".")
        st["httpsCerts"] = bool(s.get("CertDomains"))
        if st["signedIn"] and st["dnsName"]:
            cfg = _serve_config()
            mine = "http://127.0.0.1:%s" % port
            for host, web in (cfg.get("Web") or {}).items():
                for h in ((web or {}).get("Handlers") or {}).values():
                    if (h or {}).get("Proxy", "").rstrip("/") in (mine, "http://localhost:%s" % port): st["serving"] = True
                    else: st["servingOther"] = True
            st["funnel"] = any((cfg.get("AllowFunnel") or {}).values())
            if st["serving"]: st["url"] = "https://%s/" % st["dnsName"]
    _TS_CACHE.update(at=now, port=port, value=st)
    return st


TS_INSTALL = {"winget": "Tailscale.Tailscale", "script": "curl -fsSL https://tailscale.com/install.sh | sh"}


def tailscale_install():
    line = osops.install_line(TS_INSTALL)
    if not line: return {"ok": False, "error": "Install Tailscale from tailscale.com/download, then come back here."}
    ok, msg = osops.open_terminal("Install - Tailscale", line)
    _TS_CACHE["value"] = None
    return {"ok": ok, "message": "A terminal window opened and is installing Tailscale. Then sign in." if ok else msg}


def tailscale_signin():
    exe = tailscale_bin()
    if not exe: return {"ok": False, "error": "Install Tailscale first."}
    line = osops.quote(exe) + " up" if os.name == "nt" else "sudo " + osops.quote(exe) + " up --operator=$USER"
    ok, msg = osops.open_terminal_keep_shell("Sign in - Tailscale", line)
    _TS_CACHE["value"] = None
    return {"ok": ok, "message": "Follow the sign-in steps in the window (a browser page opens). Use the same account on your phone." if ok else msg}


def tailscale_serve(on):
    """Publish the phone listener on this computer's private Tailscale address, or stop publishing it."""
    port = _listener["port"] or _load()["port"]
    st = tailscale_state(port, fresh=True)
    if not st["installed"]: return {"ok": False, "error": "Install Tailscale first."}
    if not on:
        if st["serving"]: _ts(["serve", "--https=443", "off"], timeout=20)
        _TS_CACHE["value"] = None
        return {"ok": True}
    if _listener["server"] is None: return {"ok": False, "error": "Turn on phone access first."}
    if not st["signedIn"]: return {"ok": False, "error": "Sign in to Tailscale first."}
    if st["servingOther"]: return {"ok": False, "error": "Tailscale on this computer already publishes something else (tailscale serve). The app does not change it."}
    if st["serving"]: return {"ok": True, "url": st["url"]}
    rc, out = _ts(["serve", "--bg", "http://127.0.0.1:%s" % port], timeout=25)
    _TS_CACHE["value"] = None
    st = tailscale_state(port, fresh=True)
    if st["serving"]: return {"ok": True, "url": st["url"]}
    link = re.search(r"https://login\.tailscale\.com/\S+", out or "")
    if link:
        _ts(["serve", "--https=443", "off"], timeout=10)       # the waiting command was stopped; leave nothing half set up
        return {"ok": False, "needsEnable": link.group(0).rstrip(".,"),
                "error": "Your Tailscale account must allow HTTPS for this. Open the link, press Enable, then press Use from anywhere again."}
    return {"ok": False, "error": "Tailscale did not start serving: " + " ".join((out or "no answer").split())[:300]}


def start_listener(handler_cls):
    """Listen on all local network addresses on the phone port. Returns an error message or ""."""
    from http.server import ThreadingHTTPServer
    if _listener["server"] is not None: return ""
    port = _load()["port"]
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", port), handler_cls)
    except OSError as e:
        _listener["error"] = "Could not listen on port %s (%s). Another program may be using it." % (port, e.strerror or e)
        return _listener["error"]
    t = threading.Thread(target=srv.serve_forever, name="phone-listener", daemon=True)
    t.start()
    _listener.update(server=srv, thread=t, port=port, error="")
    return ""


def stop_listener():
    srv = _listener["server"]
    _listener.update(server=None, thread=None, port=None)
    if srv is not None:
        srv.shutdown(); srv.server_close()


def set_enabled(on, handler_cls):
    with _lock:
        data = _load(); data["enabled"] = bool(on); _save(data)
    if on:
        err = start_listener(handler_cls)
        return {"ok": not err, "error": err, **status()}
    tailscale_serve(False) if tailscale_bin() else None
    stop_listener(); cancel_pairing()
    _listener["error"] = ""
    return {"ok": True, **status()}


def pair_url(code):
    st = status()
    if not st["running"]: return ""
    ts = st.get("tailscale") or {}
    if ts.get("serving") and ts.get("url"): return ts["url"] + "phone#pair=" + code       # works at home and away
    if not st["addresses"]: return ""
    return "http://%s:%s/phone#pair=%s" % (st["addresses"][0], st["port"], code)


# ---------------------------------------------------------------- QR code (optional: needs the segno package)
def qr_svg(text):
    try:
        import segno
    except ImportError:
        return ""
    import io
    buf = io.BytesIO()
    segno.make(text, error="m", micro=False).save(buf, kind="svg", scale=6, border=2, dark="#0b0f14", light="#ffffff", xmldecl=False, svgns=True)
    return buf.getvalue().decode("utf-8")
