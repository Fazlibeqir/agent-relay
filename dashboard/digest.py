import hashlib, html, pathlib, re, subprocess
from urllib.parse import urlencode

import appapi
import reviewmedia

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
    runtime = appapi.runtime_for(project)
    state = runtime.state()
    artifacts = state.get("artifacts", {})
    reviews = state.get("reviews", {})
    ids.extend(reviews)
    for tid in dict.fromkeys(ids):
        t = tasks.get(tid)
        if not t: continue
        title, note = notes.get(tid, (re.sub(r"^T\d+[a-z]?\s*[-—]\s*", "", t["title"]), ""))
        m = NOTE_SPLIT.search(note)
        summary, to_try = (note[:m.start()].strip(" —-"), note[m.end() if "owner should" not in m.group(0).lower() else m.start():].strip()) if m else ("", note)
        gates = _open_gates(project, tid)
        history = reviews.get(tid, [])
        active = history[-1] if history and history[-1]["status"] == "pending" and t["state"] == "owner" else None
        if active:
            try:
                runtime.require_review(state, tid, active["id"])
            except ValueError:
                active = None
        files = artifacts.get(tid, [])
        shown = [a for a in files if a.get("reviewId") == active["id"]] if active else [
            a for a in files if not a.get("reviewEvidence")]
        if history:
            shown = shown if active else []
            to_try = active["toTry"] if active else ""
            summary = ""
        out.append({"id": tid, "title": title, "completion": t["completion"], "gates": t["gates"], "gatesDone": t["gatesDone"], "state": t["state"], "summary": summary, "toTry": to_try,
                    "openGates": gates, "commits": _commits(project, tid), "artifacts": shown,
                    "review": active, "history": [dict(r, artifacts=[a for a in files if a.get("reviewId") == r["id"]])
                                                  for r in history if r != active],
                    "approved": any(r["task"] == tid and r["kind"] == "approved" for r in reqs), "fix": any(r["task"] == tid and r["kind"] == "fix" for r in reqs) or t.get("fixRequested", False)})
    return out


def artifact_links(task, artifacts, esc):
    return "".join(
        '<p><a href="/api/artifact?%s" target="_blank" rel="noopener">%s</a> <span class="s m">%s</span></p>'
        % (esc(urlencode({"task": task, "path": item["path"]})), esc(pathlib.Path(item["path"]).name), esc(item.get("note", "")))
        for item in artifacts
    )


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
        review = k["review"]
        attributes = (' data-revision="%s" data-build="%s"' % (esc(review["id"]), esc(review["build"]))) if review else ""
        actions_allowed = bool(review) or (mode == "relay.sh" and not k["history"])
        if review: badge = '<span class="b warn">Needs human sign-off</span>'
        elif k["approved"]: badge = '<span class="b ok">approved by you</span>'
        elif k["fix"]: badge = '<span class="b warn">fix requested</span>'
        elif mode == "relay.sh": badge = '<span class="b warn">waiting for you</span>'
        else: badge = ""
        gates = "".join("<li>%s</li>" % esc(g) for g in k["openGates"]) or "<li class=m>none listed</li>"
        commits = "".join('<div class="s m"><code>%s</code> %s %s</div>' % (esc(c[0]), esc(c[1]), esc(c[2])) for c in k["commits"]) or '<div class="s m">no commits found for this task</div>'
        summ = ('<details><summary>What the AI did and proved</summary><p>%s</p></details>' % esc(k["summary"])) if k["summary"] else ""
        attachments = artifact_links(k["id"], k["artifacts"], esc)
        if actions_allowed:
            attachments += '<label class="s" for="artifact-%s">Repository-relative screenshot, video or build path</label><div class="acts"><input id="artifact-%s" data-artifact-path="%s" placeholder="captures/example.png"><button data-attach="%s">Attach file</button></div>' % (esc(k["id"]), esc(k["id"]), esc(k["id"]), esc(k["id"]))
        clips = []
        for item in k["artifacts"]:
            url = "/api/artifact?" + urlencode({"task": k["id"], "path": item["path"]})
            suffix = pathlib.Path(item["path"]).suffix.lower()
            if review and suffix in (".png", ".jpg", ".jpeg", ".webp"):
                clips.append('<figure><a href="%s"><img loading="lazy" src="%s" alt="%s"></a></figure>' %
                             (esc(url), esc(url), esc(item.get("note") or "Screenshot of the review build")))
            if suffix not in (".webm", ".mp4"): continue
            media = item.get("media", {})
            caption = (item.get("note") or "Workflow clip") + " · attached to commit " + item["head"][:8]
            if media.get("projectCommitAtStart"): caption += " · project commit at recording start: " + media["projectCommitAtStart"][:8]
            if media.get("taskChangedDuringCapture"): caption += " · task changed during capture: compare with the current checks"
            clips.append('<figure><video controls preload="metadata" src="%s"></video><figcaption>%s</figcaption><a href="%s" target="_blank" rel="noopener">Open or download clip</a></figure>' % (esc(url), esc(caption), esc(url)))
        recording = '''<section class="capture" data-capture-task="%(task)s"><h3>Show the workflow</h3>
<p class="s m">Choose an app or window, show the steps, then stop and attach the clip. Screen recordings have no audio and stop after 3 minutes or near 45 MB. Clips stay in this project.</p>
<label class="s" for="clip-note-%(task)s">What should the reviewer look for?</label><input id="clip-note-%(task)s" data-clip-note maxlength="300" placeholder="Steps, expected result, or the issue to inspect">
<div class="acts"><button data-record>Record workflow</button><button data-stop disabled>Stop &amp; attach</button><button data-retry hidden>Retry save</button><a data-download-clip hidden>Download unsaved clip</a><span class="s m" data-capture-timer aria-live="off"></span></div>
<label class="s upload" for="clip-upload-%(task)s">Or upload an existing WebM / MP4 clip (up to 45 MB)</label><input type="file" id="clip-upload-%(task)s" data-clip-upload accept="video/webm,video/mp4,.webm,.mp4">
<p class="s m" data-capture-status role="status" aria-live="polite">Recording starts after you choose what to share. Saving a clip does not approve the task.</p>
</section><div data-clips="%(task)s">%(clips)s</div>''' % {"task": esc(k["id"]), "clips": "".join(clips)}
        if not actions_allowed:
            recording = '<div>%s</div>' % "".join(clips)
        history = "".join('<details><summary>Review %d · %s · build %s</summary><p>%s</p>%s%s</details>' %
                          (old["number"], esc(old["status"]), esc(old["build"][:12]), esc(old["toTry"]),
                           '<p><b>Feedback</b> %s</p>' % esc(old["feedback"]) if old.get("feedback") else "",
                           artifact_links(k["id"], old["artifacts"], esc))
                          for old in reversed(k["history"]))
        title = esc(k["title"]) + (" · Review %d" % review["number"] if review else "")
        build = '<p class="s m">Build <code title="%s">%s</code></p>' % (esc(review["build"]), esc(review["build"][:12])) if review else ""
        play = '<p data-playthrough><b>Playthrough needed</b></p>' if review and not clips else ""
        if not review and mode == "relay-files.sh" and k["state"] == "owner":
            play = '<p class="s m">This task needs a fresh review tied to a tested build.</p>'
        actions = ('<div class="acts"><button class="go" data-a="approve" data-t="%s">Looks good</button>'
                   '<button data-a="changes" data-t="%s">Needs changes…</button></div>' % (esc(k["id"]), esc(k["id"]))) if actions_allowed else ""
        if play and not review and mode == "relay-files.sh":
            actions = '<button data-refresh-review="%s">Request a fresh review</button>' % esc(k["id"])
        try_ = ('<div class="try"><div class="h">What to try</div>%s</div>' % _points(k["toTry"], esc)) if k["toTry"] else ""
        cards.append(
            '<div class="card" id="c-%(id)s"%(attrs)s><div class="top"><div><span class="id">%(id)s</span> <span class="t">%(title)s</span></div>%(badge)s</div>'
            '%(build)s%(play)s<div class="bar"><i style="width:%(pct)d%%"></i></div><div class="s m">%(pct)d%% done · %(gd)d of %(g)d checks ticked</div>%(try_)s%(summ)s'
            '<div class="h" style="margin-top:10px">What to check</div><ul>%(gates)s</ul>%(recording)s<details><summary>Latest work on it</summary>%(commits)s</details><details><summary>Files to review</summary>%(attachments)s</details>'
            '%(actions)s%(history)s</div>'
            % {"id": esc(k["id"]), "title": title, "badge": badge, "attrs": attributes, "build": build, "play": play,
               "pct": k["completion"], "gd": k["gatesDone"], "g": k["gates"], "try_": try_, "summ": summ, "gates": gates,
               "commits": commits, "attachments": attachments, "recording": recording, "actions": actions, "history": history})
    waiting = sum(bool(k["review"]) or (mode == "relay.sh" and not k["history"] and not k["approved"]) for k in its)
    idle = "".join("<li>%s</li>" % esc(x) for x in _idle(project))
    how = ("Your buttons are written to <code>OWNER-REQUESTS.md</code> for the relay's dispatcher to read. <b>Looks good</b> records your approval; the <i>Human signed off</i> box in the board is still yours to tick. "
           "<b>Needs changes</b> is your feedback: the task stays open until you approve it.") if mode == "relay.sh" else \
          ("<b>Looks good</b> ticks your own check and finishes the task. <b>Needs changes</b> adds your note as a new check; the relay builds the fix next.")
    return """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Review: %(name)s</title>
<link rel="stylesheet" href="/dialogs.css">
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
button:disabled{opacity:.55;cursor:default}button:focus-visible,input:focus-visible,summary:focus-visible,a:focus-visible{outline:2px solid var(--acc);outline-offset:3px}
.capture{border-top:1px solid var(--line);margin-top:14px;padding-top:10px}.capture h3{font-size:16px;margin:0}.capture p{margin:6px 0}.capture input{width:100%%;min-width:0;font:inherit;padding:7px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink)}.upload{display:block;margin-top:10px}figure{margin:14px 0}video{display:block;width:100%%;max-height:480px;background:#000}figure img{display:block;max-width:100%%;max-height:480px}figcaption{font-size:12px;color:var(--mute);overflow-wrap:anywhere}a[hidden],button[hidden]{display:none}
</style><main data-review-project="%(projectid)s" data-max-clip-bytes="%(maxbytes)d" data-max-clip-seconds="%(maxseconds)d">
<div class="s"><a href="/">&larr; Back to Agent Relay</a></div>
<h1>Review: %(name)s</h1>
<div class="m">%(n)d task%(s)s waiting for your check</div>
<p class="s m">%(tasksource)s</p>
<div class="intro">Try the change or watch the attached clip. %(how)s</div>
%(cards)s%(empty)s
%(idle)s
</main><script src="/dialogs.js"></script><script>
async function post(p,b){const r=await fetch(p,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b)});return r.json()}
document.querySelectorAll("[data-attach]").forEach(button=>button.onclick=async()=>{
  const task=button.dataset.attach,path=document.querySelector('[data-artifact-path="'+task+'"]').value.trim();
  if(!path)return;
  const {revision,build}=button.closest('.card').dataset;
  if(build&&!await RelayDialog.ask({heading:"Confirm the attachment’s build",detail:"Attach this file as evidence for build "+build.slice(0,12)+".",action:"Attach file"}))return;
  const result=await post('/api/artifacts/add',{task,path,revision,build});
  if(!result.ok){RelayDialog.show(result.error||'Could not attach file',true);return}location.reload();
});
document.querySelectorAll("button[data-a]").forEach(b=>b.onclick=async()=>{const t=b.dataset.t,a=b.dataset.a,{revision,build}=b.closest('.card').dataset;let r;
  if(a==="approve"){
    if(!await RelayDialog.ask({heading:"Approve "+t+"?",detail:build?"Confirm you reviewed build "+build.slice(0,12)+".":"This records your sign-off.",action:"Approve review"}))return;
    r=await post("/api/task/approve",{task:t,revision})
  }else{
    const note=await RelayDialog.ask({heading:"Request changes for "+t,fieldLabel:"What should change in this review?",action:"Send feedback"});
    if(!note)return;
    r=await post("/api/task/changes",{task:t,note,revision})
  }
  if(!r.ok){RelayDialog.show(r.error||"Could not save the review decision",true);return}location.reload()});
document.querySelectorAll("[data-refresh-review]").forEach(b=>b.onclick=async()=>{
  const r=await post("/api/task/feedback",{task:b.dataset.refreshReview,note:"Run the checks and queue a review tied to the tested build."});
  if(!r.ok){RelayDialog.show(r.error||"Could not request a review",true);return}location.reload()
});
</script><script src="/review.js" defer></script>""" % {"name": esc(name), "n": waiting, "s": "" if waiting == 1 else "s", "how": how, "cards": "".join(cards), "projectid": hashlib.sha256(str(pathlib.Path(project).resolve()).encode()).hexdigest(), "maxbytes": reviewmedia.MAX_CLIP_BYTES, "maxseconds": reviewmedia.MAX_CAPTURE_SECONDS, "tasksource": "Custom project workflow with local task snapshots. External board access depends on its configured integration." if mode == "relay.sh" else "Tasks and reviews are stored in this project. No Notion account is needed.",
                "empty": '<div class="intro">Nothing is waiting for you right now.</div>' if not its else "",
                "idle": ('<div class="intro"><div class="h">Checks the relay ran while it waited</div><ul>%s</ul></div>' % idle) if idle else ""}
