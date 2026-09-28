# yt-autosub: Multi-Account YouTube Studio Automation

Multi-account YouTube Studio browser automation using **`uv`**, **`camoufox`** (stealth anti-detect browser), persistent profile isolation, and session cookie capture/injection. Fully integrated with NixOS **`devenv`**.

---

## Architecture Overview

- **Account Isolation**: Each account runs its own persistent Camoufox instance storing browser cache, device tokens, and IndexedDB in `data/profiles/{account_id}/`.
- **Session Injection**: Exported Playwright `storage_state` and cookies are saved in `data/sessions/{account_id}.json` and automatically sanitized and re-injected on startup.
- **Stealth Camoufox**: Spoofs fingerprints, WebGL, canvas, navigator, and audio contexts to evade Google bot detection.
- **NixOS devenv**: Dynamically provides all required system libraries (GTK3, NSS, ALSA, Pango, X11, Mesa, FFmpeg) via `devenv shell`.

---

## Getting Started

### 1. Enter the Environment
Ensure your terminal is inside the NixOS development environment:
```bash
devenv shell
```

### 2. Authenticate an Account (`auth`)
Launch Camoufox in headful GUI mode for your account (e.g. `acc1`). A browser window will open for you to log into Google / YouTube Studio and complete any 2FA/verification:
```bash
yt-autosub auth acc1
```
*(Optional proxy support: `yt-autosub auth acc1 --proxy http://user:pass@host:port`)*

The script actively monitors the session and automatically captures cookies and profile state once you land in the YouTube Studio dashboard.

### 3. Verify Session Health (`test-auth`)
Verify whether an account's session remains active and logged in (runs headless by default):
```bash
yt-autosub test-auth acc1
```
Or test with browser visible:
```bash
yt-autosub test-auth acc1 --headful
```

### 4. Interactively Inspect YouTube Studio DOM (`inspect`)
Analyze YouTube Studio's live web structure, selectors, and upload modal using Playwright:
```bash
# Check key Studio selectors (Create button, navigation, dashboard elements) and save a screenshot:
yt-autosub inspect acc1 --action overview

# Trigger the Create -> Upload dialog and inspect modal elements:
yt-autosub inspect acc1 --action open-upload

# Save full page HTML to data/logs/studio_acc1.html:
yt-autosub inspect acc1 --action dump-html

# Run all inspections and capture screenshots & HTML:
yt-autosub inspect acc1 --action all
```

### 5. List All Accounts (`list-accounts`)
View all configured accounts, profile directories, and session statuses:
```bash
yt-autosub list-accounts
```

---

## Backend API & Remote Browser Streaming (Web SaaS)

Run the backend FastAPI server to enable interactive WebSocket streaming, REST management, and multi-tenancy for your web frontend:

```bash
yt-autosub serve --host 127.0.0.1 --port 8000
```

- **Interactive API Documentation**: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- **Detailed API & Frontend Guide**: See [docs/API.md](docs/API.md) for full request/response schemas, WebSocket streaming protocol, and ready-to-use Svelte & Next.js/React component code.

