"""Estimates for the dashboard's progress bar.

The old estimate was the median of the last five phase durations found in the last 1.5 MB of relay.log. A project whose log holds full test output
(the game's log is about 70 MB) kept only one or two rounds in that window, so one no-op build (a retry that made 0 commits, 85 s) became the BUILD
estimate against real builds of 6 to 24 minutes. This module reads the whole log once and then only what was added (incremental, cached), cleans the
samples, and keeps separate samples for what really differs:

  - no-op builds (0 commits) and a suite that was rerun after the known T72 flake are not samples;
  - serial builds are keyed by builder, parallel lanes by builder, and a parallel round's build time is its slowest lane;
  - the test suite is measured by the "[checks]" lines the runner prints, against the size of the last runs.
"""
import json, os, pathlib, re, statistics, threading, time

LOG_LINE = re.compile(rb"^\[(20\d\d-\d\d-\d\d \d\d:\d\d:\d\d)\] (.*)$", re.M)
FIRST_READ_LIMIT = 200_000_000     # a log bigger than this is read from its end
KEEP_EVENTS = 4000
SERIAL_BUILDER = re.compile(r"— (\w+) building$")
LANE_START = re.compile(r"(T\d+[a-z]?): (\w+) builds in its own worktree")
LANE_DONE = re.compile(r"^\[(T\d+[a-z]?)\] committed for ")
ALL_DONE = re.compile(r"all builders done after (\d+)s")
FLAKE = "rerunning the whole suite"
CHECK_LINE = re.compile(rb"^\[checks\] (.+?)\r?$", re.M)

_lock = threading.Lock()
_logs = {}


def history(relay):
    """Every timestamped line of relay.log as {"t", "ts", "msg"}. The first call reads the whole file; later calls read only the new bytes."""
    path = pathlib.Path(relay) / "relay.log"
    with _lock:
        entry = _logs.setdefault(str(path), {"offset": 0, "events": []})
        try: size = path.stat().st_size
        except OSError: return []
        if size < entry["offset"]: entry["offset"], entry["events"] = 0, []
        if size > entry["offset"]:
            start = entry["offset"] if entry["offset"] or size <= FIRST_READ_LIMIT else size - FIRST_READ_LIMIT
            try:
                with open(path, "rb") as f:
                    f.seek(start); data = f.read(size - start)
            except OSError:
                return list(entry["events"])
            cut = data.rfind(b"\n")
            if cut >= 0:
                data = data[:cut + 1]
                entry["offset"] = start + len(data)
                for m in LOG_LINE.finditer(data):
                    stamp = m.group(1).decode("ascii")
                    try: t = time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S"))
                    except (ValueError, OverflowError): continue
                    entry["events"].append({"t": t, "ts": stamp[11:16], "msg": m.group(2).decode("utf-8", errors="replace").rstrip("\r")})
                if len(entry["events"]) > KEEP_EVENTS: del entry["events"][:len(entry["events"]) - KEEP_EVENTS]
        return list(entry["events"])


def observations(events, kind_of):
    """Clean duration samples (seconds, oldest first) per phase, read from the log's own lines."""
    obs = {}
    add = lambda key, value: obs.setdefault(key, []).append(value)
    base = disp = build = suite = review = None
    disp_last = 0.0
    flake = parallel_build = False
    provider = ""
    lanes = {}
    for e in events:
        t, msg = e["t"], e["msg"]
        k = kind_of(msg)
        if FLAKE in msg: flake = True
        m = LANE_START.search(msg)
        if m: lanes[m.group(1)] = (t, m.group(2).lower()); parallel_build = True
        m = LANE_DONE.match(msg)
        if m and m.group(1) in lanes and t - lanes[m.group(1)][0] >= 30:
            add("PLANE:" + lanes[m.group(1)][1], t - lanes[m.group(1)][0])
        m = ALL_DONE.search(msg)
        if m: add("PBUILD", int(m.group(1))); build = None; lanes = {}
        if "did not make it in parallel mode" in msg: suite = review = None
        if k == "BASELINE": base, flake = t, False
        elif k == "BASELINE_OK":
            if base is not None and not flake and t - base >= 30: add("BASELINE", t - base)
            base = None
        elif k == "STOPPED": base = suite = None
        elif k == "DISPATCH":
            if disp is None or t - disp_last > 120: disp = t
            disp_last = t
        elif k == "BUILD":
            if disp is not None and t - disp >= 5: add("DISPATCH", t - disp)
            disp = None
            if build is None:
                build = t
                m = SERIAL_BUILDER.search(msg)
                provider = m.group(1).lower() if m else ""
        elif k == "SUITE":
            if build is not None and not parallel_build and "made 0 commit" not in msg and t - build >= 60:
                add("BUILD", t - build)
                if provider: add("BUILD:" + provider, t - build)
            build, parallel_build, suite, flake = None, False, t, False
        elif k == "REVIEW":
            if suite is not None:
                if not flake and t - suite >= 30: add("SUITE", t - suite)
                suite = None
            if review is None: review = t
        elif k == "ROUND_DONE":
            if review is not None and t - review >= 5: add("REVIEW", t - review)
            review = None
    return obs


def stats(values):
    values = values[-8:]
    if not values: return None
    median = statistics.median(values[-5:])
    if len(values) >= 3:
        q = statistics.quantiles(values, n=4, method="inclusive"); low, high = q[0], q[2]
    else:
        low = high = median
    return {"n": len(values), "median": median, "low": low, "high": high}


def estimates(obs, defaults):
    """-> (estimate per phase, spread per phase) with the same keys as `defaults`; a phase with no clean sample keeps its default."""
    spread = {k: stats(obs.get(k, [])) for k in defaults}
    return {k: (spread[k]["median"] if spread[k] else defaults[k]) for k in defaults}, spread


def build_estimate(obs, est, provider=None, lane_providers=None):
    """How long the build in progress should take. A parallel round is as slow as its slowest lane; a serial build is judged by its own builder."""
    if lane_providers:
        per = [stats(obs.get("PLANE:" + p, [])) for p in lane_providers]
        per = [s for s in per if s]
        if per: return max(s["median"] for s in per), max(per, key=lambda s: s["median"])
        s = stats(obs.get("PBUILD", []))
        if s: return s["median"], s
    elif provider:
        s = stats(obs.get("BUILD:" + provider, []))
        if s and s["n"] >= 2: return s["median"], s
    return None, None


def count_checks(relay, since):
    """(number of "[checks]" lines the current test run has printed, the last one). 0 when the log is older than this run."""
    path = pathlib.Path(relay) / "relay-tests.log"
    try:
        st = path.stat()
        if st.st_mtime < since - 5: return 0, ""
        with open(path, "rb") as f:
            f.seek(max(0, st.st_size - 16_000_000)); data = f.read()
    except OSError:
        return 0, ""
    found = CHECK_LINE.findall(data)
    return len(found), (found[-1].decode("utf-8", errors="replace") if found else "")


def note_checks(store, relay, run_key, done):
    """Remember how many checks each test run reached; the expected size of a run is the largest of the last three (a failed run stops early,
    so it can only be smaller than a complete one). Returns that expected total, or None before any run was seen."""
    key = str(relay)
    with _lock:
        try: data = json.loads(pathlib.Path(store).read_text(encoding="utf-8"))
        except (OSError, ValueError): data = {}
        if not isinstance(data, dict): data = {}
        entry = data.get(key) if isinstance(data.get(key), dict) else {}
        runs = [int(x) for x in entry.get("runs", []) if isinstance(x, int)][-5:]
        cur = entry.get("current") if isinstance(entry.get("current"), dict) else {}
        before = json.dumps(entry, sort_keys=True)
        if cur.get("key") != run_key:
            if cur.get("max", 0) > 0: runs.append(int(cur["max"]))
            cur = {"key": run_key, "max": int(done)}
        elif done > cur.get("max", 0):
            cur["max"] = int(done)
        entry = {"runs": runs[-5:], "current": cur}
        if json.dumps(entry, sort_keys=True) != before:
            data[key] = entry
            try:
                pathlib.Path(store).parent.mkdir(parents=True, exist_ok=True)
                tmp = pathlib.Path(str(store) + ".tmp")
                tmp.write_text(json.dumps(data), encoding="utf-8"); os.replace(tmp, store)
            except OSError:
                pass
        return max(runs[-3:]) if runs else None


QUIET_AFTER = 300


def quiet_seconds(relay, since, now):
    """How long the test run has printed nothing. A hung test (a Godot script that failed to compile never quits) stops all output, so this is
    what tells a stuck run from a slow one; the number of finished checks is not a reliable measure of time left (on the game's suite the last
    third of the checks takes half of the time)."""
    try: last = (pathlib.Path(relay) / "relay-tests.log").stat().st_mtime
    except OSError: last = 0
    return max(0.0, now - max(last, since))
