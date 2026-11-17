"""Agent Relay: the app. Starts the local dashboard server and opens it in the browser. Packaged to AgentRelay.exe with build-app.ps1.

  python agent_relay_app.py [--relay <folder>] [--port 8765]
"""
import os, socket, sys, pathlib

if sys.stdout is None: sys.stdout = open(os.devnull, "w")      # the packaged app has no console
if sys.stderr is None: sys.stderr = open(os.devnull, "w")
here = pathlib.Path(getattr(sys, "_MEIPASS", pathlib.Path(__file__).parent / "dashboard"))
sys.path.insert(0, str(here))
import server


def free_port(start):
    for p in range(start, start + 20):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0: return p
    return start


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--port" not in args: args += ["--port", str(free_port(8765))]
    sys.argv = [sys.argv[0], "--open"] + args
    server.main()
