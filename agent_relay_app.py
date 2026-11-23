"""Agent Relay: the desktop app. Starts the local engine and shows the interface in its own native window (Windows and Linux),
with a splash screen while it loads. Linux source launches use GTK and WebKitGTK.

  python agent_relay_app.py [--relay <folder>] [--port 8765] [--open]
  AGENT_RELAY_NO_WINDOW=1 runs the engine only (used by the build checks)
"""
import os, socket, sys, pathlib, threading, time

if sys.stdout is None: sys.stdout = open(os.devnull, "w")      # the packaged app has no console
if sys.stderr is None: sys.stderr = open(os.devnull, "w")
base = pathlib.Path(getattr(sys, "_MEIPASS", pathlib.Path(__file__).parent))
sys.path.insert(0, str(base if hasattr(sys, "_MEIPASS") else base / "dashboard"))


def unblock_download():
    """Clear the download marker on this bundle's own DLLs before .NET loads them."""
    if os.name != "nt" or not hasattr(sys, "_MEIPASS"): return
    for library in base.rglob("*.dll"):
        if not library.is_symlink() and library.resolve().is_relative_to(base):
            try: os.unlink(str(library) + ":Zone.Identifier")
            except OSError: pass


import osops, server

ASSETS = base / "assets"
SPLASH = """<!doctype html><meta charset="utf-8"><title>Agent Relay</title>
<style>html,body{margin:0;height:100%;background:#0b0f14;overflow:hidden;font-family:Segoe UI,system-ui,sans-serif;color:#e6edf3}
.w{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px;background:radial-gradient(circle at 50% 38%,#0f1c2b 0,#0b0f14 62%)}
svg{animation:rise .8s ease both}.n{font-size:24px;letter-spacing:.18em;font-weight:600;animation:rise .9s .15s ease both}.s{font-size:13px;color:#8b949e}
.ld{width:160px;height:3px;background:#1a222c;border-radius:3px;overflow:hidden}.ld i{display:block;width:40%;height:100%;background:linear-gradient(90deg,#22d3ee,#3b82f6);border-radius:3px;animation:go 1.3s ease-in-out infinite}
.by{position:absolute;bottom:26px;left:0;right:0;display:flex;align-items:center;justify-content:center;gap:12px;color:#8b949e;font-size:11px;letter-spacing:.14em;text-transform:uppercase}
.by .dv{width:1px;height:26px;background:#2a313a}.by b{font-size:19px;letter-spacing:.2em;color:#e6edf3;font-weight:700;display:block;line-height:1}.by img{height:46px}.by .fb{background:#0b0f14;border-radius:10px;padding:4px 12px;display:inline-flex}.by a{letter-spacing:.06em;text-transform:none;font-size:12px}
@keyframes rise{from{opacity:0;transform:translateY(10px) scale(.96)}to{opacity:1;transform:none}}@keyframes go{0%{margin-left:-40%}100%{margin-left:100%}}</style>
<div class="w">@@SVG@@<div class="n">AGENT RELAY</div><div class="s">Getting things ready&hellip;</div><div class="ld"><i></i></div><div class="by">@@BY@@</div></div>"""


def splash_html():
    try: svg = (ASSETS / "agent-relay.svg").read_text(encoding="utf-8").replace('width="256" height="256"', 'width="104" height="104"')
    except OSError: svg = ""
    by = '<span>Driven by<b>FLUX</b></span>'
    for ext, mime in (("svg", "image/svg+xml"), ("png", "image/png")):          # the real Flux logo, if it has been dropped into assets/
        f = ASSETS / ("flux-logo." + ext)
        if f.is_file():
            import base64
            by = '<span class="fb"><img src="data:%s;base64,%s"></span>' % (mime, base64.b64encode(f.read_bytes()).decode("ascii")); break
    return SPLASH.replace("@@SVG@@", svg).replace("@@BY@@", by)


def free_port(start):
    for p in range(start, start + 20):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0: return p
    return start


def run_window(url):
    """Show the interface in a native window, with the splash first."""
    if sys.platform.startswith("linux") and not hasattr(sys, "_MEIPASS"):
        from linux_window import run

        return run(url, splash_html(), osops.DIALOGS, server.Handler)
    try:
        windows = sys.platform.startswith("win")
        if windows:
            from windows_window import ensure_runtime

            ensure_runtime(base / "MicrosoftEdgeWebview2Setup.exe")
            unblock_download()
        import webview
        if windows:
            from webview.guilib import initialize

            if initialize("edgechromium").renderer != "edgechromium":
                raise RuntimeError("WebView2 could not start. Check that the Windows .NET Framework 4.6.2 or newer is installed.")
        fd = getattr(webview, "FileDialog", None)
        folder_kind = fd.FOLDER if fd else webview.FOLDER_DIALOG
        open_kind = fd.OPEN if fd else webview.OPEN_DIALOG
        page = {"url": url} if windows else {"html": splash_html()}
        window = webview.create_window("Agent Relay", **page, width=1240, height=880, min_size=(980, 640), text_select=True, background_color="#0b0f14")
        pick = lambda kind: (lambda title: (lambda r: r[0] if r else "")(window.create_file_dialog(kind)))
        osops.DIALOGS["folder"] = pick(folder_kind); osops.DIALOGS["file"] = pick(open_kind)
        server.Handler.on_quit = window.destroy                # the in-page "Quit app" button closes the window
        if windows:
            def show_notification(title, message):
                from System import Action
                from System.Drawing import SystemIcons
                from System.Windows.Forms import NotifyIcon, ToolTipIcon

                def show():
                    if not hasattr(show_notification, "icon"):
                        icon = NotifyIcon()
                        icon.Icon = SystemIcons.Information
                        icon.Text = "Agent Relay"
                        icon.BalloonTipShown += lambda *_: setattr(server.Handler, "native_notification_shown_at", time.time())
                        icon.Visible = True
                        show_notification.icon = icon
                    show_notification.icon.ShowBalloonTip(8000, title, message, ToolTipIcon.Info)

                window.native.Invoke(Action(show))

            def clear_notification():
                if hasattr(show_notification, "icon"):
                    show_notification.icon.Dispose()

            server.Handler.on_notify = show_notification
            window.events.closing += clear_notification

        def load(win):
            time.sleep(0.9)                                    # let the splash be seen; the page shows the same splash until its first data is in
            win.load_url(url)
        icon = str(ASSETS / "agent-relay-512.png")
        if windows:
            ready = threading.Event()
            timed_out = threading.Event()
            window.events.loaded += ready.set
            window.events.closed += ready.set

            def check_load():
                if not ready.wait(30):
                    timed_out.set()
                    window.destroy()

            webview.start(check_load, gui="edgechromium")
            if timed_out.is_set():
                raise RuntimeError("The native WebView2 window did not load the dashboard within 30 seconds.")
        else:
            try: webview.start(load, window, gui="qt", icon=icon)               # Linux: the bundled Qt web engine
            except Exception: webview.start(load, window)
        return True
    except Exception as error:
        try:                                                   # leave a note for whoever has to find out why there was no window
            import traceback
            log = (osops.pathlib.Path.home() / ".agent-relay-window-error.log")
            log.write_text(traceback.format_exc(), encoding="utf-8")
        except Exception:
            pass
        if sys.platform.startswith("win"):
            from windows_window import native_message

            native_message(f"Agent Relay could not open its desktop window.\n\n{error}\n\n"
                           "Startup details: %USERPROFILE%\\.agent-relay-window-error.log")
        return False


def main(args):
    if "--port" not in args: args += ["--port", str(free_port(8765))]
    srv, options = server.setup(args)
    url = f"http://{options.host}:{srv.server_port}/"
    with srv:
        if os.environ.get("AGENT_RELAY_NO_WINDOW"):
            try:
                srv.serve_forever()
            except KeyboardInterrupt:
                pass
            return 0

        engine = threading.Thread(target=srv.serve_forever, daemon=True)
        engine.start()
        try:
            if options.open:
                if not os.environ.get("AGENT_RELAY_NO_BROWSER"):
                    import webbrowser

                    webbrowser.open(url)
                engine.join()
                return 0
            if run_window(url):
                if sys.platform.startswith("win"):
                    os._exit(0)  # Preserve the WebView2 process shutdown path.
                return 0
            if sys.platform.startswith("win"):
                return 1
            if sys.platform.startswith("linux"):
                message = "Agent Relay could not open its Linux desktop window."
                if not hasattr(sys, "_MEIPASS"):
                    message += " On Fedora, use system Python with python3-gobject, gtk4 and webkitgtk6.0."
                print(message, file=sys.stderr)
                return 1
            if not os.environ.get("AGENT_RELAY_NO_BROWSER"):
                import webbrowser

                webbrowser.open(url)
            engine.join()
        except KeyboardInterrupt:
            pass
        finally:
            srv.shutdown()
            engine.join()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
