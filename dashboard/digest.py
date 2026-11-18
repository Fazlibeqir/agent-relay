"""The review page (/digest): one readable card per task that waits for the person, built from the task files, PLAYTEST_QUEUE.md and git.
No AI and no generated file: it is rebuilt every time the page is opened, so it is never stale."""
import html, json, pathlib, re, subprocess

import appapi

NOTE_SPLIT = re.compile(r"(only human gates? remain[^:]*:|human gates? (?:remain|still open|open)[^:]*:|owner should)", re.I)


def _queue_notes(project):
    notes, order = {}, []
    q = pathlib.Path(project) / "PLAYTEST_QUEUE.md"
    if not q.exists(): return notes, order
    for line in q.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^(T\d+[a-z]?)\s+[—-]\s+(.*?)\s+[—-]\s+(.*)$", line)
        if m:
            notes[m.group(1)] = (m.group(2), m.group(3)); order.append(m.group(1))
    return notes, order


def _open_gates(project, tid):
    f = pathlib.Path(project) / "tasks" / (tid + ".md")
    if not f.exists(): return []
    out = []
    for l in f.read_text(encoding="utf-8", errors="replace").splitlines():
        m = appapi.GATE_LINE.match(l)
        if m and m.group(1) == " ": out.append(m.group(2).strip())
    return out


def _commits(project, tid):
    try:
        r = subprocess.run(["git", "-C", str(project), "log", "--format=%h|%ad|%s", "--date=short", "-3", "-E", "--grep", "^" + tid + "[: ]"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", creationflags=appapi.NO_WINDOW)
        return [l.split("|", 2) for l in r.stdout.splitlines() if l.count("|") >= 2]
    except OSError:
        return []


def items(project):
    tasks = {t["id"]: t for t in appapi.list_tasks(project)}
    notes, order = _queue_notes(project)
    ids = list(order) + [i for i, t in tasks.items() if t["state"] == "owner" and i not in order]
    mode = appapi.relay_script(project)
    reqs = appapi.owner_requests(project) if mode == "relay.sh" else []
    out = []
    for tid in dict.fromkeys(ids):
        t = tasks.get(tid)
        if not t: continue
        title, note = notes.get(tid, (re.sub(r"^T\d+[a-z]?\s*[-—]\s*", "", t["title"]), ""))
        m = NOTE_SPLIT.search(note)
        summary, to_try = (note[:m.start()].strip(" —-"), note[m.end() if "owner should" not in m.group(0).lower() else m.start():].strip()) if m else ("", note)
        gates = _open_gates(project, tid)
        out.append({"id": tid, "title": title, "completion": t["completion"], "gates": t["gates"], "gatesDone": t["gatesDone"], "state": t["state"], "summary": summary, "toTry": to_try,
                    "openGates": gates, "commits": _commits(project, tid),
                    "approved": any(r["task"] == tid and r["kind"] == "approved" for r in reqs), "fix": any(r["task"] == tid and r["kind"] == "fix" for r in reqs) or t.get("fixRequested", False)})
    return out


def _points(text, esc):
    """'(1) do this ... (2) and that ...' becomes numbered rows; text without numbers stays a paragraph."""
    parts = re.split(r"\s*\((\d{1,2})\)\s+", text)
    if len(parts) < 5: return "<p>%s</p>" % esc(text)
    rows = "".join('<div class="pt"><b>%s</b><span>%s</span></div>' % (esc(parts[i]), esc(parts[i + 1].strip())) for i in range(1, len(parts) - 1, 2))
    return ("<p>%s</p>" % esc(parts[0].strip()) if parts[0].strip() else "") + rows


def _idle(project):
    return [p.read_text(encoding="utf-8", errors="replace").strip() for p in sorted(pathlib.Path(project).glob("relay-idle-*.result"))]


def page(project):
    esc = lambda s: html.escape(str(s or ""))
    its = items(project); mode = appapi.relay_script(project)
    name = pathlib.Path(project).name
    cards = []
    for k in its:
        if k["approved"]: badge = '<span class="b ok">approved by you</span>'
        elif k["fix"]: badge = '<span class="b warn">fix requested</span>'
        else: badge = '<span class="b warn">waiting for you</span>'
        gates = "".join("<li>%s</li>" % esc(g) for g in k["openGates"]) or "<li class=m>none listed</li>"
        commits = "".join('<div class="s m"><code>%s</code> %s %s</div>' % (esc(c[0]), esc(c[1]), esc(c[2])) for c in k["commits"]) or '<div class="s m">no commits found for this task</div>'
        summ = ('<details><summary>What the AI did and proved</summary><p>%s</p></details>' % esc(k["summary"])) if k["summary"] else ""
        try_ = ('<div class="try"><div class="h">What you need to try and judge</div>%s</div>' % _points(k["toTry"], esc)) if k["toTry"] else \
               '<div class="try"><div class="h">What you need to try and judge</div><p class="m">The AI left no note. Open the task in the Tasks tab and look at its open checks below.</p></div>'
        cards.append(
            '<div class="card" id="c-%(id)s"><div class="top"><div><span class="id">%(id)s</span> <span class="t">%(title)s</span></div>%(badge)s</div>'
            '<div class="bar"><i style="width:%(pct)d%%"></i></div><div class="s m">%(pct)d%% done · %(gd)d of %(g)d checks ticked</div>%(try_)s%(summ)s'
            '<div class="h" style="margin-top:10px">Your open checks</div><ul>%(gates)s</ul><details><summary>Latest work on it</summary>%(commits)s</details>'
            '<div class="acts"><button class="go" data-a="approve" data-t="%(id)s">Looks good</button><button data-a="changes" data-t="%(id)s">Needs changes…</button></div></div>'
            % {"id": esc(k["id"]), "title": esc(k["title"]), "badge": badge, "pct": k["completion"], "gd": k["gatesDone"], "g": k["gates"], "try_": try_, "summ": summ, "gates": gates, "commits": commits})
    idle = "".join("<li>%s</li>" % esc(x) for x in _idle(project))
    how = ("Your buttons are written to <code>OWNER-REQUESTS.md</code> for the relay's dispatcher to read. <b>Looks good</b> records your approval; the <i>Human signed off</i> box in the board is still yours to tick. "
           "<b>Needs changes</b> is your feedback: the task stays open until you approve it.") if mode == "relay.sh" else \
          ("<b>Looks good</b> ticks your own check and finishes the task. <b>Needs changes</b> adds your note as a new check; the relay builds the fix next.")
    return """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Review: %(name)s</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1c2128;--mute:#667085;--line:#e3e6ea;--ok:#1a7f4b;--warn:#b26a00;--bar:#e6e9ee;--acc:#2f6fed}
@media (prefers-color-scheme:dark){:root{--bg:#0e1116;--card:#161b22;--ink:#e6edf3;--mute:#8b949e;--line:#2a313a;--ok:#3fb950;--warn:#d29922;--bar:#262d36;--acc:#5b8cff;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,Segoe UI,sans-serif}
main{max-width:860px;margin:0 auto;padding:18px 16px 60px}a{color:var(--acc)}h1{font-size:22px;margin:4px 0 2px}
.m{color:var(--mute)}.s{font-size:12.5px}.intro{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin:12px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:12px 0}.top{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:baseline}
.id{font-weight:700}.t{font-weight:600}.b{font-size:12px;padding:2px 10px;border-radius:99px;border:1px solid}.b.warn{color:var(--warn);border-color:var(--warn)}.b.ok{color:var(--ok);border-color:var(--ok)}
.bar{height:7px;background:var(--bar);border-radius:4px;overflow:hidden;margin:8px 0 2px}.bar i{display:block;height:100%%;background:var(--ok)}
.try{border-left:4px solid var(--acc);background:var(--bar);border-radius:6px;padding:8px 12px;margin:10px 0}.try p{margin:2px 0}.pt{display:flex;gap:10px;margin:6px 0}.pt b{min-width:22px;height:22px;border-radius:50%%;background:var(--acc);color:#fff;font-size:12px;display:inline-flex;align-items:center;justify-content:center;flex:none;margin-top:2px}.h{font-size:12px;letter-spacing:.05em;text-transform:uppercase;color:var(--mute);font-weight:600}
ul{margin:4px 0 8px 18px;padding:0}li{margin:2px 0}details{margin:6px 0}summary{cursor:pointer;color:var(--mute)}code{font:12px ui-monospace,Consolas,monospace;background:var(--bar);padding:1px 5px;border-radius:4px}
.acts{display:flex;gap:8px;margin-top:12px;flex-wrap:wrap}button{font:inherit;padding:7px 16px;border-radius:7px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}.go{background:var(--ok);color:#fff;border-color:var(--ok)}
</style><main>
<div class="s"><a href="/">&larr; Back to Agent Relay</a></div>
<h1>Review: %(name)s</h1>
<div class="m">%(n)d task%(s)s waiting for your check</div>
<div class="intro"><b>What is this page?</b> These are tasks where the AI has finished every automatic check it can do, and the rest needs <b>you</b>: usually playing, opening or reading the result and saying whether it is right.
Each card shows what to try and judge, your open checks, and the latest work. %(how)s</div>
%(cards)s%(empty)s
%(idle)s
</main><script>
async function post(p,b){const r=await fetch(p,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b)});return r.json()}
document.querySelectorAll("button[data-a]").forEach(b=>b.onclick=async()=>{const t=b.dataset.t,a=b.dataset.a;let r;
  if(a==="approve"){if(!confirm("Mark "+t+" as OK? You are the one saying it is fine."))return;r=await post("/api/task/approve",{task:t})}
  else{const note=prompt("What should be fixed or changed in "+t+"? Be specific.");if(!note)return;r=await post("/api/task/changes",{task:t,note})}
  if(!r.ok){alert(r.error||"Could not do that");return}location.reload()});
</script>""" % {"name": esc(name), "n": len(its), "s": "" if len(its) == 1 else "s", "how": how, "cards": "".join(cards),
                "empty": '<div class="intro">Nothing is waiting for you right now.</div>' if not its else "",
                "idle": ('<div class="intro"><div class="h">Checks the relay ran while it waited</div><ul>%s</ul></div>' % idle) if idle else ""}
