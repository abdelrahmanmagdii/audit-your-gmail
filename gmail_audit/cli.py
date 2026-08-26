import argparse
import os
import sys

from gmail_audit.hardware import detect_machine
from gmail_audit.recommend import (
    CONFIG_FILE,
    GMAIL_SETUP_STEPS,
    installed_ollama_names,
    load_saved_config,
    model_installed,
    ollama_pull,
    ollama_running,
    open_gmail_console,
    print_ollama_install_help,
    recommend_models,
    save_config
)


def _ok(ok):
    return "ok" if ok else "MISSING"


def cmd_doctor(_args):
    machine = detect_machine()
    saved = load_saved_config()
    plan = saved or recommend_models(machine)
    backend = plan.get("backend", "ollama")
    fast = plan.get("fast_model")
    deep = plan.get("deep_model")

    running, models = ollama_running()
    names = installed_ollama_names(models)

    credentials = os.path.exists("credentials.json")
    token = os.path.exists("token.json")
    config = os.path.exists(CONFIG_FILE)

    print()
    print("gmail-audit doctor")
    print("-" * 50)
    print(f"OS            {machine['os']} {machine['arch']}")
    print(f"Python        {machine['python']}")
    print(f"RAM           {machine['ram_gb']} GB")
    print(f"Accelerator   {machine['accelerator']}")
    print(f"Ollama binary {_ok(bool(machine['ollama_bin']))}")
    print(f"Ollama server {_ok(running)}")
    print(f"Backend       {backend}")
    print(f"Stage 1 model {fast}  [{_ok(model_installed(names, fast)) if running else 'n/a'}]")
    print(f"Stage 2 model {deep}  [{_ok(model_installed(names, deep)) if running else 'n/a'}]")
    print(f"credentials.json {_ok(credentials)}")
    print(f"token.json       {_ok(token)}  (created on first run)")
    print(f"{CONFIG_FILE:<16} {_ok(config)}")

    if machine["apple_silicon"]:
        print(f"MLX extra     {_ok(machine['mlx_available'])}  (optional Stage 1)")

    print()

    ready = (
        credentials
        and running
        and model_installed(names, fast)
        and model_installed(names, deep)
    )

    if ready:
        print("Ready. First try:  gmail-audit run --limit 200")
        print("Full inbox:        gmail-audit run")
        return 0

    print("Not ready. Run:  gmail-audit setup")
    print("Gmail OAuth walkthrough: docs/setup-gmail.md")
    return 1


def cmd_setup(args):
    machine = detect_machine()
    plan = recommend_models(machine)
    backend = args.backend or "ollama"

    if backend == "mlx" and not machine["apple_silicon"]:
        print("MLX is Apple Silicon only. Using Ollama.")
        backend = "ollama"

    print()
    print("gmail-audit setup")
    print("-" * 50)
    print(
        f"Detected {machine['os']} / {machine['arch']}, "
        f"{machine['ram_gb']} GB RAM, {machine['accelerator']}."
    )
    print(plan["note"])
    print()
    print(f"  Stage 1 (screen): {plan['fast_model']}")
    print(f"  Stage 2 (extract): {plan['deep_model']}")
    print(f"  Backend: {backend}")
    print()
    print(GMAIL_SETUP_STEPS)
    print()

    if args.open_console:
        open_gmail_console()

    if not os.path.exists("credentials.json"):
        print("credentials.json is not in this directory yet.")
        print("Save the Desktop client JSON as credentials.json, then re-run setup.")
        print()

    if not machine["ollama_bin"]:
        print_ollama_install_help(machine)
        print()
        save_config(plan, backend=backend)
        print(f"Wrote {CONFIG_FILE} (models will be pulled once Ollama is installed).")
        return 1

    running, _models = ollama_running()

    if not running:
        print("Ollama is installed but not running.")
        print("Start the Ollama app, or run:  ollama serve")
        print()
        save_config(plan, backend=backend)
        print(f"Wrote {CONFIG_FILE}.")
        return 1

    if backend == "mlx":
        print("MLX Stage 1 needs:  pip install 'gmail-audit[apple]'")
        print("Weights download on first `gmail-audit run --backend mlx`.")
        print()

    needed = []
    running, models = ollama_running()
    names = installed_ollama_names(models)

    for model in (plan["fast_model"], plan["deep_model"]):
        if not model_installed(names, model) and model not in needed:
            needed.append(model)

    if not needed:
        print("Recommended Ollama models are already installed.")
        save_config(plan, backend=backend)
        print(f"Wrote {CONFIG_FILE}.")
        return 0

    print("Will download (local only, no email is sent):")
    for model in needed:
        print(f"  ollama pull {model}")
    print()

    if not args.yes:
        try:
            answer = input("Proceed? [y/N] ").strip().lower()
        except EOFError:
            answer = "n"

        if answer not in ("y", "yes"):
            print("Skipped pulls.")
            save_config(plan, backend=backend)
            print(f"Wrote {CONFIG_FILE}.")
            return 0

    failed = False

    for model in needed:
        if not ollama_pull(model):
            print(f"Failed to pull {model}")
            failed = True

    save_config(plan, backend=backend)
    print(f"Wrote {CONFIG_FILE}.")

    if failed:
        return 1

    print()
    print("Setup complete.")
    print("First try:  gmail-audit run --limit 200")
    print("Full inbox: gmail-audit run")
    return 0


def cmd_run(args):
    import audit_email

    audit_email.apply_runtime_config(
        backend=args.backend,
        fast_model=args.fast_model,
        deep_model=args.deep_model,
        limit=args.limit
    )
    audit_email.main()
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="gmail-audit",
        description=(
            "Local Gmail subscription auditor. "
            "Mail stays on this machine; models run via Ollama "
            "(MLX optional on Apple Silicon)."
        )
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser(
        "doctor",
        help="Check RAM, Ollama, models, and Gmail credentials"
    )

    setup = sub.add_parser(
        "setup",
        help="Guide Gmail OAuth and install local models for this machine"
    )
    setup.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Pull recommended Ollama models without asking"
    )
    setup.add_argument(
        "--backend",
        choices=("ollama", "mlx"),
        help="Stage 1 backend (default: ollama)"
    )
    setup.add_argument(
        "--open-console",
        action="store_true",
        help="Open Google Cloud Console in a browser"
    )

    run = sub.add_parser(
        "run",
        help="Search Gmail and resume the local audit"
    )
    run.add_argument(
        "--backend",
        choices=("ollama", "mlx"),
        help="Override Stage 1 backend"
    )
    run.add_argument("--fast-model", help="Override Stage 1 Ollama model")
    run.add_argument("--deep-model", help="Override Stage 2 Ollama model")
    run.add_argument(
        "--limit",
        type=int,
        metavar="N",
        help="Only process the N most recent matching emails (first try)"
    )

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        print()
        print("Typical path:  gmail-audit setup && gmail-audit run --limit 200")
        return 0

    if args.command == "doctor":
        return cmd_doctor(args)

    if args.command == "setup":
        return cmd_setup(args)

    if args.command == "run":
        if args.limit is not None and args.limit < 1:
            parser.error("--limit must be a positive integer")
        return cmd_run(args)

    parser.print_help()
    return 1
