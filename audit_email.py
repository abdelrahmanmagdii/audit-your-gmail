import os
import csv
import gc
import json
import base64
import sqlite3
import re
import time
from collections import defaultdict, Counter
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build


# ============================================================
# CONFIG
# ============================================================

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly"
]

CREDENTIALS_FILE = "credentials.json"
TOKEN_FILE = "token.json"

# New database so old experimental results don't interfere.
DB_PATH = "subscription_audit_v2.db"

REPORT_CSV = "subscription_report.csv"
TIMELINE_CSV = "subscription_timeline.csv"

OLLAMA_URL = "http://localhost:11434/api/chat"

# Stage 1: local MLX batched inference on Apple Silicon.
# One short prompt per email; never send mail to a remote API.
FAST_MLX_MODEL = "mlx-community/Qwen3-4B-4bit"

# Stage 2: larger local model, interesting emails only.
DEEP_MODEL = "qwen3:8b"

# Stage 1 must stay small on unified memory. 96-wide batches plus
# split-on-OOM retries can jetsam the Mac.
FAST_BATCH_SIZE = 8
FAST_PREFILL_SIZE = 4
SCREEN_MAX_TOKENS = 96

# Keep unless the screen is clearly irrelevant.
KEEP_THRESHOLD = 0.55

# Stage 2:
# 4 full emails per Qwen 8B request.
DEEP_BATCH_SIZE = 4

# We only need the beginning of the email for most billing emails.
MAX_DEEP_BODY_CHARS = 6000

# Gmail search intentionally remains reasonably broad.
# Local MLX Stage 1 does the intelligent filtering.
SEARCH_QUERY = """
newer_than:5y {
    "subscription"
    "membership"
    "automatically renew"
    "automatic renewal"
    "auto-renew"
    "auto renew"
    "set to renew"
    "will renew"
    "renewal"
    "recurring payment"
    "recurring charge"
    "recurring subscription"
    "your card will be charged"
    "will be charged"
    "will be billed"
    "next payment"
    "trial ends"
    "trial is ending"
    "trial will end"
    "price change"
    "membership fee"
    "subscription fee"
}
"""


# ============================================================
# GENERAL HELPERS
# ============================================================

def timestamp_to_iso(internal_date):
    if not internal_date:
        return None

    try:
        ts = int(internal_date) / 1000
        return datetime.fromtimestamp(
            ts,
            tz=timezone.utc
        ).isoformat()
    except Exception:
        return None


def clean_sender(sender):
    if not sender:
        return ""

    return re.sub(
        r"\s+",
        " ",
        sender
    ).strip()


def chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


# ============================================================
# GMAIL AUTH
# ============================================================

def get_gmail_service():
    creds = None

    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(
            TOKEN_FILE,
            SCOPES
        )

    if not creds or not creds.valid:

        if creds and creds.expired and creds.refresh_token:
            print("Refreshing Gmail authentication...")
            creds.refresh(Request())

        else:
            print("Opening Google authentication...")

            flow = InstalledAppFlow.from_client_secrets_file(
                CREDENTIALS_FILE,
                SCOPES
            )

            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build(
        "gmail",
        "v1",
        credentials=creds,
        cache_discovery=False
    )


# ============================================================
# GMAIL SEARCH
# ============================================================

def search_gmail(service):
    message_ids = []
    page_token = None

    while True:

        kwargs = {
            "userId": "me",
            "q": SEARCH_QUERY,
            "maxResults": 500
        }

        if page_token:
            kwargs["pageToken"] = page_token

        response = (
            service.users()
            .messages()
            .list(**kwargs)
            .execute()
        )

        for message in response.get("messages", []):
            message_ids.append(message["id"])

        page_token = response.get("nextPageToken")

        if not page_token:
            break

    return message_ids


# ============================================================
# EMAIL METADATA / PREVIEW
# ============================================================

def get_header(headers, name):
    target = name.lower()

    for header in headers:
        if header.get("name", "").lower() == target:
            return header.get("value", "")

    return ""


def get_email_preview(service, message_id):
    """
    Stage 1 does NOT download/parse the full body.

    We only ask Gmail for:
    - subject
    - sender
    - date
    - Gmail's short snippet
    """

    msg = (
        service.users()
        .messages()
        .get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=[
                "Subject",
                "From",
                "Date"
            ]
        )
        .execute()
    )

    payload = msg.get("payload", {})
    headers = payload.get("headers", [])

    return {
        "gmail_id": message_id,
        "thread_id": msg.get("threadId"),
        "internal_date": int(
            msg.get("internalDate", 0)
        ),
        "date_iso": timestamp_to_iso(
            msg.get("internalDate")
        ),
        "subject": get_header(
            headers,
            "Subject"
        ),
        "sender": clean_sender(
            get_header(headers, "From")
        ),
        "snippet": msg.get(
            "snippet",
            ""
        )[:800]
    }


# ============================================================
# FULL EMAIL PARSING
# ============================================================

def decode_gmail_data(data):
    if not data:
        return ""

    try:
        # Gmail uses URL-safe base64.
        # Add padding if necessary.
        missing_padding = len(data) % 4

        if missing_padding:
            data += "=" * (4 - missing_padding)

        decoded = base64.urlsafe_b64decode(data)

        return decoded.decode(
            "utf-8",
            errors="replace"
        )

    except Exception:
        return ""


def html_to_text(html):
    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    for tag in soup([
        "script",
        "style",
        "noscript"
    ]):
        tag.decompose()

    text = soup.get_text(
        separator="\n"
    )

    text = re.sub(
        r"[ \t]+",
        " ",
        text
    )

    text = re.sub(
        r"\n{3,}",
        "\n\n",
        text
    )

    return text.strip()


def extract_body(payload):
    plain_parts = []
    html_parts = []

    def walk(part):
        mime_type = part.get(
            "mimeType",
            ""
        )

        body = part.get(
            "body",
            {}
        )

        data = body.get("data")

        if data:
            decoded = decode_gmail_data(data)

            if mime_type == "text/plain":
                plain_parts.append(decoded)

            elif mime_type == "text/html":
                html_parts.append(decoded)

        for child in part.get(
            "parts",
            []
        ):
            walk(child)

    walk(payload)

    if plain_parts:
        return "\n".join(
            plain_parts
        ).strip()

    if html_parts:
        return html_to_text(
            "\n".join(html_parts)
        )

    return ""


def get_full_email(service, preview):
    msg = (
        service.users()
        .messages()
        .get(
            userId="me",
            id=preview["gmail_id"],
            format="full"
        )
        .execute()
    )

    payload = msg.get(
        "payload",
        {}
    )

    body = extract_body(payload)

    return {
        **preview,
        "body": body
    }


# ============================================================
# LOCAL INFERENCE — MLX (STAGE 1) + OLLAMA (STAGE 2)
# ============================================================

def check_ollama():
    try:
        response = requests.get(
            "http://localhost:11434/api/tags",
            timeout=5
        )

        response.raise_for_status()

        models = response.json().get(
            "models",
            []
        )

        names = [
            model.get("name", "")
            for model in models
        ]

        print("Ollama is running.")

        if not any(
            name.startswith(DEEP_MODEL)
            for name in names
        ):

            print()
            print("Missing Ollama model:")
            print(
                f"  ollama pull {DEEP_MODEL}"
            )

            return False

        return True

    except Exception:
        print()
        print(
            "Cannot connect to Ollama."
        )

        print(
            "Open the Ollama app or run:"
        )

        print()
        print(
            "ollama serve"
        )

        return False


def call_ollama(
    model,
    messages,
    num_ctx=8192,
    num_predict=3000,
    retries=2
):
    """
    Stage 2 only. Localhost Ollama. Email bodies never leave this machine.
    """

    last_error = None

    for attempt in range(retries + 1):

        try:

            response = requests.post(
                OLLAMA_URL,
                json={
                    "model": model,
                    "stream": False,
                    "think": False,
                    "format": "json",
                    "keep_alive": "2m",

                    "options": {
                        "temperature": 0,
                        "num_ctx": num_ctx,
                        "num_predict": num_predict
                    },

                    "messages": messages
                },
                timeout=300
            )

            response.raise_for_status()

            content = (
                response.json()
                ["message"]
                ["content"]
            )

            return json.loads(content)

        except Exception as error:
            last_error = error

            if attempt < retries:
                print(
                    f"    Ollama retry "
                    f"{attempt + 1}/{retries}..."
                )

                time.sleep(1)

    raise last_error


def load_fast_mlx_model():
    try:
        from mlx_lm import load
    except ImportError:
        print()
        print(
            "ERROR: mlx-lm is not installed."
        )
        print(
            "Install on Apple Silicon with:"
        )
        print()
        print(
            "  pip install mlx mlx-lm"
        )
        return None

    print()
    print(
        f"Loading local MLX model: {FAST_MLX_MODEL}"
    )
    print(
        "(weights stay on disk after the first download; "
        "email text is never sent to Hugging Face or any API)"
    )

    try:
        model, tokenizer = load(
            FAST_MLX_MODEL
        )
    except Exception as error:
        print()
        print(
            "ERROR: could not load the local MLX screening model."
        )
        print(error)
        return None

    print("MLX Stage 1 model ready.")

    return model, tokenizer


def clear_mlx_memory():
    gc.collect()

    try:
        import mlx.core as mx
        mx.clear_cache()
        mx.synchronize()
    except Exception:
        pass


def unload_mlx_model(model):
    del model
    clear_mlx_memory()


def unload_ollama():
    """
    Drop the Stage 2 weights from RAM before MLX Stage 1.
    """

    try:
        requests.post(
            "http://localhost:11434/api/generate",
            json={
                "model": DEEP_MODEL,
                "prompt": "",
                "keep_alive": 0
            },
            timeout=10
        )
    except Exception:
        pass


def is_memory_error(error):
    message = str(error).lower()

    return (
        isinstance(error, MemoryError)
        or "insufficient memory" in message
        or "out of memory" in message
        or "failed to allocate" in message
    )


def extract_json_object(text):
    if not text:
        return None

    cleaned = text.strip()

    if cleaned.startswith("```"):
        cleaned = re.sub(
            r"^```(?:json)?\s*",
            "",
            cleaned,
            count=1,
            flags=re.IGNORECASE
        )
        cleaned = re.sub(
            r"\s*```$",
            "",
            cleaned
        ).strip()

    try:
        json.loads(cleaned)
        return cleaned
    except Exception:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")

    if start == -1 or end == -1 or end <= start:
        return None

    return cleaned[start:end + 1]


# ============================================================
# STAGE 1 — FAST SCREENING (MLX BATCHED)
# ============================================================

FAST_SYSTEM_PROMPT = """
You are screening ONE email preview for a personal financial audit.

The user wants to discover ongoing or historical recurring financial
commitments and reconstruct their lifecycle.

Classify the email into ONE of these event types:

signup
active_renewal
recurring_charge
trial_conversion
price_change
cancellation
expiration
missed_appointment
onboarding
payment_failure
refund
irrelevant
uncertain

IMPORTANT — high recall:

Keep emails that could help reconstruct a subscription history,
including cancellations and signups.

Examples worth keeping:

- "Your membership will automatically renew"
- "Welcome to your membership"
- "Your card will be charged"
- "Your trial becomes paid tomorrow"
- "Your price is increasing"
- "Your subscription has been cancelled"
- "You missed your appointment"
- "Complete onboarding"
- "Your recurring payment failed"

Examples that are usually irrelevant:

- newsletters mentioning the word subscription
- ordinary ecommerce receipts
- a random news article mentioning memberships
- mailing-list unsubscribe footers
- one-time purchases

Return ONLY JSON for this one email:

{
  "event_type": "active_renewal",
  "interesting": true,
  "merchant_hint": "LifeMD",
  "confidence": 0.98
}

Do not invent merchant names.
Use null when unknown.
"""


def preview_user_content(preview):
    return json.dumps(
        {
            "subject": preview["subject"],
            "sender": preview["sender"],
            "date": preview["date_iso"],
            "snippet": preview["snippet"]
        },
        ensure_ascii=False
    )


def encode_screen_prompt(tokenizer, preview):
    conversation = [
        {
            "role": "system",
            "content": FAST_SYSTEM_PROMPT
        },
        {
            "role": "user",
            "content": preview_user_content(preview)
        }
    ]

    try:
        tokens = tokenizer.apply_chat_template(
            conversation,
            add_generation_prompt=True,
            enable_thinking=False
        )
    except TypeError:
        tokens = tokenizer.apply_chat_template(
            conversation,
            add_generation_prompt=True
        )

    if isinstance(tokens, str):
        tokens = tokenizer.encode(tokens)

    if hasattr(tokens, "tolist"):
        tokens = tokens.tolist()

    tokens = list(tokens)

    if not tokens:
        tokens = tokenizer.encode(" ")

    return tokens


def keep_screen_result(item):
    """
    High recall: keep unless clearly irrelevant with enough confidence.
    """

    event_type = item.get("event_type") or "uncertain"
    confidence = item.get("confidence")

    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.0

    raw_interesting = item.get("interesting")

    if raw_interesting is None:
        interesting = True
    else:
        interesting = bool(raw_interesting)

    if event_type in (
        "uncertain",
        None,
        ""
    ):
        interesting = True

    if event_type != "irrelevant":
        interesting = True

    if confidence < KEEP_THRESHOLD:
        interesting = True

    item["event_type"] = event_type
    item["confidence"] = confidence
    item["interesting"] = interesting

    return item


def parse_screen_result(text, gmail_id):
    fallback = {
        "id": gmail_id,
        "event_type": "uncertain",
        "interesting": True,
        "merchant_hint": None,
        "confidence": 0.0
    }

    raw = extract_json_object(text)

    if not raw:
        return fallback

    try:
        obj = json.loads(raw)
    except Exception:
        return fallback

    if not isinstance(obj, dict):
        return fallback

    return keep_screen_result({
        "id": gmail_id,
        "event_type": obj.get("event_type") or "uncertain",
        "interesting": obj.get("interesting"),
        "merchant_hint": obj.get("merchant_hint"),
        "confidence": obj.get("confidence")
    })


def _mlx_stop_tokens(tokenizer):
    eos_ids = getattr(tokenizer, "eos_token_ids", None)

    if eos_ids:
        return [[token] for token in eos_ids]

    eos_id = getattr(tokenizer, "eos_token_id", None)

    if eos_id is not None:
        return [[eos_id]]

    return []


_screen_one_at_a_time = False


def _mlx_generate_one(model, tokenizer, prompt_tokens, max_tokens):
    from mlx_lm import generate
    from mlx_lm.sample_utils import make_sampler

    return generate(
        model,
        tokenizer,
        prompt=prompt_tokens,
        max_tokens=max_tokens,
        sampler=make_sampler(temp=0.0),
        verbose=False
    )


def _mlx_batch_texts(model, tokenizer, prompts, max_tokens=SCREEN_MAX_TOKENS):
    """
    Tiny Metal batches. Never pad completion size above the real prompt count.
    """

    from mlx_lm.generate import BatchGenerator
    from mlx_lm.sample_utils import make_sampler

    n = len(prompts)
    gen = None

    try:
        gen = BatchGenerator(
            model,
            stop_tokens=_mlx_stop_tokens(tokenizer),
            sampler=make_sampler(temp=0.0),
            prefill_batch_size=min(FAST_PREFILL_SIZE, n),
            completion_batch_size=n,
        )

        uids = gen.insert(
            prompts,
            [max_tokens] * n
        )

        collected = {
            uid: []
            for uid in uids
        }

        while True:
            responses = gen.next_generated()

            if not responses:
                break

            for response in responses:
                if response.finish_reason != "stop":
                    collected[response.uid].append(
                        response.token
                    )

        return [
            tokenizer.decode(collected[uid])
            for uid in uids
        ]

    finally:
        if gen is not None:
            gen.close()
        clear_mlx_memory()


def _screen_one(model, tokenizer, preview):
    prompt = encode_screen_prompt(tokenizer, preview)

    try:
        text = _mlx_generate_one(
            model,
            tokenizer,
            prompt,
            SCREEN_MAX_TOKENS
        )
    finally:
        clear_mlx_memory()

    return parse_screen_result(
        text,
        preview["gmail_id"]
    )


def _screen_batch_once(model, tokenizer, previews):
    prompts = [
        encode_screen_prompt(tokenizer, preview)
        for preview in previews
    ]

    texts = _mlx_batch_texts(
        model,
        tokenizer,
        prompts,
        max_tokens=SCREEN_MAX_TOKENS
    )

    return [
        parse_screen_result(text, preview["gmail_id"])
        for text, preview in zip(texts, previews)
    ]


def screen_batch(model, tokenizer, previews):
    """
    One short prompt per email. Email contents stay in-process.

    On Metal OOM, switch to one-at-a-time generation for the rest of
    the run. Never split-and-retry into a full GPU.
    """

    global _screen_one_at_a_time

    if not previews:
        return []

    if _screen_one_at_a_time:
        results = []

        for preview in previews:
            try:
                results.append(
                    _screen_one(model, tokenizer, preview)
                )
            except Exception as error:
                if is_memory_error(error):
                    clear_mlx_memory()
                    print(
                        f"    Memory still exhausted on "
                        f"{preview['gmail_id']}; keeping as uncertain."
                    )
                    results.append(
                        parse_screen_result(
                            "",
                            preview["gmail_id"]
                        )
                    )
                    time.sleep(1)
                    continue

                print(
                    f"    Screen failed for "
                    f"{preview['gmail_id']}: {error}"
                )
                results.append(
                    parse_screen_result(
                        "",
                        preview["gmail_id"]
                    )
                )

        return results

    try:
        return _screen_batch_once(
            model,
            tokenizer,
            previews
        )

    except Exception as error:
        clear_mlx_memory()

        if is_memory_error(error):
            _screen_one_at_a_time = True
            print()
            print(
                "    Metal ran out of memory. Switching Stage 1 "
                "to one email at a time so the Mac does not panic."
            )
            print()
            time.sleep(1)
            return screen_batch(model, tokenizer, previews)

        print(
            f"    MLX batch failed "
            f"({len(previews)}): {error}"
        )

        return [
            parse_screen_result("", preview["gmail_id"])
            for preview in previews
        ]


# ============================================================
# STAGE 2 — DEEP ANALYSIS
# ============================================================

DEEP_SYSTEM_PROMPT = """
You are performing a detailed audit of emails related to subscriptions,
memberships and recurring financial commitments.

Analyze each supplied email independently.

Possible event_type values:

signup
active_renewal
recurring_charge
trial_conversion
price_change
cancellation
expiration
missed_appointment
onboarding
payment_failure
refund
other_relevant
irrelevant

For each email extract:

- canonical merchant/company name
- event type
- whether it indicates a recurring financial commitment
- status
- amount
- currency
- cadence
- renewal date if explicitly stated
- category
- risk
- concise evidence

Risk guidance:

critical:
Strong evidence of substantial unexplained billing, continued billing
despite cancellation, or similarly serious issue.

high:
Recurring charge with concerning signs such as apparent non-use,
missed appointment, incomplete onboarding, unexpected automatic
renewal, difficult cancellation, etc.

medium:
Normal active paid subscription or upcoming renewal worth reviewing.

low:
Routine lifecycle event with little financial concern.

Do NOT claim that someone was charged unless the email actually says so.

Do NOT assume a renewal notice proves a charge occurred.

Do NOT invent amounts, dates or services.

Return ONLY JSON:

{
  "results": [
    {
      "id": "gmail id",
      "merchant": "LifeMD",
      "category": "medical membership",
      "event_type": "active_renewal",
      "is_recurring": true,
      "status": "active",
      "amount": 149.00,
      "currency": "USD",
      "cadence": "monthly",
      "renewal_date": null,
      "confidence": 0.99,
      "risk_level": "high",
      "risk_flags": [
        "automatic renewal",
        "possible non-use"
      ],
      "summary": "Weight management membership is set to renew automatically.",
      "evidence": "Your membership will automatically renew."
    }
  ]
}

Rules:

- amount is numeric or null
- confidence is 0 through 1
- currency is e.g. USD or null
- cadence should preferably be:
  weekly
  monthly
  quarterly
  yearly
  unknown
- status should preferably be:
  active
  cancelled
  expired
  failed
  refunded
  unknown
- risk_level:
  low
  medium
  high
  critical
- evidence must be short
- return one result for every supplied email
"""


def deep_analyze_batch(full_emails):
    payload = []

    for email in full_emails:

        payload.append({
            "id": email["gmail_id"],
            "subject": email["subject"],
            "sender": email["sender"],
            "date": email["date_iso"],
            "body": email["body"][
                :MAX_DEEP_BODY_CHARS
            ]
        })

    messages = [
        {
            "role": "system",
            "content": DEEP_SYSTEM_PROMPT
        },
        {
            "role": "user",
            "content": json.dumps(
                payload,
                ensure_ascii=False
            )
        }
    ]

    result = call_ollama(
        DEEP_MODEL,
        messages,
        num_ctx=12288,
        num_predict=5000
    )

    returned = {
        item.get("id"): item
        for item in result.get(
            "results",
            []
        )
        if item.get("id")
    }

    final_results = []

    for email in full_emails:

        gmail_id = email["gmail_id"]

        if gmail_id in returned:
            final_results.append(
                returned[gmail_id]
            )

        else:
            final_results.append({
                "id": gmail_id,
                "merchant": None,
                "category": None,
                "event_type": "other_relevant",
                "is_recurring": False,
                "status": "unknown",
                "amount": None,
                "currency": None,
                "cadence": "unknown",
                "renewal_date": None,
                "confidence": 0.0,
                "risk_level": "low",
                "risk_flags": [],
                "summary": "Model did not return a result.",
                "evidence": None
            })

    return final_results


# ============================================================
# DATABASE
# ============================================================

def init_db():
    conn = sqlite3.connect(
        DB_PATH
    )

    conn.execute("""
        CREATE TABLE IF NOT EXISTS screened_emails (
            gmail_id TEXT PRIMARY KEY,
            thread_id TEXT,
            internal_date INTEGER,
            date_iso TEXT,
            sender TEXT,
            subject TEXT,
            snippet TEXT,

            event_type TEXT,
            interesting INTEGER,
            merchant_hint TEXT,
            confidence REAL,

            screened_at TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS analyzed_emails (
            gmail_id TEXT PRIMARY KEY,
            thread_id TEXT,
            internal_date INTEGER,
            date_iso TEXT,
            sender TEXT,
            subject TEXT,

            merchant TEXT,
            category TEXT,
            event_type TEXT,

            is_recurring INTEGER,
            status TEXT,

            amount REAL,
            currency TEXT,
            cadence TEXT,

            renewal_date TEXT,

            confidence REAL,
            risk_level TEXT,

            risk_flags TEXT,
            summary TEXT,
            evidence TEXT,

            analyzed_at TEXT
        )
    """)

    conn.commit()

    return conn


def is_screened(conn, gmail_id):
    row = conn.execute(
        """
        SELECT 1
        FROM screened_emails
        WHERE gmail_id = ?
        """,
        (gmail_id,)
    ).fetchone()

    return row is not None


def is_analyzed(conn, gmail_id):
    row = conn.execute(
        """
        SELECT 1
        FROM analyzed_emails
        WHERE gmail_id = ?
        """,
        (gmail_id,)
    ).fetchone()

    return row is not None


def save_screening(
    conn,
    preview,
    result
):
    conn.execute(
        """
        INSERT OR REPLACE INTO screened_emails (
            gmail_id,
            thread_id,
            internal_date,
            date_iso,
            sender,
            subject,
            snippet,

            event_type,
            interesting,
            merchant_hint,
            confidence,

            screened_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            preview["gmail_id"],
            preview["thread_id"],
            preview["internal_date"],
            preview["date_iso"],
            preview["sender"],
            preview["subject"],
            preview["snippet"],

            result.get("event_type"),
            int(bool(
                result.get("interesting")
            )),
            result.get("merchant_hint"),
            result.get("confidence"),

            datetime.now(
                timezone.utc
            ).isoformat()
        )
    )


def save_deep_result(
    conn,
    email,
    result
):
    flags = result.get(
        "risk_flags",
        []
    )

    if not isinstance(flags, list):
        flags = []

    conn.execute(
        """
        INSERT OR REPLACE INTO analyzed_emails (
            gmail_id,
            thread_id,
            internal_date,
            date_iso,
            sender,
            subject,

            merchant,
            category,
            event_type,

            is_recurring,
            status,

            amount,
            currency,
            cadence,

            renewal_date,

            confidence,
            risk_level,

            risk_flags,
            summary,
            evidence,

            analyzed_at
        )
        VALUES (
            ?, ?, ?, ?, ?, ?,
            ?, ?, ?,
            ?, ?,
            ?, ?, ?,
            ?,
            ?, ?,
            ?, ?, ?,
            ?
        )
        """,
        (
            email["gmail_id"],
            email["thread_id"],
            email["internal_date"],
            email["date_iso"],
            email["sender"],
            email["subject"],

            result.get("merchant"),
            result.get("category"),
            result.get("event_type"),

            int(bool(
                result.get("is_recurring")
            )),
            result.get("status"),

            result.get("amount"),
            result.get("currency"),
            result.get("cadence"),

            result.get("renewal_date"),

            result.get("confidence"),
            result.get("risk_level"),

            json.dumps(flags),
            result.get("summary"),
            result.get("evidence"),

            datetime.now(
                timezone.utc
            ).isoformat()
        )
    )


# ============================================================
# STAGE 1 RUNNER
# ============================================================

def save_screening_batch(conn, previews, results):
    for preview, result in zip(previews, results):
        save_screening(
            conn,
            preview,
            result
        )

    conn.commit()

    interesting = sum(
        1
        for result in results
        if result.get("interesting")
    )

    print(
        f"    {interesting}/"
        f"{len(results)} kept"
    )


def flush_screening_batch(
    conn,
    model,
    tokenizer,
    previews,
    index,
    total
):
    print(
        f"Screening Gmail emails "
        f"{index}/{total} "
        f"(batch of {len(previews)})..."
    )

    results = screen_batch(
        model,
        tokenizer,
        previews
    )

    save_screening_batch(
        conn,
        previews,
        results
    )


def run_screening(
    service,
    conn,
    message_ids,
    model,
    tokenizer
):
    print()
    print("=" * 70)
    print("STAGE 1 — FAST SCREENING WITH LOCAL MLX")
    print("=" * 70)
    print(
        f"Batch size: {FAST_BATCH_SIZE} "
        "(falls back to 1 on Metal OOM)"
    )

    pending_previews = []

    total = len(message_ids)
    fetched = 0

    for index, message_id in enumerate(
        message_ids,
        start=1
    ):

        if is_screened(
            conn,
            message_id
        ):
            continue

        try:
            preview = get_email_preview(
                service,
                message_id
            )
        except KeyboardInterrupt:
            conn.commit()
            raise
        except Exception as error:
            print(
                f"Preview error "
                f"{message_id}: {error}"
            )
            continue

        pending_previews.append(
            preview
        )
        fetched += 1

        if len(pending_previews) >= FAST_BATCH_SIZE:
            batch = pending_previews[:FAST_BATCH_SIZE]
            pending_previews = pending_previews[FAST_BATCH_SIZE:]
            try:
                flush_screening_batch(
                    conn,
                    model,
                    tokenizer,
                    batch,
                    index,
                    total
                )
            except KeyboardInterrupt:
                conn.commit()
                raise

    if pending_previews:
        try:
            flush_screening_batch(
                conn,
                model,
                tokenizer,
                pending_previews,
                total,
                total
            )
        except KeyboardInterrupt:
            conn.commit()
            raise

    total_screened = conn.execute(
        """
        SELECT COUNT(*)
        FROM screened_emails
        """
    ).fetchone()[0]

    interesting = conn.execute(
        """
        SELECT COUNT(*)
        FROM screened_emails
        WHERE interesting = 1
        """
    ).fetchone()[0]

    print()
    print(
        f"Screened:    {total_screened}"
    )

    print(
        f"Kept:        {interesting}"
    )


# ============================================================
# STAGE 2 RUNNER
# ============================================================

def load_interesting_previews(conn):
    rows = conn.execute(
        """
        SELECT
            gmail_id,
            thread_id,
            internal_date,
            date_iso,
            sender,
            subject,
            snippet
        FROM screened_emails
        WHERE interesting = 1
        ORDER BY internal_date ASC
        """
    ).fetchall()

    results = []

    for row in rows:

        results.append({
            "gmail_id": row[0],
            "thread_id": row[1],
            "internal_date": row[2],
            "date_iso": row[3],
            "sender": row[4],
            "subject": row[5],
            "snippet": row[6]
        })

    return results


def count_pending_deep_analysis(conn):
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM screened_emails
        WHERE interesting = 1
          AND gmail_id NOT IN (
              SELECT gmail_id
              FROM analyzed_emails
          )
        """
    ).fetchone()

    return row[0]


def count_pending_screening(conn, message_ids):
    if not message_ids:
        return 0

    screened = {
        row[0]
        for row in conn.execute(
            """
            SELECT gmail_id
            FROM screened_emails
            """
        )
    }

    return sum(
        1
        for message_id in message_ids
        if message_id not in screened
    )


def run_deep_analysis(
    service,
    conn
):
    print()
    print("=" * 70)
    print("STAGE 2 — DEEP ANALYSIS WITH QWEN3 8B")
    print("=" * 70)

    previews = load_interesting_previews(
        conn
    )

    pending = [
        preview
        for preview in previews
        if not is_analyzed(
            conn,
            preview["gmail_id"]
        )
    ]

    print(
        f"Emails requiring deep analysis: "
        f"{len(pending)}"
    )

    if not pending:
        return

    processed = 0

    for preview_batch in chunks(
        pending,
        DEEP_BATCH_SIZE
    ):

        full_emails = []

        for preview in preview_batch:

            try:

                full_email = get_full_email(
                    service,
                    preview
                )

                full_emails.append(
                    full_email
                )

            except Exception as error:

                print(
                    f"Could not fetch "
                    f"{preview['subject'][:50]}: "
                    f"{error}"
                )

        if not full_emails:
            continue

        try:

            print()
            print(
                f"Analyzing "
                f"{processed + 1}-"
                f"{processed + len(full_emails)}"
                f"/{len(pending)}..."
            )

            results = deep_analyze_batch(
                full_emails
            )

            for email, result in zip(
                full_emails,
                results
            ):

                save_deep_result(
                    conn,
                    email,
                    result
                )

                merchant = (
                    result.get("merchant")
                    or "Unknown"
                )

                event = (
                    result.get("event_type")
                    or "unknown"
                )

                risk = (
                    result.get("risk_level")
                    or "unknown"
                )

                amount = result.get(
                    "amount"
                )

                if amount is not None:

                    print(
                        f"    {merchant}: "
                        f"{event} | "
                        f"{amount} "
                        f"{result.get('currency') or ''}"
                        f" | risk={risk}"
                    )

                else:

                    print(
                        f"    {merchant}: "
                        f"{event} | "
                        f"risk={risk}"
                    )

            conn.commit()

            processed += len(
                full_emails
            )

        except KeyboardInterrupt:
            conn.commit()
            raise

        except Exception as error:

            print(
                f"Deep analysis error: "
                f"{error}"
            )


# ============================================================
# REPORT GENERATION
# ============================================================

RISK_SCORE = {
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4
}


def normalize_merchant(name):
    if not name:
        return "Unknown"

    name = name.strip()

    return re.sub(
        r"\s+",
        " ",
        name
    )


def get_all_analysis(conn):
    rows = conn.execute(
        """
        SELECT
            gmail_id,
            internal_date,
            date_iso,
            sender,
            subject,
            merchant,
            category,
            event_type,
            is_recurring,
            status,
            amount,
            currency,
            cadence,
            renewal_date,
            confidence,
            risk_level,
            risk_flags,
            summary,
            evidence
        FROM analyzed_emails
        ORDER BY internal_date ASC
        """
    ).fetchall()

    columns = [
        "gmail_id",
        "internal_date",
        "date_iso",
        "sender",
        "subject",
        "merchant",
        "category",
        "event_type",
        "is_recurring",
        "status",
        "amount",
        "currency",
        "cadence",
        "renewal_date",
        "confidence",
        "risk_level",
        "risk_flags",
        "summary",
        "evidence"
    ]

    return [
        dict(zip(columns, row))
        for row in rows
    ]


def export_timeline(rows):
    with open(
        TIMELINE_CSV,
        "w",
        newline="",
        encoding="utf-8"
    ) as f:

        writer = csv.writer(f)

        writer.writerow([
            "date",
            "merchant",
            "event_type",
            "status",
            "amount",
            "currency",
            "cadence",
            "risk",
            "subject",
            "summary",
            "evidence"
        ])

        for row in rows:

            writer.writerow([
                row["date_iso"],
                row["merchant"],
                row["event_type"],
                row["status"],
                row["amount"],
                row["currency"],
                row["cadence"],
                row["risk_level"],
                row["subject"],
                row["summary"],
                row["evidence"]
            ])


def create_merchant_report(rows):
    merchants = defaultdict(list)

    for row in rows:

        merchant = normalize_merchant(
            row["merchant"]
        )

        if merchant == "Unknown":
            continue

        merchants[merchant].append(row)

    report_rows = []

    for merchant, events in merchants.items():

        events.sort(
            key=lambda x:
            x["internal_date"] or 0
        )

        first = events[0]
        last = events[-1]

        amounts = [
            event["amount"]
            for event in events
            if event["amount"] is not None
        ]

        currencies = [
            event["currency"]
            for event in events
            if event["currency"]
        ]

        cadences = [
            event["cadence"]
            for event in events
            if event["cadence"]
            and event["cadence"] != "unknown"
        ]

        statuses = [
            event["status"]
            for event in events
            if event["status"]
            and event["status"] != "unknown"
        ]

        risk_levels = [
            event["risk_level"]
            for event in events
            if event["risk_level"]
        ]

        max_risk = "low"

        for risk in risk_levels:

            if (
                RISK_SCORE.get(risk, 0)
                >
                RISK_SCORE.get(max_risk, 0)
            ):
                max_risk = risk

        flags = []

        for event in events:

            try:

                parsed = json.loads(
                    event["risk_flags"]
                    or "[]"
                )

                if isinstance(parsed, list):
                    flags.extend(parsed)

            except Exception:
                pass

        unique_flags = []

        for flag in flags:

            if flag not in unique_flags:
                unique_flags.append(flag)

        latest_status = (
            statuses[-1]
            if statuses
            else "unknown"
        )

        common_cadence = (
            Counter(cadences)
            .most_common(1)[0][0]
            if cadences
            else "unknown"
        )

        currency = (
            Counter(currencies)
            .most_common(1)[0][0]
            if currencies
            else None
        )

        observed_amount = (
            max(amounts)
            if amounts
            else None
        )

        report_rows.append({
            "merchant": merchant,
            "first_seen": first["date_iso"],
            "last_seen": last["date_iso"],
            "email_events": len(events),
            "observed_amount": observed_amount,
            "currency": currency,
            "cadence": common_cadence,
            "latest_status": latest_status,
            "highest_risk": max_risk,
            "flags": "; ".join(
                unique_flags
            )
        })

    report_rows.sort(
        key=lambda x: (
            -RISK_SCORE.get(
                x["highest_risk"],
                0
            ),
            -x["email_events"]
        )
    )

    return report_rows


def export_merchant_report(report_rows):
    with open(
        REPORT_CSV,
        "w",
        newline="",
        encoding="utf-8"
    ) as f:

        fieldnames = [
            "merchant",
            "first_seen",
            "last_seen",
            "email_events",
            "observed_amount",
            "currency",
            "cadence",
            "latest_status",
            "highest_risk",
            "flags"
        ]

        writer = csv.DictWriter(
            f,
            fieldnames=fieldnames
        )

        writer.writeheader()
        writer.writerows(report_rows)


def print_report(report_rows):
    print()
    print("=" * 70)
    print("SUBSCRIPTION / RECURRING PAYMENT AUDIT")
    print("=" * 70)

    if not report_rows:

        print(
            "No relevant merchants found."
        )

        return

    for merchant in report_rows:

        print()
        print("-" * 70)

        print(
            merchant["merchant"]
        )

        print(
            f"Risk:         "
            f"{merchant['highest_risk'].upper()}"
        )

        print(
            f"Status:       "
            f"{merchant['latest_status']}"
        )

        if (
            merchant["observed_amount"]
            is not None
        ):

            print(
                f"Amount seen:  "
                f"{merchant['currency'] or ''} "
                f"{merchant['observed_amount']}"
            )

        print(
            f"Cadence:      "
            f"{merchant['cadence']}"
        )

        print(
            f"Email events: "
            f"{merchant['email_events']}"
        )

        print(
            f"First seen:   "
            f"{merchant['first_seen']}"
        )

        print(
            f"Last seen:    "
            f"{merchant['last_seen']}"
        )

        if merchant["flags"]:

            print(
                f"Flags:        "
                f"{merchant['flags']}"
            )


# ============================================================
# LIFE-CYCLE TIMELINE DISPLAY
# ============================================================

def print_high_risk_timelines(rows):
    grouped = defaultdict(list)

    for row in rows:

        merchant = normalize_merchant(
            row["merchant"]
        )

        if merchant != "Unknown":
            grouped[merchant].append(row)

    print()
    print("=" * 70)
    print("HIGH-RISK TIMELINES")
    print("=" * 70)

    found_any = False

    for merchant, events in grouped.items():

        highest = max(
            (
                RISK_SCORE.get(
                    event["risk_level"],
                    0
                )
                for event in events
            ),
            default=0
        )

        if highest < 3:
            continue

        found_any = True

        events.sort(
            key=lambda x:
            x["internal_date"] or 0
        )

        print()
        print(
            f"{merchant}"
        )

        print(
            "-" * len(merchant)
        )

        for event in events:

            date = (
                event["date_iso"][:10]
                if event["date_iso"]
                else "unknown"
            )

            event_type = (
                event["event_type"]
                or "unknown"
            )

            summary = (
                event["summary"]
                or event["subject"]
            )

            print(
                f"{date} | "
                f"{event_type:<20} | "
                f"{summary}"
            )

    if not found_any:

        print(
            "No high-risk timelines identified."
        )


# ============================================================
# MAIN
# ============================================================

def main():
    print()
    print("=" * 70)
    print("LOCAL EMAIL SUBSCRIPTION AUDITOR")
    print("=" * 70)

    print()
    print(
        "AI inference: LOCAL ONLY "
        "(MLX Stage 1, Ollama Stage 2)"
    )

    print(
        f"Fast model:   {FAST_MLX_MODEL}"
    )

    print(
        f"Deep model:   {DEEP_MODEL} "
        "(interesting emails only)"
    )

    # --------------------------------------------------------
    # Prerequisites
    # --------------------------------------------------------

    if not os.path.exists(
        CREDENTIALS_FILE
    ):

        print()
        print(
            "ERROR: credentials.json not found."
        )

        return

    # --------------------------------------------------------
    # Gmail + DB
    # --------------------------------------------------------

    print()
    print(
        "Connecting to Gmail..."
    )

    gmail = get_gmail_service()

    conn = init_db()

    # --------------------------------------------------------
    # Search
    # --------------------------------------------------------

    print()
    print(
        "Searching Gmail..."
    )

    message_ids = search_gmail(
        gmail
    )

    print(
        f"Gmail candidate emails: "
        f"{len(message_ids)}"
    )

    fast_model = None
    fast_tokenizer = None

    # --------------------------------------------------------
    # Stage 1
    # --------------------------------------------------------

    try:

        pending_screen = count_pending_screening(
            conn,
            message_ids
        )

        if pending_screen:

            print()
            print(
                f"Stage 1 pending previews: "
                f"{pending_screen}"
            )

            print(
                "Releasing Ollama weights from RAM "
                "before MLX Stage 1..."
            )
            unload_ollama()

            loaded = load_fast_mlx_model()

            if loaded:
                fast_model, fast_tokenizer = loaded

                run_screening(
                    gmail,
                    conn,
                    message_ids,
                    fast_model,
                    fast_tokenizer
                )

                unload_mlx_model(fast_model)
                fast_model = None
                fast_tokenizer = None

            else:
                print()
                print(
                    "Skipping Stage 1 because the "
                    "local MLX model could not be loaded."
                )

        else:
            print()
            print(
                "Stage 1 already complete."
            )

        # ----------------------------------------------------
        # Stage 2
        # ----------------------------------------------------

        pending_deep = count_pending_deep_analysis(
            conn
        )

        if pending_deep:

            print()
            print(
                f"Stage 2 needs {pending_deep} "
                "flagged email(s). Using local 8B..."
            )

            if not check_ollama():
                print()
                print(
                    "Skipping Stage 2. Stage 1 results "
                    "are saved locally."
                )
            else:
                run_deep_analysis(
                    gmail,
                    conn
                )

        else:
            print()
            print(
                "No flagged emails need deep analysis."
            )

    except KeyboardInterrupt:

        print()
        print()
        print(
            "Stopped."
        )

        print(
            "Progress has been saved."
        )

    finally:

        if fast_model is not None:
            unload_mlx_model(fast_model)
            fast_model = None
            fast_tokenizer = None

    # --------------------------------------------------------
    # Reports
    # --------------------------------------------------------

    rows = get_all_analysis(
        conn
    )

    export_timeline(
        rows
    )

    report_rows = create_merchant_report(
        rows
    )

    export_merchant_report(
        report_rows
    )

    print_report(
        report_rows
    )

    print_high_risk_timelines(
        rows
    )

    print()
    print("=" * 70)
    print("FILES CREATED")
    print("=" * 70)

    print(
        f"Database: {DB_PATH}"
    )

    print(
        f"Merchant report: {REPORT_CSV}"
    )

    print(
        f"Detailed timeline: {TIMELINE_CSV}"
    )

    print()
    print(
        "NOTE: amounts in the report are amounts "
        "observed in emails, not confirmed total spend."
    )

    print(
        "For actual spend, the next step is to "
        "reconcile this against your bank transaction CSV."
    )

    conn.close()


if __name__ == "__main__":
    main()