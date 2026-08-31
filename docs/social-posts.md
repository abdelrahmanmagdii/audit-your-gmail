# Posts

Copy after the GitHub README matches this tree. Do not attach a real inbox screenshot. The sample CSVs in `examples/` are fake.

Repo: https://github.com/abdelrahmanmagdii/audit-your-gmail

## LinkedIn

I built a local tool that scans Gmail for subscriptions and renewals without sending mail to a cloud LLM.

Gmail read-only → a small local model screens sender/subject/snippet → a larger local model reads only the hits → CSV and SQLite on your machine.

https://github.com/abdelrahmanmagdii/audit-your-gmail

- Inference stays on localhost (Ollama; optional MLX on Apple Silicon)
- Resume-safe SQLite: stop with Ctrl+C and run again
- The first `gmail-audit run` caps itself at the 200 most recent matches so you are not processing five years overnight; `--all` does the rest

You create your own Google Cloud Desktop OAuth client. I cannot ship a shared Gmail login — Google treats gmail.readonly as a restricted scope.

Ollama will download a few GB of weights. Amounts in the report are values seen in email, not confirmed spend.

Clone, `pip install -e .`, `gmail-audit setup`, then `gmail-audit run`.

## X (short)

Local Gmail subscription audit. Mail never hits a cloud LLM.

Read-only Gmail → local models → CSV on your machine.

github.com/abdelrahmanmagdii/audit-your-gmail

You bring Ollama + your own Google Desktop OAuth client.

## X (longer)

I open-sourced a local Gmail subscription auditor.

It searches for renewal/trial/charge language, screens metadata with a small local model, and only then reads the body of the hits. Nothing is sent to a remote LLM API.

https://github.com/abdelrahmanmagdii/audit-your-gmail

macOS, Windows, Linux. `gmail-audit setup` picks models from your RAM. First `gmail-audit run` caps itself at 200 recent matches; `--all` does the rest.

Constraint: you create a Google Cloud Desktop client (gmail.readonly is restricted, so there is no shared “Sign in with Google” for this repo) and you run Ollama locally.
