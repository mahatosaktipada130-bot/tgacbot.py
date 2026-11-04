#!/usr/bin/env python3
"""Enhanced multi-user Telegram bot hosting panel (powerful build — UNLIMITED).

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
DATA_DIR = Path(os.getenv("HOSTING_DATA_DIR", "data")).resolve()
PROJECTS_DIR = Path(os.getenv("HOSTING_PROJECTS_DIR", "hosted_bots")).resolve()
DB_FILE = DATA_DIR / "hosting_db.json"
KEY_FILE = DATA_DIR / "master.key"

# ── Unlimited-ish caps ─────────────────────────────────────────────────────
MAX_UPLOAD        = 10 * 1024**4        # 10 TB (practical: Telegram's own limit)
MAX_EXTRACTED     = 10 * 1024**4        # 10 TB
MAX_FILES         = 10_000_000
MAX_SINGLE_FILE   = 10 * 1024**4        # 10 TB
MAX_BOTS_PER_USER = int(os.getenv("MAX_BOTS_PER_USER", "1000000"))
MAX_LOG_BYTES     = 1024 * 1024 * 1024  # 1 GB before rotation
MAX_BACKUP_MEM    = 2 * 1024**3         # 2 GB — safe in-memory ZIP cap
MAX_BOT_RAM_MB    = int(os.getenv("MAX_BOT_RAM_MB", "0"))   # 0 = unlimited
MAX_BOT_CPU_PCT   = int(os.getenv("MAX_BOT_CPU_PCT", "0"))  # 0 = unlimited
RESOURCE_CHECK_INTERVAL = 5
BLOCKED_ENV = {"PATH", "PYTHONPATH", "LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONHOME", "VIRTUAL_ENV"}

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

# ── Auto-pip: import scan + PYPI mapping ──────────────────────────────────
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

# ── Env build ──────────────────────────────────────────────────────────────
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

# ── Dependency install (with cache + auto-pip) ────────────────────────────
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
    """Hash of requirements.txt + sorted import list, used to skip pip installs."""
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

    # Cache: skip pip when nothing changed (only for auto installs)
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

    # Step 1: requirements.txt if present
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

    # Step 2: auto-detect missing imports
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

# ── Process build / lifecycle ─────────────────────────────────────────────
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

# ── File helpers ──────────────────────────────────────────────────────────
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
        "deps_signature": "",
    }
    data = db_load()
    data["bots"][new_id] = new_rec
    db_save(data)
    return new_rec

# ── GitHub ─────────────────────────────────────────────────────────────────
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

# ── Status helpers ─────────────────────────────────────────────────────────
def _format_bytes(n: float) -> str:
    x = float(max(0, n))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if x < 1024 or unit == "TB":
            return f"{x:.1f} {unit}"
        x /= 1024
    return f"{x:.1f} TB"

def esc(v: Any) -> str:
    return str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

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
    return "\n".join(lines)

# ── Telegram UI ───────────────────────────────────────────────────────────
bot = telebot.TeleBot(BOT_TOKEN, threaded=True, num_threads=8, parse_mode="HTML")

def send_pre_chunks(chat_id: int, title: str, content: str,
                    kb_markup: Optional[types.InlineKeyboardMarkup] = None) -> None:
    """Send long text inside <pre>, split across messages (Telegram 4096-char limit)."""
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
                         reply_markup=kb_markup if i == len(chunks) - 1 else None)

def kb(rows: list[list[tuple[str, str]]]) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=2)
    for row in rows:
        markup.row(*(types.InlineKeyboardButton(text=t, callback_data=d) for t, d in row))
    return markup

def main_kb() -> types.InlineKeyboardMarkup:
    return kb([
        [('📦 My Bots', 'mybots'), ('➕ Upload Bot', 'upload')],
        [('🐙 GitHub Host', 'github'), ('📊 Dashboard', 'dashboard')],
        [('▶️ Start All', 'startall'), ('⏹ Stop All', 'stopall')],
        [('❔ Help', 'help')],
    ])

def bot_kb(bot_id: str, running: bool) -> types.InlineKeyboardMarkup:
    return kb([
        [('⏹ Stop' if running else '▶️ Start', f"{'stop' if running else 'start'}:{bot_id}"),
         ('🔄 Restart', f"restart:{bot_id}")],
        [('📊 Status', f"status:{bot_id}"), ('📜 Logs', f"logs:{bot_id}")],
        [('🔁 Crash Recovery', f"auto:{bot_id}"), ('⏱ Auto Restart', f"cron:{bot_id}")],
        [('📁 Files', f"files:{bot_id}"), ('🔐 Env Vars', f"env:{bot_id}")],
        [('📦 Pip', f"pip:{bot_id}"), ('🌐 Tunnel', f"tunnel:{bot_id}")],
        [('⛔ Stop Tunnel', f"tunnelstop:{bot_id}"), ('⬇️ Download', f"download:{bot_id}")],
        [('🗃 Backup/Restore', f"backup:{bot_id}"), ('✏️ Rename/Entry', f"edit:{bot_id}")],
        [('📋 Clone Bot', f"clone:{bot_id}"), ('🗑 Delete', f"delete:{bot_id}")],
        [('⬅️ My Bots', 'mybots')],
    ])

def send_home(chat_id: int, uid: int) -> None:
    bot.send_message(chat_id,
        "<b>⚡ Advanced Bot Hosting</b>\nHost & manage Python/Node bots.\n\nChoose:",
        reply_markup=main_kb())

def send_bot_card(chat_id: int, uid: int, bot_id: str) -> None:
    b = owned_bot(uid, bot_id)
    if not b:
        bot.send_message(chat_id, "Bot not found.")
        return
    with _proc_lock:
        st = RUNNING.get(bot_id)
    running = bool(st and st.get("proc") and st["proc"].poll() is None)
    bot.send_message(chat_id, bot_status(bot_id), reply_markup=bot_kb(bot_id, running))

def set_state(uid: int, flow: str, bot_id: Optional[str] = None, **extra: Any) -> None:
    STATES[uid] = {"flow": flow, "bot_id": bot_id, **extra}

def throttled(uid: int) -> bool:
    now = time.time()
    if now - LAST_ACTION.get(uid, 0) < 0.7:
        return True
    LAST_ACTION[uid] = now
    return False

def show_dashboard(chat_id: int, uid: int) -> None:
    items = user_bots(uid)
    if not items:
        bot.send_message(chat_id, "No bots yet.", reply_markup=main_kb())
        return
    lines = [f"<b>📊 Dashboard — {len(items)}/{MAX_BOTS_PER_USER} bots</b>\n"]
    running_count = 0
    for b in items[:200]:
        state = RUNNING.get(b["id"])
        running = state and state.get("proc") and state["proc"].poll() is None
        if running:
            running_count += 1
        icon = "🟢" if running else ("⚠️" if b.get("resource_killed") else "🔴")
        extra = ""
        if running and psutil:
            try:
                p = psutil.Process(state["proc"].pid)
                extra = f" | {_format_bytes(p.memory_info().rss)} | {int(time.time() - state['started'])}s"
            except Exception:
                pass
        lines.append(f"{icon} <code>{b['id']}</code> <b>{esc(b['name'][:60])}</b>{extra}")
    lines.append(f"\n🟢 Running: {running_count} / {len(items)}")
    bot.send_message(chat_id, "\n".join(lines),
        reply_markup=kb([[("🔄 Refresh", "dashboard"), ("⬅️ Home", "home")]]))

# ── Commands ──────────────────────────────────────────────────────────────
@bot.message_handler(commands=["start", "menu"])
def cmd_start(m: types.Message) -> None:
    if m.chat.type != "private":
        return
    STATES.pop(m.from_user.id, None)
    if not get_user(m.from_user.id):
        save_user(m.from_user.id, id=m.from_user.id,
                  name=m.from_user.first_name or "",
                  username=m.from_user.username or "",
                  joined=int(time.time()))
    send_home(m.chat.id, m.from_user.id)

@bot.message_handler(commands=["help"])
def cmd_help(m: types.Message) -> None:
    bot.reply_to(m,
        "<b>Help</b>\n"
        "• Upload .py/.js/.zip or clone public/private GitHub\n"
        "• Each project gets its own Python venv\n"
        "• Env vars encrypted at rest\n"
        "• Auto-pip installs missing imports\n"
        "• RAM cap: " + (f"{MAX_BOT_RAM_MB} MB" if MAX_BOT_RAM_MB else "unlimited") + "\n"
        "• Commands: /start /bots /cancel /id")

@bot.message_handler(commands=["bots"])
def cmd_bots(m: types.Message) -> None:
    show_bots(m.chat.id, m.from_user.id)

@bot.message_handler(commands=["cancel"])
def cmd_cancel(m: types.Message) -> None:
    STATES.pop(m.from_user.id, None)
    bot.reply_to(m, "Cancelled.")

@bot.message_handler(commands=["id"])
def cmd_id(m: types.Message) -> None:
    bot.reply_to(m, f"<code>{m.from_user.id}</code>")

def show_bots(chat_id: int, uid: int) -> None:
    items = user_bots(uid)
    if not items:
        bot.send_message(chat_id, "No bots yet.", reply_markup=main_kb())
        return
    rows = []
    for item in items[:200]:
        state = RUNNING.get(item["id"])
        running = state and state.get("proc") and state["proc"].poll() is None
        label = ("🟢 " if running else "⚫ ") + item["name"][:60]
        rows.append([(label, f"view:{item['id']}")])
    rows.append([("➕ Upload Bot", "upload"), ("⬅️ Home", "home")])
    bot.send_message(chat_id, f"<b>My Bots</b> · {len(items)}/{MAX_BOTS_PER_USER}",
                     reply_markup=kb(rows))

# ── Callbacks ─────────────────────────────────────────────────────────────
@bot.callback_query_handler(func=lambda c: True)
def callback(c: types.CallbackQuery) -> None:
    uid = c.from_user.id
    data = c.data or ""
    try:
        bot.answer_callback_query(c.id)
    except Exception:
        pass
    if throttled(uid):
        return
    if data == "home":
        send_home(c.message.chat.id, uid); return
    if data == "help":
        bot.send_message(c.message.chat.id, "Send .py/.js/.zip after Upload. GitHub accepts HTTPS URLs."); return
    if data == "mybots":
        show_bots(c.message.chat.id, uid); return
    if data == "dashboard":
        show_dashboard(c.message.chat.id, uid); return
    if data == "upload":
        set_state(uid, "upload_new")
        bot.send_message(c.message.chat.id,
            "Send .py / .js / .zip (max 20 MB).\n\n/cancel to abort.")
        return
    if data == "github":
        set_state(uid, "github_url")
        bot.send_message(c.message.chat.id,
            "Send GitHub repo URL.\nPublic: https://github.com/user/repo\n\n"
            "Private: set token first via /menu → GitHub Host → Set Token.")
        return
    if data == "startall":
        items = user_bots(uid)
        ok = 0
        for b in items:
            st = RUNNING.get(b["id"])
            if not (st and st.get("proc") and st["proc"].poll() is None):
                r, _ = start_process(b["id"])
                ok += int(r)
        bot.send_message(c.message.chat.id, f"Started {ok} bot(s).")
        show_dashboard(c.message.chat.id, uid)
        return
    if data == "stopall":
        items = user_bots(uid)
        for b in items:
            mutate_bot(b["id"], desired_running=False)
            stop_process(b["id"])
        bot.send_message(c.message.chat.id, f"Stopped {len(items)} bot(s).")
        show_dashboard(c.message.chat.id, uid)
        return
    if data == "gh_token":
        set_state(uid, "gh_token")
        bot.send_message(c.message.chat.id, "Send GitHub personal access token:")
        return
    if data.startswith("view:"):
        send_bot_card(c.message.chat.id, uid, data.split(":", 1)[1]); return
    if ":" not in data:
        return

    parts = data.split(":")
    action = parts[0]
    bot_id = parts[1] if len(parts) > 1 else ""
    extra = ":".join(parts[2:]) if len(parts) > 2 else ""
    b = owned_bot(uid, bot_id)
    if not b:
        bot.send_message(c.message.chat.id, "Bot not found."); return
    root = project_dir(bot_id)

    if action == "start":
        ok, msg = start_process(bot_id)
        bot.send_message(c.message.chat.id, ("✅ " if ok else "❌ ") + esc(msg))
        send_bot_card(c.message.chat.id, uid, bot_id)
    elif action == "stop":
        mutate_bot(bot_id, desired_running=False)
        msg = stop_process(bot_id)
        bot.send_message(c.message.chat.id, "⏹ " + esc(msg))
        send_bot_card(c.message.chat.id, uid, bot_id)
    elif action == "restart":
        ok, msg = restart_process(bot_id)
        bot.send_message(c.message.chat.id, ("✅ " if ok else "❌ ") + esc(msg))
        send_bot_card(c.message.chat.id, uid, bot_id)
    elif action == "status":
        st = RUNNING.get(bot_id)
        running = bool(st and st.get("proc") and st["proc"].poll() is None)
        bot.send_message(c.message.chat.id, bot_status(bot_id),
                         reply_markup=bot_kb(bot_id, running))
    elif action == "auto":
        enabled = not bool(b.get("auto_restart", True))
        mutate_bot(bot_id, auto_restart=enabled)
        bot.send_message(c.message.chat.id, f"Crash recovery {'on' if enabled else 'off'}.")
        send_bot_card(c.message.chat.id, uid, bot_id)
    elif action == "logs":
        text = tail_logs(bot_id)
        send_pre_chunks(
            c.message.chat.id, "Logs", text,
            kb_markup=kb([[('🔄 Refresh', f'logs:{bot_id}'), ('🧹 Clear', f'clearlogs:{bot_id}')],
                          [('⬅️ Back', f'view:{bot_id}')]]))
    elif action == "clearlogs":
        try:
            _log_path(bot_id).write_text("", encoding="utf-8")
        except Exception:
            pass
        bot.send_message(c.message.chat.id, "Logs cleared.",
                         reply_markup=kb([[('⬅️ Back', f'view:{bot_id}')]]))
    elif action == "files":
        files = project_files(bot_id)
        lines = [f"<code>{esc(p.relative_to(root).as_posix())}</code> · {_format_bytes(p.stat().st_size)}"
                 for p in files[:200]]
        choices = [p.relative_to(root).as_posix() for p in files[:50]]
        STATES[uid] = {"flow": "file_select", "bot_id": bot_id, "files": choices}
        rows = [
            [("➕ Upload File", f"fileupload:{bot_id}"), ("🗑 Delete File", f"filedel:{bot_id}")],
            [("⬇️ Download Project", f"download:{bot_id}")],
        ]
        for i, rel in enumerate(choices):
            rows.append([(f"📄 {rel[:30]}", f"fileview:{bot_id}:{i}")])
        rows.append([("⬅️ Back", f"view:{bot_id}")])
        bot.send_message(c.message.chat.id,
            "<b>Project files</b>\n" + ("\n".join(lines) if lines else "No files."),
            reply_markup=kb(rows))
    elif action == "fileview":
        try:
            choices = STATES.get(uid, {})
            files_for_user = choices.get("files", []) if choices.get("bot_id") == bot_id else []
            rel = files_for_user[int(extra)]
            target = safe_project_path(root, rel)
            if not target.is_file():
                raise ValueError("Not found")
            if target.stat().st_size > 200 * 1024 * 1024:
                raise ValueError("File too large to preview (200 MB) — use Download")
            content = target.read_text(encoding="utf-8", errors="replace")
            if len(content) > 100_000:
                content = content[:100_000] + "\n\n... (truncated, use Download for full file)"
            send_pre_chunks(c.message.chat.id, rel, content,
                            kb_markup=kb([[("⬅️ Back", f"files:{bot_id}")]]))
        except Exception as exc:
            bot.send_message(c.message.chat.id, "View failed: " + esc(exc))
    elif action == "fileupload":
        set_state(uid, "upload_file", bot_id)
        bot.send_message(c.message.chat.id, "Send a file to add/replace.")
    elif action == "filedel":
        set_state(uid, "file_delete", bot_id)
        bot.send_message(c.message.chat.id, "Send relative path (e.g. <code>src/main.py</code>).")
    elif action == "env":
        envs = decrypt_obj(b.get("env_enc", ""))
        text = "\n".join(
            f"<code>{esc(k)}</code> = <code>{'•' * min(len(v), 12)}</code>"
            for k, v in envs.items()) or "No custom vars."
        bot.send_message(c.message.chat.id,
            "<b>Env Vars</b>\n" + text,
            reply_markup=kb([[('➕ Set One', f'envset:{bot_id}'), ('📥 Bulk Import', f'envbulk:{bot_id}')],
                             [('➖ Delete', f'envdel:{bot_id}')],
                             [('⬅️ Back', f'view:{bot_id}')]]))
    elif action == "envset":
        set_state(uid, "env_set", bot_id)
        bot.send_message(c.message.chat.id, "Send KEY=VALUE")
    elif action == "envbulk":
        set_state(uid, "env_bulk", bot_id)
        bot.send_message(c.message.chat.id,
            "Send multiple lines, one per var:\n<code>KEY1=val1\nKEY2=val2</code>\n\nSend /cancel to abort.")
    elif action == "envdel":
        set_state(uid, "env_delete", bot_id)
        bot.send_message(c.message.chat.id, "Send the variable name to delete.")
    elif action == "pip":
        set_state(uid, "pip_install", bot_id)
        bot.send_message(c.message.chat.id,
            "Send packages separated by spaces.\n<code>requests python-dotenv</code>\n\n"
            "Send <code>requirements</code> to install requirements.txt.\n"
            "Send <code>auto</code> to scan imports and install missing.")
    elif action == "cron":
        cur = int(b.get("restart_interval", 0))
        bot.send_message(c.message.chat.id,
            f"Periodic restart: {cur or 'OFF'} sec",
            reply_markup=kb([[('Off', f'cronset:{bot_id}:0'), ('30 min', f'cronset:{bot_id}:1800')],
                             [('1 hour', f'cronset:{bot_id}:3600'), ('6 hours', f'cronset:{bot_id}:21600')],
                             [('⬅️ Back', f'view:{bot_id}')]]))
    elif action == "cronset":
        sec = int(extra)
        mutate_bot(bot_id, restart_interval=sec, last_interval_restart=int(time.time()))
        bot.send_message(c.message.chat.id, f"Periodic restart: {sec or 'OFF'} sec.")
        send_bot_card(c.message.chat.id, uid, bot_id)
    elif action == "tunnel":
        set_state(uid, "tunnel_port", bot_id)
        bot.send_message(c.message.chat.id,
            "Send local port for Cloudflare quick tunnel (requires cloudflared).")
    elif action == "tunnelstop":
        with _proc_lock:
            tunnel = RUNNING.get(bot_id, {}).get("tunnel")
        if tunnel:
            try:
                if os.name == "nt":
                    if tunnel.poll() is None:
                        tunnel.terminate()
                else:
                    os.killpg(tunnel.pid, signal.SIGTERM)
                with _proc_lock:
                    if bot_id in RUNNING:
                        RUNNING[bot_id]["tunnel"] = None
                bot.send_message(c.message.chat.id, "⛔ Tunnel stopped.")
            except Exception as exc:
                bot.send_message(c.message.chat.id, "Stop failed: " + esc(exc))
        else:
            bot.send_message(c.message.chat.id, "No tunnel running.")
    elif action == "download":
        try:
            raw = make_backup(bot_id)
            buf = io.BytesIO(raw)
            buf.name = f"{safe_name(b['name'])}-{bot_id}.zip"
            bot.send_document(c.message.chat.id, buf,
                caption="Full backup (source + bot_meta.json; env excluded from source files).")
        except Exception as exc:
            bot.send_message(c.message.chat.id, "Failed: " + esc(exc))
    elif action == "backup":
        bot.send_message(c.message.chat.id,
            "Backup ZIP = source + bot_meta.json (env, name, cron).",
            reply_markup=kb([[('⬇️ Download', f'download:{bot_id}')],
                             [('♻️ Restore ZIP', f'restore:{bot_id}')],
                             [('⬅️ Back', f'view:{bot_id}')]]))
    elif action == "restore":
        set_state(uid, "restore_zip", bot_id)
        bot.send_message(c.message.chat.id, "Send a ZIP backup. Source files will be replaced.")
    elif action == "edit":
        bot.send_message(c.message.chat.id, "Choose:",
            reply_markup=kb([[('✏️ Rename', f'rename:{bot_id}'), ('🎯 Set Entry', f'entry:{bot_id}')],
                             [('⬅️ Back', f'view:{bot_id}')]]))
    elif action == "rename":
        set_state(uid, "rename", bot_id)
        bot.send_message(c.message.chat.id, "Send new name (max 200 chars).")
    elif action == "entry":
        files = [p.relative_to(root).as_posix() for p in project_files(bot_id)
                 if p.suffix.lower() in {".py", ".js", ".mjs", ".cjs"}]
        if not files:
            bot.send_message(c.message.chat.id, "No entry candidates.")
            return
        STATES[uid] = {"flow": "entry_select", "bot_id": bot_id, "entries": files[:200]}
        rows = [[(f[:35], f'entrypick:{bot_id}:{i}')] for i, f in enumerate(files[:200])]
        rows.append([('⬅️ Back', f'view:{bot_id}')])
        bot.send_message(c.message.chat.id, "Select entry:", reply_markup=kb(rows))
    elif action == "entrypick":
        choices = STATES.get(uid, {}).get("entries", [])
        try:
            idx = int(extra)
            if idx < 0 or idx >= len(choices):
                raise ValueError("Selection expired")
            rel = choices[idx]
        except (ValueError, IndexError):
            bot.send_message(c.message.chat.id, "Selection expired.")
            return
        try:
            target = safe_project_path(root, rel)
            if not target.is_file() or target.suffix.lower() not in {".py", ".js", ".mjs", ".cjs"}:
                raise ValueError("Invalid entry")
            mutate_bot(bot_id, entry=rel)
            bot.send_message(c.message.chat.id, f"Entry → <code>{esc(rel)}</code>")
        except Exception as exc:
            bot.send_message(c.message.chat.id, "Failed: " + esc(exc))
        send_bot_card(c.message.chat.id, uid, bot_id)
    elif action == "clone":
        try:
            new_rec = clone_bot(uid, bot_id)
            if new_rec:
                bot.send_message(c.message.chat.id,
                    f"📋 Cloned as <b>{esc(new_rec['name'])}</b>\nID: <code>{new_rec['id']}</code>")
                send_bot_card(c.message.chat.id, uid, new_rec["id"])
        except Exception as exc:
            bot.send_message(c.message.chat.id, "Clone failed: " + esc(exc))
    elif action == "delete":
        bot.send_message(c.message.chat.id,
            f"Delete <b>{esc(b['name'])}</b> and all files?",
            reply_markup=kb([[('❌ Confirm', f'deleteyes:{bot_id}')], [('Cancel', f'view:{bot_id}')]]))
    elif action == "deleteyes":
        stop_process(bot_id)
        d = db_load()
        d["bots"].pop(bot_id, None)
        db_save(d)
        shutil.rmtree(root, ignore_errors=True)
        bot.send_message(c.message.chat.id, "Deleted.", reply_markup=main_kb())

# ── Document uploads ─────────────────────────────────────────────────────
@bot.message_handler(content_types=["document"])
def on_document(m: types.Message) -> None:
    uid = m.from_user.id
    st = STATES.get(uid, {})
    flow = st.get("flow")
    if flow not in {"upload_new", "upload_file", "restore_zip"}:
        bot.reply_to(m, "Tap Upload Bot or File Manager first.")
        return
    doc = m.document
    if not doc or (doc.file_size or 0) > MAX_UPLOAD:
        bot.reply_to(m, f"Too large. Max {_format_bytes(MAX_UPLOAD)}."); return
    rec = None
    try:
        info = bot.get_file(doc.file_id)
        raw = bot.download_file(info.file_path)
        fname = safe_name(doc.file_name or "upload.bin")
        if flow == "upload_new":
            rec = _create_record(uid, Path(fname).stem)
            root = project_dir(rec["id"])
            if fname.lower().endswith(".zip"):
                count = safe_extract_zip(raw, root)
            elif Path(fname).suffix.lower() in {".py", ".js", ".mjs", ".cjs"}:
                (root / fname).write_bytes(raw)
                count = 1
            else:
                shutil.rmtree(root, ignore_errors=True)
                d = db_load(); d["bots"].pop(rec["id"], None); db_save(d)
                raise ValueError("Only .py/.js/.mjs/.cjs/.zip")
            entry = detect_entry(root)
            mutate_bot(rec["id"], entry=entry)
            STATES.pop(uid, None)
            bot.reply_to(m, f"✅ <b>{esc(rec['name'])}</b>\nFiles: {count}\nEntry: <code>{esc(entry)}</code>")
            send_bot_card(m.chat.id, uid, rec["id"])
        elif flow == "upload_file":
            bid = st.get("bot_id")
            if not owned_bot(uid, bid):
                raise ValueError("Access denied")
            if fname.lower().endswith(".zip") or len(raw) > MAX_SINGLE_FILE:
                raise ValueError("ZIP / oversized files not allowed here")
            target = safe_project_path(project_dir(bid), fname)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            STATES.pop(uid, None)
            bot.reply_to(m, f"✅ Saved <code>{esc(fname)}</code>")
            send_bot_card(m.chat.id, uid, bid)
        elif flow == "restore_zip":
            bid = st.get("bot_id")
            if not owned_bot(uid, bid):
                raise ValueError("Access denied")
            if not fname.lower().endswith(".zip"):
                raise ValueError("Restore requires ZIP")
            count = restore_backup(bid, raw)
            STATES.pop(uid, None)
            bot.reply_to(m, f"✅ Restored {count} files.")
            send_bot_card(m.chat.id, uid, bid)
    except Exception as exc:
        if flow == "upload_new" and rec:
            shutil.rmtree(project_dir(rec["id"]), ignore_errors=True)
            d = db_load(); d["bots"].pop(rec["id"], None); db_save(d)
        bot.reply_to(m, "❌ " + esc(exc))

# ── Text handler (state machine) ─────────────────────────────────────────
@bot.message_handler(content_types=["text"])
def on_text(m: types.Message) -> None:
    uid = m.from_user.id
    st = STATES.get(uid)
    if not st:
        return
    flow = st.get("flow")
    value = (m.text or "").strip()
    if value == "/cancel":
        STATES.pop(uid, None); bot.reply_to(m, "Cancelled."); return
    bid = st.get("bot_id")
    b = owned_bot(uid, bid) if bid else None
    try:
        if flow == "github_url":
            if len(user_bots(uid)) >= MAX_BOTS_PER_USER:
                raise ValueError("Hosting limit reached")
            rec = _create_record(uid, "GitHub Project", "github")
            root = project_dir(rec["id"])
            token = get_user(uid).get("gh_token")
            tmp = Path(tempfile.mkdtemp(prefix="ghclone-", dir=str(PROJECTS_DIR)))
            try:
                ok, msg = clone_public_github(value, tmp / "repo", token=token)
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
            send_bot_card(m.chat.id, uid, rec["id"])
        elif flow == "gh_token":
            save_user(uid, gh_token=value)
            STATES.pop(uid, None)
            bot.reply_to(m, "✅ Token saved.")
        elif flow == "env_set":
            if not b:
                raise ValueError("Bot not found")
            if "=" not in value:
                raise ValueError("Format: KEY=VALUE")
            key, val = value.split("=", 1)
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
            bot.reply_to(m, f"✅ Saved <code>{esc(key)}</code> (encrypted).")
        elif flow == "env_bulk":
            if not b:
                raise ValueError("Bot not found")
            envs = decrypt_obj(b.get("env_enc", ""))
            added = 0
            for line in value.splitlines():
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
            bot.reply_to(m, f"✅ Added/updated {added} variables.")
        elif flow == "env_delete":
            if not b:
                raise ValueError("Bot not found")
            envs = decrypt_obj(b.get("env_enc", ""))
            envs.pop(value, None)
            mutate_bot(bid, env_enc=encrypt_obj(envs))
            STATES.pop(uid, None)
            bot.reply_to(m, "Deleted.")
        elif flow == "pip_install":
            if not b:
                raise ValueError("Bot not found")
            low = value.lower()
            if low == "requirements":
                ok, out = install_requirements(bid)
            elif low == "auto":
                ok, msg = ensure_venv(bid)
                if not ok:
                    raise ValueError(msg)
                miss = missing_imports(project_dir(bid), _venv_python(project_dir(bid)))
                if not miss:
                    STATES.pop(uid, None)
                    bot.reply_to(m, "✅ All imports OK.")
                    return
                ok, out = install_requirements(bid, miss)
            else:
                pkgs = value.split()
                ok, out = install_requirements(bid, pkgs)
            STATES.pop(uid, None)
            bot.reply_to(m, ("✅ " if ok else "❌ ") + "<pre>" + esc(out[-3800:]) + "</pre>")
        elif flow == "tunnel_port":
            if not b:
                raise ValueError("Bot not found")
            port = int(value)
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
            bot.reply_to(m, "🌐 Tunnel started. Check logs for the public URL.")
        elif flow == "file_delete":
            if not b:
                raise ValueError("Bot not found")
            target = safe_project_path(project_dir(bid), value)
            if not target.is_file() or target.name == ".hosting.log":
                raise ValueError("Not found or protected")
            target.unlink()
            STATES.pop(uid, None)
            bot.reply_to(m, f"🗑 Deleted <code>{esc(value)}</code>")
            send_bot_card(m.chat.id, uid, bid)
        elif flow == "rename":
            if not b:
                raise ValueError("Bot not found")
            if not value or len(value) > 200:
                raise ValueError("Name 1–200 chars")
            mutate_bot(bid, name=value)
            STATES.pop(uid, None)
            bot.reply_to(m, "✅ Renamed.")
            send_bot_card(m.chat.id, uid, bid)
        else:
            STATES.pop(uid, None)
    except Exception as exc:
        bot.reply_to(m, "❌ " + esc(exc))

# ── Monitor loop (crash recovery + resource limits) ─────────────────────
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

                # Resource limits (only if set)
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

                # Periodic restart
                if alive and int(b.get("restart_interval", 0)) > 0:
                    last = int(b.get("last_interval_restart", int(now)))
                    if now - last >= int(b["restart_interval"]):
                        write_log(bid, "Periodic restart")
                        stop_process(bid)
                        mutate_bot(bid, last_interval_restart=int(now))
                        start_process(bid, manual=False)
                        continue

                # Detect exit
                if state and proc and not alive:
                    code = proc.returncode
                    with _proc_lock:
                        tunnel = state.get("tunnel")
                        if tunnel and tunnel.poll() is None:
                            RUNNING[bid] = {"proc": None, "started": now, "tunnel": tunnel}
                        else:
                            RUNNING.pop(bid, None)
                    mutate_bot(bid, last_exit=code)
                    write_log(bid, f"Exited code {code}")
                    b = get_bot(bid) or b

                # Crash recovery (with backoff)
                if not alive and b.get("desired_running") and b.get("auto_restart", True):
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
    start_health_server()
    for bid, b in db_load().get("bots", {}).items():
        if b.get("desired_running"):
            threading.Thread(target=lambda x=bid: start_process(x, manual=False), daemon=True).start()
    print("[hosting] panel started (powerful build — UNLIMITED)", flush=True)
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
