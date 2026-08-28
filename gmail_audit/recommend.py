import json
import os
import shutil
import subprocess
import time
import webbrowser

import requests

from gmail_audit.hardware import detect_machine
from gmail_audit.paths import migrate_and_bind


OLLAMA_TAGS_URL = "http://127.0.0.1:11434/api/tags"
OLLAMA_DOWNLOAD = "https://ollama.com/download"

GMAIL_SETUP_STEPS = """
Gmail API (read-only) — you create your own Desktop OAuth client.
Google does not let this project ship a shared Gmail login.

1. Open https://console.cloud.google.com/
2. Create a project (or pick one).
3. Enable the Gmail API.
4. OAuth consent: External + Testing. Add YOUR Gmail as a test user.
5. Add the scope gmail.readonly (Data Access / scopes).
6. Create an OAuth client ID of type Desktop app.
7. Download the JSON. setup will pick it up from Downloads.

The first sign-in shows “Google hasn’t verified this app”.
That is expected for a private Desktop client. Advanced → Continue.

Full walkthrough: docs/setup-gmail.md
""".strip()


def config_file():
    return migrate_and_bind().config


def load_saved_config():
    path = config_file()

    if not os.path.exists(path):
        return None

    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def save_config(plan, backend="ollama"):
    path = config_file()
    payload = {
        "backend": backend,
        "fast_model": plan["fast_model"],
        "deep_model": plan["deep_model"],
        "mlx_model": plan.get("mlx_model")
    }

    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")

    return payload


def recommend_models(machine=None):
    machine = machine or detect_machine()
    ram = machine.get("ram_gb") or 8

    if ram < 10:
        plan = {
            "fast_model": "qwen2.5:1.5b",
            "deep_model": "qwen2.5:1.5b",
            "note": "8 GB class machine. Stage 2 uses the same small model."
        }
    elif ram < 18:
        plan = {
            "fast_model": "qwen2.5:3b",
            "deep_model": "qwen2.5:7b",
            "note": "16 GB class machine. Small screen model, 7B for extraction."
        }
    elif ram < 40:
        plan = {
            "fast_model": "qwen2.5:3b",
            "deep_model": "qwen3:8b",
            "note": "24–32 GB class machine. 3B screen, 8B extraction."
        }
    else:
        plan = {
            "fast_model": "qwen2.5:7b",
            "deep_model": "qwen2.5:14b",
            "note": "High-RAM machine. Larger local models are OK."
        }

    backend = "ollama"
    mlx_optional = False

    if machine.get("apple_silicon"):
        mlx_optional = True
        plan["note"] += (
            " Apple Silicon: Ollama is the default. "
            "MLX is an optional faster Stage 1 (`gmail-audit setup --backend mlx`)."
        )

    plan["backend"] = backend
    plan["mlx_optional"] = mlx_optional
    plan["mlx_model"] = "mlx-community/Qwen3-4B-4bit"
    plan["ram_gb"] = ram
    plan["accelerator"] = machine.get("accelerator")
    return plan


def ollama_running():
    try:
        response = requests.get(OLLAMA_TAGS_URL, timeout=3)
        response.raise_for_status()
        return True, response.json().get("models", [])
    except Exception:
        return False, []


def installed_ollama_names(models):
    names = []

    for model in models:
        name = model.get("name") or ""
        if name:
            names.append(name)

    return names


def model_installed(names, wanted):
    wanted = wanted.lower()

    for name in names:
        lowered = name.lower()
        if lowered == wanted or lowered.startswith(wanted):
            return True

    return False


def _spawn_ollama(machine):
    ollama = machine.get("ollama_bin")
    system = machine.get("os")

    if system == "Darwin":
        subprocess.Popen(
            ["open", "-a", "Ollama"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        return True

    if not ollama:
        return False

    kwargs = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL
    }

    if system == "Windows":
        flags = 0
        if hasattr(subprocess, "DETACHED_PROCESS"):
            flags |= subprocess.DETACHED_PROCESS
        if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            flags |= subprocess.CREATE_NEW_PROCESS_GROUP
        kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True

    subprocess.Popen([ollama, "serve"], **kwargs)
    return True


def ensure_ollama_running(timeout=25, quiet=False):
    running, models = ollama_running()

    if running:
        return True, models

    machine = detect_machine()

    if not machine.get("ollama_bin"):
        return False, []

    if not quiet:
        print("Ollama is installed but not running. Starting it...")

    try:
        _spawn_ollama(machine)
    except Exception:
        return False, []

    deadline = time.time() + timeout

    while time.time() < deadline:
        time.sleep(1)
        running, models = ollama_running()
        if running:
            if not quiet:
                print("Ollama is running.")
            return True, models

    return False, []


def ollama_pull(model):
    ollama = shutil.which("ollama")

    if not ollama:
        return False

    print(f"  ollama pull {model}")
    result = subprocess.run(
        [ollama, "pull", model],
        check=False
    )
    return result.returncode == 0


def print_ollama_install_help(machine):
    system = machine.get("os")

    print("Install Ollama, then re-run `gmail-audit setup`.")
    print()
    print(f"  {OLLAMA_DOWNLOAD}")
    print()

    if system == "Darwin":
        print("macOS (Homebrew):")
        print("  brew install ollama")
        print("  open -a Ollama")
    elif system == "Windows":
        print("Windows (winget):")
        print("  winget install Ollama.Ollama")
        print("Then start Ollama from the Start menu.")
    else:
        print("Linux:")
        print("  curl -fsSL https://ollama.com/install.sh | sh")
        print("  ollama serve")


def open_gmail_console():
    try:
        webbrowser.open("https://console.cloud.google.com/")
    except Exception:
        pass
