# Audit Your Gmail

Local subscription auditor for Gmail. It searches the last five years for recurring-billing language, screens sender/subject/snippet on **Apple Silicon with MLX**, then runs a larger **local Ollama** model only on emails flagged as relevant.

Email contents are never sent to a remote LLM API. Gmail is used only to fetch your mail. Model weights may download once from Hugging Face into a local cache.

## Requirements

- macOS with Apple Silicon
- Python 3.10+
- [Ollama](https://ollama.com) for Stage 2
- A Google Cloud OAuth desktop client with the Gmail API enabled

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Pull the Stage 2 model:

```bash
ollama pull qwen3:8b
```

The Stage 1 MLX model (`mlx-community/Qwen3-4B-4bit`) downloads automatically on first run.

### Gmail API credentials

1. In [Google Cloud Console](https://console.cloud.google.com/), create a project (or pick one).
2. Enable the **Gmail API**.
3. Configure the OAuth consent screen. Add your Google account as a test user if the app is in testing.
4. Create an OAuth client ID of type **Desktop app**.
5. Download the JSON and save it as `credentials.json` in this directory.

A dummy shape lives in `credentials.example.json`. Do not commit real `credentials.json` or `token.json`.

## Run

```bash
python audit_email.py
```

The first run opens a browser for Gmail read-only OAuth and writes `token.json` locally.

Progress is saved to SQLite. Interrupt with Ctrl+C and re-run to resume: already-screened messages are skipped.

## What it does

1. **Search** — broad Gmail query for subscription / renewal / trial / recurring-charge language (`newer_than:5y`).
2. **Stage 1 (MLX)** — classifies metadata only (sender, subject, snippet) in small batches of 8. If Metal runs out of memory it switches to one email at a time instead of retrying into a full GPU. High recall: uncertain or failed screens are kept.
3. **Stage 2 (Ollama 8B)** — fetches the body and extracts merchant, amount, cadence, risk, and evidence **only** for emails Stage 1 marked interesting.

## Outputs (local, gitignored)

| File | Contents |
|---|---|
| `subscription_audit_v2.db` | Screened + analyzed rows |
| `subscription_report.csv` | Per-merchant summary |
| `subscription_timeline.csv` | Event timeline |

Amounts in the report are values **observed in emails**, not confirmed spend. Reconcile against bank or card CSVs for actual totals.

## Privacy

- Inference stays on this machine (MLX in-process, Ollama on `localhost`).
- `credentials.json`, `token.json`, the database, and CSV reports are gitignored.
- The Gmail scope is read-only: `gmail.readonly`.
