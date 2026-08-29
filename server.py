#!/usr/bin/env python3
"""tdl GUI - A web-based GUI wrapper for the tdl Telegram downloader tool.

Runs a local HTTP server that serves a web UI and executes tdl commands,
streaming their output to the browser in real time via SSE.
"""

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
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent.resolve()
CONFIG_FILE = BASE_DIR / "config.json"
INDEX_FILE = BASE_DIR / "index.html"

DEFAULT_CONFIG = {
    "tdl_path": r"D:\ruanjian\ruanjian\tdl_Windows_64bit\tdl.exe",
    "tdl_home": r"D:\ruanjian\ruanjian\tdl_Windows_64bit\tdl_home",
    "namespace": "default",
    "proxy": "",
    "pool": 8,
    "limit": 2,
    "threads": 4,
    "download_dir": "downloads",
    "host": "127.0.0.1",
    "port": 8765,
}


def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            return {**DEFAULT_CONFIG, **saved}
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def save_config(cfg):
    merged = {**DEFAULT_CONFIG, **cfg}
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
    return merged


# ---------------------------------------------------------------------------
# Task management
# ---------------------------------------------------------------------------

class Task:
    def __init__(self, task_id, command, env, label="", cleanup_paths=None):
        self.id = task_id
        self.command = command
        self.env = env
        self.label = label
        self.process = None
        self.output_queue = queue.Queue()
        self.status = "pending"  # pending, running, done, error
        self.exit_code = None
        self.lines = []
        self.start_time = time.time()
        self.end_time = None
        self.result = None  # optional parsed result (e.g. chat list JSON)
        self.cleanup_paths = list(cleanup_paths or [])


tasks = {}
tasks_lock = threading.Lock()
preview_files = {}
preview_files_lock = threading.Lock()

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
        for line in iter(task.process.stdout.readline, ""):
            clean = strip_ansi(line).rstrip("\n")
            task.lines.append(clean)
            task.output_queue.put(("line", clean))
        task.process.wait()
        task.exit_code = task.process.returncode
        task.status = "done" if task.exit_code == 0 else "error"
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
        task.end_time = time.time()
        task.output_queue.put(("end", {"status": task.status, "exit_code": task.exit_code}))
        for path in task.cleanup_paths:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass


def create_task(command, env, label="", cleanup_paths=None):
    task_id = uuid.uuid4().hex[:12]
    task = Task(task_id, command, env, label, cleanup_paths)
    with tasks_lock:
        active = next((t for t in tasks.values() if t.status in ("pending", "running")), None)
        if active:
            return None, f"已有任务正在运行：{active.label or active.id}，请先停止或等待结束"
        tasks[task_id] = task
    t = threading.Thread(target=_run_task, args=(task,), daemon=True)
    t.start()
    return task_id, None


def stop_task_process(task):
    if not task or not task.process:
        return False, "task not running"
    pid = task.process.pid
    try:
        if sys.platform == "win32":
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.stdout:
                for line in result.stdout.splitlines():
                    clean = strip_ansi(line).strip()
                    if clean:
                        task.lines.append(clean)
                        task.output_queue.put(("line", clean))
            return result.returncode == 0, result.stdout.strip() or "taskkill finished"
        task.process.terminate()
        try:
            task.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            task.process.kill()
        return True, "terminated"
    except Exception as exc:
        return False, str(exc)


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
    return [cfg["tdl_path"]]


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
            [cfg["tdl_path"], "version"],
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


def _find_downloaded_files(filenames, roots):
    wanted = {Path(name).name.casefold() for name in filenames if name}
    found = {}
    visited = set()
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        root_key = str(root).casefold()
        if root_key in visited:
            continue
        visited.add(root_key)
        for current, _, files in os.walk(root):
            for filename in files:
                key = filename.casefold()
                if key in wanted and key not in found:
                    found[key] = (Path(current) / filename).resolve()
            if len(found) == len(wanted):
                return found
    return found


def build_preview(export_file, download_dir):
    export_path = _resolve_user_path(export_file)
    if not export_path.is_file():
        raise FileNotFoundError(f"Export JSON not found: {export_path}")
    with export_path.open("r", encoding="utf-8-sig") as stream:
        payload = json.load(stream)
    groups = _export_groups(payload)
    if not groups:
        raise ValueError("The JSON file does not contain a supported tdl message list")

    items = []
    filenames = []
    for group_index, group in enumerate(groups):
        chat_id = group.get("id") or group.get("chat_id") or group.get("dialog_id")
        for message_index, message in enumerate(group["messages"]):
            if not isinstance(message, dict):
                continue
            filename = _message_file(message)
            if not filename:
                continue
            filenames.append(filename)
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
            })

    dl_path = _resolve_user_path(download_dir or "downloads")
    found = _find_downloaded_files(filenames, [dl_path, export_path.parent])
    for item in items:
        local_path = found.get(Path(item["filename"]).name.casefold())
        if not local_path:
            item["downloaded"] = False
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
    task_id, error = create_task(command, build_env(cfg), f"Download {_message_file(message)}", [temp_path])
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

        if path.startswith("/api/preview/media/"):
            token = path.rsplit("/", 1)[-1]
            with preview_files_lock:
                media_path = preview_files.get(token)
            if not media_path or not media_path.is_file():
                self._send_json({"error": "preview file not found"}, 404)
                return
            self._serve_media(media_path, parsed.query)
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
                result = build_preview(body.get("export_file"), body.get("download_dir") or cfg.get("download_dir"))
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

        # task-starting endpoints
        for name, builder in COMMAND_BUILDERS.items():
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
            if task and task.process:
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

                if task.status in ("done", "error") and idx >= len(task.lines):
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
            if task.status in ("done", "error") and task.output_queue.empty():
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

def find_port(host, start):
    import socket
    for port in range(start, start + 20):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((host, port))
                return port
        except OSError:
            continue
    return start


def main():
    cfg = load_config()
    host = cfg.get("host", "127.0.0.1")
    port = find_port(host, int(cfg.get("port", 8765)))

    # validate tdl path
    tdl = Path(cfg.get("tdl_path", ""))
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

    server = http.server.ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    print(f"\n  tdl GUI running at {url}")
    print("  Press Ctrl+C to stop.\n")

    # auto-open browser
    try:
        if os.environ.get("TDL_GUI_NO_BROWSER") != "1":
            webbrowser.open(url)
    except Exception:
        pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
