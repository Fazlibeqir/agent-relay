"""Everything that differs between Windows and Linux: finding tools, opening terminals, listing drives, processes, installing packages, starting the relay.
Standard library only. Windows is tested; the Linux branches are written to the same shape but have not been run on a Linux machine yet."""
import ctypes, json, os, pathlib, shutil, string, subprocess, sys, time

IS_WIN = os.name == "nt"
NO_WINDOW = 0x08000000 if IS_WIN else 0
HOME = pathlib.Path.home()


# ---------------------------------------------------------------- finding programs
def extra_bin_dirs():
    dirs = []
    if IS_WIN:
        for e in ("APPDATA", "LOCALAPPDATA"):
            v = os.environ.get(e)
            if v: dirs += [pathlib.Path(v) / "npm", pathlib.Path(v) / "Programs" / "Git" / "cmd"]
        dirs += [pathlib.Path(r"C:\Program Files\GitHub CLI"), pathlib.Path(r"C:\Program Files\Git\cmd"), pathlib.Path(r"C:\Program Files\nodejs")]
    else:
        dirs += [HOME / ".local" / "bin", HOME / ".npm-global" / "bin", HOME / ".opencode" / "bin", HOME / ".cargo" / "bin", HOME / "go" / "bin",
                 pathlib.Path("/usr/local/bin"), pathlib.Path("/snap/bin"), pathlib.Path("/opt/homebrew/bin")]
    return [d for d in dirs if d.is_dir()]


def which(name):
    """A program on PATH, or in the usual global-install folders (npm, ~/.local/bin, ...)."""
    hit = shutil.which(name)
    if hit: return hit
    exts = (".cmd", ".exe", ".bat", "") if IS_WIN else ("",)
    for d in extra_bin_dirs():
        for e in exts:
            c = d / (name + e)
            if c.is_file(): return str(c)
    return None


def glob_first(patterns):
    import glob
    for g in patterns:
        hits = sorted(glob.glob(g), reverse=True)
        if hits: return hits[0]
    return None


def run(found, args, timeout=20, cwd=None, stdin=None):
    """Run a found program and return (exit code, output text). Windows .cmd shims go through cmd.exe."""
    cmd = (["cmd.exe", "/c", found] if IS_WIN and found.lower().endswith((".cmd", ".bat")) else [found]) + list(args)
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, cwd=cwd,
                       creationflags=NO_WINDOW, stdin=stdin if stdin is not None else subprocess.DEVNULL)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def python_name():
    return "python" if IS_WIN else ("python3" if shutil.which("python3") else "python")


# ---------------------------------------------------------------- terminals
LINUX_TERMINALS = (("x-terminal-emulator", ["-e"]), ("gnome-terminal", ["--"]), ("konsole", ["-e"]), ("xfce4-terminal", ["-x"]),
                   ("mate-terminal", ["-x"]), ("kitty", []), ("alacritty", ["-e"]), ("xterm", ["-e"]))


def open_terminal(title, line, keep=True, cwd=None):
    """Open a visible terminal window that runs one command line and waits, so the person can watch and answer prompts. Returns (ok, message)."""
    if IS_WIN:
        tail = " & echo. & echo Finished. You can close this window. & pause" if keep else ""
        subprocess.Popen('start "%s" cmd /c "%s%s"' % (title, line, tail), shell=True, cwd=cwd)
        return True, ""
    script = line + ('; echo; echo "Finished. Press Enter to close this window."; read _' if keep else "")
    for term, flag in LINUX_TERMINALS:
        path = shutil.which(term)
        if path:
            subprocess.Popen([path] + flag + ["bash", "-c", script], cwd=cwd)
            return True, ""
    return False, "No terminal program was found. Open a terminal yourself and run: " + line


def open_terminal_keep_shell(title, line, cwd=None):
    """Like open_terminal but the window stays open as a shell afterwards (used for sign-in, where the tool may keep running)."""
    if IS_WIN:
        subprocess.Popen('start "%s" cmd /k "%s"' % (title, line), shell=True, cwd=cwd)
        return True, ""
    return open_terminal(title, line, True, cwd)


def quote(path):
    return '"%s"' % path if IS_WIN else "'%s'" % str(path).replace("'", "'\"'\"'")


# ---------------------------------------------------------------- packages
def package_manager():
    if IS_WIN: return "winget" if shutil.which("winget") else None
    for pm in ("apt-get", "dnf", "pacman", "zypper", "brew"):
        if shutil.which(pm): return pm
    return None


def install_line(spec):
    """spec: {'winget': id, 'apt': pkgs, 'dnf': pkgs, 'pacman': pkgs, 'script': shell line (any OS), 'npm': package}. Returns a command line for this machine, or ''."""
    if spec.get("npm"): return "npm install -g " + spec["npm"]
    pm = package_manager()
    if IS_WIN:
        if spec.get("winget") and pm == "winget": return "winget install -e --id %s --accept-package-agreements --accept-source-agreements" % spec["winget"]
        return spec.get("script_win", "")
    if pm == "apt-get" and spec.get("apt"): return "sudo apt-get install -y " + spec["apt"]
    if pm == "dnf" and spec.get("dnf"): return "sudo dnf install -y " + spec["dnf"]
    if pm == "pacman" and spec.get("pacman"): return "sudo pacman -S --noconfirm " + spec["pacman"]
    if pm == "brew" and spec.get("brew"): return "brew install " + spec["brew"]
    return spec.get("script", "")


# ---------------------------------------------------------------- drives
def drives():
    """Places a project could live: [{path, label, total, free, usedPct}] in GB. Windows: fixed drives. Linux: the home folder and real mounts."""
    out = []
    if IS_WIN:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for i, letter in enumerate(string.ascii_uppercase):
            if not mask & (1 << i): continue
            root = letter + ":\\"
            if ctypes.windll.kernel32.GetDriveTypeW(root) != 3: continue          # 3 = fixed disk (skips USB sticks, CD, network)
            try: u = shutil.disk_usage(root)
            except OSError: continue
            out.append(_drive(root, "Disk " + letter + ":", u))
    else:
        seen = set()
        try: mounts = [l.split() for l in open("/proc/mounts", encoding="utf-8")]
        except OSError: mounts = []
        cands = [str(HOME)] + [m[1] for m in mounts if len(m) > 2 and m[2] in ("ext4", "ext3", "xfs", "btrfs", "f2fs", "ntfs", "ntfs3", "exfat", "vfat", "zfs")
                               and not m[1].startswith(("/boot", "/snap", "/var/lib/docker"))]
        for c in cands:
            try: u = shutil.disk_usage(c); key = (u.total, u.free)
            except OSError: continue
            if key in seen and c != str(HOME): continue
            seen.add(key)
            out.append(_drive(c, "Home folder" if c == str(HOME) else c, u))
    return out


def _drive(path, label, u):
    gb = 1024 ** 3
    return {"path": path, "label": label, "totalGb": round(u.total / gb, 1), "freeGb": round(u.free / gb, 1), "usedPct": round(100 * (u.total - u.free) / max(u.total, 1))}


def recommended_drive(ds):
    """The drive with the most free space, ignoring ones that are nearly full (under 5 GB free or over 90 percent used) unless nothing else is left."""
    ok = [d for d in ds if d["freeGb"] >= 5 and d["usedPct"] <= 90] or ds
    return max(ok, key=lambda d: d["freeGb"]) if ok else None


def projects_root(drive_path):
    """Where projects go on a drive: <drive>Projects on Windows, <home>/Projects on Linux when the home folder was chosen, else <mount>/Projects."""
    return str(pathlib.Path(drive_path) / "Projects")


# ---------------------------------------------------------------- processes
def process_table():
    rows = {}
    if IS_WIN:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                            "Get-CimInstance Win32_Process | ForEach-Object { \"$($_.ProcessId)|$($_.ParentProcessId)|$($_.Name)|$($_.CommandLine)\" }"],
                           capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
        for line in r.stdout.splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4 and parts[0].strip().isdigit() and parts[1].strip().isdigit():
                rows[int(parts[0])] = {"pid": int(parts[0]), "ppid": int(parts[1]), "name": parts[2], "cmd": parts[3]}
    else:
        r = subprocess.run(["ps", "-eo", "pid=,ppid=,comm=,args="], capture_output=True, text=True, timeout=30)
        for line in r.stdout.splitlines():
            parts = line.split(None, 3)
            if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit():
                rows[int(parts[0])] = {"pid": int(parts[0]), "ppid": int(parts[1]), "name": parts[2], "cmd": parts[3] if len(parts) > 3 else parts[2]}
    return rows


def kill_pid(pid):
    if IS_WIN: subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, creationflags=NO_WINDOW)
    else:
        try: os.kill(pid, 9)
        except OSError: pass


def relay_roots(rows, project, me):
    """The launcher of THIS project's relay: on Windows its PowerShell window process, on Linux the bash running the relay script in that folder."""
    key = str(pathlib.Path(project).resolve()).lower()
    out = []
    for p in rows.values():
        if p["pid"] == me: continue
        cmd = p["cmd"].lower()
        if IS_WIN:
            if p["name"].lower().startswith("powershell") and "relay-awake" in cmd and key in cmd: out.append(p["pid"])
        else:
            if "relay-" in cmd and ".sh" in cmd and key in cmd and p["name"] in ("bash", "sh", "systemd-inhibit", "nohup"): out.append(p["pid"])
    return out


def start_background(project, script):
    """Start the relay for a project without a visible window. Windows: relay-awake.ps1 (keeps the PC awake). Linux: bash, under systemd-inhibit when present."""
    proj = pathlib.Path(project)
    if IS_WIN:
        ps1 = proj / "relay-awake.ps1"
        subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(ps1), "-Script", script, "-NoDashboard"],
                         cwd=str(proj), creationflags=NO_WINDOW)
        return
    inner = ["bash", "./" + script, str(proj)]
    if shutil.which("systemd-inhibit"): inner = ["systemd-inhibit", "--what=sleep:idle", "--why=Agent Relay is working"] + inner
    log = open(proj / "relay-console.log", "ab")
    subprocess.Popen(inner, cwd=str(proj), stdout=log, stderr=log, start_new_session=True)


def pick_file(title="Choose a file"):
    try:
        import tkinter
        from tkinter import filedialog
        r = tkinter.Tk(); r.withdraw(); r.attributes("-topmost", True)
        f = filedialog.askopenfilename(title=title, filetypes=[("Task lists", "*.md *.txt *.csv *.json *.yaml *.yml *.markdown"), ("All files", "*.*")])
        r.destroy()
        return {"ok": True, "path": f or ""}
    except Exception as ex:
        return {"ok": False, "error": "A file picker is not available here (" + str(ex)[:80] + "). Type the path instead."}


def pick_folder(title="Choose a folder"):
    try:
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk(); root.withdraw(); root.attributes("-topmost", True)
        d = filedialog.askdirectory(title=title)
        root.destroy()
        return {"ok": True, "path": d or ""}
    except Exception as ex:
        return {"ok": False, "error": "A folder picker is not available here (" + str(ex)[:80] + "). Type the path instead."}
