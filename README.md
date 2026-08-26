# Audit Your Gmail

Local subscription auditor for Gmail. It searches recent mail for recurring-billing language, screens sender/subject/snippet with a **small local model**, then runs a **larger local model** only on the hits.

Works on **macOS, Windows, and Linux**. Default inference is [Ollama](https://ollama.com). Apple Silicon can optionally use MLX for Stage 1.

Email bodies are never sent to a remote LLM API. Gmail is used only to fetch your mail.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .
gmail-audit setup
gmail-audit run --limit 200        # first try (minutes)
gmail-audit run                    # rest of the inbox (resumes)
```

`setup` detects RAM/OS/GPU, tells you which Ollama models to pull, and walks through Gmail OAuth. `run` resumes from SQLite if you stop it.

```bash
gmail-audit doctor                 # what is missing?
gmail-audit setup --yes            # pull models without a prompt
gmail-audit run --backend ollama   # default, all platforms
gmail-audit run --backend mlx      # Apple Silicon only
```

Apple Silicon extra (optional faster Stage 1):

```bash
pip install -e ".[apple]"
gmail-audit setup --backend mlx
```

## Gmail API credentials

You create your own Desktop OAuth client. This repo cannot ship a shared Gmail login: `gmail.readonly` is a [restricted Google scope](https://developers.google.com/workspace/gmail/api/auth/scopes).

**Walkthrough:** [docs/setup-gmail.md](docs/setup-gmail.md)

Short version:

1. [Google Cloud Console](https://console.cloud.google.com/) → new project
2. Enable the **Gmail API**
3. OAuth consent: **External**, stay in **Testing**, add **yourself** as a test user, add scope `gmail.readonly`
4. OAuth client ID → **Desktop app**
5. Save the JSON as `credentials.json` in this directory

See `credentials.example.json`. Do not commit `credentials.json` or `token.json`.

The first browser sign-in shows **“Google hasn’t verified this app”**. That is expected. Advanced → Continue.

## How it picks models

| RAM | Stage 1 (screen) | Stage 2 (extract) |
|---|---|---|
| ~8 GB | `qwen2.5:1.5b` | same small model |
| ~16 GB | `qwen2.5:3b` | `qwen2.5:7b` |
| ~24–32 GB | `qwen2.5:3b` | `qwen3:8b` |
| 64 GB+ | `qwen2.5:7b` | `qwen2.5:14b` |

Only one stage is loaded at a time. Fanless laptops will still get warm on Stage 2; use a hard desk and plug in. CPU-only Windows/Linux works and is slow.

Ollama downloads are local model weights (a few GB). `setup` asks before pulling.

## What it does

1. **Search** — Gmail query for subscription / renewal / trial / charge language (`newer_than:5y`).
2. **Stage 1** — classifies metadata only. High recall: uncertain screens are kept.
3. **Stage 2** — reads the body and extracts merchant, amount, cadence, risk, evidence **only** for interesting mail.

`--limit N` processes the N most recent matches so a first run finishes in minutes. Re-run without `--limit` to continue; already-screened messages are skipped.

## Outputs (gitignored)

| File | Contents |
|---|---|
| `subscription_audit_v2.db` | Screened + analyzed rows |
| `subscription_report.csv` | Per-merchant summary (after Stage 2) |
| `subscription_timeline.csv` | Event timeline (after Stage 2) |
| `.gmail-audit.json` | Models chosen by `setup` |

Fake sample (not a real inbox): [`examples/sample_subscription_report.csv`](examples/sample_subscription_report.csv).

Amounts are values **seen in email**, not confirmed spend. Reconcile against bank/card CSVs.

## Privacy

- Inference is local (Ollama on localhost, optional MLX in-process).
- Gmail scope is read-only: `gmail.readonly`.
- Secrets, the database, and CSV reports are gitignored.

## License

MIT. See [LICENSE](LICENSE).
