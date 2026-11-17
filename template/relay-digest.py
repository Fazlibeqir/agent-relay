"""Owner review digest: one short page per task that waits for the owner, built only from files and git (no AI, no tokens).

  python relay-digest.py [repo-dir]   writes OWNER-REVIEW-DIGEST.md (ignored by git)
Reads PLAYTEST_QUEUE.md (task ids), tasks/<id>.md (status, open gates) and git log (last commits per task), plus
relay-idle-*.result (idle chore results) when present.
"""
import pathlib, re, subprocess, sys, datetime

repo = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
queue = (repo / "PLAYTEST_QUEUE.md").read_text(encoding="utf-8", errors="replace").splitlines()
ids, why = [], {}
for line in queue:
    m = re.match(r"^(T\d+[a-z]?)\s+[\u2014-]\s+(.*)$", line)
    if m:
        ids.append(m.group(1)); why[m.group(1)] = m.group(2)

def git(*a):
    r = subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.stdout.strip()

out = ["# Owner review digest", "",
       "Generated %s by relay-digest.py from PLAYTEST_QUEUE.md, tasks/*.md and git. No AI was used. Nothing here ticks a gate." % datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), ""]
results = sorted(repo.glob("relay-idle-*.result"))
if results:
    out += ["## Idle chores (run while the relay waited for a usage limit)", ""]
    out += ["- " + r.read_text(encoding="utf-8", errors="replace").strip() for r in results] + [""]
out += ["## Waiting for you (%d tasks)" % len(ids), ""]
for tid in ids:
    tf = repo / "tasks" / (tid + ".md")
    title, status, gates = tid, "", []
    if tf.exists():
        lines = tf.read_text(encoding="utf-8", errors="replace").splitlines()
        if lines: title = lines[0].lstrip("# ").strip()
        for l in lines[:6]:
            if l.startswith("Status:"): status = l
        gates = [l[6:].strip() for l in lines if l.startswith("- [ ]")]
    commits = git("log", "--oneline", "-3", "--grep", "^" + tid + "[: ]", "-E").splitlines()
    out.append("### " + title)
    if status: out.append(status)
    out.append("")
    out.append("- Open gates: " + ("; ".join(g[:90] for g in gates) if gates else "none listed"))
    out.append("- Last commits: " + (" | ".join(c[:80] for c in commits) if commits else "none found"))
    out.append("- Reviewer note: " + re.sub(r"\s+", " ", why[tid])[:420] + ("..." if len(why[tid]) > 420 else ""))
    out.append("")
(repo / "OWNER-REVIEW-DIGEST.md").write_text("\n".join(out) + "\n", encoding="utf-8")
print("wrote OWNER-REVIEW-DIGEST.md: %d tasks" % len(ids))
