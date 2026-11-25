# YouTube Automation API Documentation

This document describes the REST endpoints, background queue system, WebSocket streaming protocol, TypeScript interfaces, and frontend integration examples (Svelte & Next.js/React) for the **yt-autosub** backend service.

---

## 1. Overview & Setup

- **Base URL**: `http://127.0.0.1:8000` (by default)
- **Interactive Swagger UI**: `http://127.0.0.1:8000/docs`
- **ReDoc UI**: `http://127.0.0.1:8000/redoc`

### Starting the Server
```bash
devenv shell
yt-autosub serve --host 127.0.0.1 --port 8000 --reload
```

### Authentication & Security
If `YT_AUTOSUB_API_KEY` is configured in the environment:
- **REST Endpoints**: Pass `X-API-Key: <your-key>` header (or `?token=<your-key>` query parameter).
- **WebSocket Streaming**: Pass `?token=<your-key>` in the WebSocket connection URL:
  `ws://127.0.0.1:8000/api/v1/auth/session/{id}/ws?token=<your-key>`

---

## 2. Multi-Tenancy & Path Resolution

To allow multiple SaaS users to connect accounts without collisions (e.g. multiple users having an account named `acc1`):
- **`user_id`**: The unique identifier of the user in your database (e.g. `usr_89f02a`, `6AnkfOE3XqE87i8OxBfYYTywysLahTIi`).
- **`account_alias`**: The friendly channel label chosen by the user (e.g. `acc1`, `tech_channel`).
- **`channel_id`**: Automatically generated composite ID: `{user_id}_{account_alias}`.

### Storage Layout on Server
- **Persistent Browser Profiles**: `data/profiles/{user_id}/{account_alias}/`
- **Exported Cookie & Storage Sessions**: `data/sessions/{user_id}/{account_alias}.json`

### Automatic Fallback Behavior
The backend includes intelligent fallback resolution:
1. It looks for user-scoped session data first: `data/sessions/{user_id}/{account_alias}.json`.
2. If absent, it checks if a root session exists: `data/sessions/{account_alias}.json` (e.g., created during CLI debugging `yt-autosub auth acc1`).
3. If an upload job passes a composite `account_id` (e.g. `6AnkfOE3XqE87i8OxBfYYTywysLahTIi_acc1`), the backend automatically decomposes it to locate the exact session.

---

## 3. TypeScript Interfaces for Frontend

You can copy-paste these types directly into your frontend codebase (`src/types/yt-autosub.ts`):

```typescript
export type Visibility = "PRIVATE" | "UNLISTED" | "PUBLIC";

export interface ProxyConfig {
  server: string; // e.g. "http://proxy.host:8080" or "socks5://ip:port"
  username?: string;
  password?: string;
}

// --- Auth & Session ---
export interface CreateAuthSessionRequest {
  user_id: string;
  account_alias: string;
  proxy?: ProxyConfig | null;
}

export interface AuthSessionResponse {
  session_id: string;
  channel_id: string;
  user_id: string;
  account_alias: string;
  status: "starting" | "active" | "authenticated" | "closed" | "error";
  ws_endpoint: string;
  embed_url: string;
}

export interface AuthSessionStatusResponse {
  session_id: string;
  channel_id: string;
  status: "starting" | "active" | "authenticated" | "closed" | "error";
  is_authenticated: boolean;
  channel_url?: string | null;
  error?: string | null;
}

// --- Channels ---
export interface ChannelItem {
  user_id: string;
  account_alias: string;
  channel_id: string;
  has_session: boolean;
  session_file: string;
}

export interface ListChannelsResponse {
  user_id: string;
  channels: ChannelItem[];
}

// --- Direct Upload ---
export interface DirectUploadRequest {
  user_id: string;
  account_alias: string;
  video_path: string;
  title: string;
  description?: string;
  tags?: string[];
  thumbnail_path?: string | null;
  visibility?: Visibility;
  scheduled_publish_time?: string | null; // e.g. "2026-10-25T18:00:00" or "2026-10-25 18:00"
  set_as_premiere?: boolean; // Set as Premiere (Công chiếu)
  proxy?: ProxyConfig | null;
}

export interface UploadResult {
  account_id: string;
  video_path: string;
  title: string;
  success: boolean;
  video_url?: string | null;
  error_message?: string | null;
  verification_required?: boolean;
  verification_session_id?: string | null;
  verification_url?: string | null;
}

// --- Queue Upload ---
export interface QueueUploadRequest {
  job_id: string;
  user_id: string;
  account_aliases: string[]; // e.g. ["acc1", "acc2"]
  video_path: string;
  title: string;
  description?: string;
  tags?: string[];
  thumbnail_path?: string | null;
  visibility?: Visibility;
  scheduled_publish_time?: string | null; // e.g. "2026-10-25T18:00:00" or "2026-10-25 18:00"
  set_as_premiere?: boolean; // Set as Premiere (Công chiếu)
  proxy?: ProxyConfig | null;
}

export interface QueueUploadResponse {
  job_id: string;
  status: "queued" | "processing" | "completed" | "partial_failed" | "failed" | "needs_verification";
  total_channels: number;
}

export interface QueueChannelTask {
  account_alias: string;
  status: "queued" | "uploading" | "completed" | "failed" | "needs_verification";
  video_url?: string | null;
  error_message?: string | null;
  verification_session_id?: string | null;
  verification_url?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
}

export interface QueueJobStatus {
  job_id: string;
  user_id: string;
  status: "queued" | "processing" | "completed" | "partial_failed" | "failed" | "needs_verification";
  title: string;
  visibility?: Visibility;
  scheduled_publish_time?: string | null;
  set_as_premiere?: boolean;
  created_at: string;
  updated_at: string;
  tasks: QueueChannelTask[];
}
```

---

## 4. REST API Reference

### 4.1 Health Check
Verify backend server status.

```http
GET /api/v1/health
```

**Response (200 OK):**
```json
{
  "status": "ok",
  "service": "yt-autosub-api"
}
```

---

### 4.2 Start Browser Auth Session
Spins up an isolated Camoufox instance in the background navigating to YouTube Studio. Returns the streaming URL.

```http
POST /api/v1/auth/session
Content-Type: application/json
```

**Request Body:**
```json
{
  "user_id": "usr_10293",
  "account_alias": "tech_vlog",
  "proxy": {
    "server": "http://residential-proxy.net:8080",
    "username": "optional_user",
    "password": "optional_password"
  }
}
```
*(The `proxy` field is optional. If omitted, connects directly.)*

**Response (200 OK):**
```json
{
  "session_id": "sess_4a8b9c1d2e3f",
  "channel_id": "usr_10293_tech_vlog",
  "user_id": "usr_10293",
  "account_alias": "tech_vlog",
  "status": "starting",
  "ws_endpoint": "/api/v1/auth/session/sess_4a8b9c1d2e3f/ws",
  "embed_url": "/stream/sess_4a8b9c1d2e3f"
}
```

---

### 4.3 Get Session Status
Check if a user has completed login or if the session is still active.

```http
GET /api/v1/auth/session/{session_id}
```

**Response (200 OK):**
```json
{
  "session_id": "sess_4a8b9c1d2e3f",
  "channel_id": "usr_10293_tech_vlog",
  "status": "authenticated",
  "is_authenticated": true,
  "channel_url": "https://studio.youtube.com/channel/UCIAbGMO6wKo_29eC33V9PPg",
  "error": null
}
```

*Status values:*
- `"starting"`: Browser initializing and navigating to Google/YouTube.
- `"active"`: Browser ready; user can interact and log in.
- `"authenticated"`: User successfully completed login & 2FA; cookies exported.
- `"closed"`: Browser session terminated.
- `"error"`: An error occurred during session launch.

---

### 4.4 Terminate / Close Session
Manually close the remote browser instance if the user cancels or closes the modal.

```http
DELETE /api/v1/auth/session/{session_id}
```

**Response (200 OK):**
```json
{
  "message": "Session closed and profile purged"
}
```

---

### 4.5 List User's Connected Channels
Returns all channels that have active sessions for a given user.

```http
GET /api/v1/channels?user_id=usr_10293
```

**Response (200 OK):**
```json
{
  "user_id": "usr_10293",
  "channels": [
    {
      "user_id": "usr_10293",
      "account_alias": "tech_vlog",
      "channel_id": "usr_10293_tech_vlog",
      "has_session": true,
      "session_file": "/app/data/sessions/usr_10293/tech_vlog.json"
    }
  ]
}
```

---

### 4.6 Delete Connected Channel
Purges the persistent profile directory and saved session cookies for a user's channel.

```http
DELETE /api/v1/channels/{account_alias}?user_id=usr_10293
```

**Response (200 OK):**
```json
{
  "message": "Channel data for tech_vlog deleted"
}
```

---

### 4.7 Direct Synchronous Upload
Uploads a video to YouTube using the authenticated channel's profile and session. Blocks until upload finishes or fails.

```http
POST /api/v1/upload
Content-Type: application/json
```

**Request Body:**
```json
{
  "user_id": "usr_10293",
  "account_alias": "tech_vlog",
  "video_path": "data/test_video.mp4",
  "title": "My Awesome Video Title",
  "description": "Video description here...\nLinks & info.",
  "tags": ["tech", "vlog", "coding"],
  "thumbnail_path": null,
  "visibility": "PRIVATE",
  "scheduled_publish_time": "2026-10-25 18:00",
  "set_as_premiere": false,
  "proxy": null
}
```

*Field details:*
- `user_id` (string, required): The tenant / user ID.
- `account_alias` (string, required): The channel alias (must have an active session).
- `video_path` (string, required): Absolute or project-relative path to video file on server.
- `title` (string, required): Up to 100 characters.
- `description` (string, optional): Default `""`.
- `tags` (string array, optional): Default `[]`.
- `thumbnail_path` (string, optional): Path to custom thumbnail on server.
- `visibility` (string, optional): `"PRIVATE"`, `"UNLISTED"`, or `"PUBLIC"` (default: `"PRIVATE"`). Ignored if `scheduled_publish_time` is set.
- `scheduled_publish_time` (string, optional): ISO 8601 or datetime string (e.g. `"2026-10-25 18:00"`) to schedule native release in YouTube Studio.
- `set_as_premiere` (boolean, optional): Set to `true` to release the scheduled video as a Premiere (Công chiếu). Default `false`.
- `proxy` (object, optional): Proxy override for upload.

**Response (200 OK):**
```json
{
  "account_id": "usr_10293_tech_vlog",
  "video_path": "/home/devtrung/Projects/yt-autosub/data/test_video.mp4",
  "title": "My Awesome Video Title",
  "success": true,
  "video_url": "https://youtu.be/Yv6fTZyh8BQ",
  "error_message": null
}
```

---

### 4.8 Background Queue Upload (Async Batch)
Enqueues an automated upload job that runs asynchronously across one or multiple accounts with built-in concurrency management.

```http
POST /api/v1/queue/upload
Content-Type: application/json
```

**Request Body:**
```json
{
  "job_id": "job_2026_09_001",
  "user_id": "usr_10293",
  "account_aliases": ["tech_vlog", "gaming_channel"],
  "video_path": "data/test_video.mp4",
  "title": "Synced Multi-Channel Broadcast",
  "description": "Broadcast description",
  "tags": ["broadcast", "sync"],
  "thumbnail_path": null,
  "visibility": "PRIVATE",
  "scheduled_publish_time": "2026-10-25 18:00",
  "set_as_premiere": false,
  "proxy": null
}
```

**Response (202 Accepted):**
```json
{
  "job_id": "job_2026_09_001",
  "status": "queued",
  "total_channels": 2
}
```

---

### 4.9 Get Queue Job Status
Poll the progress of an asynchronous upload job across its assigned channels.

```http
GET /api/v1/queue/jobs/{job_id}
```

**Response (200 OK):**
```json
{
  "job_id": "job_2026_09_001",
  "user_id": "usr_10293",
  "status": "processing",
  "title": "Synced Multi-Channel Broadcast",
  "visibility": "PRIVATE",
  "scheduled_publish_time": "2026-10-25 18:00",
  "set_as_premiere": false,
  "created_at": "2026-09-29T11:00:23.880Z",
  "updated_at": "2026-09-29T11:01:05.120Z",
  "tasks": [
    {
      "account_alias": "tech_vlog",
      "status": "completed",
      "video_url": "https://youtu.be/w6ZVpSnFiKo",
      "error_message": null,
      "started_at": "2026-09-29T11:00:24.000Z",
      "completed_at": "2026-09-29T11:00:58.000Z"
    },
    {
      "account_alias": "gaming_channel",
      "status": "uploading",
      "video_url": null,
      "error_message": null,
      "started_at": "2026-09-29T11:00:59.000Z",
      "completed_at": null
    }
  ]
}
```

*Job Status Values:* `"queued"`, `"processing"`, `"completed"`, `"partial_failed"`, `"failed"`.  
*Task Status Values:* `"queued"`, `"uploading"`, `"completed"`, `"failed"`.

---

### 4.10 List Queue Jobs
List recent queue jobs, optionally filtered by user ID.

```http
GET /api/v1/queue/jobs?user_id=usr_10293
```

**Response (200 OK):**
```json
{
  "jobs": [
    {
      "job_id": "job_2026_09_001",
      "user_id": "usr_10293",
      "status": "completed",
      "title": "Synced Multi-Channel Broadcast",
      "created_at": "2026-09-29T11:00:23.880Z",
      "updated_at": "2026-09-29T11:01:45.000Z",
      "tasks": [...]
    }
  ]
}
```

---

## 5. WebSocket Browser Streaming Protocol

- **URL**: `ws://{host}:{port}/api/v1/auth/session/{session_id}/ws`
- *(Optional token)*: `ws://{host}:{port}/api/v1/auth/session/{session_id}/ws?token={API_KEY}`

### 5.1 Server $\to$ Client Messages

#### Video Frame (Broadcast ~10fps + immediately on action)
```json
{
  "type": "frame",
  "data": "data:image/jpeg;base64,/9j/4AAQSkZJRg...",
  "status": "active",
  "url": "https://accounts.google.com/signin/v2/identifier"
}
```

#### Authentication Success Event
Emitted immediately when YouTube Studio dashboard is detected:
```json
{
  "type": "authenticated",
  "user_id": "usr_10293",
  "account_alias": "tech_vlog",
  "channel_id": "usr_10293_tech_vlog",
  "channel_url": "https://studio.youtube.com/channel/UCIAbGMO6wKo_29eC33V9PPg"
}
```

---

### 5.2 Client $\to$ Server Messages

Send coordinates normalized from `0.0` to `1.0` (independent of canvas display size).

#### Mouse Click
```json
{
  "type": "mouse",
  "action": "click",
  "x": 0.452,
  "y": 0.381,
  "button": "left"
}
```
*Actions:* `"click"`, `"move"`, `"down"`, `"up"`.  
*Buttons:* `"left"`, `"right"`, `"middle"`.

#### Typing Characters
```json
{
  "type": "keyboard",
  "action": "type",
  "text": "my_email@gmail.com"
}
```

#### Special Key Press (Enter, Backspace, Tab, Arrows)
```json
{
  "type": "keyboard",
  "action": "press",
  "key": "Enter"
}
```

#### Mouse Scroll / Wheel
```json
{
  "type": "scroll",
  "deltaX": 0,
  "deltaY": 120
}
```

---

## 6. Frontend Integration Examples

### Option A: Direct `<iframe>` Embed (Zero frontend code)
If you don't want to build a custom canvas client, embed the built-in streamer directly in your modal:

```html
<iframe 
  src="http://127.0.0.1:8000/stream/{session_id}" 
  width="100%" 
  height="720px" 
  style="border: none; border-radius: 8px;">
</iframe>
```

---

### Option B: Svelte / SvelteKit Component

Save as `src/lib/components/RemoteBrowser.svelte`:

```svelte
<script lang="ts">
  import { onMount, onDestroy, createEventDispatcher } from 'svelte';

  export let sessionId: string;
  export let serverUrl: string = "http://127.0.0.1:8000";

  const dispatch = createEventDispatcher();
  let canvas: HTMLCanvasElement;
  let ctx: CanvasRenderingContext2D | null = null;
  let ws: WebSocket;
  let statusText = "Connecting...";
  let isAuthenticated = false;

  onMount(() => {
    ctx = canvas.getContext('2d');
    const wsProto = serverUrl.startsWith('https') ? 'wss:' : 'ws:';
    const host = serverUrl.replace(/^https?:\/\//, '');
    ws = new WebSocket(`${wsProto}//${host}/api/v1/auth/session/${sessionId}/ws`);

    const img = new Image();
    img.onload = () => {
      if (ctx) ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    };

    ws.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      if (msg.type === 'frame') {
        img.src = msg.data;
        statusText = "Active (Sign in to Google)";
      } else if (msg.type === 'authenticated') {
        isAuthenticated = true;
        statusText = "Channel Connected!";
        dispatch('authenticated', msg);
      }
    };
  });

  onDestroy(() => {
    if (ws) ws.close();
  });

  function getCoords(e: MouseEvent) {
    const rect = canvas.getBoundingClientRect();
    return {
      x: (e.clientX - rect.left) / rect.width,
      y: (e.clientY - rect.top) / rect.height
    };
  }

  function handleClick(e: MouseEvent) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const { x, y } = getCoords(e);
    ws.send(JSON.stringify({ type: 'mouse', action: 'click', x, y, button: 'left' }));
  }

  function handleKeyDown(e: KeyboardEvent) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    if (['Tab', 'Backspace', 'ArrowUp', 'ArrowDown'].includes(e.key)) {
      e.preventDefault();
    }
    if (e.key.length === 1 && !e.ctrlKey && !e.altKey && !e.metaKey) {
      ws.send(JSON.stringify({ type: 'keyboard', action: 'type', text: e.key }));
    } else {
      ws.send(JSON.stringify({ type: 'keyboard', action: 'press', key: e.key }));
    }
  }
</script>

<div class="browser-wrapper">
  <div class="status-bar">Status: {statusText}</div>
  <canvas 
    bind:this={canvas} 
    width="1280" 
    height="720" 
    tabindex="0"
    on:click={handleClick}
    on:keydown={handleKeyDown}>
  </canvas>
</div>

<style>
  .browser-wrapper { position: relative; width: 100%; max-width: 1280px; }
  .status-bar { padding: 8px; font-weight: bold; background: #222; color: #fff; }
  canvas { width: 100%; height: auto; display: block; border: 1px solid #444; }
</style>
```

---

### Option C: React / Next.js Component

Save as `components/RemoteBrowser.tsx`:

```tsx
"use client";

import React, { useEffect, useRef, useState } from "react";

interface RemoteBrowserProps {
  sessionId: string;
  serverUrl?: string;
  onAuthenticated?: (data: any) => void;
}

export const RemoteBrowser: React.FC<RemoteBrowserProps> = ({
  sessionId,
  serverUrl = "http://127.0.0.1:8000",
  onAuthenticated,
}) => {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const [status, setStatus] = useState("Connecting...");

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");

    const wsProto = serverUrl.startsWith("https") ? "wss:" : "ws:";
    const host = serverUrl.replace(/^https?:\/\//, "");
    const ws = new WebSocket(`${wsProto}//${host}/api/v1/auth/session/${sessionId}/ws`);
    wsRef.current = ws;

    const img = new Image();
    img.onload = () => {
      if (ctx) ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    };

    ws.onmessage = (event) => {
      const msg = JSON.parse(event.data);
      if (msg.type === "frame") {
        img.src = msg.data;
        setStatus("Active (Interact to Sign In)");
      } else if (msg.type === "authenticated") {
        setStatus("✓ Channel Connected!");
        if (onAuthenticated) onAuthenticated(msg);
      }
    };

    return () => {
      ws.close();
    };
  }, [sessionId, serverUrl, onAuthenticated]);

  const handleClick = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    const ws = wsRef.current;
    if (!canvas || !ws || ws.readyState !== WebSocket.OPEN) return;

    const rect = canvas.getBoundingClientRect();
    const x = (e.clientX - rect.left) / rect.width;
    const y = (e.clientY - rect.top) / rect.height;

    ws.send(JSON.stringify({ type: "mouse", action: "click", x, y, button: "left" }));
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLCanvasElement>) => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;

    if (["Tab", "Backspace", "ArrowUp", "ArrowDown"].includes(e.key)) {
      e.preventDefault();
    }

    if (e.key.length === 1 && !e.ctrlKey && !e.altKey && !e.metaKey) {
      ws.send(JSON.stringify({ type: "keyboard", action: "type", text: e.key }));
    } else {
      ws.send(JSON.stringify({ type: "keyboard", action: "press", key: e.key }));
    }
  };

  return (
    <div style={{ maxWidth: 1280, width: "100%", margin: "0 auto" }}>
      <div style={{ padding: 8, background: "#18181b", color: "#fafafa" }}>
        Status: <strong>{status}</strong>
      </div>
      <canvas
        ref={canvasRef}
        width={1280}
        height={720}
        tabIndex={0}
        onClick={handleClick}
        onKeyDown={handleKeyDown}
        style={{ width: "100%", height: "auto", display: "block", border: "1px solid #333" }}
      />
    </div>
  );
};
```
