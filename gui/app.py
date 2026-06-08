"""
Desktop app shell (PyWebView) for Cain (the save editor).

Wraps the existing stdlib HTTP server (gui/server.py) in a NATIVE OS webview
window — no bundled Chromium, no browser needed. Adds a native file-open dialog
exposed to the page as `window.pywebview.api.pick_file()`.

  python3 gui/app.py                 # desktop window
  python3 gui/app.py --browser       # fall back to plain-browser server mode
  python3 gui/app.py --mpq <path>    # override MPQ (else auto-detected)

Packaged with PyInstaller this becomes a single double-clickable executable
(see build.py / the .spec). Pure-Python core + OS webview => ~10-20MB, not 200MB.
"""
from __future__ import annotations

import os
import sys
import threading
import importlib.util
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import server as srv  # gui/server.py


HOST = "127.0.0.1"


def _start_server(port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((HOST, port), srv.Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind((HOST, 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Api:
    """Methods callable from JS as window.pywebview.api.<name>(...)."""

    def __init__(self, window_getter):
        self._win = window_getter

    def pick_file(self):
        """Native open dialog. Returns the chosen path (str) or '' if cancelled."""
        import webview
        win = self._win()
        result = win.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=(
                "D2 saves (*.d2s;*.d2x;*.sss;*.stash)",
                "All files (*.*)",
            ),
        )
        if not result:
            return ""
        return result[0] if isinstance(result, (list, tuple)) else result

    def pick_mpq(self):
        """Native MPQ picker. Returns the chosen path (str) or '' if cancelled."""
        import webview
        win = self._win()
        result = win.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=("MPQ files (*.mpq)", "All files (*.*)"),
        )
        if not result:
            return ""
        return result[0] if isinstance(result, (list, tuple)) else result

    def pick_save_dir(self):
        """Native folder picker — returns a directory path or ''."""
        import webview
        win = self._win()
        result = win.create_file_dialog(webview.FOLDER_DIALOG)
        if not result:
            return ""
        return result[0] if isinstance(result, (list, tuple)) else result


def run_desktop(mpq: str | None):
    import webview

    if mpq:
        srv._mpq = mpq
    elif not srv._mpq:
        srv._mpq = srv._autodetect_mpq()

    port = _free_port()
    _start_server(port)

    holder = {}
    api = Api(lambda: holder["win"])
    win = webview.create_window(
        "Cain",
        url=f"http://{HOST}:{port}/",
        width=1100,
        height=760,
        min_size=(760, 520),
        js_api=api,
    )
    holder["win"] = win
    webview.start()


def run_browser(mpq: str | None, port: int | None = None):
    if mpq:
        srv._mpq = mpq
    elif not srv._mpq:
        srv._mpq = srv._autodetect_mpq()
    if port is None:
        port = _free_port()
    httpd = ThreadingHTTPServer((HOST, port), srv.Handler)
    url = f"http://{HOST}:{port}/"
    print(f"Cain on {url}  (mpq={srv._mpq or 'NOT FOUND — set in UI or --mpq'})")
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass
    httpd.serve_forever()


def _has_linux_webview_backend() -> bool:
    """pywebview on Linux needs either PyGObject/GTK or qtpy plus Qt bindings."""
    if not sys.platform.startswith("linux"):
        return True
    if importlib.util.find_spec("gi") is not None:
        return True
    return importlib.util.find_spec("qtpy") is not None


def main():
    args = sys.argv[1:]
    mpq = None
    browser = False
    port = None
    for i, a in enumerate(args):
        if a == "--mpq" and i + 1 < len(args):
            mpq = args[i + 1]
        elif a == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
        elif a == "--browser":
            browser = True

    if browser:
        run_browser(mpq, port)
        return
    try:
        import webview  # noqa: F401
    except ImportError:
        print("pywebview not installed; falling back to browser mode.\n"
              "  pip install pywebview   (for the native desktop window)")
        run_browser(mpq)
        return
    if not _has_linux_webview_backend():
        print("No Linux pywebview backend found; falling back to browser mode.\n"
              "  Install PyGObject/GTK or qtpy+Qt bindings for a native window.")
        run_browser(mpq)
        return
    run_desktop(mpq)


if __name__ == "__main__":
    main()
