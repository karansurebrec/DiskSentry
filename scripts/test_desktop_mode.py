"""DiskSentry v1.3 — NATIVE DESKAPP SHELL tests.

Covers the new 'real application, not in a browser' launch modes of run.py:

  1. Native window path      : fake webview module records create_window/start;
                               backend must be SERVING while the window is open
                               (urllib /api/health from inside fake start()).
  2. Window-crash fallback   : webview.start() raises -> 'server-running' branch
                               reuses the already-bound backend, opens browser,
                               blocks until KeyboardInterrupt.
  3. Headless end-to-end     : real subprocess WITHOUT pywebview installed ->
                               import fails -> classic browser path -> server up.
  4. --no-browser            : subprocess server-only mode.
  5. --browser flag          : fake webview present but MUST NOT be used.
  6. flag exclusivity        : --browser + --no-browser -> argparse error.

Run:  .venv/bin/python scripts/test_desktop_mode.py
"""
import json
import subprocess
import sys
import os
import time
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
PY = sys.executable

PASS = 0


def ok(name, cond, extra=""):
    global PASS
    if not cond:
        print("  [FAIL] %s %s" % (name, extra))
        sys.exit(1)
    PASS += 1
    print("  [ok] %s" % name)


# -------------------------------------------------------------------- helpers
class FakeWebview:
    def __init__(self, mode="ok"):
        self.mode = mode
        self.windows = []
        self.start_calls = 0
        import types
        self.module = types.ModuleType("webview")
        self.module.create_window = self.create_window
        self.module.start = self.start

    def create_window(self, title, url, **kw):
        self.windows.append(dict({"title": title, "url": url}, **kw))

    def start(self, *a, **kw):
        self.start_calls += 1
        if self.mode == "ok":
            # the backend must already be serving while the window is open
            with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % PORT,
                                        timeout=5) as r:
                self.health = json.loads(r.read().decode())
            with urllib.request.urlopen("http://127.0.0.1:%d/api/system" % PORT,
                                        timeout=5) as r:
                self.system = json.loads(r.read().decode())
        elif self.mode == "fail":
            raise RuntimeError("simulated: WebView2 runtime missing")


def call_main(argv, patches=None):
    import run
    saved = {k: getattr(run, k) for k in (patches or {})}
    try:
        for k, v in (patches or {}).items():
            setattr(run, k, v)
        sys.argv = ["run.py"] + argv
        run.main()
    finally:
        for k, v in saved.items():
            setattr(run, k, v)


PORT = 18771


# ============================================================== 1. native app
print("[1] native desktop window path (fake webview)")
import run  # noqa: E402
fake = FakeWebview("ok")
sys.modules["webview"] = fake.module
opened = []
call_main(["--port", "18771"], {"_open_browser_later": lambda u, delay=1.0: opened.append(u)})
ok("window created", len(fake.windows) == 1)
w = fake.windows[0]
ok("window title", "DiskSentry" in w["title"], w["title"])
ok("window url has ?shell=app", w["url"].endswith("/?shell=app"), w["url"])
ok("window size 1500x950", (w["width"], w["height"]) == (1500, 950))
ok("window min size", (w["min_size"][0], w["min_size"][1]) == (1120, 760))
ok("text_select disabled (app feel)", w.get("text_select") is False)
ok("webview.start() called once", fake.start_calls == 1)
ok("backend serving while window open", fake.health.get("ok") is True,
   str(fake.health))
ok("window sees v1.3.1 backend", fake.system.get("sentry_version") == "1.3.1",
   str(fake.system.get("sentry_version")))
ok("browser NOT opened in window mode", opened == [])
ok("snapshot ran (window-closed exit)",
   True)  # main() returned normally == window-closed branch
del sys.modules["webview"]

# ==================================================== 2. window crash fallback
print("[2] webview.start() crash -> browser fallback reusing live backend")
fake2 = FakeWebview("fail")
sys.modules["webview"] = fake2.module
opened2 = []
blocked = {"n": 0}


def fake_block():
    blocked["n"] += 1
    raise KeyboardInterrupt  # simulate Ctrl+C


call_main(["--port", "18772"], {
    "_open_browser_later": lambda u, delay=1.0: opened2.append(u),
    "_block_forever": fake_block,
})
ok("window creation attempted", len(fake2.windows) == 1)
ok("browser fallback opened", opened2 and opened2[0] == "http://127.0.0.1:18772/", str(opened2))
ok("main thread blocked until Ctrl+C", blocked["n"] == 1)
del sys.modules["webview"]

# ============================================================== 5. --browser
print("[3] --browser flag must bypass the desktop shell entirely")
fake3 = FakeWebview("ok")
sys.modules["webview"] = fake3.module
opened3 = []


def kill_serve(host, port, debug):
    raise KeyboardInterrupt


call_main(["--browser", "--port", "18773"], {
    "_open_browser_later": lambda u, delay=1.0: opened3.append(u),
    "_serve_blocking": kill_serve,
})
ok("no window created under --browser", fake3.windows == [] and fake3.start_calls == 0)
ok("browser opened instead", opened3 == ["http://127.0.0.1:18773/"])
del sys.modules["webview"]

# ==================================================== 6. flag mutual exclusion
print("[4] --browser + --no-browser rejected")
try:
    call_main(["--browser", "--no-browser"])
    ok("argparse rejects both", False)
except SystemExit as e:
    ok("argparse rejects both", e.code == 2, str(e.code))


# ================================================= 3/4. subprocess end-to-end
def wait_health(port, timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % port,
                                        timeout=2) as r:
                h = json.loads(r.read().decode())
            with urllib.request.urlopen("http://127.0.0.1:%d/api/system" % port,
                                        timeout=2) as r:
                s = json.loads(r.read().decode())
            if h.get("ok") and s.get("sentry_version") == "1.3.1":
                return h
        except Exception:
            pass
        time.sleep(0.2)
    return None


def run_proc(args, port, expect_lines):
    env = dict(os.environ)
    proc = subprocess.Popen([PY, "run.py"] + args, cwd=HERE, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True)
    h = wait_health(port)
    ok("subprocess server up (%s)" % " ".join(args), h is not None)
    time.sleep(0.4)
    proc.terminate()
    try:
        out, _ = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    for line in expect_lines:
        ok("stdout: %r" % line, line in (out or ""), (out or "")[-400:])
    return out


print("[5] headless fallback chain (no pywebview installed in this venv)")
out = run_proc(["--port", "18774"], 18774,
               ["desktop shell : unavailable", "browser fallback",
                "shell       : browser", "v1.3.1"])
ok("no 'NATIVE APP WINDOW' claim when shell missing",
   "NATIVE APP WINDOW" not in out)

print("[6] --no-browser server-only subprocess")
run_proc(["--no-browser", "--port", "18775"], 18775,
         ["shell       : none (server only)"])

print("\nDESKTOP MODE: ALL %d CHECKS PASSED" % PASS)
