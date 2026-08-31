import argparse
import os
import sqlite3

from gmail_audit.hardware import detect_machine
from gmail_audit.oauth import explain_gmail_error, import_desktop_credentials
from gmail_audit.paths import (
    Files,
    data_dir,
    db_has_screening,
    migrate_and_bind,
    open_path
)
from gmail_audit.recommend import (
    GMAIL_SETUP_STEPS,
    config_file,
    ensure_ollama_running,
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

FIRST_RUN_LIMIT = 200


def _ok(ok):
    return "ok" if ok else "MISSING"


def _bind():
    return migrate_and_bind()


def cmd_doctor(_args):
    files = _bind()
    machine = detect_machine()
    saved = load_saved_config()
    plan = saved or recommend_models(machine)
    backend = plan.get("backend", "ollama")
    fast = plan.get("fast_model")
    deep = plan.get("deep_model")

    running, models = ensure_ollama_running()
    names = installed_ollama_names(models)

    credentials = os.path.exists(files.credentials)
    token = os.path.exists(files.token)
    config = os.path.exists(files.config)

    print()
    print("gmail-audit doctor")
    print("-" * 50)
    print(f"Data dir      {files.root}")
    print(f"OS            {machine['os']} {machine['arch']}")
    print(f"Python        {machine['python']}")
    print(f"RAM           {machine['ram_gb']} GB")
    print(f"Accelerator   {machine['accelerator']}")
    print(f"Ollama binary {_ok(bool(machine['ollama_bin']))}")
    print(f"Ollama server {_ok(running)}")
    print(f"Backend       {backend}")
    print(f"Stage 1 model {fast}  [{_ok(model_installed(names, fast)) if running else 'n/a'}]")
    print(f"Stage 2 model {deep}  [{_ok(model_installed(names, deep)) if running else 'n/a'}]")
    print(f"credentials   {_ok(credentials)}  {files.credentials}")
    print(f"token         {_ok(token)}  (created on first run)")
    print(f"config        {_ok(config)}")
    print(f"database      {_ok(os.path.exists(files.db))}")

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
        if db_has_screening(files.db):
            print("Ready. Continue:  gmail-audit run --all")
            print("Reprint CSVs:     gmail-audit report")
        else:
            print("Ready. First try:  gmail-audit run")
            print(f"(defaults to {FIRST_RUN_LIMIT} most recent matches)")
        return 0

    print("Not ready. Run:  gmail-audit setup")
    print("Gmail OAuth walkthrough: docs/setup-gmail.md")
    return 1


def _ensure_credentials(files, args):
    if os.path.exists(files.credentials):
        return True

    imported = import_desktop_credentials(files.credentials)
    if imported:
        return True

    if args.no_open:
        pass
    else:
        print("Opening Google Cloud Console...")
        open_gmail_console()

    if not args.yes:
        try:
            input(
                "Download the Desktop app JSON, then press Enter. "
                "I'll look in Downloads. "
            )
        except EOFError:
            print()

        imported = import_desktop_credentials(files.credentials)
        if imported:
            return True

    print()
    print(f"credentials.json is not in {files.root} yet.")
    print("Save the Desktop client JSON there, or leave it in Downloads and re-run setup.")
    print()
    return os.path.exists(files.credentials)


def cmd_setup(args):
    files = _bind()
    machine = detect_machine()
    plan = recommend_models(machine)
    backend = args.backend or "ollama"

    if backend == "mlx" and not machine["apple_silicon"]:
        print("MLX is Apple Silicon only. Using Ollama.")
        backend = "ollama"

    print()
    print("gmail-audit setup")
    print("-" * 50)
    print(f"Data dir: {files.root}")
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

    _ensure_credentials(files, args)

    if not machine["ollama_bin"]:
        print_ollama_install_help(machine)
        print()
        save_config(plan, backend=backend)
        print(f"Wrote {config_file()} (models will be pulled once Ollama is installed).")
        return 1

    running, _models = ensure_ollama_running()

    if not running:
        print("Could not start Ollama.")
        print("Start the Ollama app, or run:  ollama serve")
        print()
        save_config(plan, backend=backend)
        print(f"Wrote {config_file()}.")
        return 1

    if backend == "mlx":
        print("MLX Stage 1 needs:  pip install 'gmail-audit[apple]'")
        print("Weights download on first `gmail-audit run --backend mlx`.")
        print()

    running, models = ollama_running()
    names = installed_ollama_names(models)
    needed = []

    for model in (plan["fast_model"], plan["deep_model"]):
        if not model_installed(names, model) and model not in needed:
            needed.append(model)

    if not needed:
        print("Recommended Ollama models are already installed.")
        save_config(plan, backend=backend)
        print(f"Wrote {config_file()}.")
        _setup_next_step(files)
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
            print(f"Wrote {config_file()}.")
            return 0

    failed = False

    for model in needed:
        if not ollama_pull(model):
            print(f"Failed to pull {model}")
            failed = True

    save_config(plan, backend=backend)
    print(f"Wrote {config_file()}.")

    if failed:
        return 1

    _setup_next_step(files)
    return 0


def _setup_next_step(files):
    print()
    print("Setup complete.")
    if db_has_screening(files.db):
        print("Continue:  gmail-audit run --all")
    else:
        print(f"First try:  gmail-audit run")
        print(f"(defaults to {FIRST_RUN_LIMIT} most recent matches)")


def cmd_run(args):
    if args.all and args.limit is not None:
        print("Use --limit or --all, not both.")
        return 2

    from gmail_audit import audit as audit_email

    files = _bind()
    audit_email.bind_paths(files)

    if args.all:
        limit = None
    elif args.limit is not None:
        limit = args.limit
    elif not db_has_screening(files.db):
        limit = FIRST_RUN_LIMIT
        print()
        print(
            f"First run: processing {FIRST_RUN_LIMIT} most recent matches."
        )
        print("When that finishes:  gmail-audit run --all")
    else:
        limit = None

    audit_email.apply_runtime_config(
        backend=args.backend,
        fast_model=args.fast_model,
        deep_model=args.deep_model,
        limit=limit
    )
    audit_email.main()
    return 0


def cmd_report(args):
    from gmail_audit import audit as audit_email

    files = _bind()
    audit_email.bind_paths(files)
    audit_email.apply_runtime_config()

    if not os.path.exists(files.db):
        print()
        print("No database yet. Run:  gmail-audit run")
        return 1

    conn = sqlite3.connect(files.db)

    try:
        rows = audit_email.get_all_analysis(conn)
    except sqlite3.Error:
        print()
        print("Database is empty or unreadable. Run:  gmail-audit run")
        conn.close()
        return 1

    if not rows:
        print()
        print("No Stage 2 results yet. Run:  gmail-audit run")
        conn.close()
        return 1

    audit_email.export_timeline(rows)
    report_rows = audit_email.create_merchant_report(rows)
    audit_email.export_merchant_report(report_rows)
    audit_email.print_report(report_rows, limit=args.top)
    audit_email.print_high_risk_timelines(rows)

    print()
    print("=" * 70)
    print("FILES")
    print("=" * 70)
    print(f"Database: {files.db}")
    print(f"Merchant report: {files.report}")
    print(f"Detailed timeline: {files.timeline}")

    conn.close()

    if args.open:
        if not open_path(files.report):
            print(f"Could not open {files.report}")
            return 1

        print(f"Opened {files.report}")

    return 0


def cmd_purge(args):
    # Do not use _bind() here: migrate_and_bind would first copy
    # stray files INTO the directory we are about to delete.
    files = Files(data_dir())

    targets = [
        (files.token, "Gmail token"),
        (files.credentials, "OAuth client"),
        (files.config, "model config"),
        (files.db, "audit database"),
        (files.report, "merchant report CSV"),
        (files.timeline, "timeline CSV"),
    ]

    existing = [
        (path, label)
        for path, label in targets
        if os.path.exists(path)
    ]

    if not existing:
        print(f"Nothing to delete in {files.root}.")
        _print_revoke_hint()
        return 0

    print()
    print("This permanently deletes:")

    for path, label in existing:
        print(f"  {path}  ({label})")

    if not args.yes:
        try:
            answer = input("Delete? [y/N] ").strip().lower()
        except EOFError:
            answer = "n"

        if answer not in ("y", "yes"):
            print("Nothing deleted.")
            return 1

    for path, label in existing:
        try:
            os.remove(path)
            print(f"Deleted {path}")
        except OSError as error:
            print(f"Could not delete {path}: {error}")

    try:
        os.rmdir(files.root)
        print(f"Removed {files.root}")
    except OSError:
        pass

    _print_revoke_hint()
    return 0


def _print_revoke_hint():
    print()
    print("To also revoke the app's Gmail access from your Google")
    print("account (recommended if you are done with the tool):")
    print("  https://myaccount.google.com/permissions")
    print()
    print("Downloaded Ollama models are managed separately:")
    print("  ollama rm <model>")


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
        "--no-open",
        action="store_true",
        help="Do not open Google Cloud Console"
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
        help="Only process the N most recent matching emails"
    )
    run.add_argument(
        "--all",
        action="store_true",
        help="Process every matching email (skip the first-run cap)"
    )

    report = sub.add_parser(
        "report",
        help="Reprint CSVs from the local database (no Gmail call)"
    )
    report.add_argument(
        "--top",
        type=int,
        default=10,
        metavar="N",
        help="Merchants to print (default: 10)"
    )
    report.add_argument(
        "--open",
        action="store_true",
        help="Open the merchant CSV"
    )

    purge = sub.add_parser(
        "purge",
        help="Delete the token, credentials, database, and CSVs"
    )
    purge.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Delete without asking"
    )

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        print()
        print("Typical path:  gmail-audit setup && gmail-audit run")
        return 0

    if args.command == "doctor":
        return cmd_doctor(args)

    if args.command == "setup":
        return cmd_setup(args)

    if args.command == "run":
        if args.limit is not None and args.limit < 1:
            parser.error("--limit must be a positive integer")
        return cmd_run(args)

    if args.command == "report":
        if args.top < 1:
            parser.error("--top must be a positive integer")
        return cmd_report(args)

    if args.command == "purge":
        return cmd_purge(args)

    parser.print_help()
    return 1
