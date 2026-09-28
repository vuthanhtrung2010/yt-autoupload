# YouTube Automation API Documentation

This document describes the REST endpoints, WebSocket streaming protocol, and frontend integration examples (Svelte & Next.js/React) for the **yt-autosub** backend service.

---

## 1. Overview & Setup

- **Base URL**: `http://127.0.0.1:8000` (by default)
- **Interactive Swagger Docs**: `http://127.0.0.1:8000/docs`
- **ReDoc**: `http://127.0.0.1:8000/redoc`

### Starting the Server
```bash
devenv shell
yt-autosub serve --host 127.0.0.1 --port 8000
```

### Authentication & Security
If `YT_AUTOSUB_API_KEY` is configured on the backend:
- Pass the header in all REST calls: `X-API-Key: <your-key>`
- Pass the token in WebSocket queries: `ws://127.0.0.1:8000/api/v1/auth/session/{id}/ws?token=<your-key>`

---

## 2. Multi-Tenancy Resolution

To allow multiple users to use common account aliases like `acc1` or `channel_1` without collision, each channel is uniquely scoped:
- **`user_id`**: The unique identifier of the user in your database (e.g. `usr_89f02a`).
- **`account_alias`**: The friendly channel label chosen by the user (e.g. `acc1`).
- **`channel_id`**: Automatically generated composite ID: `{user_id}_{account_alias}`.

Storage on the server is physically isolated:
- Profile: `data/profiles/{user_id}/{account_alias}/`
- Session: `data/sessions/{user_id}/{account_alias}.json`

---

## 3. REST API Endpoints

### 3.1 Health Check
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

### 3.2 Create Channel Authentication Session
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
    "server": "http://user:pass@residential-proxy.net:8080",
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

### 3.3 Get Session Status
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

Status values:
- `"starting"`: Browser initializing and navigating to Google/YouTube.
- `"active"`: Browser ready; user can interact and log in.
- `"authenticated"`: User successfully completed login & 2FA; cookies exported.
- `"closed"`: Browser session terminated.
- `"error"`: An error occurred during session launch.

---

### 3.4 Terminate / Close Session
Manually close the remote browser instance if the user cancels or closes the modal.

```http
DELETE /api/v1/auth/session/{session_id}
```

**Response (200 OK):**
```json
{
  "message": "Session closed"
}
```

---

### 3.5 List User's Connected Channels
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

### 3.6 Trigger Video Upload
Uploads a video to YouTube using the authenticated channel's profile and session.

```http
POST /api/v1/upload
Content-Type: application/json
```

**Request Body:**
```json
{
  "user_id": "usr_10293",
  "account_alias": "tech_vlog",
  "video_path": "/path/to/server_video.mp4",
  "title": "My Awesome Video Title",
  "description": "Video description here...\nLinks & info.",
  "tags": ["tech", "vlog", "coding"],
  "thumbnail_path": "/path/to/custom_thumb.jpg",
  "visibility": "PRIVATE",
  "proxy": {
    "server": "http://proxy.host:8080"
  }
}
```

*Visibility options:* `"PRIVATE"`, `"UNLISTED"`, or `"PUBLIC"`.

**Response (200 OK):**
```json
{
  "account_id": "usr_10293_tech_vlog",
  "video_path": "/path/to/server_video.mp4",
  "title": "My Awesome Video Title",
  "success": true,
  "video_url": "https://youtu.be/w6ZVpSnFiKo",
  "error_message": null
}
```

---

## 4. WebSocket Browser Streaming Protocol

- **URL**: `ws://{host}:{port}/api/v1/auth/session/{session_id}/ws`
- *(Optional token)*: `ws://{host}:{port}/api/v1/auth/session/{session_id}/ws?token={API_KEY}`

### 4.1 Server $\to$ Client Messages

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

### 4.2 Client $\to$ Server Messages

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

## 5. Frontend Integration Examples

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
