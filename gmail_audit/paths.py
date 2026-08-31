import os
import platform
import shutil
import sqlite3
import subprocess
from pathlib import Path


APP_NAME = "gmail-audit"
ENV_HOME = "GMAIL_AUDIT_HOME"

CREDENTIALS_NAME = "credentials.json"
TOKEN_NAME = "token.json"
CONFIG_NAME = "config.json"
LEGACY_CONFIG_NAME = ".gmail-audit.json"
DB_NAME = "subscription_audit_v2.db"
REPORT_NAME = "subscription_report.csv"
TIMELINE_NAME = "subscription_timeline.csv"

_COPIED = set()


class Files:
    def __init__(self, root):
        self.root = Path(root)
        self.credentials = str(self.root / CREDENTIALS_NAME)
        self.token = str(self.root / TOKEN_NAME)
        self.config = str(self.root / CONFIG_NAME)
        self.db = str(self.root / DB_NAME)
        self.report = str(self.root / REPORT_NAME)
        self.timeline = str(self.root / TIMELINE_NAME)


def data_dir():
    override = os.environ.get(ENV_HOME)

    if override:
        return Path(override).expanduser().resolve()

    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(
            Path.home() / "AppData" / "Roaming"
        )
        return Path(base) / APP_NAME

    return Path.home() / f".{APP_NAME}"


def ensure_data_dir():
    path = data_dir()
    path.mkdir(parents=True, exist_ok=True)

    # The directory holds OAuth secrets and email-derived data:
    # keep other local users out.
    if os.name != "nt":
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass

    return path


def restrict_to_owner(path):
    """Owner-only read/write (no-op on Windows)."""

    if os.name == "nt":
        return

    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def write_private_file(path, text):
    """Write text to path readable only by the current user."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd = os.open(
        str(path),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600
    )

    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)

    restrict_to_owner(path)


def _copy_if_needed(src, dest, label, secret=False):
    src = Path(src)
    dest = Path(dest)

    if dest.is_file() or not src.is_file():
        return False

    key = str(dest)

    if key in _COPIED:
        return False

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)

    if secret:
        restrict_to_owner(dest)

    _COPIED.add(key)
    print(f"Copied {label} → {dest}")
    return True


def migrate_and_bind():
    """
    Prefer ~/.gmail-audit (or %APPDATA%\\gmail-audit).
    Copy leftover files from the current directory once.
    """

    root = ensure_data_dir()
    cwd = Path.cwd()

    _copy_if_needed(
        cwd / CREDENTIALS_NAME,
        root / CREDENTIALS_NAME,
        CREDENTIALS_NAME,
        secret=True
    )
    _copy_if_needed(
        cwd / TOKEN_NAME,
        root / TOKEN_NAME,
        TOKEN_NAME,
        secret=True
    )
    _copy_if_needed(
        cwd / DB_NAME,
        root / DB_NAME,
        DB_NAME
    )
    _copy_if_needed(
        cwd / REPORT_NAME,
        root / REPORT_NAME,
        REPORT_NAME
    )
    _copy_if_needed(
        cwd / TIMELINE_NAME,
        root / TIMELINE_NAME,
        TIMELINE_NAME
    )
    _copy_if_needed(
        cwd / LEGACY_CONFIG_NAME,
        root / CONFIG_NAME,
        LEGACY_CONFIG_NAME
    )
    _copy_if_needed(
        cwd / CONFIG_NAME,
        root / CONFIG_NAME,
        CONFIG_NAME
    )

    return Files(root)


def db_has_screening(db_path):
    path = Path(db_path)

    if not path.is_file():
        return False

    try:
        conn = sqlite3.connect(str(path))
        try:
            row = conn.execute(
                """
                SELECT COUNT(*)
                FROM sqlite_master
                WHERE type = 'table'
                  AND name = 'screened_emails'
                """
            ).fetchone()

            if not row or row[0] == 0:
                return False

            count = conn.execute(
                "SELECT COUNT(*) FROM screened_emails"
            ).fetchone()[0]
            return count > 0
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def open_path(path):
    target = str(Path(path).resolve())

    if os.name == "nt":
        os.startfile(target)  # noqa: S606
        return True

    opener = "open" if platform.system() == "Darwin" else "xdg-open"

    try:
        subprocess.Popen(
            [opener, target],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        return True
    except Exception:
        return False
