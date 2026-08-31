# Gmail OAuth setup

This tool talks to Gmail with a **Desktop** OAuth client that **you** create. There is no shared login. Google treats `gmail.readonly` as a restricted scope, so a public app with one client ID would need a long verification process. For a personal clone, you stay in **Testing** and add yourself as a test user.

You only do this once per Google Cloud project. The JSON never leaves your machine.

## What you will have at the end

A file named `credentials.json` in the app data directory:

- macOS / Linux: `~/.gmail-audit/credentials.json`
- Windows: `%APPDATA%\gmail-audit\credentials.json`

`gmail-audit setup` copies a Desktop JSON from **Downloads** (`client_secret_….json`) into that folder. You do not have to rename it yourself.

```json
{
  "installed": {
    "client_id": "...apps.googleusercontent.com",
    "client_secret": "...",
    "redirect_uris": ["http://localhost"]
  }
}
```

See `credentials.example.json`. Do not commit the real file.

## 1. Create or pick a Google Cloud project

1. Open [Google Cloud Console](https://console.cloud.google.com/).
2. Sign in with the Google account whose Gmail you want to audit.
3. Project picker (top bar) → **New project**.
4. Name it something like `gmail-audit-local`. Create it, then select it.

## 2. Enable the Gmail API

1. Open [Gmail API](https://console.cloud.google.com/apis/library/gmail.googleapis.com).
2. Confirm the project in the top bar is `gmail-audit-local`.
3. Click **Enable**.

## 3. Configure the OAuth consent screen

Google has been moving this under **Google Auth Platform**. Use whichever of these you see.

### Google Auth Platform (current)

1. Open [Google Auth Platform](https://console.cloud.google.com/auth/overview).
2. If prompted, click **Get started**.
3. **App information**
   - App name: `Gmail Audit (local)`
   - User support email: your address
4. **Audience**: **External** (required for personal `@gmail.com` accounts). Internal is only for Google Workspace.
5. **Contact information**: your email.
6. Finish / create.

Then:

1. **Audience** (left nav) → **Add users** → add the same Gmail address you will sign in with. Testing apps only work for test users.
2. **Data Access** → **Add or remove scopes** → filter `Gmail API` → check
   `https://www.googleapis.com/auth/gmail.readonly`
   → Update → Save.
3. Leave **Publishing status** as **Testing**. Do not click Publish app.

### Classic “OAuth consent screen”

1. [APIs & Services → OAuth consent screen](https://console.cloud.google.com/apis/credentials/consent)
2. User type **External** → Create.
3. App name `Gmail Audit (local)`, your email as support/developer contact.
4. Scopes → Add → `gmail.readonly`.
5. Test users → Add your Gmail address.
6. Save. Stay in Testing.

## 4. Create a Desktop OAuth client

1. Open [Clients](https://console.cloud.google.com/auth/clients) (or **APIs & Services → Credentials** → **Create credentials** → **OAuth client ID**).
2. Application type: **Desktop app**.
3. Name: `gmail-audit-desktop`.
4. Create.
5. **Download JSON**.
6. Leave it in Downloads. Re-run (or press Enter in) `gmail-audit setup`.

If you prefer to place it yourself, save it as `credentials.json` in `~/.gmail-audit/` (Windows: `%APPDATA%\gmail-audit\`).

## 5. Sign in once

```bash
gmail-audit run
```

A browser window opens. Sign in with the **test user** you added.

### “Google hasn’t verified this app”

Expected. This is your unpublished Desktop client, not a store app.

1. Click **Advanced**.
2. Click **Go to Gmail Audit (local) (unsafe)**.
3. Allow read-only Gmail access.

That writes `token.json` next to `credentials.json` in the data directory (readable only by your user account).

**Note:** because the app stays in Testing, Google expires this token after **7 days**. `gmail-audit run` detects that and reopens the browser sign-in automatically — you do not need to delete anything by hand.

## If it fails

| Symptom | Fix |
|---|---|
| `credentials.json not found` | Leave the Desktop JSON in Downloads and re-run `gmail-audit setup`, or copy it to `~/.gmail-audit/credentials.json`. |
| Access blocked / 403 | You are not a test user, or you used a different Google account than the one you added. Delete `token.json` and retry. |
| `invalid_grant` / token expired or revoked | Normal after 7 days in Testing. `gmail-audit run` re-opens the sign-in automatically; if it doesn't, delete `token.json` and retry. |
| Redirect / localhost error | Client type must be **Desktop app**, not Web. |
| Scope missing | Add `gmail.readonly` under Data Access, then delete `token.json` and sign in again. |
| “App is in production” but login still blocked | Personal Gmail tools should stay in **Testing**. Publishing triggers Google verification. |

Read-only only: the scope cannot send, delete, or change mail.
