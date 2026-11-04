#!/usr/bin/env python3
"""Enhanced multi-user Telegram bot hosting panel — reply-keyboard build.

Run: export BOT_TOKEN='123:abc'; python hosting_powerful.py
"""
from __future__ import annotations

import io, json, os, re, shutil, signal, subprocess, sys, tempfile, threading, time, zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Optional

try:
    import telebot
    from telebot import types
except ImportError as exc:
    raise SystemExit("pip install pyTelegramBotAPI cryptography psutil flask") from exc
try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError as exc:
    raise SystemExit("pip install cryptography") from exc
try:
    import psutil
except Exception:
    psutil = None
try:
    from flask import Flask, jsonify
except Exception:
    Flask = None
    jsonify = None

# ── Config ─────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is required.")

PARTY_EFFECT_ID = "5046509860389126442"

DATA_DIR = Path(os.getenv("HOSTING_DATA_DIR", "data")).resolve()
PROJECTS_DIR = Path(os.getenv("HOSTING_PROJECTS_DIR", "hosted_bots")).resolve()
DB_FILE = DATA_DIR / "hosting_db.json"
KEY_FILE = DATA_DIR / "master.key"

_MAX_MB = int(os.getenv("MAX_UPLOAD_MB", "75"))
MAX_UPLOAD        = _MAX_MB * 1024 * 1024
MAX_EXTRACTED     = _MAX_MB * 1024 * 1024
MAX_FILES         = 500
MAX_SINGLE_FILE   = _MAX_MB * 1024 * 1024
MAX_BOTS_PER_USER = int(os.getenv("MAX_BOTS_PER_USER", "1000000"))
MAX_LOG_BYTES     = 1024 * 1024 * 1024
MAX_BACKUP_MEM    = 2 * 1024**3
MAX_BOT_RAM_MB    = int(os.getenv("MAX_BOT_RAM_MB", "0"))
MAX_BOT_CPU_PCT   = int(os.getenv("MAX_BOT_CPU_PCT", "0"))
# 🆕 PATCH 4a: Per-bot disk quota
MAX_BOT_DISK_MB   = int(os.getenv("MAX_BOT_DISK_MB", "500"))
RESOURCE_CHECK_INTERVAL = 5
BLOCKED_ENV = {"PATH", "PYTHONPATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONHOME", "VIRTUAL_ENV"}
# 🆕 PATCH 7a: Auto-backup config
BACKUP_CHANNEL = os.getenv("BACKUP_CHANNEL", "").strip()
BACKUP_INTERVAL_HOURS = int(os.getenv("BACKUP_INTERVAL_HOURS", "24"))

_TOKEN_PATTERN = re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{33,}\b")

DATA_DIR.mkdir(parents=True, exist_ok=True)
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
try:
    os.chmod(DATA_DIR, 0o700); os.chmod(PROJECTS_DIR, 0o700)
except OSError:
    pass

_db_lock = threading.RLock()
_proc_lock = threading.RLock()
RUNNING: dict[str, dict[str, Any]] = {}
RETRY_AT: dict[str, float] = {}
STATES: dict[int, dict[str, Any]] = {}
LAST_ACTION: dict[int, float] = {}
_CPU_SAMPLES: dict[str, tuple[float, float]] = {}

MENU_LEVEL: dict[int, str] = {}
SELECTED_BOT: dict[int, str] = {}
BOT_INDEX: dict[int, list[str]] = {}
# 🆕 PATCH 6a: Live log streaming state
LIVE_LOGS_ACTIVE: dict[str, int] = {}
LIVE_LOGS_STOP: dict[str, threading.Event] = {}

# ── Crypto + DB ────────────────────────────────────────────────────────────
def _load_key() -> bytes:
    if KEY_FILE.exists():
        key = KEY_FILE.read_bytes().strip()
        Fernet(key)
        return key
    key = Fernet.generate_key()
    fd = os.open(str(KEY_FILE), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key); fh.flush(); os.fsync(fh.fileno())
    return key

FERNET = Fernet(_load_key())

def encrypt_obj(value: dict[str, str]) -> str:
    return FERNET.encrypt(json.dumps(value, separators=(",", ":")).encode()).decode()

def decrypt_obj(value: str) -> dict[str, str]:
    if not value:
        return {}
    try:
        data = json.loads(FERNET.decrypt(value.encode()).decode())
        return data if isinstance(data, dict) else {}
    except (InvalidToken, ValueError, TypeError):
        return {}

def db_load() -> dict[str, Any]:
    with _db_lock:
        if not DB_FILE.exists():
            return {"bots": {}, "users": {}}
        try:
            data = json.loads(DB_FILE.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("invalid db")
            data.setdefault("bots", {})
            data.setdefault("users", {})
            return data
        except Exception:
            try:
                shutil.copy2(DB_FILE, DB_FILE.with_suffix(".corrupt.json"))
            except OSError:
                pass
            return {"bots": {}, "users": {}}

def db_save(data: dict[str, Any]) -> None:
    with _db_lock:
        tmp = DB_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, DB_FILE)

def mutate_bot(bot_id: str, **changes: Any) -> Optional[dict[str, Any]]:
    data = db_load()
    item = data["bots"].get(bot_id)
    if not item:
        return None
    item.update(changes)
    db_save(data)
    return item

def get_bot(bot_id: str) -> Optional[dict[str, Any]]:
    return db_load()["bots"].get(bot_id)

def user_bots(uid: int) -> list[dict[str, Any]]:
    return [b for b in db_load()["bots"].values() if int(b.get("owner", 0)) == uid]

def owned_bot(uid: int, bot_id: str) -> Optional[dict[str, Any]]:
    b = get_bot(bot_id)
    return b if b and int(b.get("owner", 0)) == uid else None

def get_user(uid: int) -> dict[str, Any]:
    d = db_load()
    return d["users"].get(str(uid), {})

def save_user(uid: int, **changes: Any) -> dict[str, Any]:
    d = db_load()
    d["users"].setdefault(str(uid), {})
    d["users"][str(uid)].update(changes)
    db_save(d)
    return d["users"][str(uid)]

# ── Path safety ────────────────────────────────────────────────────────────
def project_dir(bot_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{10}", bot_id):
        raise ValueError("Invalid project ID")
    return (PROJECTS_DIR / bot_id).resolve()

def safe_name(name: str) -> str:
    name = Path(name.replace("\\", "/")).name
    name = re.sub(r"[^A-Za-z0-9_. -]", "_", name).strip(" .")
    if not name or name in {".", ".."}:
        raise ValueError("Invalid filename")
    return name[:200]

def safe_project_path(root: Path, relative: str) -> Path:
    rel = PurePosixPath(relative.replace("\\", "/"))
    if rel.is_absolute() or any(p in {"", ".", ".."} for p in rel.parts):
        raise ValueError("Unsafe path")
    if any(p in {".git", ".venv", "__pycache__"} or p == ".env" or p.startswith(".env.") for p in rel.parts):
        raise ValueError("Reserved path")
    target = (root / Path(*rel.parts)).resolve()
    if root.resolve() not in target.parents and target != root.resolve():
        raise ValueError("Escapes project")
    return target

def safe_extract_zip(raw: bytes, dest: Path) -> int:
    total = 0
    count = 0
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_FILES:
            raise ValueError(f"Too many files (>{MAX_FILES})")
        for info in infos:
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            mode = (info.external_attr >> 16) & 0xFFFF
            if mode and (mode & 0o170000) == 0o120000:
                raise ValueError("Symlinks not allowed")
            target = safe_project_path(dest, name)
            size = int(info.file_size)
            if size > MAX_SINGLE_FILE:
                raise ValueError(f"File too large: {name}")
            total += size; count += 1
            if total > MAX_EXTRACTED:
                raise ValueError("Extracted size limit exceeded")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
    if not count:
        raise ValueError("Empty ZIP")
    return count

def detect_entry(root: Path) -> str:
    for name in ("main.py", "bot.py", "app.py", "run.py", "index.js", "bot.js", "app.js", "server.js"):
        if (root / name).is_file():
            return name
    cands = sorted(p for p in root.iterdir() if p.is_file() and p.suffix.lower() in {".py", ".js", ".mjs", ".cjs"})
    return cands[0].name if cands else "main.py"

def _scan_hardcoded_token(bot_id: str) -> Optional[str]:
    try:
        root = project_dir(bot_id)
        for src_file in root.rglob("*"):
            if not src_file.is_file() or src_file.is_symlink():
                continue
            if src_file.suffix.lower() not in {".py", ".js", ".mjs", ".cjs"}:
                continue
            if any(part in {".venv", "__pycache__", "node_modules"} for part in src_file.parts):
                continue
            try:
                content = src_file.read_text(errors="ignore")
            except Exception:
                continue
            m = _TOKEN_PATTERN.search(content)
            if m:
                return m.group(0)
        return None
    except Exception:
        return None


# 🆕 PATCH 2a: Duplicate token detection
def _find_duplicate_token(uid: int, current_bot_id: str, token: str) -> Optional[str]:
    if not token or len(token) < 20:
        return None
    for b in user_bots(uid):
        if b["id"] == current_bot_id:
            continue
        envs = decrypt_obj(b.get("env_enc", ""))
        if envs.get("BOT_TOKEN", "") == token:
            return b["id"]
    return None


def _extract_env_token(bot_id: str) -> str:
    b = get_bot(bot_id)
    if not b:
        return ""
    envs = decrypt_obj(b.get("env_enc", ""))
    return envs.get("BOT_TOKEN", "").strip()


# ── Record + log helpers ───────────────────────────────────────────────────
def _create_record(uid: int, name: str, source: str = "upload") -> dict[str, Any]:
    if len(user_bots(uid)) >= MAX_BOTS_PER_USER:
        raise ValueError(f"Limit reached ({MAX_BOTS_PER_USER} bots)")
    bot_id = os.urandom(5).hex()
    root = project_dir(bot_id)
    root.mkdir(parents=True, mode=0o700)
    now = int(time.time())
    rec = {
        "id": bot_id, "owner": uid, "name": (name[:200] or "My Bot"), "source": source,
        "entry": "main.py", "created": now, "desired_running": False,
        "auto_restart": True, "restart_interval": 0, "last_interval_restart": now,
        "restart_count": 0, "env_enc": "", "last_exit": None,
        "deps_signature": "", "resource_killed": False,
        "daily_restart": "",
        "last_daily_restart": 0,
        # 🆕 PATCH 1a: Crash loop protection fields
        "recent_crashes": [],
        "failed_state": False,
        "failed_reason": "",
    }
    data = db_load()
    data["bots"][bot_id] = rec
    db_save(data)
    return rec

def _log_path(bot_id: str) -> Path:
    return project_dir(bot_id) / ".hosting.log"

def write_log(bot_id: str, message: str) -> None:
    try:
        p = _log_path(bot_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8", errors="replace") as fh:
            fh.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + message.rstrip() + "\n")
        if p.stat().st_size > MAX_LOG_BYTES:
            size = p.stat().st_size
            with p.open("rb") as fh:
                fh.seek(size - MAX_LOG_BYTES // 2)
                tail = fh.read()
            p.write_bytes(tail)
    except Exception:
        pass

def tail_logs(bot_id: str, lines: int = 500) -> str:
    p = _log_path(bot_id)
    if not p.exists():
        return "No logs yet."
    try:
        with p.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 500_000))
            data = fh.read().decode("utf-8", "replace")
        out = data.splitlines()[-lines:]
        return "\n".join(out) if out else "No logs yet."
    except Exception as exc:
        return f"Read error: {exc}"

# ── Auto-pip ──────────────────────────────────────────────────────────────
_IMPORT_PYPI = {
    "telebot": "pyTelegramBotAPI", "telegram": "python-telegram-bot",
    "aiogram": "aiogram", "pyrogram": "pyrogram", "telethon": "telethon",
    "PIL": "Pillow", "cv2": "opencv-python", "bs4": "beautifulsoup4",
    "yaml": "PyYAML", "dotenv": "python-dotenv", "Crypto": "pycryptodome",
    "Cryptodome": "pycryptodomex", "sklearn": "scikit-learn", "skimage": "scikit-image",
    "dateutil": "python-dateutil", "OpenSSL": "pyOpenSSL", "MySQLdb": "mysqlclient",
    "psycopg2": "psycopg2-binary", "serial": "pyserial", "magic": "python-magic",
    "googleapiclient": "google-api-python-client", "nacl": "PyNaCl",
    "jose": "python-jose", "pkg_resources": "setuptools", "discord": "discord.py",
    "github": "PyGithub", "git": "GitPython", "cryptography": "cryptography",
    "requests": "requests", "aiohttp": "aiohttp", "httpx": "httpx",
    "flask": "flask", "fastapi": "fastapi", "psutil": "psutil",
    "redis": "redis", "motor": "motor", "pymongo": "pymongo",
    "numpy": "numpy", "pandas": "pandas", "matplotlib": "matplotlib",
    "firebase_admin": "firebase-admin",
}

def scan_imports(root: Path) -> set[str]:
    import ast
    found: set[str] = set()
    for f in root.rglob("*.py"):
        if any(p in {".venv", "__pycache__", ".git"} for p in f.parts):
            continue
        try:
            tree = ast.parse(f.read_text(errors="ignore"))
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    for a in n.names:
                        found.add(a.name.split(".")[0])
                elif isinstance(n, ast.ImportFrom):
                    if n.level == 0 and n.module:
                        found.add(n.module.split(".")[0])
        except Exception:
            pass
    return found

def missing_imports(root: Path, venv_py: Path) -> list[str]:
    stdlib = set(getattr(sys, "stdlib_module_names", set()))
    skip = stdlib | {"__future__", ""}
    for p in root.rglob("*.py"):
        if any(x in {".venv", "__pycache__", ".git"} for x in p.parts):
            continue
        skip.add(p.stem)
    imports = sorted(scan_imports(root) - skip)
    if not imports:
        return []
    missing: list[str] = []
    for imp in imports:
        try:
            r = subprocess.run([str(venv_py), "-c", f"__import__('{imp}')"],
                               capture_output=True, timeout=60)
            if r.returncode != 0:
                missing.append(_IMPORT_PYPI.get(imp, imp))
        except Exception:
            missing.append(_IMPORT_PYPI.get(imp, imp))
    return missing

def _base_child_env(bot: dict[str, Any]) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT"}}
    env.update({"PYTHONUNBUFFERED": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"})
    custom = decrypt_obj(bot.get("env_enc", ""))
    for k, v in custom.items():
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", k) and k not in BLOCKED_ENV:
            env[k] = v
    return env

def _venv_python(root: Path) -> Path:
    return root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

def ensure_venv(bot_id: str, timeout: int = 3600) -> tuple[bool, str]:
    root = project_dir(bot_id)
    py = _venv_python(root)
    if py.exists():
        return True, "venv ready"
    try:
        p = subprocess.run([sys.executable, "-m", "venv", str(root / ".venv")],
                           cwd=root, capture_output=True, text=True, timeout=timeout)
        if p.returncode:
            return False, (p.stderr or p.stdout)[-3800:]
        return True, "venv created"
    except Exception as exc:
        return False, str(exc)

def _deps_signature(root: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    req = root / "requirements.txt"
    if req.exists():
        try:
            h.update(req.read_bytes())
        except OSError:
            pass
    h.update(",".join(sorted(scan_imports(root))).encode())
    return h.hexdigest()

def install_requirements(bot_id: str, packages: Optional[list[str]] = None) -> tuple[bool, str]:
    root = project_dir(bot_id)
    ok, msg = ensure_venv(bot_id)
    if not ok:
        return False, msg
    py = _venv_python(root)
    bot = get_bot(bot_id) or {}

    sig = _deps_signature(root)
    if not packages and bot.get("deps_signature") == sig:
        return True, "Dependencies cached"

    pip = [str(py), "-m", "pip", "install", "--disable-pip-version-check"]
    installed_any = False

    if packages:
        for item in packages:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+(?:[<>=!~]=?[A-Za-z0-9*_.+-]+)?", item):
                return False, f"Invalid package spec: {item}"
        try:
            p = subprocess.run(pip + packages, cwd=root, capture_output=True, text=True, timeout=7200)
            installed_any = True
            out = (p.stdout + "\n" + p.stderr).strip()
            if p.returncode != 0:
                return False, out[-3800:]
            return True, out[-3800:] or "Installed"
        except subprocess.TimeoutExpired:
            return False, "pip timed out"
        except Exception as exc:
            return False, str(exc)

    req = root / "requirements.txt"
    if req.is_file():
        try:
            p = subprocess.run(pip + ["-r", str(req)], cwd=root, capture_output=True, text=True, timeout=7200)
            installed_any = True
            if p.returncode != 0:
                write_log(bot_id, "requirements.txt install failed")
                return False, (p.stdout + p.stderr)[-3800:]
        except subprocess.TimeoutExpired:
            return False, "pip install -r timed out"
        except Exception as exc:
            return False, str(exc)

    try:
        miss = missing_imports(root, py)
        if miss:
            write_log(bot_id, f"auto-pip: {', '.join(miss[:50])}")
            p = subprocess.run(pip + miss, cwd=root, capture_output=True, text=True, timeout=7200)
            installed_any = True
            if p.returncode != 0:
                return False, (p.stdout + p.stderr)[-3800:]
    except Exception as exc:
        write_log(bot_id, f"auto-pip error: {exc}")

    if not installed_any:
        return True, "No requirements found"
    mutate_bot(bot_id, deps_signature=_deps_signature(root))
    write_log(bot_id, "Dependencies OK")
    return True, "Installed"

def install_node_requirements(bot_id: str) -> tuple[bool, str]:
    root = project_dir(bot_id)
    pkg = root / "package.json"
    if not pkg.is_file():
        return True, "No package.json"
    if (root / "node_modules").exists():
        return True, "node_modules cached"
    npm = shutil.which("npm")
    if not npm:
        return False, "npm not installed"
    try:
        p = subprocess.run([npm, "install", "--omit=dev", "--no-audit", "--no-fund"],
                           cwd=root, capture_output=True, text=True, timeout=3600)
        out = (p.stdout + "\n" + p.stderr).strip()
        write_log(bot_id, "npm install: " + ("OK" if p.returncode == 0 else "FAILED"))
        return p.returncode == 0, out[-3800:] or ("Installed" if p.returncode == 0 else "npm failed")
    except subprocess.TimeoutExpired:
        return False, "npm timed out"
    except Exception as exc:
        return False, str(exc)

def _command_for(bot: dict[str, Any]) -> list[str]:
    root = project_dir(bot["id"])
    entry = safe_project_path(root, bot.get("entry", "main.py"))
    if not entry.is_file():
        raise FileNotFoundError(f"Entry missing: {entry.name}")
    ext = entry.suffix.lower()
    if ext == ".py":
        py = _venv_python(root)
        return [str(py if py.exists() else Path(sys.executable)), str(entry)]
    if ext in {".js", ".mjs", ".cjs"}:
        node = shutil.which("node")
        if not node:
            raise RuntimeError("Node.js not installed")
        return [node, str(entry)]
    raise RuntimeError("Entry must be .py / .js / .mjs / .cjs")

def stop_process(bot_id: str, stop_tunnel: bool = True) -> str:
    with _proc_lock:
        state = RUNNING.pop(bot_id, None)
    if not state:
        return "Already stopped"
    procs = [state.get("proc")]
    if stop_tunnel and state.get("tunnel"):
        procs.append(state.get("tunnel"))
    for proc in procs:
        if not proc:
            continue
        try:
            if os.name == "nt":
                if proc.poll() is None:
                    proc.terminate()
            else:
                os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    time.sleep(0.8)
    for proc in procs:
        if not proc:
            continue
        try:
            if os.name == "nt":
                if proc.poll() is None:
                    proc.kill()
            else:
                os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    RETRY_AT.pop(bot_id, None)
    _CPU_SAMPLES.pop(bot_id, None)
    return "Stopped"

def start_process(bot_id: str, manual: bool = True) -> tuple[bool, str]:
    bot = get_bot(bot_id)
    if not bot:
        return False, "Bot not found"
    root = project_dir(bot_id)
    if not root.exists():
        return False, "Project folder missing"
    with _proc_lock:
        ex = RUNNING.get(bot_id)
        if ex and ex.get("proc") and ex["proc"].poll() is None:
            return True, "Already running"
    try:
        # 🆕 PATCH 2c: Duplicate token check before start
        my_token = _extract_env_token(bot_id)
        if my_token:
            dup = _find_duplicate_token(bot["owner"], bot_id, my_token)
            if dup:
                dup_bot = get_bot(dup)
                return False, (
                    f"Duplicate BOT_TOKEN — same as '{dup_bot['name'] if dup_bot else dup}'\n"
                    f"Use different token or stop the other bot first."
                )

        ext = Path(bot.get("entry", "main.py")).suffix.lower()
        if ext == ".py":
            ok, msg = install_requirements(bot_id)
            if not ok:
                return False, "Dependency setup failed:\n" + msg
        elif ext in {".js", ".mjs", ".cjs"}:
            ok, msg = install_node_requirements(bot_id)
            if not ok:
                return False, "Node dependency setup failed:\n" + msg
        cmd = _command_for(bot)
        log = _log_path(bot_id)
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as fh:
            proc = subprocess.Popen(
                cmd, cwd=root, env=_base_child_env(bot),
                stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                start_new_session=(os.name != "nt"),
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
            )
        with _proc_lock:
            prev = RUNNING.get(bot_id, {})
            RUNNING[bot_id] = {"proc": proc, "started": time.time(), "tunnel": prev.get("tunnel")}
        mutate_bot(bot_id, desired_running=True, last_interval_restart=int(time.time()),
                   resource_killed=False)
        RETRY_AT.pop(bot_id, None)
        write_log(bot_id, f"Process started pid={proc.pid}")
        return True, f"Started (PID {proc.pid})"
    except Exception as exc:
        write_log(bot_id, f"Start failed: {exc}")
        if manual:
            mutate_bot(bot_id, desired_running=False)
        return False, str(exc)

def restart_process(bot_id: str) -> tuple[bool, str]:
    stop_process(bot_id)
    time.sleep(0.3)
    return start_process(bot_id)

def project_files(bot_id: str) -> list[Path]:
    root = project_dir(bot_id)
    skip = {".venv", ".git", "__pycache__"}
    out: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file() or p.is_symlink():
            continue
        if any(part in skip for part in p.relative_to(root).parts):
            continue
        if p.name in {".hosting.log", ".env"} or p.name.startswith(".env."):
            continue
        out.append(p)
    return sorted(out, key=lambda p: str(p.relative_to(root)))

def make_backup(bot_id: str) -> bytes:
    root = project_dir(bot_id)
    bot = get_bot(bot_id) or {}
    buf = io.BytesIO()
    total = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in project_files(bot_id):
            total += p.stat().st_size
            if total > MAX_BACKUP_MEM:
                raise ValueError("Backup too large for in-memory ZIP (>2 GB)")
            zf.write(p, p.relative_to(root).as_posix())
        meta = {
            "id": bot.get("id"),
            "name": bot.get("name"),
            "entry": bot.get("entry"),
            "env": decrypt_obj(bot.get("env_enc", "")),
            "restart_interval": bot.get("restart_interval", 0),
            "auto_restart": bot.get("auto_restart", True),
            "daily_restart": bot.get("daily_restart", ""),
        }
        zf.writestr("bot_meta.json", json.dumps(meta, indent=2))
    return buf.getvalue()

def restore_backup(bot_id: str, raw: bytes) -> int:
    stop_process(bot_id)
    root = project_dir(bot_id)
    temp = Path(tempfile.mkdtemp(prefix="restore-", dir=str(PROJECTS_DIR)))
    try:
        count = safe_extract_zip(raw, temp)
        for p in list(root.iterdir()):
            if p.name in {".venv", ".hosting.log"}:
                continue
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
        for p in temp.rglob("*"):
            if p.is_file():
                dest = safe_project_path(root, p.relative_to(temp).as_posix())
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dest)
        meta_path = root / "bot_meta.json"
        changes: dict[str, Any] = {"entry": detect_entry(root), "desired_running": False,
                                    "deps_signature": ""}
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text())
                if isinstance(meta.get("env"), dict):
                    changes["env_enc"] = encrypt_obj(meta["env"])
                if isinstance(meta.get("name"), str):
                    changes["name"] = meta["name"][:200]
                if isinstance(meta.get("restart_interval"), int):
                    changes["restart_interval"] = max(0, meta["restart_interval"])
                if isinstance(meta.get("auto_restart"), bool):
                    changes["auto_restart"] = meta["auto_restart"]
                if isinstance(meta.get("daily_restart"), str):
                    changes["daily_restart"] = meta["daily_restart"]
                meta_path.unlink()
            except Exception:
                pass
        mutate_bot(bot_id, **changes)
        return count
    finally:
        shutil.rmtree(temp, ignore_errors=True)

def clone_bot(uid: int, bot_id: str) -> Optional[dict[str, Any]]:
    b = owned_bot(uid, bot_id)
    if not b:
        return None
    if len(user_bots(uid)) >= MAX_BOTS_PER_USER:
        raise ValueError(f"Limit reached ({MAX_BOTS_PER_USER})")
    new_id = os.urandom(5).hex()
    src = project_dir(bot_id)
    dst = project_dir(new_id)
    dst.mkdir(parents=True, mode=0o700)
    for p in project_files(bot_id):
        rel = p.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
    now = int(time.time())
    new_rec = {**b,
        "id": new_id, "name": (b["name"] + " (copy)")[:200],
        "created": now, "desired_running": False,
        "restart_count": 0, "last_interval_restart": now,
        "last_daily_restart": 0,
        "deps_signature": "",
        "recent_crashes": [], "failed_state": False, "failed_reason": "",
    }
    data = db_load()
    data["bots"][new_id] = new_rec
    db_save(data)
    return new_rec

def clone_public_github(url: str, dest: Path, token: Optional[str] = None) -> tuple[bool, str]:
    m = re.fullmatch(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?", url.strip())
    if not m:
        return False, "Only GitHub HTTPS URLs accepted."
    clean = f"https://github.com/{m.group(1)}/{m.group(2)}.git"
    if token:
        clean = f"https://{token}@github.com/{m.group(1)}/{m.group(2)}.git"
    if not shutil.which("git"):
        return False, "git not installed"
    try:
        p = subprocess.run(["git", "clone", "--depth", "1", clean, str(dest)],
                           capture_output=True, text=True, timeout=1800)
        if p.returncode:
            return False, (p.stderr or p.stdout)[-3800:]
        return True, "Cloned"
    except subprocess.TimeoutExpired:
        return False, "git clone timeout"
    except Exception as exc:
        return False, str(exc)

def _format_bytes(n: float) -> str:
    x = float(max(0, n))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if x < 1024 or unit == "TB":
            return f"{x:.1f} {unit}"
        x /= 1024
    return f"{x:.1f} TB"

# 🆕 PATCH 4b: Disk usage helper
def _bot_disk_usage_mb(bot_id: str) -> float:
    try:
        root = project_dir(bot_id)
        if not root.exists():
            return 0.0
        total = 0
        for p in root.rglob("*"):
            if p.is_file() and not p.is_symlink():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
        return total / (1024 * 1024)
    except Exception:
        return 0.0


def esc(v: Any) -> str:
    return str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# 🆕 PATCH 7b: Auto-backup functions
def _do_auto_backup() -> None:
    if not BACKUP_CHANNEL:
        return
    try:
        target = DATA_DIR / f"panel_backup_{int(time.time())}.zip"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            if DB_FILE.exists():
                zf.write(DB_FILE, arcname="hosting_db.json")
            if KEY_FILE.exists():
                zf.write(KEY_FILE, arcname="master.key")
            for bid, b in db_load().get("bots", {}).items():
                meta = {
                    "id": bid, "name": b.get("name"), "entry": b.get("entry"),
                    "env": decrypt_obj(b.get("env_enc", "")),
                    "created": b.get("created"),
                }
                zf.writestr(f"bots/{bid}/meta.json", json.dumps(meta, indent=2))
        size = target.stat().st_size
        with target.open("rb") as fh:
            bot.send_document(
                BACKUP_CHANNEL, fh,
                caption=f"💾 <b>Panel Backup</b>\nSize: {_format_bytes(size)}\n"
                        f"Bots: {len(db_load().get('bots', {}))}",
                parse_mode="HTML",
                visible_file_name=f"backup_{time.strftime('%Y%m%d_%H%M')}.zip",
            )
        target.unlink(missing_ok=True)
        print(f"[auto-backup] sent {_format_bytes(size)}", flush=True)
    except Exception as exc:
        print(f"[auto-backup] failed: {exc}", flush=True)


def _auto_backup_loop() -> None:
    if not BACKUP_CHANNEL:
        return
    time.sleep(60)
    while True:
        try:
            _do_auto_backup()
        except Exception as exc:
            print(f"[auto-backup] loop error: {exc}", flush=True)
        time.sleep(BACKUP_INTERVAL_HOURS * 3600)


# 🆕 PATCH 3a: Health check
def _check_bot_health(bot_id: str) -> Optional[str]:
    try:
        log = tail_logs(bot_id, lines=200)
        if log == "No logs yet.":
            return None
        log_lower = log.lower()

        if "401" in log and "unauthorized" in log_lower:
            return "❌ <b>401 Unauthorized</b> — BOT_TOKEN invalid. Get new token from @BotFather."
        if "409" in log and "conflict" in log_lower:
            return "⚠️ <b>409 Conflict</b> — Same token running elsewhere."
        if "429" in log:
            return "⚠️ <b>429 Rate Limited</b> — Telegram throttling. Wait a few minutes."
        if "modulenotfounderror" in log_lower:
            m = re.search(r"ModuleNotFoundError: No module named '([^']+)'", log)
            mod = m.group(1) if m else "unknown"
            return f"❌ <b>Missing module</b>: <code>{esc(mod)}</code> — Install via 📦 Pip"
        if "syntaxerror" in log_lower:
            return "❌ <b>SyntaxError</b> — Python file has syntax error. Fix and re-upload."
        if "importerror" in log_lower:
            return "❌ <b>ImportError</b> — Module installed but can't import. Check 📜 Logs."
        if "permissionerror" in log_lower:
            return "⚠️ <b>PermissionError</b> — File access denied."
        if "connectionerror" in log_lower or "timeout" in log_lower:
            return "⚠️ <b>Connection error</b> — Network issue. Auto-recovers usually."
        return None
    except Exception:
        return None


# 🥈 Crash notification helper
def _send_crash_notification(bot_id: str, exit_code: int) -> None:
    try:
        b = get_bot(bot_id)
        if not b:
            return
        owner_uid = int(b.get("owner", 0))
        if not owner_uid:
            return
        if exit_code in (0, None):
            return
        tail = tail_logs(bot_id, lines=20)
        count = int(b.get("restart_count", 0))
        delay = min(60, 2 ** min(count + 1, 6))
        bot.send_message(
            owner_uid,
            f"🚨 <b>Bot Crashed!</b>\n\n"
            f"Bot: <b>{esc(b['name'])}</b>\n"
            f"ID: <code>{bot_id}</code>\n"
            f"Exit code: <code>{exit_code}</code>\n\n"
            f"<b>Last logs:</b>\n"
            f"<pre>{esc(tail[-1000:])}</pre>\n\n"
            f"⏱ Auto-restart in {delay}s...",
            parse_mode="HTML",
        )
    except Exception:
        pass

def bot_status(bot_id: str) -> str:
    b = get_bot(bot_id)
    if not b:
        return "Bot not found"
    with _proc_lock:
        st = RUNNING.get(bot_id)
    proc = st.get("proc") if st else None
    running = bool(proc and proc.poll() is None)
    lines = [f"<b>{esc(b['name'])}</b>", f"ID: <code>{bot_id}</code>",
             f"State: {'🟢 Running' if running else '🔴 Stopped'}",
             f"Entry: <code>{esc(b.get('entry', 'main.py'))}</code>",
             f"Restarts: {int(b.get('restart_count', 0))}"]
    if b.get("resource_killed"):
        lines.append("⚠️ Last stop was by resource limit")
    if running:
        lines.append(f"PID: <code>{proc.pid}</code>")
        lines.append(f"Uptime: {int(time.time() - st['started'])} sec")
        if psutil:
            try:
                p = psutil.Process(proc.pid)
                lines.append(f"RAM: {_format_bytes(p.memory_info().rss)}")
                cpu = p.cpu_percent(interval=0.1)
                lines.append(f"CPU: {cpu:.1f}%")
                if MAX_BOT_RAM_MB:
                    lines.append(f"RAM cap: {MAX_BOT_RAM_MB} MB")
            except Exception:
                pass
    if b.get("restart_interval"):
        lines.append(f"Auto-restart: {int(b['restart_interval'])} sec")
    if b.get("daily_restart"):
        lines.append(f"⏰ Daily restart: {b['daily_restart']} UTC")
    # 🆕 PATCH 3b: Failed state + health display
    if b.get("failed_state"):
        lines.append(f"\n🔴 <b>AUTO-RESTART DISABLED</b>\n<i>{esc(b.get('failed_reason', '?'))}</i>")
    health = _check_bot_health(bot_id)
    if health:
        lines.append(f"\n<b>Health:</b>\n{health}")
    return "\n".join(lines)

# ═══════════════════════════════════════════════════════════════════════════
# REPLY KEYBOARD SYSTEM
# ═══════════════════════════════════════════════════════════════════════════
def _btn(text: str, style: str = "primary") -> types.KeyboardButton:
    try:
        return types.KeyboardButton(text=text, style=style)
    except TypeError:
        return types.KeyboardButton(text=text)

def _rkm(row_width: int = 2) -> types.ReplyKeyboardMarkup:
    return types.ReplyKeyboardMarkup(
        resize_keyboard=True, is_persistent=True, row_width=row_width
    )

def kb_main() -> types.ReplyKeyboardMarkup:
    m = _rkm(2)
    m.row(_btn("📦 My Bots", "primary"), _btn("➕ Upload Bot", "success"))
    m.row(_btn("🐙 GitHub Host", "primary"), _btn("📊 Dashboard", "primary"))
    m.row(_btn("❔ Help", "primary"))
    return m

def kb_bots(uid: int) -> types.ReplyKeyboardMarkup:
    m = _rkm(1)
    bots = BOT_INDEX.get(uid, [])
    for i, bid in enumerate(bots[:30], 1):
        b = get_bot(bid)
        if b:
            m.row(_btn(f"{i}. {b['name'][:40]}", "primary"))
    m.row(_btn("⬅️ Main Menu", "primary"))
    return m

def kb_actions(uid: int) -> types.ReplyKeyboardMarkup:
    bid = SELECTED_BOT.get(uid)
    b = get_bot(bid) if bid else None
    if not b:
        return kb_main()
    st = RUNNING.get(bid)
    running = bool(st and st.get("proc") and st["proc"].poll() is None)
    m = _rkm(2)
    if running:
        m.row(_btn("🔴 Stop", "danger"), _btn("🔄 Restart", "primary"))
    else:
        m.row(_btn("🟢 Start", "success"), _btn("🔄 Restart", "primary"))
    m.row(_btn("📊 Status", "primary"), _btn("📜 Logs", "primary"))
    # 🆕 PATCH 6c: Live button
    m.row(_btn("🔴 Live", "danger"))
    m.row(_btn("📁 Files", "primary"), _btn("🔐 Env Vars", "primary"))
    m.row(_btn("📦 Pip", "primary"), _btn("🌐 Tunnel", "primary"))
    m.row(_btn("⛔ Stop Tunnel", "danger"), _btn("🗃 Backup", "primary"))
    m.row(_btn("✏️ Edit", "primary"), _btn("📋 Clone Bot", "primary"))
    m.row(_btn("🗑 Delete Bot", "danger"))
    m.row(_btn("⬅️ My Bots", "primary"))
    return m

def kb_env() -> types.ReplyKeyboardMarkup:
    m = _rkm(2)
    m.row(_btn("👁 Show Vars", "primary"))
    m.row(_btn("➕ Set Variable", "success"), _btn("📥 Bulk Import", "success"))
    m.row(_btn("➖ Delete Variable", "danger"))
    m.row(_btn("⬅️ Bot Menu", "primary"))
    return m

def kb_backup() -> types.ReplyKeyboardMarkup:
    m = _rkm(2)
    m.row(_btn("⬇️ Download Backup", "primary"), _btn("♻️ Restore Backup", "primary"))
    m.row(_btn("⬅️ Bot Menu", "primary"))
    return m

def kb_edit() -> types.ReplyKeyboardMarkup:
    m = _rkm(2)
    m.row(_btn("✏️ Rename", "primary"), _btn("🎯 Set Entry", "primary"))
    m.row(_btn("⏰ Daily Restart", "primary"))
    m.row(_btn("⬅️ Bot Menu", "primary"))
    return m

def kb_files() -> types.ReplyKeyboardMarkup:
    m = _rkm(2)
    m.row(_btn("📋 List Files", "primary"), _btn("➕ Upload File", "success"))
    m.row(_btn("⬇️ Download File", "primary"), _btn("⬇️ Download Project", "primary"))
    m.row(_btn("🗑 Delete File", "danger"))
    m.row(_btn("⬅️ Bot Menu", "primary"))
    return m

def kb_delete_confirm() -> types.ReplyKeyboardMarkup:
    m = _rkm(1)
    m.row(_btn("🔴 YES DELETE", "danger"))
    m.row(_btn("⬅️ Cancel", "primary"))
    return m

def current_kb(uid: int) -> types.ReplyKeyboardMarkup:
    level = MENU_LEVEL.get(uid, "main")
    if level == "bots":           return kb_bots(uid)
    if level == "actions":        return kb_actions(uid)
    if level == "env":            return kb_env()
    if level == "backup":         return kb_backup()
    if level == "edit":           return kb_edit()
    if level == "files":          return kb_files()
    if level == "delete_confirm": return kb_delete_confirm()
    return kb_main()

# ── Telegram UI ────────────────────────────────────────────────────────────
bot = telebot.TeleBot(BOT_TOKEN, threaded=True, num_threads=8, parse_mode="HTML")

def send_pre_chunks(chat_id: int, title: str, content: str,
                    reply_markup: Optional[types.ReplyKeyboardMarkup] = None) -> None:
    LIMIT = 3800
    content = content or "(empty)"
    chunks: list[list[str]] = []
    cur: list[str] = []
    cur_len = 0
    for line in content.split("\n"):
        while len(line) > LIMIT:
            if cur:
                chunks.append(cur); cur = []; cur_len = 0
            chunks.append([line[:LIMIT]])
            line = line[LIMIT:]
        ll = len(line) + 1
        if cur and cur_len + ll > LIMIT:
            chunks.append(cur); cur = []; cur_len = 0
        cur.append(line); cur_len += ll
    if cur:
        chunks.append(cur)
    for i, chunk in enumerate(chunks):
        body = "\n".join(chunk)
        header = f"<b>{esc(title)}</b>\n" if i == 0 else ""
        text = header + "<pre>" + esc(body) + "</pre>"
        if len(text) > 4050:
            text = text[:4050] + "…</pre>"
        bot.send_message(chat_id, text,
                         reply_markup=reply_markup if i == len(chunks) - 1 else None)

def set_state(uid: int, flow: str, bot_id: Optional[str] = None, **extra: Any) -> None:
    STATES[uid] = {"flow": flow, "bot_id": bot_id, **extra}

def throttled(uid: int) -> bool:
    now = time.time()
    if now - LAST_ACTION.get(uid, 0) < 0.5:
        return True
    LAST_ACTION[uid] = now
    return False

def _reset_menus(uid: int) -> None:
    MENU_LEVEL.pop(uid, None)
    SELECTED_BOT.pop(uid, None)
    BOT_INDEX.pop(uid, None)
    STATES.pop(uid, None)

def send_home(chat_id: int, uid: int, celebrate: bool = False) -> None:
    MENU_LEVEL[uid] = "main"
    header = "🎉 <b>Rᴜsʜᴇʀ Kɪɴɢ</b> 🎉\n\n" if celebrate else ""
    text = (
        f"{header}"
        "<b>⚡ Advanced Bot Hosting</b>\n"
        "Host & manage Python/Node bots.\n\n"
        "Choose:"
    )
    if celebrate:
        try:
            bot.send_message(chat_id, text, reply_markup=kb_main(), message_effect_id=PARTY_EFFECT_ID)
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=kb_main())

def show_bots(chat_id: int, uid: int) -> None:
    bots = user_bots(uid)
    BOT_INDEX[uid] = [b["id"] for b in bots]
    STATES.pop(uid, None)
    if not bots:
        MENU_LEVEL[uid] = "main"
        bot.send_message(chat_id, "No bots yet. Just send me a .py / .js / .zip file to get started!",
                         reply_markup=kb_main())
        return
    MENU_LEVEL[uid] = "bots"
    lines = [f"<b>📦 My Bots</b> · {len(bots)}/{MAX_BOTS_PER_USER}", ""]
    for i, b in enumerate(bots[:30], 1):
        st = RUNNING.get(b["id"])
        running = bool(st and st.get("proc") and st["proc"].poll() is None)
        icon = "🟢" if running else "🔴"
        lines.append(f"{i}. {icon} <b>{esc(b['name'][:60])}</b>")
    if len(bots) > 30:
        lines.append(f"\n... and {len(bots)-30} more (see 📊 Dashboard)")
    lines.append("\nTap a bot below to manage it.")
    bot.send_message(chat_id, "\n".join(lines), reply_markup=kb_bots(uid))

def show_bot_card(chat_id: int, uid: int, bot_id: str) -> None:
    b = owned_bot(uid, bot_id)
    if not b:
        MENU_LEVEL[uid] = "main"
        bot.send_message(chat_id, "Bot not found.", reply_markup=kb_main())
        return
    SELECTED_BOT[uid] = bot_id
    MENU_LEVEL[uid] = "actions"
    STATES.pop(uid, None)
    bot.send_message(chat_id, bot_status(bot_id), reply_markup=kb_actions(uid))

def show_dashboard(chat_id: int, uid: int) -> None:
    MENU_LEVEL[uid] = "main"
    items = user_bots(uid)
    if not items:
        bot.send_message(chat_id, "No bots yet.", reply_markup=kb_main())
        return
    lines = [f"<b>📊 Dashboard — {len(items)}/{MAX_BOTS_PER_USER} bots</b>\n"]
    running_count = 0
    for b in items[:200]:
        st = RUNNING.get(b["id"])
        running = bool(st and st.get("proc") and st["proc"].poll() is None)
        if running:
            running_count += 1
        icon = "🟢" if running else ("⚠️" if b.get("resource_killed") else "🔴")
        extra = ""
        if running and psutil:
            try:
                p = psutil.Process(st["proc"].pid)
                extra = f" | {_format_bytes(p.memory_info().rss)} | {int(time.time() - st['started'])}s"
            except Exception:
                pass
        lines.append(f"{icon} <code>{b['id']}</code> <b>{esc(b['name'][:60])}</b>{extra}")
    lines.append(f"\n🟢 Running: {running_count} / {len(items)}")
    bot.send_message(chat_id, "\n".join(lines), reply_markup=kb_main())

def show_env(chat_id: int, uid: int, bot_id: str) -> None:
    b = owned_bot(uid, bot_id)
    if not b:
        MENU_LEVEL[uid] = "main"
        bot.send_message(chat_id, "Bot not found.", reply_markup=kb_main())
        return
    SELECTED_BOT[uid] = bot_id
    MENU_LEVEL[uid] = "env"
    STATES.pop(uid, None)
    envs = decrypt_obj(b.get("env_enc", ""))
    if envs:
        lines = ["<b>🔐 Env Vars</b>", ""]
        for k, v in envs.items():
            masked = "•" * min(len(v), 12) if v else "(empty)"
            lines.append(f"<code>{esc(k)}</code> = <code>{masked}</code>")
        body = "\n".join(lines)
    else:
        body = "<b>🔐 Env Vars</b>\n\nNo custom variables yet."
    bot.send_message(chat_id, body, reply_markup=kb_env())

def show_files(chat_id: int, uid: int, bot_id: str) -> None:
    b = owned_bot(uid, bot_id)
    if not b:
        MENU_LEVEL[uid] = "main"
        bot.send_message(chat_id, "Bot not found.", reply_markup=kb_main())
        return
    SELECTED_BOT[uid] = bot_id
    MENU_LEVEL[uid] = "files"
    STATES.pop(uid, None)
    files = project_files(bot_id)
    if not files:
        body = "<b>📁 Files</b>\n\nNo files."
    else:
        root = project_dir(bot_id)
        lines = ["<b>📁 Files</b>", ""]
        total = 0
        for p in files[:200]:
            sz = p.stat().st_size
            total += sz
            lines.append(f"<code>{esc(p.relative_to(root).as_posix())}</code> · {_format_bytes(sz)}")
        if len(files) > 200:
            lines.append(f"\n... and {len(files)-200} more")
        lines.append(f"\nTotal: {_format_bytes(total)}")
        body = "\n".join(lines)
    bot.send_message(chat_id, body, reply_markup=kb_files())

# ── Commands ──────────────────────────────────────────────────────────────
@bot.message_handler(commands=["start", "menu"])
def cmd_start(m: types.Message) -> None:
    if m.chat.type != "private":
        return
    uid = m.from_user.id
    _reset_menus(uid)
    if not get_user(uid):
        save_user(uid, id=uid,
                  name=m.from_user.first_name or "",
                  username=m.from_user.username or "",
                  joined=int(time.time()))
    send_home(m.chat.id, uid, celebrate=True)

@bot.message_handler(commands=["help"])
def cmd_help(m: types.Message) -> None:
    ram = f"{MAX_BOT_RAM_MB} MB" if MAX_BOT_RAM_MB else "unlimited"
    disk = f"{MAX_BOT_DISK_MB} MB" if MAX_BOT_DISK_MB else "unlimited"
    bot.reply_to(m,
        "<b>Help</b>\n"
        "• Send .py/.js/.zip directly → bot auto-created and started\n"
        "• Or use 🐙 GitHub Host to clone a repo\n"
        "• Each project gets its own Python venv\n"
        "• Env vars encrypted at rest\n"
        "• Auto-pip installs missing imports\n"
        f"• Upload limit: {_MAX_MB} MB\n"
        f"• RAM cap: {ram}\n"
        f"• Disk cap: {disk}\n"
        "• Commands: /start /bots /cancel /id /token /grep",
        reply_markup=current_kb(m.from_user.id))

@bot.message_handler(commands=["bots"])
def cmd_bots(m: types.Message) -> None:
    show_bots(m.chat.id, m.from_user.id)

@bot.message_handler(commands=["cancel"])
def cmd_cancel(m: types.Message) -> None:
    uid = m.from_user.id
    STATES.pop(uid, None)
    bot.reply_to(m, "Cancelled.", reply_markup=current_kb(uid))

@bot.message_handler(commands=["id"])
def cmd_id(m: types.Message) -> None:
    bot.reply_to(m, f"<code>{m.from_user.id}</code>")

@bot.message_handler(commands=["token"])
def cmd_token(m: types.Message) -> None:
    set_state(m.from_user.id, "gh_token")
    bot.reply_to(m, "Send GitHub personal access token:\n/cancel to abort.")

@bot.message_handler(commands=["grep"])
def cmd_grep(m: types.Message) -> None:
    uid = m.from_user.id
    bid = SELECTED_BOT.get(uid)
    if not bid:
        bot.reply_to(m, "Pehle koi bot select karo (📦 My Bots → bot)")
        return
    args = (m.text or "").split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        bot.reply_to(
            m,
            "Usage: <code>/grep ERROR</code>\n\n"
            "Examples:\n"
            "<code>/grep ERROR</code>\n"
            "<code>/grep Traceback</code>\n"
            "<code>/grep 409</code>\n"
            "<code>/grep ModuleNotFound</code>",
            parse_mode="HTML",
        )
        return
    pattern = args[1].strip()
    log = tail_logs(bid, lines=5000)
    if log == "No logs yet.":
        bot.reply_to(m, "No logs available.")
        return
    matches = [ln for ln in log.splitlines() if pattern.lower() in ln.lower()]
    if not matches:
        bot.reply_to(
            m,
            f"🔍 No matches for <code>{esc(pattern)}</code> in last 5000 lines.",
            parse_mode="HTML",
        )
        return
    matches = matches[-100:]
    title = f"🔍 '{pattern}' — {len(matches)} match(es)"
    send_pre_chunks(m.chat.id, title, "\n".join(matches),
                    reply_markup=kb_actions(uid))

# ═══════════════════════════════════════════════════════════════════════════
# UPLOAD + AUTO-START
# ═══════════════════════════════════════════════════════════════════════════
def _do_upload_new_bot(m: types.Message) -> None:
    uid = m.from_user.id
    doc = m.document
    if not doc:
        return
    fname = safe_name(doc.file_name or "upload.bin")
    fname_lower = fname.lower()

    if not fname_lower.endswith((".py", ".js", ".mjs", ".cjs", ".zip")):
        bot.reply_to(
            m,
            f"❌ Unsupported file type: <code>{esc(fname)}</code>\n\n"
            f"Allowed: <code>.py</code>, <code>.js</code>, <code>.mjs</code>, <code>.cjs</code>, <code>.zip</code>",
            parse_mode="HTML",
        )
        return

    rec = None
    try:
        try:
            status_msg = bot.reply_to(m, "📥 <b>Downloading file...</b>", parse_mode="HTML")
        except Exception:
            status_msg = None

        def _edit_status(text: str) -> None:
            if not status_msg:
                return
            try:
                bot.edit_message_text(text, chat_id=status_msg.chat.id,
                                      message_id=status_msg.message_id,
                                      parse_mode="HTML")
            except Exception:
                pass

        info = bot.get_file(doc.file_id)
        raw = bot.download_file(info.file_path)

        _edit_status("📦 <b>Extracting &amp; saving...</b>")

        rec = _create_record(uid, Path(fname).stem)
        root = project_dir(rec["id"])

        if fname_lower.endswith(".zip"):
            count = safe_extract_zip(raw, root)
        else:
            (root / fname).write_bytes(raw)
            count = 1

        entry = detect_entry(root)
        mutate_bot(rec["id"], entry=entry)

        _edit_status("🔍 <b>Running security scan...</b>")

        hardcoded = _scan_hardcoded_token(rec["id"])

        STATES.pop(uid, None)
        if status_msg:
            try:
                bot.delete_message(status_msg.chat.id, status_msg.message_id)
            except Exception:
                pass

        bot.send_message(
            m.chat.id,
            f"✅ <b>{esc(rec['name'])}</b>\n"
            f"Files: {count}\n"
            f"Entry: <code>{esc(entry)}</code>",
            parse_mode="HTML",
        )

        if hardcoded:
            masked = hardcoded[:12] + "..." + hardcoded[-4:]
            bot.send_message(
                m.chat.id,
                f"⚠️ <b>Security Warning — Hardcoded BOT_TOKEN Detected!</b>\n\n"
                f"Found in source: <code>{esc(masked)}</code>\n\n"
                f"<b>Why is this bad?</b>\n"
                f"• Token leaks if you share code, screenshot, or backup\n"
                f"• Anyone with token can control your bot\n\n"
                f"<b>How to fix (recommended):</b>\n"
                f"1. Remove token from your code\n"
                f"2. Use <code>os.getenv('BOT_TOKEN')</code> instead\n"
                f"3. Add BOT_TOKEN via 🔐 Env Vars\n\n"
                f"<b>Example:</b>\n"
                f"<pre>import os, telebot\n"
                f"bot = telebot.TeleBot(os.getenv('BOT_TOKEN'))</pre>",
                parse_mode="HTML",
            )

            # 🆕 PATCH 2b: Duplicate token check
            dup_bot_id = _find_duplicate_token(uid, rec["id"], hardcoded)
            if dup_bot_id:
                dup_bot = get_bot(dup_bot_id)
                dup_name = dup_bot["name"] if dup_bot else dup_bot_id
                bot.send_message(
                    m.chat.id,
                    f"🔴 <b>Duplicate Token Detected!</b>\n\n"
                    f"Token already used by: <b>{esc(dup_name)}</b>\n\n"
                    f"Telegram will throw <b>409 Conflict</b> — both bots fail.\n\n"
                    f"<b>Fix:</b> Use a different BOT_TOKEN.\n\n"
                    f"Bot created but <b>NOT auto-started</b>.",
                    parse_mode="HTML",
                )
                mutate_bot(rec["id"], failed_state=True, failed_reason="Duplicate token")
                show_bot_card(m.chat.id, uid, rec["id"])
                return

        start_msg = bot.send_message(
            m.chat.id,
            f"🚀 <b>Auto-starting {esc(rec['name'])}...</b>\n"
            f"<i>Installing dependencies and starting. This may take 30-60s...</i>",
            parse_mode="HTML",
        )

        def _bg_start():
            try:
                ok, msg = start_process(rec["id"], manual=False)
                if ok:
                    try:
                        bot.edit_message_text(
                            f"🟢 <b>{esc(rec['name'])}</b> ✅ started\n{esc(msg)}",
                            chat_id=start_msg.chat.id,
                            message_id=start_msg.message_id,
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
                else:
                    try:
                        bot.edit_message_text(
                            f"❌ <b>Start failed</b>: {esc(msg)}\n\n"
                            f"Tip: check logs with 📜 Logs, or set BOT_TOKEN via 🔐 Env Vars then press 🟢 Start",
                            chat_id=start_msg.chat.id,
                            message_id=start_msg.message_id,
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
            except Exception as exc:
                try:
                    bot.edit_message_text(
                        f"❌ Start error: <code>{esc(exc)}</code>",
                        chat_id=start_msg.chat.id,
                        message_id=start_msg.message_id,
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

        threading.Thread(target=_bg_start, daemon=True, name=f"auto-start-{rec['id']}").start()

        def _show_card_after():
            time.sleep(2.0)
            try:
                show_bot_card(m.chat.id, uid, rec["id"])
            except Exception:
                pass
        threading.Thread(target=_show_card_after, daemon=True).start()

    except Exception as exc:
        if rec:
            try:
                shutil.rmtree(project_dir(rec["id"]), ignore_errors=True)
                d = db_load(); d["bots"].pop(rec["id"], None); db_save(d)
            except Exception:
                pass
        STATES.pop(uid, None)
        bot.reply_to(m, "❌ " + esc(exc), reply_markup=current_kb(uid))


# ── Document handler ─────────────────────────────────────────────────────
@bot.message_handler(content_types=["document"])
def on_document(m: types.Message) -> None:
    uid = m.from_user.id
    st = STATES.get(uid, {})
    flow = st.get("flow")
    doc = m.document
    if not doc:
        return

    if (doc.file_size or 0) > MAX_UPLOAD:
        bot.reply_to(
            m,
            f"❌ File too large. Max {_format_bytes(MAX_UPLOAD)}.",
            reply_markup=current_kb(uid),
        )
        return

    fname = safe_name(doc.file_name or "upload.bin")
    fname_lower = fname.lower()

    if flow == "restore_zip":
        bid = st.get("bot_id")
        if not bid or not owned_bot(uid, bid):
            bot.reply_to(m, "Access denied.", reply_markup=current_kb(uid))
            STATES.pop(uid, None)
            return
        if not fname_lower.endswith(".zip"):
            bot.reply_to(m, "Restore requires ZIP.", reply_markup=current_kb(uid))
            return
        try:
            info = bot.get_file(doc.file_id)
            raw = bot.download_file(info.file_path)
            count = restore_backup(bid, raw)
            STATES.pop(uid, None)
            bot.reply_to(m, f"✅ Restored {count} files.")
            show_bot_card(m.chat.id, uid, bid)
        except Exception as exc:
            STATES.pop(uid, None)
            bot.reply_to(m, "❌ " + esc(exc), reply_markup=current_kb(uid))
        return

    if flow == "upload_file":
        bid = st.get("bot_id")
        if not bid or not owned_bot(uid, bid):
            bot.reply_to(m, "Access denied.", reply_markup=current_kb(uid))
            STATES.pop(uid, None)
            return
        if fname_lower.endswith(".zip"):
            bot.reply_to(m, "ZIP not allowed here. Upload as a new bot instead.",
                         reply_markup=current_kb(uid))
            return
        try:
            info = bot.get_file(doc.file_id)
            raw = bot.download_file(info.file_path)
            target = safe_project_path(project_dir(bid), fname)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            STATES.pop(uid, None)
            bot.reply_to(m, f"✅ Saved <code>{esc(fname)}</code>",
                         reply_markup=kb_files())
        except Exception as exc:
            STATES.pop(uid, None)
            bot.reply_to(m, "❌ " + esc(exc), reply_markup=current_kb(uid))
        return

    _do_upload_new_bot(m)


# ── Bot action router ────────────────────────────────────────────────────
def _handle_bot_action(m: types.Message, uid: int, text: str, bid: str) -> bool:
    b = owned_bot(uid, bid)
    if not b:
        bot.send_message(m.chat.id, "Bot not found.", reply_markup=kb_main())
        MENU_LEVEL[uid] = "main"
        return True
    st = RUNNING.get(bid)
    running = bool(st and st.get("proc") and st["proc"].poll() is None)

    if text in ("🟢 Start", "🔴 Stop"):
        if running:
            mutate_bot(bid, desired_running=False)
            stop_process(bid)
            bot.send_message(m.chat.id, "⏹ Stopped.", reply_markup=kb_actions(uid))
        else:
            # 🆕 PATCH 1d: Reset crash loop on manual start
            mutate_bot(bid, failed_state=False, failed_reason="", recent_crashes=[])
            status_msg = bot.send_message(m.chat.id, "🚀 Starting (installing deps)...",
                                          reply_markup=kb_actions(uid))

            def _bg_start():
                try:
                    ok, msg = start_process(bid)
                    try:
                        bot.edit_message_text(
                            ("✅ " if ok else "❌ ") + esc(msg),
                            chat_id=status_msg.chat.id,
                            message_id=status_msg.message_id,
                            reply_markup=kb_actions(uid),
                        )
                    except Exception:
                        pass
                except Exception as exc:
                    try:
                        bot.edit_message_text(
                            f"❌ {esc(exc)}",
                            chat_id=status_msg.chat.id,
                            message_id=status_msg.message_id,
                        )
                    except Exception:
                        pass
            threading.Thread(target=_bg_start, daemon=True).start()
        return True

    if text == "🔄 Restart":
        status_msg = bot.send_message(m.chat.id, "🔄 Restarting...", reply_markup=kb_actions(uid))
        def _bg_restart():
            try:
                ok, msg = restart_process(bid)
                try:
                    bot.edit_message_text(
                        ("✅ " if ok else "❌ ") + esc(msg),
                        chat_id=status_msg.chat.id,
                        message_id=status_msg.message_id,
                        reply_markup=kb_actions(uid),
                    )
                except Exception:
                    pass
            except Exception as exc:
                try:
                    bot.edit_message_text(f"❌ {esc(exc)}",
                                          chat_id=status_msg.chat.id,
                                          message_id=status_msg.message_id)
                except Exception:
                    pass
        threading.Thread(target=_bg_restart, daemon=True).start()
        return True

    if text == "📊 Status":
        bot.send_message(m.chat.id, bot_status(bid), reply_markup=kb_actions(uid))
        return True

    # 🆕 PATCH 5a: Log download
    if text == "📜 Logs":
        log_content = tail_logs(bid, lines=5000)
        send_pre_chunks(m.chat.id, "Logs", log_content[-10000:], reply_markup=kb_actions(uid))
        try:
            full_log = tail_logs(bid, lines=50000)
            buf = io.BytesIO(full_log.encode("utf-8"))
            buf.name = f"{bid}_logs.txt"
            b_obj = get_bot(bid)
            bot.send_document(
                m.chat.id, buf,
                caption=f"📄 Full log ({len(full_log.splitlines())} lines)",
                visible_file_name=f"{safe_name(b_obj['name'] if b_obj else bid)}_logs.txt",
            )
        except Exception:
            pass
        return True

    # 🆕 PATCH 6b: Live log streaming
    if text == "🔴 Live":
        if bid in LIVE_LOGS_ACTIVE:
            bot.send_message(m.chat.id, "Already streaming. Press any button to stop.",
                             reply_markup=kb_actions(uid))
            return True
        LIVE_LOGS_ACTIVE[bid] = m.chat.id
        stop_flag = threading.Event()
        LIVE_LOGS_STOP[bid] = stop_flag
        msg = bot.send_message(m.chat.id, "🔴 <b>Live streaming...</b>\n<i>Tap any button to stop</i>",
                                parse_mode="HTML", reply_markup=kb_actions(uid))
        last_size = [0]
        def _stream():
            try:
                p = _log_path(bid)
                if p.exists():
                    last_size[0] = p.stat().st_size
                while not stop_flag.is_set():
                    time.sleep(2.0)
                    if not p.exists():
                        continue
                    cur_size = p.stat().st_size
                    if cur_size <= last_size[0]:
                        continue
                    with p.open("rb") as fh:
                        fh.seek(last_size[0])
                        new_data = fh.read(cur_size - last_size[0])
                    last_size[0] = cur_size
                    new_text = new_data.decode("utf-8", "replace")[-2000:]
                    if not new_text.strip():
                        continue
                    try:
                        bot.edit_message_text(
                            f"🔴 <b>Live: {esc(b['name'])}</b>\n<pre>{esc(new_text)}</pre>",
                            chat_id=msg.chat.id, message_id=msg.message_id,
                            parse_mode="HTML", reply_markup=kb_actions(uid),
                        )
                    except Exception:
                        pass
            except Exception:
                pass
            finally:
                LIVE_LOGS_ACTIVE.pop(bid, None)
                LIVE_LOGS_STOP.pop(bid, None)
        threading.Thread(target=_stream, daemon=True).start()
        return True

    if text == "📁 Files":
        show_files(m.chat.id, uid, bid)
        return True

    if text == "🔐 Env Vars":
        show_env(m.chat.id, uid, bid)
        return True

    if text == "📦 Pip":
        set_state(uid, "pip_install", bid)
        bot.send_message(m.chat.id,
            "Send packages separated by spaces (e.g. <code>requests python-dotenv</code>)\n\n"
            "Or send:\n<code>requirements</code> → install requirements.txt\n"
            "<code>auto</code> → scan imports and install missing",
            reply_markup=kb_actions(uid))
        return True

    if text == "🌐 Tunnel":
        set_state(uid, "tunnel_port", bid)
        bot.send_message(m.chat.id,
            "Send local port for Cloudflare quick tunnel (requires cloudflared).",
            reply_markup=kb_actions(uid))
        return True

    if text == "⛔ Stop Tunnel":
        with _proc_lock:
            tunnel = RUNNING.get(bid, {}).get("tunnel")
        if tunnel and tunnel.poll() is None:
            try:
                if os.name == "nt":
                    tunnel.terminate()
                else:
                    os.killpg(tunnel.pid, signal.SIGTERM)
                with _proc_lock:
                    if bid in RUNNING:
                        RUNNING[bid]["tunnel"] = None
                bot.send_message(m.chat.id, "⛔ Tunnel stopped.", reply_markup=kb_actions(uid))
            except Exception as exc:
                bot.send_message(m.chat.id, "Stop failed: " + esc(exc),
                                 reply_markup=kb_actions(uid))
        else:
            bot.send_message(m.chat.id, "No tunnel running.", reply_markup=kb_actions(uid))
        return True

    if text == "🗃 Backup":
        MENU_LEVEL[uid] = "backup"
        bot.send_message(m.chat.id, "Backup menu", reply_markup=kb_backup())
        return True

    if text == "✏️ Edit":
        MENU_LEVEL[uid] = "edit"
        bot.send_message(m.chat.id, "Edit menu", reply_markup=kb_edit())
        return True

    if text == "📋 Clone Bot":
        try:
            new_rec = clone_bot(uid, bid)
            if new_rec:
                bot.send_message(m.chat.id,
                    f"📋 Cloned as <b>{esc(new_rec['name'])}</b>\nID: <code>{new_rec['id']}</code>",
                    reply_markup=kb_actions(uid))
            else:
                bot.send_message(m.chat.id, "Clone failed.", reply_markup=kb_actions(uid))
        except Exception as exc:
            bot.send_message(m.chat.id, "Clone failed: " + esc(exc),
                             reply_markup=kb_actions(uid))
        return True

    if text == "🗑 Delete Bot":
        MENU_LEVEL[uid] = "delete_confirm"
        bot.send_message(m.chat.id,
            f"Delete <b>{esc(b['name'])}</b> and all files?\n\nTap 🔴 YES DELETE to confirm.",
            reply_markup=kb_delete_confirm())
        return True

    return False


# ── Menu button router ───────────────────────────────────────────────────
def _handle_menu_button(m: types.Message, uid: int, text: str) -> bool:
    level = MENU_LEVEL.get(uid, "main")

    # 🆕 PATCH 6d: Stop live logs on any button press
    bid_sel = SELECTED_BOT.get(uid)
    if bid_sel and bid_sel in LIVE_LOGS_STOP:
        LIVE_LOGS_STOP[bid_sel].set()
        LIVE_LOGS_ACTIVE.pop(bid_sel, None)

    if text == "📦 My Bots":
        show_bots(m.chat.id, uid); return True
    if text == "➕ Upload Bot":
        set_state(uid, "upload_new")
        bot.send_message(m.chat.id,
            f"📤 Just send me your <code>.py</code>, <code>.js</code>, or <code>.zip</code> file directly.\n"
            f"Bot will be created and started automatically!\n\n"
            f"Max size: {_MAX_MB} MB",
            parse_mode="HTML",
            reply_markup=kb_main())
        MENU_LEVEL[uid] = "main"
        return True
    if text == "🐙 GitHub Host":
        set_state(uid, "github_url")
        bot.send_message(m.chat.id,
            "Send GitHub repo URL.\nPublic: <code>https://github.com/user/repo</code>\n\n"
            "For private repos send /token first to set token.",
            reply_markup=kb_main())
        MENU_LEVEL[uid] = "main"
        return True
    if text == "📊 Dashboard":
        show_dashboard(m.chat.id, uid); return True
    if text == "❔ Help":
        cmd_help(m); return True
    if text == "⬅️ Main Menu":
        _reset_menus(uid)
        MENU_LEVEL[uid] = "main"
        bot.send_message(m.chat.id, "Main menu.", reply_markup=kb_main()); return True
    if text == "⬅️ My Bots":
        show_bots(m.chat.id, uid); return True
    if text == "⬅️ Bot Menu":
        bid = SELECTED_BOT.get(uid)
        if bid:
            show_bot_card(m.chat.id, uid, bid)
        else:
            show_bots(m.chat.id, uid)
        return True
    if text == "⬅️ Cancel":
        bid = SELECTED_BOT.get(uid)
        if bid:
            show_bot_card(m.chat.id, uid, bid)
        else:
            send_home(m.chat.id, uid)
        return True

    if level == "bots":
        mm = re.match(r"^(\d+)\.\s", text)
        if mm:
            try:
                idx = int(mm.group(1)) - 1
                bots = BOT_INDEX.get(uid, [])
                if 0 <= idx < len(bots):
                    show_bot_card(m.chat.id, uid, bots[idx])
                    return True
            except Exception:
                pass

    if level == "actions":
        bid = SELECTED_BOT.get(uid)
        if bid and _handle_bot_action(m, uid, text, bid):
            return True

    if level == "env":
        bid = SELECTED_BOT.get(uid)
        if not bid:
            send_home(m.chat.id, uid); return True
        if text == "👁 Show Vars":
            show_env(m.chat.id, uid, bid); return True
        if text == "➕ Set Variable":
            set_state(uid, "env_set", bid)
            bot.send_message(m.chat.id, "Send <code>KEY=VALUE</code>", reply_markup=kb_env())
            return True
        if text == "📥 Bulk Import":
            set_state(uid, "env_bulk", bid)
            bot.send_message(m.chat.id,
                "Send multiple lines, one per var:\n<code>KEY1=val1\nKEY2=val2</code>",
                reply_markup=kb_env())
            return True
        if text == "➖ Delete Variable":
            set_state(uid, "env_delete", bid)
            bot.send_message(m.chat.id, "Send variable name to delete.",
                             reply_markup=kb_env())
            return True

    if level == "backup":
        bid = SELECTED_BOT.get(uid)
        if not bid:
            send_home(m.chat.id, uid); return True
        if text == "⬇️ Download Backup":
            try:
                b = get_bot(bid)
                raw = make_backup(bid)
                buf = io.BytesIO(raw)
                buf.name = f"{safe_name(b['name'])}-{bid}.zip"
                bot.send_document(m.chat.id, buf,
                    caption="Full backup (source + bot_meta.json).",
                    reply_markup=kb_backup())
            except Exception as exc:
                bot.send_message(m.chat.id, "Failed: " + esc(exc),
                                 reply_markup=kb_backup())
            return True
        if text == "♻️ Restore Backup":
            set_state(uid, "restore_zip", bid)
            bot.send_message(m.chat.id, "Send a ZIP backup. Source files will be replaced.",
                             reply_markup=kb_backup())
            return True

    if level == "edit":
        bid = SELECTED_BOT.get(uid)
        if not bid:
            send_home(m.chat.id, uid); return True
        if text == "✏️ Rename":
            set_state(uid, "rename", bid)
            bot.send_message(m.chat.id, "Send new name (max 200 chars).",
                             reply_markup=kb_edit())
            return True
        if text == "🎯 Set Entry":
            root = project_dir(bid)
            cands = [p.relative_to(root).as_posix() for p in project_files(bid)
                     if p.suffix.lower() in {".py", ".js", ".mjs", ".cjs"}]
            set_state(uid, "entry_set", bid)
            hint = "\n".join(f"• <code>{esc(c)}</code>" for c in cands[:20]) or "(none)"
            bot.send_message(m.chat.id,
                f"Send path to entry file:\n{hint}", reply_markup=kb_edit())
            return True
        if text == "⏰ Daily Restart":
            b = get_bot(bid)
            current = (b.get("daily_restart") or "").strip() or "(disabled)"
            bot.send_message(
                m.chat.id,
                f"<b>⏰ Daily Auto-Restart</b>\n\n"
                f"Current: <code>{esc(current)}</code>\n\n"
                f"Send time in <b>HH:MM</b> (UTC), e.g. <code>04:30</code>\n"
                f"Or send <code>off</code> to disable.\n\n"
                f"<i>Tip: 4:30 AM UTC = low traffic, best for restarts.</i>",
                parse_mode="HTML",
                reply_markup=kb_edit(),
            )
            set_state(uid, "daily_restart_set", bid)
            return True

    if level == "files":
        bid = SELECTED_BOT.get(uid)
        if not bid:
            send_home(m.chat.id, uid); return True
        if text == "📋 List Files":
            show_files(m.chat.id, uid, bid); return True
        if text == "➕ Upload File":
            set_state(uid, "upload_file", bid)
            bot.send_message(m.chat.id, "Send a file to add/replace.",
                             reply_markup=kb_files())
            return True
        if text == "⬇️ Download File":
            set_state(uid, "file_download", bid)
            bot.send_message(m.chat.id, "Send relative path to download.",
                             reply_markup=kb_files())
            return True
        if text == "⬇️ Download Project":
            try:
                b = get_bot(bid)
                raw = make_backup(bid)
                buf = io.BytesIO(raw)
                buf.name = f"{safe_name(b['name'])}-{bid}.zip"
                bot.send_document(m.chat.id, buf,
                    caption="Full project backup.",
                    reply_markup=kb_files())
            except Exception as exc:
                bot.send_message(m.chat.id, "Failed: " + esc(exc),
                                 reply_markup=kb_files())
            return True
        if text == "🗑 Delete File":
            set_state(uid, "file_delete", bid)
            bot.send_message(m.chat.id, "Send relative path to delete.",
                             reply_markup=kb_files())
            return True

    if level == "delete_confirm":
        bid = SELECTED_BOT.get(uid)
        if not bid:
            send_home(m.chat.id, uid); return True
        if text == "🔴 YES DELETE":
            stop_process(bid)
            d = db_load()
            d["bots"].pop(bid, None)
            db_save(d)
            shutil.rmtree(project_dir(bid), ignore_errors=True)
            SELECTED_BOT.pop(uid, None)
            MENU_LEVEL[uid] = "main"
            bot.send_message(m.chat.id, "🗑 Deleted.", reply_markup=kb_main())
            return True

    return False


# ── Text handler (buttons + state flows) ─────────────────────────────────
@bot.message_handler(content_types=["text"])
def on_text(m: types.Message) -> None:
    uid = m.from_user.id
    text = (m.text or "").strip()
    if not text:
        return

    if _handle_menu_button(m, uid, text):
        return

    st = STATES.get(uid)
    if not st:
        return
    flow = st.get("flow")
    bid = st.get("bot_id")
    b = owned_bot(uid, bid) if bid else None

    if flow in {"upload_new", "upload_file", "restore_zip"}:
        bot.reply_to(m, "Please send the file, or /cancel.",
                     reply_markup=current_kb(uid))
        return

    try:
        if flow == "github_url":
            if len(user_bots(uid)) >= MAX_BOTS_PER_USER:
                raise ValueError("Hosting limit reached")
            rec = _create_record(uid, "GitHub Project", "github")
            root = project_dir(rec["id"])
            token = get_user(uid).get("gh_token")
            tmp = Path(tempfile.mkdtemp(prefix="ghclone-", dir=str(PROJECTS_DIR)))
            try:
                ok, msg = clone_public_github(text, tmp / "repo", token=token)
                if not ok:
                    shutil.rmtree(root, ignore_errors=True)
                    d = db_load(); d["bots"].pop(rec["id"], None); db_save(d)
                    raise ValueError(msg)
                repo = tmp / "repo"
                for p in repo.rglob("*"):
                    if p.is_dir() or p.is_symlink():
                        continue
                    if any(x in {".git", ".venv", "__pycache__"} for x in p.relative_to(repo).parts):
                        continue
                    rel = p.relative_to(repo).as_posix()
                    dest = safe_project_path(root, rel)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, dest)
                mutate_bot(rec["id"], name=repo.name, entry=detect_entry(root))
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            STATES.pop(uid, None)
            bot.reply_to(m, "✅ Cloned.")
            show_bot_card(m.chat.id, uid, rec["id"])
            return

        if flow == "gh_token":
            save_user(uid, gh_token=text)
            STATES.pop(uid, None)
            bot.reply_to(m, "✅ Token saved.", reply_markup=current_kb(uid))
            return

        if flow == "env_set":
            if not b:
                raise ValueError("Bot not found")
            if "=" not in text:
                raise ValueError("Format: KEY=VALUE")
            key, val = text.split("=", 1)
            key, val = key.strip(), val.strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", key):
                raise ValueError("Invalid name")
            if key in BLOCKED_ENV:
                raise ValueError("Reserved name")
            if len(val) > 4000:
                raise ValueError("Value too long (4000)")
            envs = decrypt_obj(b.get("env_enc", ""))
            envs[key] = val
            mutate_bot(bid, env_enc=encrypt_obj(envs))
            STATES.pop(uid, None)
            bot.reply_to(m, f"✅ Saved <code>{esc(key)}</code> (encrypted).",
                         reply_markup=kb_env())
            return

        if flow == "env_bulk":
            if not b:
                raise ValueError("Bot not found")
            envs = decrypt_obj(b.get("env_enc", ""))
            added = 0
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", k):
                    continue
                if k in BLOCKED_ENV or len(v) > 4000:
                    continue
                envs[k] = v
                added += 1
            mutate_bot(bid, env_enc=encrypt_obj(envs))
            STATES.pop(uid, None)
            bot.reply_to(m, f"✅ Added/updated {added} variables.", reply_markup=kb_env())
            return

        if flow == "env_delete":
            if not b:
                raise ValueError("Bot not found")
            envs = decrypt_obj(b.get("env_enc", ""))
            envs.pop(text, None)
            mutate_bot(bid, env_enc=encrypt_obj(envs))
            STATES.pop(uid, None)
            bot.reply_to(m, "🗑 Deleted.", reply_markup=kb_env())
            return

        if flow == "pip_install":
            if not b:
                raise ValueError("Bot not found")
            low = text.lower()
            if low == "requirements":
                ok, out = install_requirements(bid)
            elif low == "auto":
                ok, msg = ensure_venv(bid)
                if not ok:
                    raise ValueError(msg)
                miss = missing_imports(project_dir(bid), _venv_python(project_dir(bid)))
                if not miss:
                    STATES.pop(uid, None)
                    bot.reply_to(m, "✅ All imports OK.", reply_markup=kb_actions(uid))
                    return
                ok, out = install_requirements(bid, miss)
            else:
                pkgs = text.split()
                ok, out = install_requirements(bid, pkgs)
            STATES.pop(uid, None)
            bot.reply_to(m, ("✅ " if ok else "❌ ") + "<pre>" + esc(out[-3800:]) + "</pre>",
                         reply_markup=kb_actions(uid))
            return

        if flow == "tunnel_port":
            if not b:
                raise ValueError("Bot not found")
            port = int(text)
            if not 1 <= port <= 65535:
                raise ValueError("Port 1–65535")
            cf = shutil.which("cloudflared")
            if not cf:
                raise ValueError("cloudflared not installed")
            with _proc_lock:
                old = RUNNING.get(bid, {}).get("tunnel")
                if old and old.poll() is None:
                    old.terminate()
                with _log_path(bid).open("ab") as log:
                    tunnel = subprocess.Popen(
                        [cf, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                        cwd=project_dir(bid), stdout=log, stderr=subprocess.STDOUT,
                        start_new_session=(os.name != "nt"))
                RUNNING.setdefault(bid, {"proc": None, "started": time.time(), "tunnel": None})["tunnel"] = tunnel
            STATES.pop(uid, None)
            bot.reply_to(m, "🌐 Tunnel started. Check logs for the public URL.",
                         reply_markup=kb_actions(uid))
            return

        if flow == "daily_restart_set":
            if not b:
                raise ValueError("Bot not found")
            val = text.strip().lower()
            if val in ("off", "disable", "none", ""):
                mutate_bot(bid, daily_restart="", last_daily_restart=0)
                STATES.pop(uid, None)
                bot.reply_to(m, "✅ Daily restart disabled.",
                             reply_markup=kb_edit())
                return
            mm = re.fullmatch(r"(\d{1,2}):(\d{2})", val)
            if not mm:
                raise ValueError("Format: HH:MM (e.g. 04:30) or 'off'")
            hh, mn = int(mm.group(1)), int(mm.group(2))
            if not (0 <= hh < 24 and 0 <= mn < 60):
                raise ValueError("Invalid time (00:00 - 23:59)")
            normalized = f"{hh:02d}:{mn:02d}"
            mutate_bot(bid, daily_restart=normalized, last_daily_restart=0)
            STATES.pop(uid, None)
            bot.reply_to(
                m,
                f"✅ Daily restart set to <code>{normalized}</code> UTC\n"
                f"Bot will auto-restart every day at this time.",
                parse_mode="HTML",
                reply_markup=kb_edit(),
            )
            return

        if flow == "rename":
            if not b:
                raise ValueError("Bot not found")
            if not text or len(text) > 200:
                raise ValueError("Name 1–200 chars")
            mutate_bot(bid, name=text)
            STATES.pop(uid, None)
            bot.reply_to(m, "✅ Renamed.", reply_markup=kb_edit())
            return

        if flow == "entry_set":
            if not b:
                raise ValueError("Bot not found")
            root = project_dir(bid)
            target = safe_project_path(root, text)
            if not target.is_file() or target.suffix.lower() not in {".py", ".js", ".mjs", ".cjs"}:
                raise ValueError("Invalid entry file")
            mutate_bot(bid, entry=text)
            STATES.pop(uid, None)
            bot.reply_to(m, f"✅ Entry → <code>{esc(text)}</code>", reply_markup=kb_edit())
            return

        if flow == "file_delete":
            if not b:
                raise ValueError("Bot not found")
            target = safe_project_path(project_dir(bid), text)
            if not target.is_file() or target.name == ".hosting.log":
                raise ValueError("Not found or protected")
            target.unlink()
            STATES.pop(uid, None)
            bot.reply_to(m, f"🗑 Deleted <code>{esc(text)}</code>", reply_markup=kb_files())
            return

        if flow == "file_download":
            if not b:
                raise ValueError("Bot not found")
            target = safe_project_path(project_dir(bid), text)
            if not target.is_file():
                raise ValueError("Not found")
            if target.stat().st_size > 100 * 1024 * 1024:
                raise ValueError("File too large (>100 MB). Use Download Project.")
            with target.open("rb") as fh:
                buf = io.BytesIO(fh.read())
            buf.name = target.name
            bot.send_document(m.chat.id, buf,
                caption=f"<code>{esc(text)}</code>",
                reply_markup=kb_files())
            STATES.pop(uid, None)
            return

        STATES.pop(uid, None)
    except Exception as exc:
        bot.reply_to(m, "❌ " + esc(exc), reply_markup=current_kb(uid))


# ── Monitor loop ─────────────────────────────────────────────────────────
def monitor_loop() -> None:
    while True:
        try:
            data = db_load()
            now = time.time()
            for bid, b in list(data.get("bots", {}).items()):
                with _proc_lock:
                    state = RUNNING.get(bid)
                proc = state.get("proc") if state else None
                alive = bool(proc and proc.poll() is None)

                if alive and psutil and state and state.get("proc"):
                    try:
                        p = psutil.Process(state["proc"].pid)
                        mem_mb = p.memory_info().rss / (1024 * 1024)
                        if MAX_BOT_RAM_MB and mem_mb > MAX_BOT_RAM_MB:
                            write_log(bid, f"RAM overuse ({mem_mb:.0f}MB > {MAX_BOT_RAM_MB}MB) — killing")
                            mutate_bot(bid, resource_killed=True)
                            stop_process(bid)
                            if b.get("desired_running") and b.get("auto_restart", True):
                                RETRY_AT[bid] = now + 30
                            continue
                    except Exception:
                        pass

                    # 🆕 PATCH 4c: Disk quota check (every ~60s)
                    if int(now) % 60 == 0 and MAX_BOT_DISK_MB:
                        disk_mb = _bot_disk_usage_mb(bid)
                        if disk_mb > MAX_BOT_DISK_MB:
                            write_log(bid, f"Disk overuse ({disk_mb:.0f}MB > {MAX_BOT_DISK_MB}MB) — killing")
                            mutate_bot(bid, resource_killed=True,
                                       failed_reason=f"Disk quota: {disk_mb:.0f}MB")
                            stop_process(bid)
                            try:
                                bot.send_message(
                                    int(b.get("owner", 0)),
                                    f"💾 <b>Disk quota exceeded</b>\n\n"
                                    f"Bot: <b>{esc(b['name'])}</b>\n"
                                    f"Usage: {disk_mb:.0f} MB / {MAX_BOT_DISK_MB} MB\n\nBot stopped.",
                                    parse_mode="HTML",
                                )
                            except Exception:
                                pass
                            continue

                if alive and int(b.get("restart_interval", 0)) > 0:
                    last = int(b.get("last_interval_restart", int(now)))
                    if now - last >= int(b["restart_interval"]):
                        write_log(bid, "Periodic restart")
                        stop_process(bid)
                        mutate_bot(bid, last_interval_restart=int(now))
                        start_process(bid, manual=False)
                        continue

                daily = (b.get("daily_restart") or "").strip()
                if alive and daily:
                    try:
                        gm = time.gmtime(now)
                        current_hm = f"{gm.tm_hour:02d}:{gm.tm_min:02d}"
                        last_daily = int(b.get("last_daily_restart", 0))
                        if current_hm == daily and (now - last_daily) > 3600:
                            write_log(bid, f"Daily restart triggered at {daily} UTC")
                            restart_process(bid)
                            mutate_bot(bid, last_daily_restart=int(now))
                            continue
                    except Exception:
                        pass

                if state and proc and not alive:
                    code = proc.returncode
                    with _proc_lock:
                        tunnel = state.get("tunnel")
                        if tunnel and tunnel.poll() is None:
                            RUNNING[bid] = {"proc": None, "started": now, "tunnel": tunnel}
                        else:
                            RUNNING.pop(bid, None)
                    # 🆕 PATCH 1c: Track crash timestamp
                    recent = list(b.get("recent_crashes") or [])
                    recent.append(now)
                    recent = recent[-10:]
                    mutate_bot(bid, last_exit=code, recent_crashes=recent)
                    write_log(bid, f"Exited code {code}")
                    _send_crash_notification(bid, code)
                    b = get_bot(bid) or b

                # 🆕 PATCH 1b: Crash loop breaker
                if not alive and b.get("desired_running") and b.get("auto_restart", True):
                    if b.get("failed_state"):
                        continue
                    crashes = [t for t in (b.get("recent_crashes") or []) if now - t < 600]
                    if len(crashes) >= 5:
                        mutate_bot(bid, failed_state=True,
                                   failed_reason=f"Crash loop: {len(crashes)} crashes in 10 min")
                        write_log(bid, "CRASH LOOP — auto-restart disabled")
                        try:
                            bot.send_message(
                                int(b.get("owner", 0)),
                                f"🔴 <b>Bot crashed {len(crashes)} times in 10 min</b>\n\n"
                                f"Bot: <b>{esc(b['name'])}</b>\n"
                                f"Auto-restart <b>DISABLED</b>.\n\n"
                                f"Fix: check 📜 Logs → fix code → press 🟢 Start",
                                parse_mode="HTML",
                            )
                        except Exception:
                            pass
                        continue

                    if bid not in RETRY_AT:
                        count = int(b.get("restart_count", 0)) + 1
                        mutate_bot(bid, restart_count=count)
                        delay = min(60, 2 ** min(count, 6))
                        RETRY_AT[bid] = now + delay
                        write_log(bid, f"Recovery in {delay}s (#{count})")
                    elif now >= RETRY_AT[bid]:
                        ok, msg = start_process(bid, manual=False)
                        if not ok:
                            count = int((get_bot(bid) or {}).get("restart_count", 0)) + 1
                            mutate_bot(bid, restart_count=count)
                            delay = min(60, 2 ** min(count, 6))
                            RETRY_AT[bid] = now + delay
                            write_log(bid, f"Retry in {delay}s: {msg}")
        except Exception as exc:
            print("monitor:", exc, flush=True)
        time.sleep(RESOURCE_CHECK_INTERVAL)


# ── Health server ─────────────────────────────────────────────────────────
def start_health_server() -> None:
    if Flask is None:
        return
    app = Flask("hosting-health")

    @app.get("/health")
    def health():
        return jsonify({
            "status": "ok",
            "time": int(time.time()),
            "running_bots": sum(1 for x in RUNNING.values()
                                if x.get("proc") and x["proc"].poll() is None),
        })

    @app.get("/")
    def home():
        return "Hosting panel is alive", 200

    port = int(os.getenv("PORT", "8080"))
    threading.Thread(target=lambda: app.run(host="0.0.0.0", port=port, use_reloader=False),
                     daemon=True, name="health-http").start()

def shutdown() -> None:
    for bid in list(RUNNING):
        try:
            mutate_bot(bid, desired_running=False)
            stop_process(bid)
        except Exception:
            pass

def main() -> None:
    threading.Thread(target=monitor_loop, daemon=True, name="process-monitor").start()
    # 🆕 PATCH 7c: Start auto-backup thread
    threading.Thread(target=_auto_backup_loop, daemon=True, name="auto-backup").start()
    start_health_server()
    for bid, b in db_load().get("bots", {}).items():
        if b.get("desired_running"):
            threading.Thread(target=lambda x=bid: start_process(x, manual=False), daemon=True).start()
    print("[hosting] panel started (enhanced + 7 patches)", flush=True)
    if BACKUP_CHANNEL:
        print(f"[hosting] auto-backup enabled → {BACKUP_CHANNEL}", flush=True)
    try:
        try:
            bot.remove_webhook()
        except Exception:
            pass
        try:
            bot.set_my_commands([
                types.BotCommand("start", "Hosting menu"),
                types.BotCommand("bots", "My bots"),
                types.BotCommand("help", "Help"),
                types.BotCommand("cancel", "Cancel input"),
                types.BotCommand("id", "Show my user ID"),
                types.BotCommand("token", "Set GitHub token"),
                types.BotCommand("grep", "Search bot logs"),
            ])
        except Exception:
            pass
        while True:
            try:
                bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=25)
            except KeyboardInterrupt:
                break
            except Exception as exc:
                print("[hosting] polling:", exc, flush=True)
                time.sleep(5)
    finally:
        shutdown()


if __name__ == "__main__":
    main()
