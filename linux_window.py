"""GTK desktop host for the Linux source launcher."""

import sys
import threading
import traceback
from urllib.parse import urlsplit


def run(url, splash, dialogs, handler):
    try:
        import gi

        gi.require_version("Gdk", "4.0")
        gi.require_version("Gtk", "4.0")
        gi.require_version("WebKit", "6.0")
        from gi.repository import Gdk, Gio, GLib, Gtk, WebKit
    except (ImportError, ValueError) as exc:
        print(f"GTK desktop support is unavailable: {exc}", file=sys.stderr)
        return False
    if not Gtk.init_check() or Gdk.Display.get_default() is None:
        print("GTK could not connect to the Linux desktop.", file=sys.stderr)
        return False

    app = Gtk.Application(
        application_id="io.github.fazlibeqir.AgentRelay",
        flags=Gio.ApplicationFlags.NON_UNIQUE,
    )
    origin = urlsplit(url)
    opened = False

    def activate(application):
        nonlocal opened
        try:
            window = Gtk.ApplicationWindow(application=application, title="Agent Relay")
            window.set_default_size(1240, 880)
            view = WebKit.WebView()
            window.set_child(view)
            pending_dialogs = set()

            def pick(title, action):
                result = {}
                ready = threading.Event()

                def show():
                    dialog = None
                    try:
                        dialog = Gtk.FileChooserNative.new(title, window, action, "_Select", "_Cancel")
                        dialog.set_modal(True)

                        def respond(chooser, response):
                            try:
                                file = chooser.get_file() if response == Gtk.ResponseType.ACCEPT else None
                                result["path"] = file.get_path() if file else ""
                            except Exception as exc:
                                result["error"] = exc
                            finally:
                                pending_dialogs.discard(chooser)
                                chooser.destroy()
                                ready.set()

                        dialog.connect("response", respond)
                        pending_dialogs.add(dialog)
                        dialog.show()
                    except Exception as exc:
                        if dialog is not None:
                            pending_dialogs.discard(dialog)
                            dialog.destroy()
                        result["error"] = exc
                        ready.set()
                    return GLib.SOURCE_REMOVE

                GLib.idle_add(show)
                ready.wait()
                if "error" in result:
                    raise result["error"]
                return result["path"]

            def close():
                window.close()
                return GLib.SOURCE_REMOVE

            dialogs["folder"] = lambda title: pick(title, Gtk.FileChooserAction.SELECT_FOLDER)
            dialogs["file"] = lambda title: pick(title, Gtk.FileChooserAction.OPEN)
            handler.on_quit = lambda: GLib.idle_add(close)

            def decide_policy(webview, decision, decision_type):
                if decision_type not in (
                    WebKit.PolicyDecisionType.NAVIGATION_ACTION,
                    WebKit.PolicyDecisionType.NEW_WINDOW_ACTION,
                ):
                    return False
                action = decision.get_navigation_action()
                uri = action.get_request().get_uri()
                destination = urlsplit(uri)
                if (destination.scheme, destination.netloc) == (origin.scheme, origin.netloc):
                    if decision_type == WebKit.PolicyDecisionType.NAVIGATION_ACTION:
                        return False
                    decision.ignore()
                    if action.is_user_gesture():
                        detail = Gtk.ApplicationWindow(application=application, title="Agent Relay")
                        detail.set_default_size(900, 650)
                        detail.set_transient_for(window)
                        detail.set_destroy_with_parent(True)
                        detail_view = WebKit.WebView()
                        detail_view.connect("decide-policy", decide_policy)
                        detail.set_child(detail_view)
                        detail_view.load_uri(uri)
                        detail.present()
                    return True
                if uri == "about:blank" or destination.scheme in ("data", "blob"):
                    return False
                decision.ignore()
                if action.is_user_gesture() and destination.scheme in ("http", "https"):
                    try:
                        Gio.AppInfo.launch_default_for_uri(uri)
                    except GLib.Error as exc:
                        print(f"Could not open the external link: {exc}", file=sys.stderr)
                return True

            def loaded(webview, event):
                if event == WebKit.LoadEvent.FINISHED and webview.get_uri() == url:
                    resource = webview.get_main_resource()
                    response = resource.get_response() if resource else None
                    if window.get_mapped() and response and response.get_status_code() == 200:
                        print("Agent Relay dashboard loaded in the GTK window.", flush=True)

            def open_dashboard():
                view.load_uri(url)
                return GLib.SOURCE_REMOVE

            view.connect("decide-policy", decide_policy)
            view.connect("load-changed", loaded)
            view.load_html(splash, None)
            window.present()
            GLib.timeout_add(900, open_dashboard)
            opened = True
        except Exception:
            traceback.print_exc()
            application.quit()

    app.connect("activate", activate)
    try:
        app.run([])
    finally:
        handler.on_quit = None
        dialogs.clear()
    return opened
