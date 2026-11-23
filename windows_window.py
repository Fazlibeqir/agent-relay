"""WebView2 startup prerequisites for the Windows desktop app."""
import ctypes
import subprocess


_RUNTIME_KEY = r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"


def runtime_installed():
    import winreg

    for hive, view in ((winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
                       (winreg.HKEY_CURRENT_USER, 0)):
        try:
            with winreg.OpenKey(hive, _RUNTIME_KEY, access=winreg.KEY_READ | view) as key:
                version, _ = winreg.QueryValueEx(key, "pv")
            if any(int(part) > 0 for part in version.split(".")):
                return True
        except (OSError, ValueError, AttributeError):
            continue
    return False


def native_message(message, flags=0x10):
    return ctypes.windll.user32.MessageBoxW(None, message, "Agent Relay", flags)


def ensure_runtime(bootstrapper):
    if runtime_installed():
        return
    if not bootstrapper.is_file():
        raise RuntimeError("Microsoft Edge WebView2 Runtime is missing. Install the Evergreen Runtime from "
                           "Microsoft, then reopen Agent Relay.")
    # Ask before installing a component on the user's computer.
    if native_message("Agent Relay needs Microsoft Edge WebView2 Runtime to open its desktop window. "
                      "Install the runtime now?", 0x124) != 6:
        raise RuntimeError("WebView2 Runtime installation was canceled.")
    result = subprocess.run([str(bootstrapper), "/install"], timeout=600, check=False)
    if not runtime_installed():
        raise RuntimeError(f"WebView2 Runtime setup did not finish (exit {result.returncode}). "
                           "Install the Evergreen Runtime from Microsoft, then reopen Agent Relay.")
