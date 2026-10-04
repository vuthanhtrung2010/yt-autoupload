# yt-autosub: Multi-Account YouTube Studio Automation

Multi-account YouTube Studio browser automation using **`uv`**, **`camoufox`** (stealth anti-detect browser), persistent profile isolation, and session cookie capture/injection. Fully integrated with NixOS **`devenv`** and a multi-tenant FastAPI backend with WebSocket remote browser streaming.

---

## Architecture Overview

- **Account & Multi-Tenancy Isolation**: Each account runs its own persistent Camoufox instance storing browser cache, device tokens, and IndexedDB in `data/profiles/{user_id}/{account_alias}/` (or `data/profiles/{account_id}/`).
- **Session Injection**: Exported Playwright `storage_state` and cookies are saved in `data/sessions/{user_id}/{account_alias}.json` and automatically sanitized and re-injected on startup.
- **Stealth Camoufox**: Spoofs fingerprints, WebGL, canvas, navigator, and audio contexts to evade Google bot detection.
- **NixOS devenv**: Dynamically provides all required system libraries (GTK3, NSS, ALSA, Pango, X11, Mesa, FFmpeg) and sets `LD_LIBRARY_PATH` via `devenv shell`.

---

## Setup Guide

Follow these steps when setting up the project on a new machine or environment.

### 1. Enter the Environment
On NixOS, enter the `devenv` shell to load all required native libraries (`gtk3`, `nss`, `alsa-lib`, `x11`, `mesa`, `ffmpeg`, etc.) into your environment:

```bash
devenv shell
```

> [!NOTE]
> `devenv shell` automatically configures `LD_LIBRARY_PATH` so Camoufox and Firefox can load `.so` dependencies without ELF linking errors.

### 2. Install Python Dependencies
Synchronize the virtual environment and install all project packages using `uv`:

```bash
uv sync
```

### 3. Fetch Camoufox Stealth Browser Binaries
Camoufox relies on a specialized, fingerprint-hardened Firefox engine. **You must download the browser binaries once before running any browser automation:**

```bash
camoufox fetch
```

Or via `uv`:
```bash
uv run camoufox fetch
```

*(This downloads the pre-built stealth browser into `~/.cache/camoufox/browsers/`).*

### 4. Verify Installation
Verify that the CLI command is available and working:

```bash
yt-autosub --help
yt-autosub list-accounts
```

---

## CLI Usage & Workflow

### 1. Authenticate an Account (`auth`)
Launch Camoufox in headful GUI mode for an account (e.g. `acc1`). A browser window will open for you to complete Google sign-in and any 2FA/verification:

```bash
yt-autosub auth acc1
```

*(Optional proxy support: `yt-autosub auth acc1 --proxy http://user:pass@host:port`)*

The script actively monitors the session and automatically captures cookies and persistent profile state once you land in the YouTube Studio dashboard.

### 2. Verify Session Health (`test-auth`)
Check whether an account's session remains active and logged in (runs headless by default):

```bash
yt-autosub test-auth acc1
```

Or run with the browser window visible:
```bash
yt-autosub test-auth acc1 --headful
```

### 3. Upload a Single Video via CLI (`upload-single`)
Directly upload a video file for a channel from the terminal:

```bash
yt-autosub upload-single acc1 \
  --video data/test_video.mp4 \
  --title "My Video Title" \
  --description "Video description here" \
  --tags "tutorial,coding,automation" \
  --visibility PRIVATE
```

*(Visibility options: `PRIVATE`, `UNLISTED`, or `PUBLIC`).*

#### Schedule a Release Date & Time (Native YouTube Studio Schedule)
To set a specific future release date and time natively in YouTube Studio (and optionally premiere it):

```bash
# Schedule release for a specific date and time:
yt-autosub upload-single acc1 \
  --video data/test_video.mp4 \
  --title "Scheduled Release" \
  --schedule "2026-10-25 18:00"

# Schedule release as a Premiere (Công chiếu):
yt-autosub upload-single acc1 \
  --video data/test_video.mp4 \
  --title "Premiere Release" \
  --schedule "2026-10-25 18:00" \
  --premiere
```

### 4. Batch Uploads via Manifest (`upload`)
Run scheduled or concurrent uploads across multiple accounts using a manifest YAML/JSON:

```bash
yt-autosub upload --manifest manifest.example.yaml --concurrency 2
```

### 5. Inspect YouTube Studio DOM (`inspect`)
Analyze YouTube Studio's live web structure and upload modal selectors using Playwright:

```bash
# Check key Studio selectors (Create button, navigation, dashboard elements):
yt-autosub inspect acc1 --action overview

# Trigger the Create -> Upload dialog and inspect modal elements:
yt-autosub inspect acc1 --action open-upload

# Save full page HTML to data/logs/studio_acc1.html:
yt-autosub inspect acc1 --action dump-html

# Run all inspections and capture screenshots & HTML:
yt-autosub inspect acc1 --action all
```

### 6. List All Configured Accounts (`list-accounts`)
View all configured accounts (both flat root accounts and multi-tenant accounts), profile directories, and session statuses:

```bash
yt-autosub list-accounts
```

---

## Backend API & Remote Browser Streaming (Web SaaS)

To run the backend server for your web application (Svelte, Next.js, React) with remote WebSocket browser streaming and asynchronous queue workers:

```bash
yt-autosub serve --host 127.0.0.1 --port 8000 --reload
```

- **Interactive Swagger UI**: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- **Built-in Quick Stream Viewer**: [http://127.0.0.1:8000](http://127.0.0.1:8000)
- **Complete API Reference & Frontend Code Samples**: See [docs/API.md](docs/API.md) for full request/response schemas, TypeScript types, and Svelte/React component examples.
