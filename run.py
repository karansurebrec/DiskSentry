"""DiskSentry — storage telemetry console. Entry point.

Usage:
    python run.py [--port 8765] [--browser | --no-browser] [--sim]

v1.3: DiskSentry now runs as a REAL DESKTOP APPLICATION — a native OS window
with its own title bar, taskbar entry and icon (powered by pywebview, which
uses Microsoft Edge WebView2 on Windows 11). No browser window, no address
bar, no "keep this console open". Close the DiskSentry window and the app
exits (TBW snapshot saved first).

Shell modes:
    default      native app window (WebView2). If the desktop shell cannot
                 start (runtime missing), DiskSentry automatically falls back
                 to your default browser so you are never left stranded.
    --browser    force browser mode, never open the desktop window
    --no-browser server only: nothing opens, connect to the URL yourself

The dashboard is still served from a local waitress thread on 127.0.0.1 —
only the shell changed. In the packaged windowed exe (console=False) console
output is mirrored to %LOCALAPPDATA%/DiskSentry/disksentry.log.
"""
import argparse
import os
import socket
import sys
import threading
import time
import webbrowser

APP_TITLE = "DiskSentry — Storage Telemetry Console"
WINDOW_SIZE = (1500, 950)
WINDOW_MIN = (1120, 760)
BACKGROUND_COLOR = "#050805"

BANNER = r"""
  ___ ____  _   _ _____ ___  ____  _____ _   _   _   _
 |_ _/ ___|| | | | ____| _ \|  _ \| ____| \ | | / \ | |
  | |\___ \| | | |  _| | |_) | |_) |  _| |  \| |/ _ \| |
  | | ___) | |_| | |___|  _ <|  _ <| |___| |\  / ___ \ |___
 |___|____/ \___/|_____|_| \_\_| \_\_____|_| \_/_/   \_\____|
        storage telemetry console  ·  v1.3.1  ·  127.0.0.1:%s
"""


# ---------------------------------------------------------------- stdio guard
def _setup_stdio():
    """A windowed (no-console) exe has no stdout/stderr: mirror output to a
    log file so failures remain diagnosable. Returns the log path or None."""
    try:
        if sys.stdout is not None and sys.stderr is not None:
            return None
        from monitor import store
        log_path = os.path.join(store.data_dir(), "disksentry.log")
        f = open(log_path, "a", buffering=1, encoding="utf-8", errors="replace")
        sys.stdout = f
        sys.stderr = f
        print("\n===== DiskSentry session %s (windowed exe) ====="
              % time.strftime("%Y-%m-%d %H:%M:%S"))
        return log_path
    except Exception:
        class _Null:
            def write(self, *a):
                return None

            def flush(self):
                return None
        sys.stdout = sys.stderr = _Null()
        return None


def _fatal_alert(msg):
    """Pop a native Windows error dialog — the only visible feedback channel
    when the windowed exe dies before the UI is up."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, "DiskSentry", 0x10)
        except Exception:
            pass


def pick_port(default=8765):
    try:
        with socket.socket() as s:
            s.bind(("127.0.0.1", default))
            return default
    except OSError:
        try:
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                return s.getsockname()[1]
        except OSError:
            return default


def _wait_server_ready(host, port, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), 0.25):
                return True
        except OSError:
            time.sleep(0.1)
    return False


# ------------------------------------------------------------- server helpers
def _serve_blocking(host, port, debug):
    """Classic mode: waitress (or flask dev server) on the MAIN thread."""
    if debug:
        app_run = __import__("app").app
        app_run.run(host=host, port=port, debug=False, threaded=True)
    else:
        from waitress import serve
        import app as app_mod
        serve(app_mod.app, host=host, port=port, threads=8,
              _quiet=True, clear_untrusted_proxy_headers=True)


def _serve_background(host, port, debug):
    t = threading.Thread(target=_serve_blocking, args=(host, port, debug),
                         name="httpd", daemon=True)
    t.start()
    return t


def _open_browser_later(url, delay=1.0):
    def _open():
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass
    threading.Thread(target=_open, daemon=True).start()


def _block_forever():
    """Keep the main thread alive (server runs as daemon) until Ctrl+C."""
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass


# ------------------------------------------------------------ desktop window
def _run_desktop(url, host, port, debug):
    """Native app window shell. Returns an outcome code:
      'window-closed'  user closed the app window — normal exit
      'server-running' window failed but the backend thread is already up
                       (browser fallback should NOT rebind the port)
      'no-webview'     desktop shell not importable — nothing started yet
      'dead'           backend failed to start at all
    """
    try:
        import webview  # noqa: F401 — presence check only
    except Exception as e:
        print("  desktop shell : unavailable (%s) -> browser fallback" % e)
        return "no-webview"

    _serve_background(host, port, debug)
    if not _wait_server_ready(host, port):
        print("  backend       : FAILED to start on %s:%s" % (host, port))
        return "dead"

    app_url = url + ("?shell=app" if "?" not in url else "&shell=app")
    try:
        import webview
        webview.create_window(
            APP_TITLE, app_url,
            width=WINDOW_SIZE[0], height=WINDOW_SIZE[1],
            min_size=(WINDOW_MIN[0], WINDOW_MIN[1]),
            background_color=BACKGROUND_COLOR,
            text_select=False,          # feel like an app, not a webpage
        )
        webview.start()                 # blocks until the window is closed
        return "window-closed"
    except KeyboardInterrupt:
        return "window-closed"
    except Exception as e:
        print("  desktop window: failed to start (%s) -> browser fallback" % e)
        return "server-running"


def main():
    ap = argparse.ArgumentParser(description="DiskSentry storage telemetry console")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "0")))
    ap.add_argument("--host", default="127.0.0.1")
    shell = ap.add_mutually_exclusive_group()
    shell.add_argument("--browser", action="store_true",
                       help="open the dashboard in your default browser instead "
                            "of the native app window")
    shell.add_argument("--no-browser", action="store_true",
                       help="server only — do not open any window or browser")
    ap.add_argument("--sim", action="store_true", help="force simulation mode")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    log_path = _setup_stdio()

    from monitor.context import MONITOR
    from monitor import store
    if args.sim:
        store.set_mode("sim")
    MONITOR.start()

    port = args.port or pick_port()
    url = "http://%s:%d/" % (args.host, port)
    print(BANNER % port)
    info = MONITOR.system_info()
    print("  mode        : %s" % MONITOR.mode.upper())
    print("  platform    : %s" % info["os"])
    print("  smartctl    : %s" % (info["smartctl"] or "not found (SMART degraded)"))
    if sys.platform == "win32" and not info["admin"]:
        print("  admin       : NO — run as Administrator for full SMART/TBW access")
    print("  data dir    : %s" % info["data_dir"])
    if log_path:
        print("  log         : %s" % log_path)

    # ---- which shell will actually run? (decide honestly BEFORE printing) --
    want_window = not args.browser and not args.no_browser
    can_window = False
    if want_window:
        try:
            import webview  # noqa: F401 — importability probe only
            can_window = True
        except Exception:
            can_window = False
    if args.no_browser:
        shell_desc = "none (server only)"
    elif not want_window:
        shell_desc = "browser (forced by --browser)"
    elif can_window:
        shell_desc = "NATIVE APP WINDOW (WebView2 · browser fallback)"
    else:
        shell_desc = "browser (desktop shell unavailable here)"
    print("  shell       : %s" % shell_desc)
    print("  stop        : %s\n" %
          ("close the DiskSentry window" if (want_window and can_window)
           else "Ctrl+C"))

    def _shutdown():
        try:
            MONITOR._snapshot_tbw()
        except Exception:
            pass
        print("\n  sentry stopped. TBW snapshot saved.\n")

    # ---- native desktop application window (default) -----------------------
    if want_window:
        outcome = _run_desktop(url, args.host, port, args.debug)
        if outcome == "window-closed":
            _shutdown()
            return
        if outcome == "server-running":
            # backend is already serving in the background thread — reuse it
            print("  shell       : browser fallback (backend already running)")
            print("  dashboard   : %s   (opening browser...)" % url)
            print("  press Ctrl+C to stop\n")
            _open_browser_later(url)
            try:
                _block_forever()
            except KeyboardInterrupt:
                pass
            _shutdown()
            return
        if outcome == "dead":
            _fatal_alert(
                "DiskSentry backend failed to start on %s:%d.\n\n"
                "Check disksentry.log in %%LOCALAPPDATA%%\\DiskSentry for details."
                % (args.host, port))
            _shutdown()
            sys.exit(1)
        # 'no-webview' -> fall through to the classic browser path below

    # ---- classic paths: browser window, or server only ---------------------
    if args.no_browser:
        print("  dashboard   : %s   (open it yourself)" % url)
        print("  press Ctrl+C to stop\n")
    else:
        print("  dashboard   : %s   (opening browser...)" % url)
        print("  press Ctrl+C to stop\n")
        _open_browser_later(url)

    try:
        _serve_blocking(args.host, port, args.debug)
    except KeyboardInterrupt:
        pass
    finally:
        _shutdown()


def _cli():
    """Frozen-app entry: never let a windowed exe die silently."""
    try:
        main()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        pass
    except Exception as e:
        import traceback
        traceback.print_exc()
        _fatal_alert("DiskSentry crashed on startup:\n\n%s\n\nDetails are in "
                     "disksentry.log (%%LOCALAPPDATA%%\\DiskSentry)." % e)
        raise


if __name__ == "__main__":
    _cli()
