#!/usr/bin/env python3
"""tdl GUI - A web-based GUI wrapper for the tdl Telegram downloader tool.

Runs a local HTTP server that serves a web UI and executes tdl commands,
streaming their output to the browser in real time via SSE.
"""

import atexit
import http.server
import hashlib
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import queue
import time
import uuid
import webbrowser
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent.resolve()
CONFIG_FILE = BASE_DIR / "config.json"
INDEX_FILE = BASE_DIR / "index.html"
DOWNLOAD_HISTORY_FILE = BASE_DIR / ".tdl-download-history.json"
INSTANCE_FILE = BASE_DIR / ".tdl-gui-instance.json"

DEFAULT_CONFIG = {
    "tdl_path": r"D:\ruanjian\ruanjian\tdl_Windows_64bit\tdl.exe",
    "tdl_home": r"D:\file\kaifa\project\ai\telegram_dw\tdl_home",
    "namespace": "default",
    "proxy": "",
    "pool": 8,
    "limit": 2,
    "threads": 4,
    "download_dir": "downloads",
    "host": "127.0.0.1",
    "port": 8765,
}


def normalize_tdl_path(value):
    """Return a Windows-friendly executable path from a saved UI value."""
    return os.path.normpath(str(value or "").strip().strip('"'))


def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            merged = {**DEFAULT_CONFIG, **saved}
            merged["tdl_path"] = normalize_tdl_path(merged.get("tdl_path"))
            return merged
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def save_config(cfg):
    merged = {**DEFAULT_CONFIG, **cfg}
    merged["tdl_path"] = normalize_tdl_path(merged.get("tdl_path"))
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
    return merged


# ---------------------------------------------------------------------------
# Task management
# ---------------------------------------------------------------------------

class Task:
    def __init__(
        self,
        task_id,
        command,
        env,
        label="",
        cleanup_paths=None,
        on_success=None,
        initial_lines=None,
        on_finish=None,
    ):
        self.id = task_id
        self.command = command
        self.env = env
        self.label = label
        self.process = None
        self.job_handle = None
        self.output_queue = queue.Queue()
        self.status = "pending"  # pending, running, done, error, stopped
        self.exit_code = None
        self.stop_requested = False
        self.lines = list(initial_lines or [])
        self.start_time = time.time()
        self.end_time = None
        self.result = None  # optional parsed result (e.g. chat list JSON)
        self.cleanup_paths = list(cleanup_paths or [])
        self.on_success = on_success
        self.on_finish = on_finish


tasks = {}
tasks_lock = threading.Lock()
preview_files = {}
preview_files_lock = threading.Lock()
preview_streams = {}
preview_streams_lock = threading.Lock()
preview_serve_sessions = {}
preview_serve_lock = threading.Lock()
download_history_lock = threading.Lock()
_single_instance_handle = None


def _win_api():
    """Return the small Win32 API surface used for process lifetime control."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return ctypes, wintypes, kernel32


def _attach_kill_job(process):
    """Put a child process in a job that dies when this GUI process exits."""
    api = _win_api()
    if not api:
        return None
    ctypes, wintypes, kernel32 = api

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = ExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
    configured = kernel32.SetInformationJobObject(
        job, 9, ctypes.byref(info), ctypes.sizeof(info)
    )
    assigned = configured and kernel32.AssignProcessToJobObject(
        job, wintypes.HANDLE(int(process._handle))
    )
    if not assigned:
        kernel32.CloseHandle(job)
        return None
    return job


def _close_win_handle(handle):
    if not handle:
        return
    api = _win_api()
    if api:
        api[2].CloseHandle(handle)


def acquire_single_instance():
    """Prevent two GUI servers from competing for the same tdl database."""
    global _single_instance_handle
    api = _win_api()
    if not api:
        return True
    ctypes, _, kernel32 = api
    name_hash = hashlib.sha256(str(BASE_DIR).casefold().encode("utf-8")).hexdigest()[:20]
    handle = kernel32.CreateMutexW(None, False, f"Local\\tdl-gui-{name_hash}")
    if not handle:
        return True
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return False
    _single_instance_handle = handle
    return True


def _process_exists(pid):
    try:
        pid = int(pid)
        if pid <= 0:
            return False
        if sys.platform == "win32":
            api = _win_api()
            if not api:
                return False
            _, wintypes, kernel32 = api
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            kernel32.CloseHandle(handle)
            return True
        os.kill(pid, 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _find_listening_pid(port):
    """Best-effort fallback for instances started before PID files existed."""
    if sys.platform != "win32":
        return None
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=8,
        )
        suffix = f":{int(port)}"
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) < 5 or fields[0].upper() != "TCP":
                continue
            if fields[1].endswith(suffix) and fields[3].upper() == "LISTENING":
                return int(fields[4])
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        pass
    return None


def read_instance_info(cfg=None):
    stale_file = False
    try:
        with INSTANCE_FILE.open("r", encoding="utf-8") as stream:
            info = json.load(stream)
        if isinstance(info, dict) and _process_exists(info.get("pid")):
            return info
        stale_file = True
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        stale_file = INSTANCE_FILE.exists()
    if stale_file:
        try:
            INSTANCE_FILE.unlink(missing_ok=True)
        except OSError:
            pass
    cfg = cfg or load_config()
    port = int(cfg.get("port", 8765))
    pid = _find_listening_pid(port)
    if not pid:
        return {"port": port}
    return {
        "pid": pid,
        "host": cfg.get("host", "127.0.0.1"),
        "port": port,
    }


def write_instance_info(host, port):
    info = {
        "pid": os.getpid(),
        "host": host,
        "port": port,
        "url": f"http://{host}:{port}",
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    temp_path = INSTANCE_FILE.with_suffix(INSTANCE_FILE.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as stream:
        json.dump(info, stream, ensure_ascii=False, indent=2)
    os.replace(temp_path, INSTANCE_FILE)
    return info


def remove_instance_info():
    try:
        with INSTANCE_FILE.open("r", encoding="utf-8") as stream:
            info = json.load(stream)
        if int(info.get("pid", -1)) != os.getpid():
            return
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return
    INSTANCE_FILE.unlink(missing_ok=True)


def print_existing_instance(info):
    pid = info.get("pid")
    host = info.get("host", "127.0.0.1")
    port = info.get("port")
    url = info.get("url") or (f"http://{host}:{port}" if port else "")
    print("[Error] tdl GUI 已经在运行，请使用现有窗口。")
    if pid:
        print(f"        PID: {pid}")
    if url:
        print(f"        访问地址: {url}")
    if info.get("started_at"):
        print(f"        启动时间: {info['started_at']}")
    print()
    if pid:
        print("如需强制终止，请确认没有正在下载的任务，然后执行：")
        print(f"  PowerShell: Stop-Process -Id {pid} -Force")
        print(f"  CMD:        taskkill /PID {pid} /T /F")
    elif port:
        print("未能确定 PID，可在 PowerShell 中执行：")
        print(f"  Get-NetTCPConnection -LocalPort {port} -State Listen | Select-Object OwningProcess")

# ANSI / control-character stripper
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][AB0]|\x1b[=>]"
)


def strip_ansi(text):
    text = _ANSI_RE.sub("", text)
    text = text.replace("\r", "")
    return text


def _run_task(task):
    """Worker thread: run the subprocess and push output lines to the queue."""
    task.status = "running"
    try:
        if task.stop_requested:
            task.status = "stopped"
            task.exit_code = -2
            return
        task.process = subprocess.Popen(
            task.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=task.env,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        task.job_handle = _attach_kill_job(task.process)
        for line in iter(task.process.stdout.readline, ""):
            clean = strip_ansi(line).rstrip("\n")
            task.lines.append(clean)
            task.output_queue.put(("line", clean))
        task.process.wait()
        task.exit_code = task.process.returncode
        if task.stop_requested:
            task.status = "stopped"
        else:
            task.status = "done" if task.exit_code == 0 else "error"
        if task.status == "done" and task.on_success:
            task.result = task.on_success()
            if isinstance(task.result, dict) and task.result.get("message"):
                message = str(task.result["message"])
                task.lines.append(message)
                task.output_queue.put(("line", message))
    except FileNotFoundError:
        msg = f"[Error] tdl executable not found: {task.command[0]}"
        task.lines.append(msg)
        task.output_queue.put(("line", msg))
        task.status = "error"
        task.exit_code = -1
    except Exception as e:
        msg = f"[Error] {e}"
        task.lines.append(msg)
        task.output_queue.put(("line", msg))
        task.status = "error"
        task.exit_code = -1
    finally:
        _close_win_handle(task.job_handle)
        task.job_handle = None
        task.end_time = time.time()
        if task.on_finish:
            try:
                finish_result = task.on_finish(task.status, task.exit_code)
                if finish_result is not None:
                    task.result = finish_result
                if isinstance(finish_result, dict) and finish_result.get("message"):
                    message = str(finish_result["message"])
                    task.lines.append(message)
                    task.output_queue.put(("line", message))
            except Exception as exc:
                task.lines.append(f"[下载记录] 回填失败：{exc}")
                task.output_queue.put(("line", task.lines[-1]))
        task.output_queue.put(("end", {"status": task.status, "exit_code": task.exit_code}))
        for path in task.cleanup_paths:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass


def create_task(
    command,
    env,
    label="",
    cleanup_paths=None,
    on_success=None,
    initial_lines=None,
    on_finish=None,
):
    # Online preview servers keep tdl's storage open; stop them before a normal task.
    stop_preview_servers()
    task_id = uuid.uuid4().hex[:12]
    task = Task(
        task_id,
        command,
        env,
        label,
        cleanup_paths,
        on_success,
        initial_lines,
        on_finish,
    )
    with tasks_lock:
        active = next((t for t in tasks.values() if t.status in ("pending", "running")), None)
        if active:
            return None, f"已有任务正在运行：{active.label or active.id}，请先停止或等待结束"
        tasks[task_id] = task
    t = threading.Thread(target=_run_task, args=(task,), daemon=True)
    t.start()
    return task_id, None


def create_completed_task(command, label="", lines=None, result=None):
    """Create an already-completed task so the browser can consume normal SSE."""
    stop_preview_servers()
    task_id = uuid.uuid4().hex[:12]
    task = Task(task_id, command, {}, label, initial_lines=lines)
    task.status = "done"
    task.exit_code = 0
    task.end_time = time.time()
    task.result = result
    with tasks_lock:
        active = next((t for t in tasks.values() if t.status in ("pending", "running")), None)
        if active:
            return None, f"已有任务正在运行：{active.label or active.id}，请先停止或等待结束"
        tasks[task_id] = task
    return task_id, None


def stop_task_process(task):
    if not task:
        return False, "task not running"
    task.stop_requested = True
    if not task.process:
        if task.status in ("pending", "running"):
            return True, "正在停止任务"
        return True, "任务已结束"
    process = task.process
    if process.poll() is not None:
        return True, "任务已结束"
    pid = process.pid
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode == 0 or process.poll() is not None:
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
                return True, "任务已停止"
            try:
                process.terminate()
                process.wait(timeout=3)
                return True, "任务已停止"
            except (OSError, subprocess.TimeoutExpired):
                pass
            return False, "停止任务失败，请稍后重试"
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        return True, "任务已停止"
    except Exception as exc:
        if process.poll() is not None:
            return True, "任务已结束"
        return False, "停止任务失败，请稍后重试"


def stop_all_tasks():
    """Best-effort cleanup for normal shutdown; job handles cover hard exits."""
    with tasks_lock:
        active = [t for t in tasks.values() if t.status in ("pending", "running")]
    for task in active:
        stop_task_process(task)


# ---------------------------------------------------------------------------
# Command builders
# ---------------------------------------------------------------------------

def build_env(cfg):
    env = os.environ.copy()
    env["USERPROFILE"] = cfg.get("tdl_home", "")
    return env


def _global_args(cfg, body=None):
    """Return global tdl flags derived from config / per-request overrides."""
    body = body or {}
    args = ["--disable-progress-ps"]
    proxy = body.get("proxy") or cfg.get("proxy", "")
    if proxy:
        args += ["--proxy", proxy]
    ns = body.get("namespace") or cfg.get("namespace", "default")
    if ns and ns != "default":
        args += ["-n", ns]
    pool = body.get("pool", cfg.get("pool", 8))
    try:
        if int(pool) != 8:
            args += ["--pool", str(int(pool))]
    except (ValueError, TypeError):
        pass
    limit = body.get("limit", cfg.get("limit", 2))
    try:
        args += ["--limit", str(int(limit))]
    except (ValueError, TypeError):
        pass
    threads = body.get("threads", cfg.get("threads", 4))
    try:
        args += ["--threads", str(int(threads))]
    except (ValueError, TypeError):
        pass
    return args


def _tdl(cfg):
    return [normalize_tdl_path(cfg.get("tdl_path"))]


# -- Login -----------------------------------------------------------------

def cmd_login(cfg, body):
    cmd = _tdl(cfg) + ["login"]
    login_type = body.get("type", "desktop")
    cmd += ["-T", login_type]
    if login_type == "desktop":
        desktop = body.get("desktop_path", "")
        if desktop:
            cmd += ["-d", desktop]
        passcode = body.get("passcode", "")
        if passcode:
            cmd += ["-p", passcode]
    cmd += _global_args(cfg, body)
    return cmd, f"Login ({login_type})"


# -- Chat list --------------------------------------------------------------

def cmd_chats(cfg, body):
    cmd = _tdl(cfg) + ["chat", "ls", "--output", "json"]
    filt = body.get("filter", "")
    if filt and filt != "true":
        cmd += ["-f", filt]
    cmd += _global_args(cfg, body)
    return cmd, "List chats"


# -- Chat export ------------------------------------------------------------

def cmd_export(cfg, body):
    cmd = _tdl(cfg) + ["chat", "export"]
    chat = body.get("chat", "")
    if chat:
        cmd += ["-c", chat]
    exp_type = body.get("type", "time")
    cmd += ["-T", exp_type]
    inp = body.get("input", "")
    if inp:
        cmd += ["-i", str(inp)]
    output = body.get("output", "tdl-export.json")
    cmd += ["-o", output]
    if body.get("all"):
        cmd += ["--all"]
    if body.get("with_content"):
        cmd += ["--with-content"]
    if body.get("raw"):
        cmd += ["--raw"]
    topic = body.get("topic")
    if topic:
        cmd += ["--topic", str(topic)]
    reply = body.get("reply")
    if reply:
        cmd += ["--reply", str(reply)]
    filt = body.get("filter", "")
    if filt and filt != "true":
        cmd += ["-f", filt]
    cmd += _global_args(cfg, body)
    return cmd, f"Export ({exp_type}) from {chat or 'Saved Messages'}"


# -- Download ---------------------------------------------------------------

def cmd_download(cfg, body):
    cmd = _tdl(cfg) + ["download"]
    mode = body.get("mode", "url")
    if mode == "url":
        urls = body.get("urls", [])
        if isinstance(urls, str):
            urls = [u.strip() for u in urls.split("\n") if u.strip()]
        for u in urls:
            cmd += ["-u", u]
    files = body.get("files", [])
    if isinstance(files, str):
        files = [f.strip() for f in files.split("\n") if f.strip()]
    for f in files:
        cmd += ["-f", f]

    dl_dir = body.get("dir") or cfg.get("download_dir", "downloads")
    cmd += ["-d", dl_dir]

    inc = body.get("include", "")
    if inc:
        cmd += ["-i", inc]
    exc = body.get("exclude", "")
    if exc:
        cmd += ["-e", exc]

    template = body.get("template", "")
    if template:
        cmd += ["--template", template]

    for flag in ("group", "desc", "skip_same", "rewrite_ext", "takeout", "continue", "restart"):
        if body.get(flag):
            cmd += [f"--{flag.replace('_', '-')}"]

    if body.get("serve"):
        cmd += ["--serve"]
        port = body.get("port", 8080)
        cmd += ["--port", str(port)]

    cmd += _global_args(cfg, body)
    return cmd, "Download"


# -- Forward ----------------------------------------------------------------

def cmd_forward(cfg, body):
    cmd = _tdl(cfg) + ["forward"]
    sources = body.get("from", [])
    if isinstance(sources, str):
        sources = [s.strip() for s in sources.split("\n") if s.strip()]
    for s in sources:
        cmd += ["--from", s]
    to = body.get("to", "")
    if to:
        cmd += ["--to", to]
    mode = body.get("mode", "direct")
    cmd += ["--mode", mode]
    for flag in ("desc", "dry_run", "silent", "single"):
        if body.get(flag):
            cmd += [f"--{flag.replace('_', '-')}"]
    edit = body.get("edit", "")
    if edit:
        cmd += ["--edit", edit]
    cmd += _global_args(cfg, body)
    return cmd, "Forward"


# -- Upload -----------------------------------------------------------------

def cmd_upload(cfg, body):
    cmd = _tdl(cfg) + ["upload"]
    paths = body.get("paths", [])
    if isinstance(paths, str):
        paths = [p.strip() for p in paths.split("\n") if p.strip()]
    for p in paths:
        cmd += ["-p", p]
    chat = body.get("chat", "")
    if chat:
        cmd += ["-c", chat]
    topic = body.get("topic")
    if topic:
        cmd += ["--topic", str(topic)]
    inc = body.get("include", "")
    if inc:
        cmd += ["-i", inc]
    exc = body.get("exclude", "")
    if exc:
        cmd += ["-e", exc]
    if body.get("photo"):
        cmd += ["--photo"]
    if body.get("rm"):
        cmd += ["--rm"]
    caption = body.get("caption", "")
    if caption:
        cmd += ["--caption", caption]
    cmd += _global_args(cfg, body)
    return cmd, "Upload"


# -- Users ------------------------------------------------------------------

def cmd_users(cfg, body):
    cmd = _tdl(cfg) + ["chat", "users"]
    chat = body.get("chat", "")
    if chat:
        cmd += ["-c", chat]
    output = body.get("output", "tdl-users.json")
    cmd += ["-o", output]
    if body.get("raw"):
        cmd += ["--raw"]
    cmd += _global_args(cfg, body)
    return cmd, f"Export users from {chat or '(none)'}"


# -- Version / status -------------------------------------------------------

def get_version(cfg):
    """Run `tdl version` synchronously and return the output."""
    try:
        env = build_env(cfg)
        result = subprocess.run(
            [normalize_tdl_path(cfg.get("tdl_path")), "version"],
            capture_output=True,
            text=True,
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=10,
        )
        out = (result.stdout or "").strip()
        # first three lines are version info
        lines = out.splitlines()[:3]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def check_login(cfg):
    """Check whether session data exists in the tdl home."""
    home = Path(cfg.get("tdl_home", ""))
    data_dir = home / ".tdl" / "data"
    if data_dir.exists():
        files = list(data_dir.iterdir())
        return len(files) > 0
    return False


# ---------------------------------------------------------------------------
# Export preview helpers
# ---------------------------------------------------------------------------

def _resolve_user_path(value):
    path = Path(os.path.expandvars(os.path.expanduser(str(value or ""))))
    if not path.is_absolute():
        path = BASE_DIR / path
    return path.resolve()


def _export_groups(payload):
    if isinstance(payload, dict) and isinstance(payload.get("messages"), list):
        return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict) and isinstance(item.get("messages"), list)]
    if isinstance(payload, dict):
        for key in ("data", "chats", "dialogs"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict) and isinstance(item.get("messages"), list)]
    return []


def _empty_download_history():
    return {"version": 1, "items": {}}


def _load_download_history_unlocked():
    if not DOWNLOAD_HISTORY_FILE.is_file():
        return _empty_download_history()
    try:
        with DOWNLOAD_HISTORY_FILE.open("r", encoding="utf-8-sig") as stream:
            payload = json.load(stream)
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), dict):
            return _empty_download_history()
        return {"version": 1, "items": payload["items"]}
    except (OSError, ValueError, json.JSONDecodeError):
        return _empty_download_history()


def load_download_history():
    with download_history_lock:
        return _load_download_history_unlocked()


def download_history_summary():
    history = load_download_history()
    return {
        "count": len(history["items"]),
        "path": str(DOWNLOAD_HISTORY_FILE),
    }


def clear_download_history():
    with download_history_lock:
        DOWNLOAD_HISTORY_FILE.unlink(missing_ok=True)
    return {"count": 0, "path": str(DOWNLOAD_HISTORY_FILE)}


def _download_history_key(group, message):
    chat_id = group.get("id") or group.get("chat_id") or group.get("dialog_id")
    message_id = message.get("id") or message.get("message_id")
    if chat_id is not None and message_id is not None:
        return f"telegram:{chat_id}:{message_id}"
    stable = json.dumps(
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "file": _message_file(message),
            "message": message,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "sha256:" + hashlib.sha256(stable.encode("utf-8")).hexdigest()


def _parse_extensions(value):
    if isinstance(value, (list, tuple, set)):
        parts = value
    else:
        parts = str(value or "").split(",")
    return {str(part).strip().lstrip(".").casefold() for part in parts if str(part).strip()}


def _message_matches_download_filters(message, body):
    filename = _message_file(message)
    if not filename:
        return False
    extension = Path(filename).suffix.lstrip(".").casefold()
    included = _parse_extensions(body.get("include"))
    excluded = _parse_extensions(body.get("exclude"))
    if included and extension not in included:
        return False
    if excluded and extension in excluded:
        return False
    return True


def _history_record(group, message, source):
    return {
        "key": _download_history_key(group, message),
        "chat_id": group.get("id") or group.get("chat_id") or group.get("dialog_id"),
        "message_id": message.get("id") or message.get("message_id"),
        "filename": _message_file(message),
        "source": str(source),
    }


def _download_file_index(download_dir):
    """Index completed files using tdl's default chat/message filename form."""
    root = _resolve_user_path(download_dir or "downloads")
    exact = {}
    by_message = {}
    by_dialog_message = {}
    if not root.is_dir():
        return exact, by_message, by_dialog_message
    try:
        paths = root.rglob("*")
        for path in paths:
            try:
                if not path.is_file() or path.suffix.casefold() in {".tmp", ".part", ".temp"}:
                    continue
                if path.stat().st_size <= 0:
                    continue
            except OSError:
                continue
            name = path.name.casefold()
            exact.setdefault(name, []).append(path)
            parts = path.name.split("_", 2)
            if len(parts) < 3 or not parts[1].lstrip("-").isdigit():
                continue
            message_id = parts[1]
            by_message.setdefault(message_id, []).append(path)
            by_dialog_message.setdefault((parts[0], message_id), []).append(path)
    except OSError:
        return exact, by_message, by_dialog_message
    return exact, by_message, by_dialog_message


def _records_with_existing_files(records, download_dir):
    """Return records whose final (not .tmp) files already exist on disk."""
    exact, by_message, by_dialog_message = _download_file_index(download_dir)
    matched = []
    for record in records:
        filename = Path(str(record.get("filename") or "")).name.casefold()
        if not filename:
            continue
        candidates = list(exact.get(filename, []))
        message_id = str(record.get("message_id") or "")
        chat_id = str(record.get("chat_id") or "")
        if not candidates and message_id:
            candidates = list(by_dialog_message.get((chat_id, message_id), []))
            if not candidates:
                candidates = list(by_message.get(message_id, []))
            if len(candidates) > 1:
                extension = Path(filename).suffix.casefold()
                same_extension = [item for item in candidates if item.suffix.casefold() == extension]
                if same_extension:
                    candidates = same_extension
        if candidates:
            matched.append(record)
    return matched


def record_download_history(records):
    unique = {record["key"]: dict(record) for record in records}
    recorded_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with download_history_lock:
        history = _load_download_history_unlocked()
        for key, record in unique.items():
            record.pop("key", None)
            record["recorded_at"] = recorded_at
            history["items"][key] = record
        DOWNLOAD_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp_path = DOWNLOAD_HISTORY_FILE.with_name(
            DOWNLOAD_HISTORY_FILE.name + f".{uuid.uuid4().hex}.tmp"
        )
        try:
            with temp_path.open("w", encoding="utf-8") as stream:
                json.dump(history, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, DOWNLOAD_HISTORY_FILE)
        finally:
            temp_path.unlink(missing_ok=True)
    count = len(unique)
    total = len(history["items"])
    return {
        "recorded": count,
        "total": total,
        "message": f"[下载记录] 已持久化 {count} 条，本地文件被移走后仍会跳过。",
    }


def prepare_history_download(body):
    """Filter previously completed messages out of tdl export JSON inputs."""
    prepared_body = dict(body)
    if body.get("mode", "url") != "file":
        return prepared_body, [], [], {"enabled": False, "skipped": 0, "pending": 0}

    files = body.get("files", [])
    if isinstance(files, str):
        files = [item.strip() for item in files.splitlines() if item.strip()]
    if not files:
        return prepared_body, [], [], {"enabled": True, "skipped": 0, "pending": 0}

    # Continue should keep the persistent-history filter. Only an explicit
    # restart is allowed to send previously recorded messages to tdl again.
    bypass_history = bool(body.get("restart"))
    prepared_files = []
    cleanup_paths = []
    records = []
    skipped = 0
    supported_sources = 0
    sources = []
    candidates = []

    for value in files:
        source_path = _resolve_user_path(value)
        if not source_path.is_file():
            raise FileNotFoundError(f"Export JSON not found: {source_path}")
        with source_path.open("r", encoding="utf-8-sig") as stream:
            payload = json.load(stream)
        groups = _export_groups(payload)
        if not groups:
            prepared_files.append(str(source_path))
            continue

        supported_sources += 1
        sources.append((source_path, payload, groups))
        if not bypass_history:
            for group in groups:
                for message in group.get("messages", []):
                    if isinstance(message, dict) and _message_matches_download_filters(message, body):
                        candidates.append(_history_record(group, message, source_path))

    if candidates and not bypass_history:
        existing = _records_with_existing_files(candidates, body.get("dir") or "downloads")
        if existing:
            record_download_history(existing)
    history_items = load_download_history()["items"]

    for source_path, payload, groups in sources:
        source_pending = 0
        source_selected = 0
        for group in groups:
            kept_messages = []
            for message in group.get("messages", []):
                if not isinstance(message, dict) or not _message_matches_download_filters(message, body):
                    kept_messages.append(message)
                    continue
                source_selected += 1
                record = _history_record(group, message, source_path)
                if not bypass_history and record["key"] in history_items:
                    skipped += 1
                    continue
                records.append(record)
                source_pending += 1
                kept_messages.append(message)
            group["messages"] = kept_messages

        if bypass_history:
            prepared_files.append(str(source_path))
        elif source_pending:
            temp_path = BASE_DIR / f".download-history-{uuid.uuid4().hex}.json"
            with temp_path.open("w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False)
            prepared_files.append(str(temp_path))
            cleanup_paths.append(temp_path)
        elif source_selected == 0:
            prepared_files.append(str(source_path))

    prepared_body["files"] = prepared_files
    stats = {
        "enabled": supported_sources > 0,
        "skipped": skipped,
        "pending": len(records),
        "all_skipped": supported_sources > 0 and skipped > 0 and not prepared_files,
        "bypassed": bypass_history,
    }
    return prepared_body, records, cleanup_paths, stats


def create_download_task(cfg, body):
    prepared_body, records, cleanup_paths, stats = prepare_history_download(body)
    if prepared_body.get("mode", "url") == "file" and not prepared_body.get("restart"):
        prepared_body["skip_same"] = True
    command, label = cmd_download(cfg, prepared_body)
    initial_lines = []
    if stats.get("enabled"):
        if stats.get("bypassed"):
            initial_lines.append(
                f"[下载记录] 已按“重新开始”要求绕过历史跳过规则，本次跟踪 {stats['pending']} 条。"
            )
        else:
            initial_lines.append(
                f"[下载记录] 已跳过 {stats['skipped']} 条历史记录，待下载 {stats['pending']} 条。"
            )

    if stats.get("all_skipped"):
        display_command, _ = cmd_download(cfg, body)
        lines = initial_lines + ["[下载记录] 所有项目均已下载过，本次无需重新下载。"]
        task_id, error = create_completed_task(
            display_command,
            label,
            lines,
            {"history": stats},
        )
        return task_id, error, display_command, stats

    def finalize_download(status, exit_code):
        # Verify the final files for every exit status. A stopped or failed
        # task may have completed some files, while a successful task should
        # never mark a missing output as downloaded.
        completed = _records_with_existing_files(
            records,
            prepared_body.get("dir") or "downloads",
        )
        if not completed:
            return {"recorded": 0, "total": download_history_summary()["count"]}
        result = record_download_history(completed)
        result["message"] = (
            f"[下载记录] 本次任务结束（退出码 {exit_code}），已确认并记录 {result['recorded']} 个已完成文件。"
        )
        return result

    task_id, error = create_task(
        command,
        build_env(cfg),
        label,
        cleanup_paths,
        initial_lines=initial_lines,
        on_finish=finalize_download if records else None,
    )
    if error:
        for path in cleanup_paths:
            Path(path).unlink(missing_ok=True)
    return task_id, error, command, stats


def _message_file(message):
    value = message.get("file") or message.get("File") or message.get("file_name")
    if isinstance(value, dict):
        value = value.get("name") or value.get("file_name") or value.get("path")
    return str(value or "").strip()


def _media_kind(filename):
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    if mime.startswith("image/"):
        return "image", mime
    if mime.startswith("video/"):
        return "video", mime
    if mime.startswith("audio/"):
        return "audio", mime
    return "file", mime


def _preview_file_index(roots):
    """Index local files by their original name and tdl's message-based name."""
    exact = {}
    by_message = {}
    by_dialog_message = {}
    visited_roots = set()
    for value in roots:
        root = _resolve_user_path(value)
        root_key = str(root).casefold()
        if root_key in visited_roots:
            continue
        visited_roots.add(root_key)
        source_exact, source_by_message, source_by_dialog_message = _download_file_index(root)
        for key, paths in source_exact.items():
            exact.setdefault(key, []).extend(paths)
        for key, paths in source_by_message.items():
            by_message.setdefault(key, []).extend(paths)
        for key, paths in source_by_dialog_message.items():
            by_dialog_message.setdefault(key, []).extend(paths)
    return exact, by_message, by_dialog_message


def _match_preview_file(item, index):
    exact, by_message, by_dialog_message = index
    filename = Path(str(item.get("filename") or "")).name.casefold()
    if not filename:
        return None
    candidates = list(exact.get(filename, []))
    if not candidates:
        message_id = str(item.get("message_id") or "")
        chat_id = str(item.get("chat_id") or "")
        if message_id and chat_id:
            candidates = list(by_dialog_message.get((chat_id, message_id), []))
        if not candidates and message_id:
            candidates = list(by_message.get(message_id, []))
    if len(candidates) > 1:
        extension = Path(filename).suffix.casefold()
        same_extension = [path for path in candidates if path.suffix.casefold() == extension]
        if same_extension:
            candidates = same_extension
    return candidates[0] if candidates else None


def _find_downloaded_files(filenames, roots):
    """Find files using their exact names (kept for callers outside preview)."""
    wanted = {Path(name).name.casefold() for name in filenames if name}
    exact, _, _ = _preview_file_index(roots)
    return {name: paths[0] for name in wanted if (paths := exact.get(name))}


def _preview_stream_token(export_path, item):
    stable = "|".join(
        [
            str(export_path),
            str(item.get("chat_id") or ""),
            str(item.get("message_id") or ""),
            str(item.get("filename") or ""),
        ]
    )
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:24]


def _register_preview_stream(export_path, item):
    token = _preview_stream_token(export_path, item)
    with preview_streams_lock:
        preview_streams[token] = {
            "export_path": export_path,
            "chat_id": item.get("chat_id"),
            "message_id": item.get("message_id"),
            "filename": Path(str(item.get("filename") or "media")).name,
            "mime": item.get("mime") or "application/octet-stream",
        }
    return token


def _terminate_preview_process(process):
    if not process or process.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
    except (OSError, subprocess.SubprocessError):
        pass


def stop_preview_servers():
    with preview_serve_lock:
        sessions = list(preview_serve_sessions.values())
        preview_serve_sessions.clear()
    for session in sessions:
        _terminate_preview_process(session.get("process"))


def _ensure_preview_server(export_path, cfg):
    key = str(export_path).casefold()
    with preview_serve_lock:
        existing = preview_serve_sessions.get(key)
        if existing and existing["process"].poll() is None:
            return existing["port"]
        if existing:
            preview_serve_sessions.pop(key, None)

        port = find_port("127.0.0.1", 18888)
        command = _tdl(cfg) + [
            "download",
            "--serve",
            "-f",
            str(export_path),
            "--port",
            str(port),
        ] + _global_args(cfg)
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        try:
            process = subprocess.Popen(
                command,
                env=build_env(cfg),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise RuntimeError(f"无法启动在线预览服务：{exc}") from exc
        preview_serve_sessions[key] = {
            "process": process,
            "port": port,
            "export_path": export_path,
        }

    deadline = time.time() + 8
    while time.time() < deadline:
        if process.poll() is not None:
            with preview_serve_lock:
                preview_serve_sessions.pop(key, None)
            raise RuntimeError("在线预览服务启动失败，请检查 tdl 登录状态和日志")
        try:
            import socket

            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return port
        except OSError:
            time.sleep(0.08)
    _terminate_preview_process(process)
    with preview_serve_lock:
        preview_serve_sessions.pop(key, None)
    raise RuntimeError("在线预览服务启动超时")


def build_preview(export_file, download_dir, cfg=None):
    export_path = _resolve_user_path(export_file)
    if not export_path.is_file():
        raise FileNotFoundError(f"Export JSON not found: {export_path}")
    with export_path.open("r", encoding="utf-8-sig") as stream:
        payload = json.load(stream)
    groups = _export_groups(payload)
    if not groups:
        raise ValueError("The JSON file does not contain a supported tdl message list")

    history_items = load_download_history()["items"]
    items = []
    for group_index, group in enumerate(groups):
        chat_id = group.get("id") or group.get("chat_id") or group.get("dialog_id")
        for message_index, message in enumerate(group["messages"]):
            if not isinstance(message, dict):
                continue
            filename = _message_file(message)
            if not filename:
                continue
            kind, mime = _media_kind(filename)
            items.append({
                "chat_id": chat_id,
                "message_id": message.get("id") or message.get("message_id"),
                "group_index": group_index,
                "message_index": message_index,
                "filename": filename,
                "kind": kind,
                "mime": mime,
                "text": message.get("content") or message.get("text") or message.get("caption") or "",
                "history_recorded": _download_history_key(group, message) in history_items,
            })

    dl_path = _resolve_user_path(download_dir or "downloads")
    file_index = _preview_file_index([dl_path, export_path.parent])
    preview_cfg = cfg or load_config()
    tdl_available = Path(normalize_tdl_path(preview_cfg.get("tdl_path"))).is_file()
    for item in items:
        local_path = _match_preview_file(item, file_index)
        if not local_path:
            item["downloaded"] = False
            if tdl_available and item["kind"] in {"image", "video", "audio"}:
                token = _register_preview_stream(export_path, item)
                item.update(
                    {
                        "previewable": True,
                        "media_url": f"/api/preview/stream/{token}",
                        "download_url": f"/api/preview/stream/{token}?download=1",
                    }
                )
            continue
        token = hashlib.sha256(str(local_path).encode("utf-8")).hexdigest()[:24]
        with preview_files_lock:
            preview_files[token] = local_path
        item.update({
            "downloaded": True,
            "size": local_path.stat().st_size,
            "media_url": f"/api/preview/media/{token}",
            "download_url": f"/api/preview/media/{token}?download=1",
        })
    return {"export_file": str(export_path), "download_dir": str(dl_path), "items": items}


def create_preview_download(cfg, body):
    export_path = _resolve_user_path(body.get("export_file"))
    if not export_path.is_file():
        raise FileNotFoundError(f"Export JSON not found: {export_path}")
    with export_path.open("r", encoding="utf-8-sig") as stream:
        payload = json.load(stream)
    groups = _export_groups(payload)
    group_index = int(body.get("group_index", -1))
    message_index = int(body.get("message_index", -1))
    if group_index < 0 or group_index >= len(groups):
        raise ValueError("Invalid chat group index")
    messages = groups[group_index]["messages"]
    if message_index < 0 or message_index >= len(messages):
        raise ValueError("Invalid message index")
    message = messages[message_index]
    if not isinstance(message, dict) or not _message_file(message):
        raise ValueError("This message does not contain a downloadable file")

    temp_path = BASE_DIR / f".preview-download-{uuid.uuid4().hex}.json"
    temp_payload = {key: value for key, value in groups[group_index].items() if key != "messages"}
    temp_payload["messages"] = [message]
    with temp_path.open("w", encoding="utf-8") as stream:
        json.dump(temp_payload, stream, ensure_ascii=False)
    command, _ = cmd_download(cfg, {
        "mode": "file",
        "files": [str(temp_path)],
        "dir": body.get("download_dir") or cfg.get("download_dir", "downloads"),
        "limit": body.get("limit") or cfg.get("limit", 2),
        "threads": body.get("threads") or cfg.get("threads", 4),
        "skip_same": True,
    })
    history_record = _history_record(groups[group_index], message, export_path)
    task_id, error = create_task(
        command,
        build_env(cfg),
        f"Download {_message_file(message)}",
        [temp_path],
        lambda: record_download_history([history_record]),
    )
    if error:
        temp_path.unlink(missing_ok=True)
    return task_id, error, command


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

COMMAND_BUILDERS = {
    "login": cmd_login,
    "chats": cmd_chats,
    "export": cmd_export,
    "download": cmd_download,
    "forward": cmd_forward,
    "upload": cmd_upload,
    "users": cmd_users,
}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "tdl-gui/1.0"

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except OSError as exc:
            if getattr(exc, "winerror", None) not in (10053, 10054):
                raise

    def log_message(self, *args):
        pass  # silence default logging

    # -- helpers ------------------------------------------------------------

    def _send_json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def _serve_index(self):
        if INDEX_FILE.exists():
            data = INDEX_FILE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)
        else:
            self._send_json({"error": "index.html not found"}, 404)

    # -- GET ----------------------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        cfg = load_config()

        if path in ("/", "/index.html"):
            self._serve_index()
            return

        if path == "/api/config":
            self._send_json(cfg)
            return

        if path == "/api/version":
            self._send_json({"version": get_version(cfg)})
            return

        if path == "/api/login-status":
            self._send_json({"logged_in": check_login(cfg)})
            return

        if path == "/api/download-history":
            self._send_json(download_history_summary())
            return

        if path.startswith("/api/preview/media/"):
            token = path.rsplit("/", 1)[-1]
            with preview_files_lock:
                media_path = preview_files.get(token)
            if not media_path or not media_path.is_file():
                self._send_json({"error": "preview file not found"}, 404)
                return
            self._serve_media(media_path, parsed.query)
            return

        if path.startswith("/api/preview/stream/"):
            token = path.rsplit("/", 1)[-1]
            self._serve_preview_stream(token, parsed.query, cfg)
            return

        if path == "/api/file":
            requested = parse_qs(parsed.query).get("path", [""])[0]
            file_path = _resolve_user_path(requested)
            if not requested or file_path.suffix.casefold() != ".json" or not file_path.is_file():
                self._send_json({"error": "file not found"}, 404)
                return
            # Export outputs are local artifacts; always return them as downloads.
            self._serve_media(file_path, "download=1")
            return

        if path == "/api/tasks":
            with tasks_lock:
                info = [
                    {
                        "id": t.id,
                        "label": t.label,
                        "status": t.status,
                        "exit_code": t.exit_code,
                        "command": t.command,
                        "lines": len(t.lines),
                    }
                    for t in tasks.values()
                ]
            self._send_json(info)
            return

        if path.startswith("/api/task/"):
            task_id = path.rsplit("/", 1)[-1]
            with tasks_lock:
                task = tasks.get(task_id)
            if not task:
                self._send_json({"error": "task not found"}, 404)
                return
            self._send_json(
                {
                    "id": task.id,
                    "label": task.label,
                    "status": task.status,
                    "exit_code": task.exit_code,
                    "command": task.command,
                    "lines": task.lines,
                    "result": task.result,
                }
            )
            return

        if path.startswith("/api/stream/"):
            task_id = path.rsplit("/", 1)[-1]
            self._handle_sse(task_id)
            return

        self._send_json({"error": "not found"}, 404)

    # -- POST ---------------------------------------------------------------

    def do_POST(self):
        path = urlparse(self.path).path
        cfg = load_config()

        if path == "/api/config":
            body = self._read_body()
            merged = save_config(body)
            self._send_json({"ok": True, "config": merged})
            return

        if path == "/api/preview":
            body = self._read_body()
            try:
                result = build_preview(
                    body.get("export_file"),
                    body.get("download_dir") or cfg.get("download_dir"),
                    cfg,
                )
                self._send_json({"ok": True, **result})
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self._send_json({"ok": False, "error": str(exc)}, 400)
            return

        if path == "/api/preview/download":
            body = self._read_body()
            try:
                tid, error, command = create_preview_download(cfg, body)
                if error:
                    self._send_json({"error": error, "command": command}, 409)
                    return
                self._send_json({"task_id": tid, "command": command})
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self._send_json({"error": str(exc)}, 400)
            return

        if path == "/api/download-history/clear":
            self._send_json({"ok": True, **clear_download_history()})
            return

        if path == "/api/download":
            body = self._read_body()
            try:
                tid, error, command, history = create_download_task(cfg, body)
                if error:
                    self._send_json({"error": error, "command": command, "history": history}, 409)
                    return
                self._send_json({"task_id": tid, "command": command, "history": history})
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self._send_json({"error": str(exc)}, 400)
            return

        # task-starting endpoints
        for name, builder in COMMAND_BUILDERS.items():
            if name == "download":
                continue
            if path == f"/api/{name}":
                body = self._read_body()
                cmd, label = builder(cfg, body)
                tid, error = create_task(cmd, build_env(cfg), label)
                if error:
                    self._send_json({"error": error, "command": cmd}, 409)
                    return
                self._send_json({"task_id": tid, "command": cmd})
                return

        if path.startswith("/api/task/") and path.endswith("/input"):
            task_id = path.split("/")[3]
            body = self._read_body()
            text = body.get("text", "")
            with tasks_lock:
                task = tasks.get(task_id)
            if task and task.process and task.process.stdin:
                try:
                    task.process.stdin.write(text + "\n")
                    task.process.stdin.flush()
                    self._send_json({"ok": True})
                except Exception as e:
                    self._send_json({"ok": False, "error": str(e)})
            else:
                self._send_json({"ok": False, "error": "task not running"})
            return

        if path.startswith("/api/task/") and path.endswith("/stop"):
            task_id = path.split("/")[3]
            with tasks_lock:
                task = tasks.get(task_id)
            if task:
                try:
                    ok, message = stop_task_process(task)
                    self._send_json({"ok": ok, "message": message})
                except Exception as e:
                    self._send_json({"ok": False, "error": str(e)})
            else:
                self._send_json({"ok": False, "error": "task not found"})
            return

        self._send_json({"error": "not found"}, 404)

    def _serve_media(self, path, query):
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        size = path.stat().st_size
        range_header = self.headers.get("Range", "")
        start, end = 0, size - 1
        status = 200
        if range_header.startswith("bytes="):
            try:
                raw_range = range_header[6:].split(",", 1)[0]
                left, right = raw_range.split("-", 1)
                if left:
                    start = int(left)
                if right:
                    end = int(right)
                else:
                    end = min(start + 1024 * 1024 - 1, size - 1)
                if start < 0 or start >= size or end < start:
                    raise ValueError
                end = min(end, size - 1)
                status = 206
            except (ValueError, IndexError):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if "download=1" in query:
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(path.name)}")
        self.send_header("Cache-Control", "private, max-age=300")
        self.end_headers()
        with path.open("rb") as stream:
            stream.seek(start)
            remaining = length
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _serve_preview_stream(self, token, query, cfg):
        with preview_streams_lock:
            entry = preview_streams.get(token)
        if not entry:
            self._send_json({"error": "preview stream not found"}, 404)
            return

        try:
            port = _ensure_preview_server(entry["export_path"], cfg)
            peer = quote(str(entry.get("chat_id") or ""), safe="")
            message = quote(str(entry.get("message_id") or ""), safe="")
            upstream = f"http://127.0.0.1:{port}/{peer}/{message}"
            headers = {}
            range_header = self.headers.get("Range")
            if range_header:
                headers["Range"] = range_header
            request = urllib.request.Request(upstream, headers=headers)
            response = urllib.request.urlopen(request, timeout=60)
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
            self._send_json({"error": f"在线预览失败：{exc}"}, 502)
            return

        try:
            status = getattr(response, "status", None) or response.getcode() or 200
            self.send_response(status)
            self.send_header("Content-Type", response.headers.get("Content-Type") or entry["mime"])
            content_length = response.headers.get("Content-Length")
            if content_length:
                self.send_header("Content-Length", content_length)
            accept_ranges = response.headers.get("Accept-Ranges")
            if accept_ranges:
                self.send_header("Accept-Ranges", accept_ranges)
            content_range = response.headers.get("Content-Range")
            if content_range:
                self.send_header("Content-Range", content_range)
            if parse_qs(query).get("download", [""])[0] == "1":
                self.send_header(
                    "Content-Disposition",
                    f"attachment; filename*=UTF-8''{quote(entry['filename'])}",
                )
            self.send_header("Cache-Control", "private, max-age=60")
            self.end_headers()
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        finally:
            response.close()

    # -- SSE ----------------------------------------------------------------

    def _handle_sse(self, task_id):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        with tasks_lock:
            task = tasks.get(task_id)

        if not task:
            self.wfile.write(b'data: {"type":"error","message":"task not found"}\n\n')
            self.wfile.flush()
            return

        idx = 0
        last_keepalive = time.time()
        while True:
            try:
                while idx < len(task.lines):
                    payload = json.dumps(
                        {"type": "line", "data": task.lines[idx]}, ensure_ascii=False
                    )
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    idx += 1
                self.wfile.flush()

                if task.status in ("done", "error", "stopped") and idx >= len(task.lines):
                    payload = json.dumps(
                        {"type": "end", "status": task.status, "exit_code": task.exit_code},
                        ensure_ascii=False,
                    )
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    break

                if time.time() - last_keepalive >= 1:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    last_keepalive = time.time()
                time.sleep(0.2)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                break
        return

        # replay existing lines
        idx = 0
        while idx < len(task.lines):
            line = task.lines[idx]
            payload = json.dumps({"type": "line", "data": line}, ensure_ascii=False)
            self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
            idx += 1
        self.wfile.flush()

        # stream new output
        while True:
            if task.status in ("done", "error", "stopped") and task.output_queue.empty():
                payload = json.dumps(
                    {"type": "end", "status": task.status, "exit_code": task.exit_code},
                    ensure_ascii=False,
                )
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
                break
            try:
                item = task.output_queue.get(timeout=1)
                if item[0] == "line":
                    payload = json.dumps(
                        {"type": "line", "data": item[1]}, ensure_ascii=False
                    )
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                elif item[0] == "end":
                    payload = json.dumps(
                        {
                            "type": "end",
                            "status": item[1]["status"],
                            "exit_code": item[1]["exit_code"],
                        },
                        ensure_ascii=False,
                    )
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    break
                self.wfile.flush()
            except queue.Empty:
                # keepalive
                try:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    break
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                break


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

class StrictThreadingHTTPServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = False
    allow_reuse_port = False


def find_port(host, start):
    import socket
    for port in range(start, start + 20):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((host, port))
                return port
        except OSError:
            continue
    raise OSError(f"No available port in range {start}-{start + 19}")


def main():
    if not acquire_single_instance():
        print_existing_instance(read_instance_info())
        return 2

    cfg = load_config()
    host = cfg.get("host", "127.0.0.1")
    port = find_port(host, int(cfg.get("port", 8765)))

    # validate tdl path
    tdl = Path(normalize_tdl_path(cfg.get("tdl_path")))
    if not tdl.exists():
        print(f"[Warning] tdl.exe not found at: {tdl}")
        print("          You can change the path in the Settings tab.")
    else:
        print(f"[OK] tdl: {tdl}")

    home = Path(cfg.get("tdl_home", ""))
    if home.exists():
        print(f"[OK] home: {home}")
    else:
        print(f"[Warning] tdl home not found: {home}")

    server = StrictThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    write_instance_info(host, port)
    print(f"\n  tdl GUI running at {url}")
    print("  Press Ctrl+C to stop.\n")

    # auto-open browser
    try:
        if os.environ.get("TDL_GUI_NO_BROWSER") != "1":
            webbrowser.open(url)
    except Exception:
        pass

    atexit.register(remove_instance_info)
    atexit.register(stop_all_tasks)
    atexit.register(stop_preview_servers)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        stop_all_tasks()
        stop_preview_servers()
        server.server_close()
        remove_instance_info()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
