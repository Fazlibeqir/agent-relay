"""Relay settings: which model plays which role. Read by relay.sh before every agent call, so a change needs no restart.

  python relay-settings.py shell [file]      print shell assignments (S_*) for relay.sh; problems go to stderr as 'settings: ...'
  python relay-settings.py check [file]      print problems as JSON {"errors": [...], "warnings": [...]}; exit 2 on errors
  python relay-settings.py show  [file]      print the effective settings as JSON

File: relay.settings.json next to relay.sh (ignored by git). Missing, unreadable or invalid -> the built-in defaults, which are exactly
what the relay did before this file existed. Environment variables set in relay.sh (CLAUDE_MODEL, CODEX_MODEL, FALLBACKS, ...) still win.

Roles: dispatcher, builder, reviewer, cleaner. Dispatcher and reviewer can fall back to another installed tool.
Reviews must use a different model from every builder that worked on the current round.
"""
import json, os, pathlib, re, sys

PROVIDERS = ("claude", "codex", "copilot", "opencode")
SAFE = re.compile(r"^[A-Za-z0-9._/:@+-]{1,80}$")
DEFAULTS = {
    "roles": {
        "dispatcher": {"provider": "claude", "model": "claude-sonnet-5-5", "effort": "high"},
        "builder":    {"provider": "codex", "model": "auto", "effort": "auto"},   # auto = follow the task's RECOMMENDED_AI line
        "reviewer":   {"provider": "claude", "model": "claude-sonnet-5-5", "effort": "high"},
        "cleaner":    {"provider": "none"},
    },
    "fallbackBuilders": ["copilot", "opencode"],
    "fallbackRoles": {"dispatcher": ["codex", "copilot", "opencode"], "reviewer": ["copilot", "opencode", "codex"]},
    # serial = one task at a time. parallel = the dispatcher hands different tasks to different builders at the same time, each in its own worktree;
    # merging, testing and review still go one at a time. Only relay-files.sh projects support it.
    "mode": "serial",
    "parallel": {"workers": ["codex", "copilot", "opencode"]},
    "fallbackMaxPerDay": 10,
    "models": {"copilot": "auto", "opencode": "opencode/deepseek-v4.1-flash-free", "advisor": "claude-opus-5-5"},
    "idleChores": ["backend", "digest"],
    "capabilities": {p: ["repository", "shell"] for p in PROVIDERS},
    "limits": {"callsPerTask": 24, "callsPerProviderDay": 100, "failuresPerTask": 3,
               "sameFailureLimit": 2, "timeoutSeconds": 5400, "idleWarningSeconds": 600},
    # who answers an AI's request for permission: the person ("ask") or an AI judge ("auto"). Secrets and uploads stay with the person unless autoSecrets is on.
    "approvals": {"mode": "ask", "autoSecrets": False, "judge": {"provider": "reviewer", "model": "auto", "effort": "auto"}},
}


def custom_providers(path):
    """Other AIs the person added (relay.providers.json next to the settings file): {id: spec}."""
    f = pathlib.Path(path).with_name("relay.providers.json")
    try: items = json.loads(f.read_text(encoding="utf-8-sig")).get("providers", [])
    except (OSError, ValueError, AttributeError): return {}
    return {x["id"]: x for x in items if isinstance(x, dict) and re.match(r"^[a-z0-9][a-z0-9-]{0,30}$", str(x.get("id", "")))}


def all_providers(path):
    return list(PROVIDERS) + [i for i in custom_providers(path) if i not in PROVIDERS]


def build_cmd(spec, model, effort):
    """The command line for a custom AI: bin + base args + model flag + effort flag (+ the prompt-file instruction). First element: STDIN or FILE."""
    args = [str(spec["bin"])] + [str(a) for a in spec.get("args", [])]
    if model and model != "auto": args += [str(a).replace("{model}", model) for a in spec.get("modelArgs", [])]
    if effort and effort != "auto": args += [str(a).replace("{effort}", effort) for a in spec.get("effortArgs", [])]
    mode = "FILE" if spec.get("promptMode") == "file" else "STDIN"
    if mode == "FILE": args += [str(a).replace("{promptfile}", "relay-fallback-prompt.txt") for a in spec.get("promptArgs", [])]
    return [mode] + args


def load(path):
    problems = {"errors": [], "warnings": []}
    cfg = json.loads(json.dumps(DEFAULTS))
    cfg["_custom"] = list(custom_providers(path))
    p = pathlib.Path(path)
    if not p.exists():
        return cfg, problems
    try:
        raw = json.loads(p.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict): raise ValueError("top level must be an object")
    except (OSError, ValueError) as ex:
        problems["errors"].append(f"{p.name} unreadable ({ex}); using the built-in defaults")
        return cfg, problems
    for role, val in (raw.get("roles") or {}).items():
        if role not in cfg["roles"]:
            problems["warnings"].append(f"unknown role '{role}' ignored"); continue
        if isinstance(val, str): val = {"provider": val}
        if not isinstance(val, dict):
            problems["errors"].append(f"role {role}: must be an object"); continue
        cfg["roles"][role] = {**cfg["roles"][role], **val} if val.get("provider", cfg["roles"][role].get("provider")) == cfg["roles"][role].get("provider") else dict(val)
    for k in ("fallbackBuilders", "fallbackMaxPerDay", "idleChores"):
        if k in raw: cfg[k] = raw[k]
    fallback_roles = raw.get("fallbackRoles")
    if isinstance(fallback_roles, dict):
        cfg["fallbackRoles"].update(fallback_roles)
    elif fallback_roles is not None:
        problems["errors"].append("fallbackRoles must be an object")
    if "mode" in raw: cfg["mode"] = raw["mode"]
    par = raw.get("parallel")
    if isinstance(par, dict): cfg["parallel"] = {**cfg["parallel"], **par}
    elif par is not None: problems["errors"].append("parallel must be an object")
    cfg["models"].update(raw.get("models") or {})
    for key in ("capabilities", "limits"):
        if key in raw:
            if isinstance(raw[key], dict): cfg[key].update(raw[key])
            else: problems["errors"].append(key + " must be an object")
    ap = raw.get("approvals")
    if isinstance(ap, dict):
        j = ap.get("judge") if isinstance(ap.get("judge"), dict) else {}
        cfg["approvals"] = {"mode": ap.get("mode", "ask"), "autoSecrets": bool(ap.get("autoSecrets", False)), "judge": {**cfg["approvals"]["judge"], **j}}
    return cfg, problems


def validate(cfg, problems):
    errs = problems["errors"]
    roles = cfg["roles"]
    for role, r in roles.items():
        prov = r.get("provider", "none")
        if prov == "none": continue
        custom = cfg.get("_custom", [])
        if prov not in PROVIDERS and prov not in custom:
            errs.append(f"role {role}: unknown provider '{prov}' (choose from {', '.join(list(PROVIDERS) + custom)} or none)")
            continue
        if prov in custom and not r.get("experimental"):
            errs.append(f"role {role}: {prov} is a custom AI that the relay cannot stop from using git (no deny list), so it needs \"experimental\": true")
        for key in ("model", "effort"):
            v = r.get(key)
            if v is not None and (not isinstance(v, str) or not SAFE.match(v)): errs.append(f"role {role}: {key} has characters that are not allowed")
    for role in ("dispatcher", "builder", "reviewer"):
        if roles[role].get("provider", "none") == "none": errs.append(f"role {role} cannot be none")
    b, rv = roles["builder"], roles["reviewer"]
    if b.get("provider") == rv.get("provider") and (b.get("model", "auto") == rv.get("model", "auto") or "auto" in (b.get("model"), rv.get("model"))):
        errs.append("reviewer must be a different model from the builder (no self-review)")
    fb = cfg["fallbackBuilders"]
    if not isinstance(fb, list) or any(x not in ["copilot", "opencode", "codex"] + cfg.get("_custom", []) for x in fb): errs.append("fallbackBuilders must be a list of copilot / opencode / codex / custom AIs")
    elif b.get("provider") in fb: problems["warnings"].append(f"builder {b.get('provider')} removed from its own fallback chain")
    if not isinstance(cfg["fallbackMaxPerDay"], int) or not 0 <= cfg["fallbackMaxPerDay"] <= 200: errs.append("fallbackMaxPerDay must be 0-200")
    for role, backups in cfg["fallbackRoles"].items():
        if role not in ("dispatcher", "reviewer"):
            errs.append(f"fallbackRoles: unknown role '{role}'")
        if not isinstance(backups, list) or any(p not in PROVIDERS for p in backups):
            errs.append(f"fallbackRoles.{role} must list built-in providers")
        elif len(backups) != len(set(backups)):
            errs.append(f"fallbackRoles.{role} contains duplicate providers")
    if cfg["mode"] not in ("serial", "parallel"): errs.append("mode must be serial or parallel")
    workers = cfg["parallel"].get("workers")
    if not isinstance(workers, list) or any(w not in ("codex", "copilot", "opencode") for w in workers):
        errs.append("parallel.workers must list codex, copilot and opencode (Claude plans and reviews; it does not build in parallel)")
    elif len(workers) != len(set(workers)): errs.append("parallel.workers contains duplicate providers")
    for k, v in cfg["models"].items():
        if not isinstance(v, str) or not SAFE.match(v): errs.append(f"models.{k} has characters that are not allowed")
    for provider, capabilities in cfg["capabilities"].items():
        if provider not in list(PROVIDERS) + cfg.get("_custom", []): errs.append("capabilities: unknown provider " + provider)
        if not isinstance(capabilities, list) or any(not isinstance(c, str) or not SAFE.fullmatch(c) for c in capabilities):
            errs.append("capabilities." + provider + " must be a list of capability names")
    for key, value in cfg["limits"].items():
        if key not in DEFAULTS["limits"] or isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 86400:
            errs.append("limits." + key + " must be an integer from 1 to 86400")
    ap = cfg["approvals"]
    if ap["mode"] not in ("ask", "auto"): errs.append("approvals.mode must be ask or auto")
    jp = ap["judge"].get("provider", "reviewer")
    if jp != "reviewer" and jp not in list(PROVIDERS) + cfg.get("_custom", []): errs.append("approvals.judge.provider: unknown provider '%s'" % jp)
    for k in ("model", "effort"):
        v = ap["judge"].get(k, "auto")
        if not isinstance(v, str) or not SAFE.match(v): errs.append("approvals.judge." + k + " has characters that are not allowed")
    if not isinstance(cfg["idleChores"], list) or any(x not in ("backend", "digest") for x in cfg["idleChores"]): errs.append("idleChores may contain backend and digest")


def effective(path):
    cfg, problems = load(path)
    validate(cfg, problems)
    if problems["errors"]:
        good, _ = load(os.devnull)   # a bad file changes nothing: run on the defaults
        return good, problems, False
    cfg["fallbackBuilders"] = [x for x in cfg["fallbackBuilders"] if x != cfg["roles"]["builder"]["provider"]]
    return cfg, problems, True


def shell(path):
    cfg, problems, ok = effective(path)
    for e in problems["errors"]: print("settings: ERROR " + e + " -- using the defaults", file=sys.stderr)
    for w in problems["warnings"]: print("settings: " + w, file=sys.stderr)
    out = []
    for role in ("dispatcher", "builder", "reviewer", "cleaner"):
        r = cfg["roles"][role]
        for k in ("provider", "model", "effort"):
            out.append(f"S_{role.upper()}_{k.upper()}='{r.get(k, '')}'")
    out.append(f"S_FALLBACKS='{' '.join(cfg['fallbackBuilders'])}'")
    for role in ("dispatcher", "reviewer"):
        out.append(f"S_{role.upper()}_FALLBACKS='{' '.join(cfg['fallbackRoles'][role])}'")
    out.append(f"S_FALLBACK_MAX='{cfg['fallbackMaxPerDay']}'")
    out.append(f"S_MODE='{cfg['mode']}'")
    out.append(f"S_PARALLEL_WORKERS='{' '.join(cfg['parallel']['workers'])}'")
    out.append(f"S_COPILOT_MODEL='{cfg['models']['copilot']}'")
    out.append(f"S_OPENCODE_MODEL='{cfg['models']['opencode']}'")
    out.append(f"S_ADVISOR='{cfg['models']['advisor']}'")
    out.append(f"S_IDLE_CHORES='{' '.join(cfg['idleChores'])}'")
    ap = cfg["approvals"]; jd = ap["judge"]
    out.append(f"S_APPROVALS_MODE='{ap['mode']}'")
    out.append(f"S_APPROVALS_SECRETS='{1 if ap['autoSecrets'] else 0}'")
    out.append(f"S_JUDGE_PROVIDER='{'' if jd.get('provider', 'reviewer') == 'reviewer' else jd['provider']}'")
    out.append(f"S_JUDGE_MODEL='{jd.get('model', 'auto')}'")
    out.append(f"S_JUDGE_EFFORT='{jd.get('effort', 'auto')}'")
    out.append(f"S_VALID='{1 if ok else 0}'")
    out.append(f"S_AGENT_TIMEOUT='{cfg['limits']['timeoutSeconds']}'")
    print("\n".join(out))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    path = sys.argv[2] if len(sys.argv) > 2 else str(pathlib.Path(__file__).with_name("relay.settings.json"))
    if cmd == "shell": shell(path)
    elif cmd == "check":
        cfg, problems = load(path); validate(cfg, problems); print(json.dumps(problems)); sys.exit(2 if problems["errors"] else 0)
    elif cmd == "cmd":      # relay-settings.py cmd <provider> <model> <effort>  -> STDIN|FILE then one argument per line
        spec = custom_providers(path).get(sys.argv[3] if len(sys.argv) > 3 else "")
        if not spec: sys.exit(1)
        print(chr(10).join(build_cmd(spec, sys.argv[4] if len(sys.argv) > 4 else "", sys.argv[5] if len(sys.argv) > 5 else "")))
    elif cmd == "show":
        cfg, problems, ok = effective(path); print(json.dumps({"settings": cfg, "valid": ok, **problems}, indent=1))
    else: sys.exit("usage: relay-settings.py shell|check|show [file]")
