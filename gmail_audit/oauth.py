import json
import os
from pathlib import Path

from gmail_audit.paths import CREDENTIALS_NAME, data_dir, ensure_data_dir


def downloads_dirs():
    dirs = []
    home = Path.home()
    dirs.append(home / "Downloads")

    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        dirs.append(Path(userprofile) / "Downloads")

    dirs.append(Path.cwd())

    unique = []
    seen = set()

    for folder in dirs:
        try:
            resolved = folder.resolve()
        except OSError:
            continue

        key = str(resolved)
        if key not in seen and folder.is_dir():
            seen.add(key)
            unique.append(folder)

    return unique


def inspect_oauth_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None, None

    if not isinstance(payload, dict):
        return None, None

    if "installed" in payload:
        return "desktop", payload

    if "web" in payload:
        return "web", payload

    return "unknown", payload


def find_downloaded_clients():
    found = []
    seen = set()

    for folder in downloads_dirs():
        for pattern in (
            "client_secret*.json",
            "*credentials*.json",
            CREDENTIALS_NAME
        ):
            for path in folder.glob(pattern):
                if not path.is_file():
                    continue

                key = str(path.resolve())
                if key in seen:
                    continue

                kind, payload = inspect_oauth_json(path)
                if kind is None:
                    continue

                client_id = ""
                if isinstance(payload, dict):
                    blob = payload.get("installed") or payload.get("web") or {}
                    client_id = str(blob.get("client_id") or "")

                if "YOUR_CLIENT_ID" in client_id or path.name.endswith(".example.json"):
                    continue

                seen.add(key)
                found.append((path, kind))

    found.sort(key=lambda item: item[0].stat().st_mtime, reverse=True)
    return found


def import_desktop_credentials(dest=None):
    """
    Copy the newest Desktop OAuth JSON from Downloads/cwd
    into the app data directory. Returns the dest path or None.
    """

    dest = Path(dest or (ensure_data_dir() / CREDENTIALS_NAME))
    candidates = find_downloaded_clients()

    if not candidates:
        return None

    desktop = [
        path for path, kind in candidates
        if kind == "desktop"
    ]

    if not desktop:
        newest, kind = candidates[0]
        print(
            f"Found {newest.name} but it is a {kind} client, "
            "not a Desktop app."
        )
        print(
            "In Google Cloud Console, create an OAuth client "
            "of type Desktop app and download that JSON."
        )
        return None

    source = desktop[0]

    if source.resolve() == dest.resolve():
        return str(dest)

    dest.parent.mkdir(parents=True, exist_ok=True)

    with open(source, encoding="utf-8") as handle:
        payload = handle.read()

    with open(dest, "w", encoding="utf-8") as handle:
        handle.write(payload)

    print(f"Imported Desktop client from {source}")
    print(f"Saved {dest}")
    return str(dest)


def explain_gmail_error(error, token_path=None):
    text = f"{type(error).__name__}: {error}".lower()
    data = data_dir()
    token_path = token_path or str(data / "token.json")

    print()
    print("Gmail sign-in failed.")

    if any(
        needle in text
        for needle in (
            "access_denied",
            "access_blocked",
            "access blocked",
            "403",
            "forbidden"
        )
    ):
        print(
            "Add the same Gmail address as a test user "
            "(Google Auth Platform → Audience)."
        )
        print("Keep the app in Testing. Do not publish it.")
        print("Then delete the token and sign in again:")
        print(f"  rm \"{token_path}\"")
        print("  gmail-audit run")
    elif "invalid_client" in text:
        print(
            "credentials.json is not a valid OAuth client. "
            "Re-download a Desktop app JSON and re-run setup."
        )
        print(f"Expected location: {data / CREDENTIALS_NAME}")
    elif "redirect" in text:
        print(
            "The OAuth client type must be Desktop app, not Web."
        )
    else:
        print(error)

    print()
    print("Walkthrough: docs/setup-gmail.md")
