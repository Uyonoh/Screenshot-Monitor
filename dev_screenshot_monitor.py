#!/usr/bin/env python3
"""
dev_screenshot_monitor.py

Features:
- Capture active window screenshots at a configurable interval.
- Activity detection: only capture if there was keyboard/mouse activity within `activity_window_sec`.
- Metadata saved as JSON alongside each screenshot.
- Organizes files into base_dir/screenshots/YYYY-MM/YYYY-MM-DD/
- CLI with start/stop/status/pause/resume/capture-once
- 'start' spawns a detached background process (on Windows).
"""

import os
import sys
import time
import json
import threading
import subprocess
import logging
from datetime import datetime
from pathlib import Path
import traceback

# External libs
try:
    import mss
    from PIL import Image
    import click
    import psutil
    import win32gui
    import win32con
    import win32process
    import ctypes
    from pynput import keyboard, mouse
except Exception as e:
    print("Missing dependency at import:", e)
    print("Run: pip install mss pillow click psutil pywin32 pynput")
    raise

# ----------------------------
# Configuration (editable)
# ----------------------------
BASE_DIR = Path.home() / "dev_screencaps"        # base directory to store screenshots & metadata
SCREENSHOT_SUBDIR = "screenshots"
INTERVAL_SEC = 30                                # default capture interval
ACTIVITY_WINDOW_SEC = 60                         # capture only if user active within last N seconds
JPEG_QUALITY = 85                                # jpeg quality (1-95)
ALLOWED_WINDOW_KEYWORDS = [                      # only capture if window title contains any of these (case-insensitive). Empty = allow all.
    "vscode", "code", "pycharm", "terminal", "cmd.exe", "powershell", "powershell", "windows terminal",
    "notepad", "sublime", "intellij", "chrome", "firefox", "edge"
]
BLACKLIST_WINDOW_KEYWORDS = [                    # if title contains any of these, skip capture
    "password", "1password", "keepass", "bank", "gmail", "inbox", "zoom", "teams", "slack", "discord"
]
PIDFILE = BASE_DIR / "monitor.pid"
CONTROL_DIR = BASE_DIR / "control"               # files here: paused (exists => paused)
LOGFILE = BASE_DIR / "monitor.log"

# ----------------------------
# Logging
# ----------------------------
os.makedirs(BASE_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOGFILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)

# ----------------------------
# Activity tracking
# ----------------------------
_last_activity_ts = time.time()

def _on_mouse_event(*args):
    global _last_activity_ts
    _last_activity_ts = time.time()

def _on_key_event(*args):
    global _last_activity_ts
    _last_activity_ts = time.time()

_mouse_listener = None
_key_listener = None

def start_activity_listeners():
    global _mouse_listener, _key_listener
    if _mouse_listener is None:
        _mouse_listener = mouse.Listener(on_move=_on_mouse_event, on_click=_on_mouse_event, on_scroll=_on_mouse_event)
        _mouse_listener.daemon = True
        _mouse_listener.start()
    if _key_listener is None:
        _key_listener = keyboard.Listener(on_press=_on_key_event)
        _key_listener.daemon = True
        _key_listener.start()

# ----------------------------
# Helpers: active window info
# ----------------------------
def get_foreground_window_info():
    try:
        hwnd = win32gui.GetForegroundWindow()
        rect = win32gui.GetWindowRect(hwnd)  # left, top, right, bottom
        title = win32gui.GetWindowText(hwnd)
        if not title:
            title = "<no-title>"
        # get process id
        tid, pid = win32process.GetWindowThreadProcessId(hwnd)
        proc_name = None
        proc_cmdline = None
        try:
            proc = psutil.Process(pid)
            proc_name = proc.name()
            proc_cmdline = " ".join(proc.cmdline())
        except Exception:
            proc_name = f"pid:{pid}"
            proc_cmdline = ""
        return {
            "hwnd": hwnd,
            "rect": rect,
            "title": title,
            "pid": pid,
            "proc_name": proc_name,
            "proc_cmdline": proc_cmdline
        }
    except Exception:
        logging.exception("Failed to get foreground window info")
        return None

# ----------------------------
# Git info helper (optional)
# ----------------------------
def get_git_info_from_path(path):
    """_summary_

    Args:
        path (str): _description_

    Returns:
        pathlib.Path: _description_
    """    
    # Attempt to get git branch and commit if inside repo
    try:
        # find nearest .git by walking upwards
        p = Path(path)
        for parent in [p] + list(p.parents):
            if (parent / ".git").exists():
                # run git commands
                branch = subprocess.check_output(["git", "-C", str(parent), "rev-parse", "--abbrev-ref", "HEAD"], stderr=subprocess.DEVNULL).decode().strip()
                commit = subprocess.check_output(["git", "-C", str(parent), "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL).decode().strip()
                return {"git_repo_root": str(parent), "git_branch": branch, "git_commit": commit}
    except Exception:
        pass
    return {}

# ----------------------------
# File & metadata helpers
# ----------------------------
def ensure_dir_for_ts(ts: datetime):
    """Ensure the filesystem directory for a given timestamp exists and return its path.
    The target directory is constructed using the module-level BASE_DIR and SCREENSHOT_SUBDIR
    and is organized by year-month and full date:
        BASE_DIR / SCREENSHOT_SUBDIR / "YYYY-MM" / "YYYY-MM-DD"
        
    Args:
    ts (datetime.datetime) :
        A timestamp used to build the directory names. Must support strftime (e.g., a
        datetime.datetime or similar object).
    Returns
    -------
    pathlib.Path
        The Path object for the created (or already existing) directory.
    Side effects
    ------------
    Creates the directory and any missing parent directories on disk using mkdir(parents=True, exist_ok=True).
    Raises
    ------
    TypeError
        If the provided ts does not support strftime.
    OSError
        If the directory cannot be created due to filesystem-related errors (permissions, disk full, etc.).
    Example
    -------
    >>> ensure_dir_for_ts(datetime.datetime(2025, 12, 02))
    PosixPath('/.../SCREENSHOT_SUBDIR/2025-12/2025-12-02')
    """
    
    y_m = ts.strftime("%Y-%m")
    y_m_d = ts.strftime("%Y-%m-%d")
    target = BASE_DIR / SCREENSHOT_SUBDIR / y_m / y_m_d
    target.mkdir(parents=True, exist_ok=True)
    return target

def write_metadata(meta: dict, path: Path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

# ----------------------------
# Capture function
# ----------------------------
def capture_active_window(save_png=False):
    info = get_foreground_window_info()
    if not info:
        return None
    title = info["title"].lower()
    # blacklist check
    for b in BLACKLIST_WINDOW_KEYWORDS:
        if b.lower() in title:
            logging.info("Skipping capture due to blacklist keyword match: %s", b)
            return None
    # allowlist check: if list not empty, require at least one match
    if ALLOWED_WINDOW_KEYWORDS:
        allowed = any(k.lower() in title for k in ALLOWED_WINDOW_KEYWORDS)
        if not allowed:
            logging.info("Skipping capture because active window didn't match allowed keywords: %s", info["title"])
            return None

    left, top, right, bottom = info["rect"]
    # sometimes windows are minimized => rect empty or weird
    if right - left <= 0 or bottom - top <= 0:
        logging.info("Invalid window rect, skipping capture: %s", info["rect"])
        return None

    ts = datetime.now()
    folder = ensure_dir_for_ts(ts)
    filebase = ts.strftime("%H-%M-%S") + "_" + sanitize_filename(info["title"])[:80]
    jpg_path = folder / f"{filebase}.jpg"
    meta_path = folder / f"{filebase}.json"

    try:
        with mss.mss() as sct:
            bbox = {"left": left, "top": top, "width": right-left, "height": bottom-top}
            sct_img = sct.grab(bbox)
            img = Image.frombytes("RGB", sct_img.size, sct_img.rgb)

            # Optionally resize if huge (not done by default) - commented out
            # max_w = 3840
            # if img.width > max_w:
            #     ratio = max_w / img.width
            #     img = img.resize((int(img.width*ratio), int(img.height*ratio)), Image.LANCZOS)

            img.save(str(jpg_path), "JPEG", quality=JPEG_QUALITY, optimize=True)

        # metadata
        meta = {
            "timestamp": ts.isoformat(),
            "file": str(jpg_path),
            "window_title": info["title"],
            "pid": info["pid"],
            "process_name": info["proc_name"],
            "process_cmdline": info["proc_cmdline"],
            "rect": info["rect"],
            "last_activity_ts": datetime.fromtimestamp(_last_activity_ts).isoformat(),
        }
        # try to add git info from proc cmdline path
        # naive heuristic: look for a path in cmdline
        try:
            cmdline = info.get("proc_cmdline") or ""
            possible_paths = []
            for token in cmdline.split():
                if os.path.exists(token):
                    possible_paths.append(token)
            if possible_paths:
                git_info = get_git_info_from_path(possible_paths[0])
                if git_info:
                    meta.update(git_info)
        except Exception:
            pass

        write_metadata(meta, meta_path)
        logging.info("Captured %s (title=%s)", jpg_path, info["title"])
        return {"image": str(jpg_path), "meta": meta}
    except Exception:
        logging.error("Failed to capture: %s", traceback.format_exc())
        return None

# ----------------------------
# Utility: sanitize filename
# ----------------------------
import re
def sanitize_filename(s: str):
    s = re.sub(r'[:/\\<>|"?*]', '-', s)
    s = re.sub(r'\s+', '_', s)
    return s

# ----------------------------
# Control helpers (pause/resume/pid)
# ----------------------------
def is_paused():
    return (CONTROL_DIR / "paused").exists()

def set_paused(flag: bool):
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    pausefile = CONTROL_DIR / "paused"
    if flag:
        pausefile.write_text("paused")
    else:
        if pausefile.exists():
            pausefile.unlink()

def write_pid(pid):
    try:
        PIDFILE.parent.mkdir(parents=True, exist_ok=True)
        PIDFILE.write_text(str(pid))
    except Exception:
        logging.exception("Failed to write pidfile")

def read_pid():
    try:
        if PIDFILE.exists():
            return int(PIDFILE.read_text().strip())
    except Exception:
        pass
    return None

def remove_pidfile():
    try:
        if PIDFILE.exists():
            PIDFILE.unlink()
    except Exception:
        pass

# ----------------------------
# Daemon runner
# ----------------------------
_stop_event = threading.Event()

def capture_loop(interval_sec=INTERVAL_SEC, activity_window_sec=ACTIVITY_WINDOW_SEC):
    logging.info("Capture loop started: interval=%s sec, activity_window=%s sec", interval_sec, activity_window_sec)
    start_activity_listeners()
    while not _stop_event.is_set():
        try:
            if is_paused():
                logging.debug("Paused; skipping capture cycle.")
            else:
                # check activity
                now = time.time()
                if (now - _last_activity_ts) <= activity_window_sec:
                    capture_active_window()
                else:
                    logging.debug("No recent activity (last_activity %s sec ago); skipping capture", int(now - _last_activity_ts))
        except Exception:
            logging.exception("Error during capture loop")
        # sleeping in small increments allows graceful shutdown
        for _ in range(int(interval_sec)):
            if _stop_event.is_set(): break
            time.sleep(1)

def run_daemon(interval_sec=INTERVAL_SEC, activity_window_sec=ACTIVITY_WINDOW_SEC):
    try:
        capture_loop(interval_sec, activity_window_sec)
    except KeyboardInterrupt:
        logging.info("Daemon interrupted")
    except Exception:
        logging.exception("Daemon crashed")
    finally:
        logging.info("Daemon exiting")

# ----------------------------
# CLI: start/stop/status/pause/resume/capture-once
# ----------------------------
@click.group()
def cli():
    pass

@cli.command()
@click.option("--interval", "-i", default=INTERVAL_SEC, help="Capture interval in seconds.")
@click.option("--activity-window", "-a", default=ACTIVITY_WINDOW_SEC, help="Activity window in seconds.")
def start(interval, activity_window):
    """Start background monitoring (detached)."""
    existing = read_pid()
    if existing:
        # check if alive
        if psutil.pid_exists(existing):
            click.echo(f"Monitor already running with PID {existing}")
            return
        else:
            remove_pidfile()

    # launch a detached worker process
    python_exe = sys.executable
    script = Path(__file__).resolve()
    args = [python_exe, str(script), "run-daemon",
            "--interval", str(interval),
            "--activity-window", str(activity_window)]
    creationflags = 0
    kwargs = {}
    if sys.platform.startswith("win"):
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        creationflags = 0x00000008 | 0x00000200
        kwargs.update(creationflags=creationflags, close_fds=True)
    # spawn
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    write_pid(proc.pid)
    click.echo(f"Monitor started (detached) with PID {proc.pid}. Logs: {LOGFILE}")

@cli.command()
def stop():
    """Stop background monitor (if running)."""
    pid = read_pid()
    if not pid:
        click.echo("Monitor not running (no pidfile).")
        return
    try:
        p = psutil.Process(pid)
        p.terminate()
        try:
            p.wait(5)
        except psutil.TimeoutExpired:
            p.kill()
        remove_pidfile()
        click.echo(f"Stopped monitor (PID {pid}).")
    except psutil.NoSuchProcess:
        remove_pidfile()
        click.echo("Monitor process not found; pidfile removed.")
    except Exception as e:
        click.echo(f"Failed to stop monitor: {e}")

@cli.command()
def status():
    """Show status."""
    pid = read_pid()
    if pid and psutil.pid_exists(pid):
        click.echo(f"Monitor running with PID {pid}. Paused: {is_paused()}")
    else:
        click.echo("Monitor not running.")

@cli.command()
def pause():
    """Pause capturing."""
    set_paused(True)
    click.echo("Monitor paused.")

@cli.command()
def resume():
    """Resume capturing."""
    set_paused(False)
    click.echo("Monitor resumed.")

@cli.command()
def capture_once():
    """Capture a single screenshot now (foreground window)."""
    start_activity_listeners()
    result = capture_active_window()
    if result:
        click.echo(f"Captured: {result['image']}")
    else:
        click.echo("No capture (window filtered or error).")

# built-in run-daemon hidden command used by start()
@cli.command(name='run-daemon', hidden=True)
@click.option("--interval", "--interval", "-i", default=INTERVAL_SEC, type=int)
@click.option("--activity-window", "--activity-window", "-a", default=ACTIVITY_WINDOW_SEC, type=int)
# @click.option("--run-daemon", is_flag=True, default=False)
def run_daemon_cmd(interval, activity_window):
    click.echo("Checking daemon.")
    if not run_daemon:
        click.echo("Use the CLI normally.")
        return
    # write pid
    pid = os.getpid()
    write_pid(pid)
    logging.info("Daemon process running with PID %s", pid)
    try:
        run_daemon(interval, activity_window)
    finally:
        remove_pidfile()
        logging.info("Daemon exited.")

# ----------------------------
# Entry point
# ----------------------------
if __name__ == "__main__":
    # If called directly with --run-daemon, argparse is handled by click above.
    cli()
