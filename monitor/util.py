"""Cross-platform helpers: subprocess without console flashes, PowerShell JSON, parsing."""
import json
import os
import re
import shutil
import subprocess
import sys

IS_WIN = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000 if IS_WIN else 0

_SIZE_UNITS = {
    "": 1, "B": 1, "K": 1024, "KB": 1024, "M": 1024**2, "MB": 1024**2,
    "G": 1024**3, "GB": 1024**3, "T": 1024**4, "TB": 1024**4,
    "P": 1024**5, "PB": 1024**5,
}


def run_cmd(cmd, timeout=15):
    """Run a command (list or shell string). Returns (rc, stdout, stderr). Never raises."""
    try:
        if isinstance(cmd, str):
            p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                               timeout=timeout, creationflags=CREATE_NO_WINDOW)
        else:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout, creationflags=CREATE_NO_WINDOW)
        return p.returncode, p.stdout or "", p.stderr or ""
    except FileNotFoundError:
        return 127, "", "command not found"
    except subprocess.TimeoutExpired:
        return -2, "", "timeout"
    except Exception as e:  # pragma: no cover
        return -1, "", str(e)


def powershell_json(snippet, timeout=25):
    """Run a PowerShell snippet that pipes to ConvertTo-Json; returns parsed JSON or None."""
    ps_cmd = (
        "$ErrorActionPreference='SilentlyContinue'; "
        + snippet
        + " | ConvertTo-Json -Depth 5 -Compress"
    )
    cmd = ["powershell", "-NoProfile", "-NonInteractive",
           "-ExecutionPolicy", "Bypass", "-Command", ps_cmd]
    rc, out, _ = run_cmd(cmd, timeout)
    if rc != 0 or not out or not out.strip():
        return None
    try:
        return json.loads(out)
    except Exception:
        return None


def to_int(v, default=None):
    try:
        if v is None:
            return default
        return int(float(str(v).strip()))
    except (ValueError, TypeError):
        return default


def to_float(v, default=None):
    try:
        if v is None:
            return default
        return float(str(v).strip())
    except (ValueError, TypeError):
        return default


def parse_size(s):
    """'931.5G' / '500.00 GB' -> bytes, else None."""
    if not s:
        return None
    m = re.match(r"^\s*([\d.]+)\s*([A-Za-z]*)\s*$", str(s))
    if not m:
        return None
    num, unit = m.group(1), m.group(2).upper()
    if unit not in _SIZE_UNITS:
        return None
    return int(float(num) * _SIZE_UNITS[unit])


def read_file(path):
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except Exception:
        return None


def find_smartctl():
    """Locate smartctl: PATH first, then every common Windows install location.

    Covers: the smartmontools installer (Program Files), winget portable
    installs (WinGet\\Packages\\<versioned> + WinGet\\Links shims), chocolatey,
    and per-user installs. PATH alone often misses winget installs because the
    shim dir is added to the *user* PATH, not the process environment of an
    elevated exe.
    """
    path = shutil.which("smartctl")
    if path:
        return path
    if not IS_WIN:
        return None
    candidates = []
    try:
        pf = os.environ.get("ProgramFiles") or r"C:\Program Files"
        pf86 = os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
        la = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/AppData/Local")
        pd = os.environ.get("ProgramData") or r"C:\ProgramData"
        candidates = [
            os.path.join(pf, "smartmontools", "bin", "smartctl.exe"),
            os.path.join(pf86, "smartmontools", "bin", "smartctl.exe"),
            os.path.join(pd, "chocolatey", "bin", "smartctl.exe"),
            os.path.join(la, "Microsoft", "WinGet", "Links", "smartctl.exe"),
            os.path.join(la, "smartmontools", "bin", "smartctl.exe"),
        ]
        # winget portable packages live in versioned folders — glob them
        wg = os.path.join(la, "Microsoft", "WinGet", "Packages")
        if os.path.isdir(wg):
            for entry in sorted(os.listdir(wg)):
                if "smartmontools" in entry.lower():
                    candidates.insert(0, os.path.join(wg, entry, "bin", "smartctl.exe"))
                    candidates.append(os.path.join(wg, entry, "smartctl.exe"))
    except Exception:
        pass
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def is_admin():
    """True if running elevated on Windows; always True elsewhere (no elevation concept enforced)."""
    if not IS_WIN:
        return True
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def normalize_key(dev):
    """Normalize a psutil disk-io key / device node to a comparable id."""
    k = dev.lower().replace("\\", "/").rstrip("/")
    k = k.rsplit("/", 1)[-1] if "/" in k else k
    # windows styles: physicaldrive0 / pd0
    m = re.match(r"^(?:physicaldrive|pd)(\d+)$", k)
    if m:
        return "pd:%s" % m.group(1)
    # linux: strip partition suffixes sda3 -> sda, nvme0n1p2 -> nvme0n1
    m = re.match(r"^((?:nvme|mmcblk)\d+n\d+|sd[a-z]+|vd[a-z]+|hd[a-z]+)\d*$", k)
    if m:
        return m.group(1)
    return k


def norm_serial(s):
    """Normalize a serial number for cross-source matching.

    Windows PowerShell renders NVMe serials like '6479_A78A_6F00_09F6.'
    (underscores + trailing dot) while smartctl reports '6479A78A6F0009F6'.
    Stripping everything but [A-Z0-9] makes both sources comparable.
    """
    if not s:
        return ""
    return re.sub(r"[^A-Za-z0-9]", "", str(s)).upper()


def partition_parent(dev):
    """Map a partition io key to its parent disk key if it looks like a partition."""
    k = dev.lower()
    m = re.match(r"^((?:nvme|mmcblk)\d+n\d+)p\d+$", k)
    if m:
        return m.group(1)
    m = re.match(r"^(sd[a-z]+|vd[a-z]+|hd[a-z]+|xvd[a-z]+)\d+$", k)
    if m:
        return m.group(1)
    return None


def fmt_bytes(n, per_sec=False):
    """Human-readable bytes for API responses (frontend mostly formats itself)."""
    if n is None:
        return "—"
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < step:
            s = ("%d" % n) if unit == "B" else ("%.1f" % n)
            return s + (" %s/s" % unit if per_sec else " " + unit)
        n /= step
    return "%.1f EB%s" % (n, "/s" if per_sec else "")
