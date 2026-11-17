"""Other AIs the person adds by name (Antigravity, Gemini CLI, Aider, ...): found on the computer, their models listed by asking the tool itself.

Saved in the project as relay.providers.json, which the relay reads (relay-settings.py cmd ...). The relay cannot put a git deny list around a tool it
does not know, so a custom AI needs "experimental": true when used as a builder; the settings page sets that for you."""
import json, os, pathlib, re, shutil, subprocess
import osops

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
MODEL_COMMANDS = (["models"], ["model", "list"], ["models", "list"], ["list-models"], ["--list-models"], ["list", "models"], ["model", "ls"])
EFFORT_FLAGS = ("--reasoning-effort", "--effort", "--reasoning", "--thinking-budget", "--thinking")
MODEL_LINE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{1,80}$")
ID_OK = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")


def file_path(project):
    return pathlib.Path(project) / "relay.providers.json"


def load(project):
    try: return json.loads(file_path(project).read_text(encoding="utf-8-sig")).get("providers", [])
    except (OSError, ValueError, AttributeError): return []


def save(project, providers):
    file_path(project).write_text(json.dumps({"providers": providers}, indent=1), encoding="utf-8")


def find_bin(name):
    """A command name on PATH or in the usual global-install folders, or a full path."""
    name = (name or "").strip().strip('"')
    if not name: return None
    p = pathlib.Path(name)
    if p.is_file(): return str(p)
    return osops.which(name)


def _run(found, args, timeout=25):
    return osops.run(found, args, timeout)


def _clean(text):
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)


def probe(bin_name):
    """Look the tool up and ask it: its help, its models, whether it takes a reasoning level. Everything is a guess the person can see and edit."""
    found = find_bin(bin_name)
    if not found: return {"ok": False, "error": f"'{bin_name}' was not found on this computer. Install it first, or type the full path to it."}
    out = {"ok": True, "found": found, "models": [], "modelsCmd": None, "efforts": [], "effortArgs": [], "promptMode": "stdin", "promptArgs": [], "help": ""}
    try: _, help_text = _run(found, ["--help"]); help_text = _clean(help_text)
    except Exception: help_text = ""
    out["help"] = help_text[:2500]
    for cmd in MODEL_COMMANDS:
        try: rc, txt = _run(found, cmd, 30)
        except Exception: continue
        lines = [l.strip() for l in _clean(txt).splitlines()]
        ids = [l for l in lines if MODEL_LINE.match(l)]
        if rc == 0 and 1 <= len(ids) <= 400 and len(ids) >= len([l for l in lines if l]) * 0.5:
            out["models"], out["modelsCmd"] = ids, cmd; break
    low = help_text.lower()
    for fl in EFFORT_FLAGS:
        if fl in low:
            out["effortArgs"] = [fl, "{effort}"]
            m = re.search(re.escape(fl) + r"[^\n]*?(?:possible values|choices|one of)[:\s]*\[?([a-z, |/]+)\]?", low)
            vals = [v.strip() for v in re.split(r"[,|/ ]+", m.group(1)) if v.strip()] if m else []
            out["efforts"] = vals if 2 <= len(vals) <= 8 else ["low", "medium", "high"]
            break
    if "stdin" not in low and re.search(r"-p,\s*--prompt", low):      # only the clear '-p, --prompt <text>' shape; otherwise the prompt goes in on stdin
        flag = "-p"
        out["promptMode"], out["promptArgs"] = "file", [flag, "Read the file {promptfile} in the current folder and carry out its instructions exactly."]
    return out


def add(project, name, bin_name, probe_result=None, extra=None):
    """Save a custom AI. The id is made from the name; the command and models come from the probe (and any edits the person made)."""
    pid = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:30]
    if not ID_OK.match(pid or ""): return {"ok": False, "error": "Give it a name made of letters and numbers."}
    if pid in ("claude", "codex", "copilot", "opencode", "none"): return {"ok": False, "error": "That name is already built in."}
    found = find_bin(bin_name)
    if not found: return {"ok": False, "error": f"'{bin_name}' was not found on this computer."}
    pr = probe_result or probe(bin_name)
    ex = extra or {}
    spec = {"id": pid, "name": name.strip(), "bin": found, "args": ex.get("args", []), "modelArgs": ex.get("modelArgs") or (["--model", "{model}"] if pr.get("models") else []),
            "effortArgs": pr.get("effortArgs", []), "efforts": pr.get("efforts", []), "promptMode": pr.get("promptMode", "stdin"), "promptArgs": pr.get("promptArgs", []),
            "modelsCmd": pr.get("modelsCmd"), "models": pr.get("models", []), "loginArgs": ex.get("loginArgs", ["login"])}
    items = [x for x in load(project) if x.get("id") != pid] + [spec]
    save(project, items)
    return {"ok": True, "id": pid, "provider": spec}


def remove(project, pid):
    save(project, [x for x in load(project) if x.get("id") != pid])
    return {"ok": True}


def refresh_models(project, pid):
    """Ask the tool again for its models (the list changes when the tool updates)."""
    items = load(project)
    for x in items:
        if x.get("id") == pid and x.get("modelsCmd"):
            try: rc, txt = _run(x["bin"], x["modelsCmd"], 30)
            except Exception as ex: return {"ok": False, "error": str(ex)}
            ids = [l.strip() for l in _clean(txt).splitlines() if MODEL_LINE.match(l.strip())]
            if ids: x["models"] = ids; save(project, items); return {"ok": True, "count": len(ids)}
    return {"ok": False, "error": "could not list models"}
