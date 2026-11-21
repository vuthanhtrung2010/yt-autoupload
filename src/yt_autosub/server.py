import asyncio
from pathlib import Path
from typing import Optional, List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Header, Query, Depends, status
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from loguru import logger

from yt_autosub.config import (
    API_SECRET_KEY,
    get_channel_id,
    get_user_profile_dir,
    get_user_session_path,
    PROFILES_DIR,
    SESSIONS_DIR,
    LOGS_DIR,
    ProxyConfig,
)
from yt_autosub.models import Visibility, UploadResult, VideoUploadTask
from yt_autosub.uploader import upload_video
from yt_autosub.auth import test_session
from yt_autosub.stream_manager import session_manager, VIEWPORT_WIDTH, VIEWPORT_HEIGHT
from yt_autosub.queue_manager import UploadQueueManager

queue_manager = UploadQueueManager.get_instance()

app = FastAPI(
    title="YouTube Multi-Account Automation API",
    description="Backend API with WebSocket remote browser streaming, multi-tenancy, and video upload automation.",
    version="0.2.0",
)

@app.on_event("startup")
async def on_startup():
    queue_manager.start_workers()

# Enable CORS for external frontends (Svelte, Next.js, React)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Security Dependency
async def verify_api_key(
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    token: Optional[str] = Query(None),
):
    if API_SECRET_KEY:
        provided = x_api_key or token
        if not provided or provided != API_SECRET_KEY:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing API key",
            )

# Pydantic Request Models
class CreateAuthSessionRequest(BaseModel):
    user_id: str = Field(..., description="Unique User ID from your SaaS auth (e.g. usr_123)")
    account_alias: str = Field(..., description="Account identifier chosen by user (e.g. acc1, tech_channel)")
    proxy: Optional[ProxyConfig] = None

class DirectUploadRequest(BaseModel):
    user_id: str
    account_alias: str
    video_path: str
    title: str
    description: str = ""
    tags: List[str] = []
    thumbnail_path: Optional[str] = None
    visibility: Visibility = Visibility.PRIVATE
    scheduled_publish_time: Optional[str] = None
    set_as_premiere: bool = False
    proxy: Optional[ProxyConfig] = None

# --- REST Endpoints ---

@app.get("/api/v1/health")
async def health_check():
    return {"status": "ok", "service": "yt-autosub-api"}

@app.post("/api/v1/auth/session", dependencies=[Depends(verify_api_key)])
async def create_auth_session(req: CreateAuthSessionRequest):
    """
    Start an interactive Camoufox browser session for a user to log into YouTube Studio.
    Returns session ID and WebSocket streaming URL.
    """
    session = session_manager.create_session(
        user_id=req.user_id,
        account_alias=req.account_alias,
        proxy=req.proxy,
    )
    # Start browser in background
    asyncio.create_task(session.start())

    return {
        "session_id": session.session_id,
        "channel_id": session.channel_id,
        "user_id": session.user_id,
        "account_alias": session.account_alias,
        "status": session.status,
        "ws_endpoint": f"/api/v1/auth/session/{session.session_id}/ws",
        "embed_url": f"/stream/{session.session_id}",
    }

@app.get("/api/v1/auth/session/{session_id}", dependencies=[Depends(verify_api_key)])
async def get_session_status(session_id: str):
    """Get the current lifecycle status of an authentication session."""
    session = session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "session_id": session.session_id,
        "channel_id": session.channel_id,
        "status": session.status,
        "is_authenticated": session.is_authenticated,
        "channel_url": session.channel_url,
        "error": session.error,
    }

@app.delete("/api/v1/auth/session/{session_id}", dependencies=[Depends(verify_api_key)])
async def close_session(session_id: str):
    """Terminate and clean up an active browser session."""
    session = session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    await session.close()
    session_manager.sessions.pop(session_id, None)
    return {"message": "Session closed and profile purged"}

@app.delete("/api/v1/channels/{account_alias}", dependencies=[Depends(verify_api_key)])
async def delete_channel_endpoint(
    account_alias: str,
    user_id: str = Query(..., description="User ID"),
):
    """Delete channel profile folder and session data on disk."""
    import shutil
    from yt_autosub.config import sanitize_identifier
    clean_user = sanitize_identifier(user_id)
    clean_alias = sanitize_identifier(account_alias)
    profile_dir = PROFILES_DIR / clean_user / clean_alias
    session_file = SESSIONS_DIR / clean_user / f"{clean_alias}.json"

    if profile_dir.exists():
        shutil.rmtree(profile_dir, ignore_errors=True)
    if session_file.exists():
        session_file.unlink(missing_ok=True)

    return {"message": f"Channel data for {clean_alias} deleted"}

@app.get("/api/v1/channels", dependencies=[Depends(verify_api_key)])
async def list_user_channels(user_id: str = Query(..., description="User ID")):
    """List all configured YouTube channels and session statuses for a specific user."""
    user_sess_dir = SESSIONS_DIR / user_id
    channels = []
    if user_sess_dir.exists():
        for sess_file in user_sess_dir.glob("*.json"):
            alias = sess_file.stem
            channels.append({
                "user_id": user_id,
                "account_alias": alias,
                "channel_id": get_channel_id(user_id, alias),
                "has_session": True,
                "session_file": str(sess_file),
            })
    # If no user-scoped sessions exist, check root sessions for developer / fallback convenience
    if not channels and SESSIONS_DIR.exists():
        for sess_file in SESSIONS_DIR.glob("*.json"):
            alias = sess_file.stem
            channels.append({
                "user_id": user_id,
                "account_alias": alias,
                "channel_id": get_channel_id(user_id, alias),
                "has_session": True,
                "session_file": str(sess_file),
            })
    return {"user_id": user_id, "channels": channels}

@app.post("/api/v1/upload", dependencies=[Depends(verify_api_key)])
async def upload_video_endpoint(req: DirectUploadRequest):
    """Trigger automated video upload for a user's authenticated channel."""
    task = VideoUploadTask(
        user_id=req.user_id,
        account_alias=req.account_alias,
        account_id=get_channel_id(req.user_id, req.account_alias),
        video_path=req.video_path,
        title=req.title,
        description=req.description,
        tags=req.tags,
        thumbnail_path=req.thumbnail_path,
        visibility=req.visibility,
        scheduled_publish_time=req.scheduled_publish_time,
        set_as_premiere=req.set_as_premiere,
    )

    result = await upload_video(task=task, headless=True, proxy=req.proxy)
    if not result.success:
        raise HTTPException(status_code=500, detail=result.error_message)
    return result

class QueueUploadRequest(BaseModel):
    job_id: str
    user_id: str
    account_aliases: List[str]
    video_path: str
    title: str
    description: str = ""
    tags: List[str] = []
    thumbnail_path: Optional[str] = None
    visibility: str = "PRIVATE"
    scheduled_publish_time: Optional[str] = None
    set_as_premiere: bool = False
    proxy: Optional[ProxyConfig] = None

@app.post("/api/v1/queue/upload", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(verify_api_key)])
async def enqueue_upload(req: QueueUploadRequest):
    """Enqueue an automated video upload job across multiple accounts."""
    job = queue_manager.create_job(
        job_id=req.job_id,
        user_id=req.user_id,
        account_aliases=req.account_aliases,
        video_path=req.video_path,
        title=req.title,
        description=req.description,
        tags=req.tags,
        thumbnail_path=req.thumbnail_path,
        visibility=req.visibility,
        scheduled_publish_time=req.scheduled_publish_time,
        set_as_premiere=req.set_as_premiere,
        proxy=req.proxy,
    )
    return {
        "job_id": job.job_id,
        "status": job.status,
        "total_channels": len(job.tasks)
    }

@app.get("/api/v1/queue/jobs/{job_id}", dependencies=[Depends(verify_api_key)])
async def get_queue_job(job_id: str):
    """Get the current progress and per-account upload status of a queued job."""
    job = queue_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_dict()

@app.get("/api/v1/queue/jobs", dependencies=[Depends(verify_api_key)])
async def list_queue_jobs(user_id: Optional[str] = Query(None)):
    """List queue jobs, optionally filtered by user_id."""
    jobs = queue_manager.list_jobs(user_id=user_id)
    return {"jobs": [j.to_dict() for j in jobs]}

# --- Admin Diagnostic Endpoints ---

class CreateDiagnosticSessionRequest(BaseModel):
    user_id: str = Field(..., description="Tenant user_id (or 'default' for root accounts)")
    account_alias: str = Field(..., description="Channel alias to inspect")
    target_url: Optional[str] = Field(default="https://studio.youtube.com", description="Initial URL to open")
    proxy: Optional[ProxyConfig] = None

@app.get("/api/v1/diagnostic/accounts", dependencies=[Depends(verify_api_key)])
async def diagnostic_list_accounts():
    """Discover all user accounts across profiles and sessions for admin diagnostics."""
    discovered = {}

    if PROFILES_DIR.exists():
        for item in PROFILES_DIR.iterdir():
            if not item.is_dir():
                continue
            # Check if multi-tenant user dir containing sub-profiles
            sub_profiles = [p for p in item.iterdir() if p.is_dir() and not p.name.startswith(".")]
            # Heuristic: Firefox profile dirs contain prefs.js or times.json directly
            is_direct_profile = (item / "prefs.js").exists() or (item / "times.json").exists()
            if is_direct_profile:
                key = f"default/{item.name}"
                discovered[key] = {"user_id": "default", "account_alias": item.name, "has_profile": True, "has_session": False}
            else:
                for sub in sub_profiles:
                    key = f"{item.name}/{sub.name}"
                    discovered[key] = {"user_id": item.name, "account_alias": sub.name, "has_profile": True, "has_session": False}

    if SESSIONS_DIR.exists():
        for item in SESSIONS_DIR.iterdir():
            if item.is_file() and item.suffix == ".json":
                alias = item.stem
                key = f"default/{alias}"
                entry = discovered.setdefault(key, {"user_id": "default", "account_alias": alias, "has_profile": False, "has_session": False})
                entry["has_session"] = True
            elif item.is_dir():
                for sf in item.glob("*.json"):
                    alias = sf.stem
                    key = f"{item.name}/{alias}"
                    entry = discovered.setdefault(key, {"user_id": item.name, "account_alias": alias, "has_profile": False, "has_session": False})
                    entry["has_session"] = True

    accounts = sorted(discovered.values(), key=lambda a: (a["user_id"], a["account_alias"]))
    return {"accounts": accounts}

@app.post("/api/v1/diagnostic/session", dependencies=[Depends(verify_api_key)])
async def create_diagnostic_session(req: CreateDiagnosticSessionRequest):
    """
    Launch an interactive diagnostic Camoufox browser for any account without auto-closing
    or purging profiles. Admin can inspect YouTube Studio upload status, errors, or notifications.
    """
    user_id = "" if req.user_id == "default" else req.user_id
    session = session_manager.create_session(
        user_id=user_id,
        account_alias=req.account_alias,
        proxy=req.proxy,
        mode="diagnostic",
        target_url=req.target_url or "https://studio.youtube.com",
    )
    asyncio.create_task(session.start())
    return {
        "session_id": session.session_id,
        "channel_id": session.channel_id,
        "user_id": req.user_id,
        "account_alias": req.account_alias,
        "mode": session.mode,
        "status": session.status,
        "ws_endpoint": f"/api/v1/auth/session/{session.session_id}/ws",
    }

@app.get("/api/v1/diagnostic/sessions", dependencies=[Depends(verify_api_key)])
async def list_diagnostic_sessions():
    """List active diagnostic/auth sessions and currently running background uploads."""
    sessions = [
        {
            "session_id": s.session_id,
            "user_id": s.user_id or "default",
            "account_alias": s.account_alias,
            "mode": s.mode,
            "status": s.status,
            "url": s.page.url if s.page and not s.page.is_closed() else None,
            "error": s.error,
        }
        for s in session_manager.sessions.values()
        if s.status not in ("closed",)
    ]
    uploads = [
        {
            "upload_id": u.upload_id,
            "account_id": u.account_id,
            "title": u.title,
            "progress": u.progress,
            "ws_endpoint": f"/api/v1/diagnostic/upload/{u.upload_id}/ws",
        }
        for u in session_manager.active_uploads.values()
    ]
    return {"sessions": sessions, "active_uploads": uploads}

@app.delete("/api/v1/diagnostic/session/{session_id}", dependencies=[Depends(verify_api_key)])
async def close_diagnostic_session(session_id: str):
    """Close a diagnostic browser session cleanly without deleting profile data."""
    session = session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    await session.close()
    return {"message": f"Diagnostic session {session_id} closed"}

@app.get("/api/v1/diagnostic/logs", dependencies=[Depends(verify_api_key)])
async def list_diagnostic_logs():
    """List recent error screenshots in data/logs."""
    screenshots = []
    if LOGS_DIR.exists():
        files = sorted(LOGS_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
        for f in files[:30]:
            screenshots.append({
                "filename": f.name,
                "size_bytes": f.stat().st_size,
                "modified_at": f.stat().st_mtime,
                "url": f"/api/v1/diagnostic/logs/screenshot/{f.name}",
            })
    return {"screenshots": screenshots}

@app.get("/api/v1/diagnostic/logs/screenshot/{filename}", dependencies=[Depends(verify_api_key)])
async def get_diagnostic_screenshot(filename: str):
    """Serve an error screenshot safely from data/logs."""
    safe_name = Path(filename).name
    target = LOGS_DIR / safe_name
    if not target.exists() or target.suffix.lower() != ".png":
        raise HTTPException(status_code=404, detail="Screenshot not found")
    return FileResponse(str(target), media_type="image/png")

@app.websocket("/api/v1/diagnostic/upload/{upload_id}/ws")
async def live_upload_websocket(websocket: WebSocket, upload_id: str, token: Optional[str] = None):
    """Stream live screenshots of an in-progress upload browser for admin diagnostics."""
    if API_SECRET_KEY and token != API_SECRET_KEY:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    upl = session_manager.get_upload(upload_id)
    if not upl:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    import base64
    try:
        while True:
            upl = session_manager.get_upload(upload_id)
            if not upl or not upl.page or upl.page.is_closed():
                await websocket.send_json({"type": "ended"})
                break
            try:
                shot = await upl.page.screenshot(type="jpeg", quality=60)
                b64 = base64.b64encode(shot).decode("ascii")
                await websocket.send_json({
                    "type": "frame",
                    "data": f"data:image/jpeg;base64,{b64}",
                    "progress": upl.progress,
                    "url": upl.page.url,
                })
            except Exception:
                pass
            await asyncio.sleep(0.25)
    except WebSocketDisconnect:
        pass

# --- WebSocket Streaming Endpoint ---

@app.websocket("/api/v1/auth/session/{session_id}/ws")
async def session_websocket(websocket: WebSocket, session_id: str, token: Optional[str] = None):
    # Verify API key if configured
    if API_SECRET_KEY and token != API_SECRET_KEY:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    session = session_manager.get_session(session_id)
    if not session:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    session.ws_clients.add(websocket)
    logger.info(f"WebSocket client connected to session: {session_id}")

    try:
        while True:
            data = await websocket.receive_json()
            await session.handle_input(data)
    except WebSocketDisconnect:
        logger.info(f"WebSocket client disconnected from session: {session_id}")
    except Exception as e:
        logger.debug(f"WebSocket error: {e}")
    finally:
        session.ws_clients.discard(websocket)

# --- Built-In Web Stream Viewer ---

@app.get("/stream/{session_id}", response_class=HTMLResponse)
async def stream_view(session_id: str):
    """Interactive HTML5 canvas streamer page for testing and iframe embedding."""
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>YouTube Connect - Remote Browser</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: #0f0f0f;
      color: #fff;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
      padding: 16px;
    }}
    .header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      width: 100%;
      max-width: 1280px;
      margin-bottom: 12px;
    }}
    .badge {{
      display: inline-block;
      padding: 4px 12px;
      border-radius: 9999px;
      font-size: 13px;
      font-weight: 600;
      background: #272727;
      color: #aaa;
    }}
    .badge.active {{ background: #065fd4; color: #fff; }}
    .badge.authenticated {{ background: #2ba640; color: #fff; }}
    .badge.error {{ background: #cc0000; color: #fff; }}
    .canvas-container {{
      position: relative;
      width: 100%;
      max-width: 1280px;
      background: #000;
      border-radius: 8px;
      overflow: hidden;
      box-shadow: 0 10px 30px rgba(0,0,0,0.5);
      border: 1px solid #333;
    }}
    canvas {{
      width: 100%;
      height: auto;
      display: block;
      cursor: default;
    }}
    .status-overlay {{
      position: absolute;
      top: 0; left: 0; right: 0; bottom: 0;
      background: rgba(15,15,15,0.85);
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      z-index: 10;
      font-size: 20px;
      font-weight: 600;
      text-align: center;
    }}
    .instructions {{
      margin-top: 10px;
      font-size: 13px;
      color: #888;
      max-width: 1280px;
      text-align: center;
    }}
  </style>
</head>
<body>
  <div class="header">
    <h2>YouTube Studio Connect</h2>
    <span id="badge" class="badge">Connecting...</span>
  </div>

  <div class="canvas-container" id="container">
    <div id="overlay" class="status-overlay">Starting remote browser...</div>
    <canvas id="screen" width="{VIEWPORT_WIDTH}" height="{VIEWPORT_HEIGHT}" tabindex="0"></canvas>
  </div>

  <div class="instructions">
    Type your Google credentials and solve 2FA directly inside the window. Once you land in YouTube Studio, your channel will connect automatically!
  </div>

  <script>
    const canvas = document.getElementById('screen');
    const ctx = canvas.getContext('2d');
    const overlay = document.getElementById('overlay');
    const badge = document.getElementById('badge');

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${{protocol}}//${{window.location.host}}/api/v1/auth/session/{session_id}/ws`;
    const ws = new WebSocket(wsUrl);

    let img = new Image();
    img.onload = () => {{
      ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
      if (overlay.style.display !== 'none' && !overlay.classList.contains('auth-done')) {{
        overlay.style.display = 'none';
        badge.textContent = 'Active (Interact to Sign In)';
        badge.className = 'badge active';
      }}
    }};

    ws.onmessage = (event) => {{
      const msg = JSON.parse(event.data);
      if (msg.type === 'frame') {{
        img.src = msg.data;
      }} else if (msg.type === 'authenticated') {{
        overlay.style.display = 'flex';
        overlay.classList.add('auth-done');
        overlay.innerHTML = '<div style="color: #2ba640; font-size: 32px; margin-bottom: 8px;">✓ Channel Connected!</div><div style="font-size: 16px; color: #aaa;">Session saved securely. You can close this window.</div>';
        badge.textContent = 'Connected';
        badge.className = 'badge authenticated';
      }}
    }};

    ws.onclose = () => {{
      if (!overlay.classList.contains('auth-done')) {{
        badge.textContent = 'Disconnected';
        badge.className = 'badge error';
      }}
    }};

    function getCoords(e) {{
      const rect = canvas.getBoundingClientRect();
      const x = (e.clientX - rect.left) / rect.width;
      const y = (e.clientY - rect.top) / rect.height;
      return {{ x, y }};
    }}

    canvas.addEventListener('click', (e) => {{
      const c = getCoords(e);
      ws.send(JSON.stringify({{ type: 'mouse', action: 'click', x: c.x, y: c.y, button: 'left' }}));
    }});

    canvas.addEventListener('keydown', (e) => {{
      // Prevent browser default keys like backspace or tab
      if (['Tab', 'Backspace', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(e.key)) {{
        e.preventDefault();
      }}
      if (e.key.length === 1 && !e.ctrlKey && !e.altKey && !e.metaKey) {{
        ws.send(JSON.stringify({{ type: 'keyboard', action: 'type', text: e.key }}));
      }} else {{
        ws.send(JSON.stringify({{ type: 'keyboard', action: 'press', key: e.key }}));
      }}
    }});

    canvas.addEventListener('wheel', (e) => {{
      e.preventDefault();
      ws.send(JSON.stringify({{ type: 'scroll', deltaX: e.deltaX, deltaY: e.deltaY }}));
    }});
  </script>
</body>
</html>
"""
    return HTMLResponse(content=html_content)

@app.get("/diagnostic", response_class=HTMLResponse)
async def admin_diagnostic_page():
    """Admin Diagnostic Console for inspecting upload progress, controlling browsers, and checking Studio notifications."""
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Admin Browser Diagnostic Console - yt-autosub</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: #0b0d12;
      color: #e4e4e7;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      padding: 20px;
    }}
    .topbar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 16px;
      padding-bottom: 12px;
      border-bottom: 1px solid #27272a;
      gap: 12px;
      flex-wrap: wrap;
    }}
    .topbar h1 {{ font-size: 20px; color: #fff; }}
    .topbar-right {{ display: flex; gap: 8px; align-items: center; }}
    .grid {{
      display: grid;
      grid-template-columns: 360px 1fr;
      gap: 16px;
      align-items: start;
    }}
    @media (max-width: 1024px) {{
      .grid {{ grid-template-columns: 1fr; }}
    }}
    .panel {{
      background: #14171f;
      border: 1px solid #27272a;
      border-radius: 10px;
      padding: 16px;
      margin-bottom: 16px;
    }}
    .panel h3 {{
      font-size: 14px;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: #a1a1aa;
      margin-bottom: 12px;
    }}
    label {{ display: block; font-size: 12px; color: #a1a1aa; margin-top: 10px; margin-bottom: 4px; }}
    input, select, button {{
      width: 100%;
      padding: 8px 10px;
      border-radius: 6px;
      border: 1px solid #3f3f46;
      background: #0b0d12;
      color: #fff;
      font-size: 13px;
    }}
    button {{
      cursor: pointer;
      font-weight: 600;
      background: #2563eb;
      border-color: #2563eb;
      margin-top: 10px;
    }}
    button:hover {{ background: #1d4ed8; }}
    button.secondary {{ background: #27272a; border-color: #3f3f46; }}
    button.secondary:hover {{ background: #3f3f46; }}
    button.danger {{ background: #dc2626; border-color: #dc2626; }}
    button.danger:hover {{ background: #b91c1c; }}
    button.inline {{ width: auto; margin-top: 0; padding: 6px 10px; font-size: 12px; }}
    .toolbar {{
      display: flex;
      gap: 8px;
      align-items: center;
      margin-bottom: 10px;
      flex-wrap: wrap;
    }}
    .toolbar input {{ flex: 1; min-width: 220px; margin: 0; }}
    .canvas-wrap {{
      position: relative;
      background: #000;
      border: 1px solid #27272a;
      border-radius: 8px;
      overflow: hidden;
    }}
    canvas {{
      width: 100%;
      height: auto;
      display: block;
      outline: none;
    }}
    .overlay {{
      position: absolute;
      inset: 0;
      display: flex;
      align-items: center;
      justify-content: center;
      background: rgba(11,13,18,0.85);
      color: #a1a1aa;
      font-size: 15px;
      text-align: center;
      padding: 20px;
    }}
    .item {{
      background: #0b0d12;
      border: 1px solid #27272a;
      border-radius: 6px;
      padding: 10px;
      margin-bottom: 8px;
      font-size: 12px;
    }}
    .badge {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 600;
      background: #27272a;
      color: #d4d4d8;
    }}
    .badge.ok {{ background: #14532d; color: #86efac; }}
    .badge.warn {{ background: #713f12; color: #fde047; }}
    .badge.err {{ background: #7f1d1d; color: #fca5a5; }}
    .badge.live {{ background: #1e3a8a; color: #93c5fd; }}
    .muted {{ color: #71717a; font-size: 12px; }}
    .presets {{ display: flex; gap: 6px; margin-top: 6px; flex-wrap: wrap; }}
    .presets button {{ flex: 1; margin-top: 0; font-size: 11px; padding: 5px; }}
    .shot-preview {{ max-width: 100%; border-radius: 6px; margin-top: 8px; border: 1px solid #27272a; }}
  </style>
</head>
<body>
  <div class="topbar">
    <div>
      <h1>Admin Browser Diagnostic Console</h1>
      <div class="muted">Inspect upload progress, check YouTube Studio errors/notifications, and directly control account browsers</div>
    </div>
    <div class="topbar-right">
      <input id="apiKeyInput" type="password" placeholder="X-API-Key (if configured)" style="width: 220px;" />
      <button class="inline secondary" onclick="saveApiKey()">Save Key</button>
      <button class="inline secondary" onclick="refreshAll()">Refresh</button>
      <a href="/" style="color:#93c5fd; font-size:13px; text-decoration:none; margin-left:6px;">Home</a>
    </div>
  </div>

  <div class="grid">
    <div>
      <div class="panel">
        <h3>1. Launch Diagnostic Browser</h3>
        <label>Discovered Account</label>
        <select id="accountSelect" onchange="onSelectAccount()">
          <option value="">-- Loading accounts --</option>
        </select>

        <label>User ID</label>
        <input id="userId" placeholder="e.g. default or usr_123" />

        <label>Account Alias</label>
        <input id="accountAlias" placeholder="e.g. acc1" />

        <label>Initial URL</label>
        <input id="targetUrl" value="https://studio.youtube.com" />
        <div class="presets">
          <button class="secondary" onclick="setTargetUrl('https://studio.youtube.com')">Studio Home</button>
          <button class="secondary" onclick="setTargetUrl('https://studio.youtube.com/channel/UC/videos/upload')">Studio Content</button>
        </div>

        <button onclick="launchDiagnosticBrowser()">Launch & Control Browser</button>
      </div>

      <div class="panel">
        <h3>2. Active Live Uploads</h3>
        <div id="activeUploadsList" class="muted">Loading...</div>
      </div>

      <div class="panel">
        <h3>3. Active Diagnostic Sessions</h3>
        <div id="activeSessionsList" class="muted">Loading...</div>
      </div>

      <div class="panel">
        <h3>4. Recent Queue Jobs</h3>
        <div id="queueJobsList" class="muted">Loading...</div>
      </div>

      <div class="panel">
        <h3>5. Error Screenshots (data/logs)</h3>
        <div id="screenshotsList" class="muted">Loading...</div>
      </div>
    </div>

    <div>
      <div class="panel">
        <div class="toolbar">
          <button class="inline secondary" onclick="sendNav('back')">Back</button>
          <button class="inline secondary" onclick="sendNav('forward')">Fwd</button>
          <button class="inline secondary" onclick="sendNav('reload')">Reload</button>
          <input id="urlBar" placeholder="Current browser URL..." onkeydown="if(event.key==='Enter') navigateUrl()" />
          <button class="inline" onclick="navigateUrl()">Go</button>
          <button class="inline secondary" onclick="goStudioContent()">Studio Content</button>
          <button class="inline danger" id="closeBtn" onclick="closeCurrentSession()" style="display:none;">Close Browser</button>
        </div>

        <div class="toolbar" style="margin-bottom: 8px;">
          <span id="streamStatus" class="badge">Idle</span>
          <span id="streamInfo" class="muted">Select an account and click "Launch & Control Browser", or click "Watch Live" on an active upload.</span>
        </div>

        <div class="canvas-wrap">
          <div id="overlay" class="overlay">No browser attached. Launch a diagnostic browser on the left to inspect YouTube Studio.</div>
          <canvas id="screen" width="{VIEWPORT_WIDTH}" height="{VIEWPORT_HEIGHT}" tabindex="0"></canvas>
        </div>
      </div>
    </div>
  </div>

  <script>
    let apiKey = localStorage.getItem('yt_autosub_api_key') || '';
    document.getElementById('apiKeyInput').value = apiKey;

    let currentWs = null;
    let currentSessionId = null;
    let currentMode = null; // 'diagnostic' | 'upload'
    let currentUrl = '';

    const canvas = document.getElementById('screen');
    const ctx = canvas.getContext('2d');
    const overlay = document.getElementById('overlay');
    const streamStatus = document.getElementById('streamStatus');
    const streamInfo = document.getElementById('streamInfo');
    const urlBar = document.getElementById('urlBar');
    const closeBtn = document.getElementById('closeBtn');

    function saveApiKey() {{
      apiKey = document.getElementById('apiKeyInput').value.trim();
      localStorage.setItem('yt_autosub_api_key', apiKey);
      refreshAll();
    }}

    function apiHeaders() {{
      const h = {{ 'Content-Type': 'application/json' }};
      if (apiKey) h['X-API-Key'] = apiKey;
      return h;
    }}

    function wsTokenQuery() {{
      return apiKey ? `?token=${{encodeURIComponent(apiKey)}}` : '';
    }}

    function setTargetUrl(u) {{
      document.getElementById('targetUrl').value = u;
    }}

    function onSelectAccount() {{
      const val = document.getElementById('accountSelect').value;
      if (!val) return;
      const [uid, alias] = val.split('|||');
      document.getElementById('userId').value = uid;
      document.getElementById('accountAlias').value = alias;
    }}

    async function loadAccounts() {{
      const res = await fetch('/api/v1/diagnostic/accounts', {{ headers: apiHeaders() }});
      if (!res.ok) return;
      const data = await res.json();
      const sel = document.getElementById('accountSelect');
      sel.innerHTML = '<option value="">-- Select an account --</option>';
      for (const acc of data.accounts || []) {{
        const opt = document.createElement('option');
        opt.value = `${{acc.user_id}}|||${{acc.account_alias}}`;
        const tags = [acc.has_profile ? 'profile' : null, acc.has_session ? 'session' : null].filter(Boolean).join(', ');
        opt.textContent = `${{acc.user_id}} / ${{acc.account_alias}} (${{tags}})`;
        sel.appendChild(opt);
      }}
    }}

    async function refreshSessionsAndUploads() {{
      const res = await fetch('/api/v1/diagnostic/sessions', {{ headers: apiHeaders() }});
      if (!res.ok) return;
      const data = await res.json();

      const uplEl = document.getElementById('activeUploadsList');
      if (!data.active_uploads || data.active_uploads.length === 0) {{
        uplEl.innerHTML = '<span class="muted">No uploads currently running.</span>';
      }} else {{
        uplEl.innerHTML = data.active_uploads.map(u => `
          <div class="item">
            <div><strong>${{u.title}}</strong> <span class="badge live">Uploading</span></div>
            <div class="muted">${{u.account_id}} — ${{u.progress}}</div>
            <button class="inline" style="margin-top:6px;" onclick="attachLiveUpload('${{u.upload_id}}', '${{u.title.replace(/'/g, "\\'")}}')">Watch Live</button>
          </div>
        `).join('');
      }}

      const sessEl = document.getElementById('activeSessionsList');
      if (!data.sessions || data.sessions.length === 0) {{
        sessEl.innerHTML = '<span class="muted">No active browser sessions.</span>';
      }} else {{
        sessEl.innerHTML = data.sessions.map(s => `
          <div class="item">
            <div><strong>${{s.user_id}} / ${{s.account_alias}}</strong> <span class="badge ok">${{s.mode}}</span></div>
            <div class="muted">${{s.session_id}} (${{s.status}})</div>
            <div style="display:flex; gap:6px; margin-top:6px;">
              <button class="inline" onclick="attachSession('${{s.session_id}}', '${{s.user_id}}/${{s.account_alias}}')">Attach</button>
              <button class="inline danger" onclick="closeSession('${{s.session_id}}')">Close</button>
            </div>
          </div>
        `).join('');
      }}
    }}

    async function loadQueueJobs() {{
      const res = await fetch('/api/v1/queue/jobs', {{ headers: apiHeaders() }});
      if (!res.ok) return;
      const data = await res.json();
      const el = document.getElementById('queueJobsList');
      const jobs = (data.jobs || []).slice(-8).reverse();
      if (jobs.length === 0) {{
        el.innerHTML = '<span class="muted">No queue jobs recorded yet.</span>';
        return;
      }}
      el.innerHTML = jobs.map(j => {{
        const cls = j.status === 'completed' ? 'ok' : (j.status === 'failed' || j.status === 'partial_failed' ? 'err' : 'warn');
        const tasksHtml = (j.tasks || []).map(t => `
          <div class="muted" style="margin-top:4px;">
            • ${{t.account_alias}}: <strong>${{t.status}}</strong>
            ${{t.video_url ? `<a href="${{t.video_url}}" target="_blank" style="color:#93c5fd;">${{t.video_url}}</a>` : ''}}
            ${{t.error_message ? `<div style="color:#fca5a5;">${{t.error_message}}</div>` : ''}}
          </div>
        `).join('');
        return `<div class="item">
          <div><strong>${{j.title}}</strong> <span class="badge ${{cls}}">${{j.status}}</span></div>
          <div class="muted">${{j.job_id}} (${{j.user_id}})</div>
          ${{tasksHtml}}
        </div>`;
      }}).join('');
    }}

    async function loadScreenshots() {{
      const res = await fetch('/api/v1/diagnostic/logs', {{ headers: apiHeaders() }});
      if (!res.ok) return;
      const data = await res.json();
      const el = document.getElementById('screenshotsList');
      const shots = (data.screenshots || []).slice(0, 8);
      if (shots.length === 0) {{
        el.innerHTML = '<span class="muted">No error screenshots found.</span>';
        return;
      }}
      el.innerHTML = shots.map(s => `
        <div class="item">
          <div style="word-break:break-all;">${{s.filename}}</div>
          <button class="inline secondary" style="margin-top:6px;" onclick="viewScreenshot('${{s.url}}')">View Screenshot</button>
        </div>
      `).join('');
    }}

    function viewScreenshot(url) {{
      if (currentWs) {{ currentWs.close(); currentWs = null; }}
      currentSessionId = null;
      currentMode = null;
      closeBtn.style.display = 'none';
      const img = new Image();
      img.onload = () => {{
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        overlay.style.display = 'none';
        streamStatus.textContent = 'Screenshot';
        streamStatus.className = 'badge warn';
        streamInfo.textContent = url;
      }};
      img.src = url + wsTokenQuery();
    }}

    async function launchDiagnosticBrowser() {{
      const user_id = document.getElementById('userId').value.trim();
      const account_alias = document.getElementById('accountAlias').value.trim();
      const target_url = document.getElementById('targetUrl').value.trim() || 'https://studio.youtube.com';
      if (!user_id || !account_alias) {{
        alert('Please enter User ID and Account Alias');
        return;
      }}
      overlay.style.display = 'flex';
      overlay.textContent = 'Launching diagnostic browser...';
      const res = await fetch('/api/v1/diagnostic/session', {{
        method: 'POST',
        headers: apiHeaders(),
        body: JSON.stringify({{ user_id, account_alias, target_url }})
      }});
      const data = await res.json();
      if (!res.ok) {{
        overlay.textContent = 'Failed: ' + JSON.stringify(data);
        return;
      }}
      attachSession(data.session_id, `${{user_id}}/${{account_alias}}`);
      refreshSessionsAndUploads();
    }}

    function attachSession(sessionId, label) {{
      if (currentWs) currentWs.close();
      currentSessionId = sessionId;
      currentMode = 'diagnostic';
      closeBtn.style.display = 'inline-block';
      overlay.style.display = 'flex';
      overlay.textContent = 'Connecting to browser stream...';
      streamStatus.textContent = 'Connecting...';
      streamStatus.className = 'badge warn';
      streamInfo.textContent = `Diagnostic: ${{label}} (${{sessionId}})`;

      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      const ws = new WebSocket(`${{proto}}//${{location.host}}/api/v1/auth/session/${{sessionId}}/ws${{wsTokenQuery()}}`);
      currentWs = ws;
      const img = new Image();
      img.onload = () => {{
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        overlay.style.display = 'none';
        streamStatus.textContent = 'Live Interactive';
        streamStatus.className = 'badge ok';
      }};
      ws.onmessage = (ev) => {{
        const msg = JSON.parse(ev.data);
        if (msg.type === 'frame') {{
          img.src = msg.data;
          if (msg.url && document.activeElement !== urlBar) {{
            currentUrl = msg.url;
            urlBar.value = msg.url;
          }}
        }} else if (msg.type === 'error') {{
          overlay.style.display = 'flex';
          overlay.textContent = 'Browser error: ' + msg.error;
          streamStatus.textContent = 'Error';
          streamStatus.className = 'badge err';
        }}
      }};
      ws.onclose = () => {{
        streamStatus.textContent = 'Disconnected';
        streamStatus.className = 'badge err';
      }};
    }}

    function attachLiveUpload(uploadId, title) {{
      if (currentWs) currentWs.close();
      currentSessionId = null;
      currentMode = 'upload';
      closeBtn.style.display = 'none';
      overlay.style.display = 'flex';
      overlay.textContent = 'Connecting to live upload browser...';
      streamStatus.textContent = 'Live Upload View';
      streamStatus.className = 'badge live';
      streamInfo.textContent = `Watching upload: ${{title}}`;

      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      const ws = new WebSocket(`${{proto}}//${{location.host}}/api/v1/diagnostic/upload/${{uploadId}}/ws${{wsTokenQuery()}}`);
      currentWs = ws;
      const img = new Image();
      img.onload = () => {{
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        overlay.style.display = 'none';
      }};
      ws.onmessage = (ev) => {{
        const msg = JSON.parse(ev.data);
        if (msg.type === 'frame') {{
          img.src = msg.data;
          urlBar.value = msg.url || '';
          streamInfo.textContent = `Watching upload: ${{title}} | ${{msg.progress || ''}}`;
        }} else if (msg.type === 'ended') {{
          streamStatus.textContent = 'Upload Ended';
          streamStatus.className = 'badge';
        }}
      }};
    }}

    async function closeSession(sid) {{
      await fetch(`/api/v1/diagnostic/session/${{sid}}`, {{ method: 'DELETE', headers: apiHeaders() }});
      if (currentSessionId === sid) {{
        if (currentWs) currentWs.close();
        currentSessionId = null;
        closeBtn.style.display = 'none';
        overlay.style.display = 'flex';
        overlay.textContent = 'Diagnostic browser closed.';
        streamStatus.textContent = 'Closed';
        streamStatus.className = 'badge';
      }}
      refreshSessionsAndUploads();
    }}

    function closeCurrentSession() {{
      if (currentSessionId) closeSession(currentSessionId);
    }}

    function sendNav(action) {{
      if (currentWs && currentMode === 'diagnostic') {{
        currentWs.send(JSON.stringify({{ type: action }}));
      }}
    }}

    function navigateUrl() {{
      const url = urlBar.value.trim();
      if (currentWs && currentMode === 'diagnostic' && url) {{
        currentWs.send(JSON.stringify({{ type: 'navigate', url }}));
      }}
    }}

    function goStudioContent() {{
      // If currently on studio.youtube.com/channel/<ID>..., jump to /videos/upload
      const m = (currentUrl || '').match(/https:\\/\\/studio\\.youtube\\.com\\/channel\\/([^/?#]+)/);
      const dest = m ? `https://studio.youtube.com/channel/${{m[1]}}/videos/upload` : 'https://studio.youtube.com';
      if (currentWs && currentMode === 'diagnostic') {{
        currentWs.send(JSON.stringify({{ type: 'navigate', url: dest }}));
      }}
    }}

    function getCoords(e) {{
      const rect = canvas.getBoundingClientRect();
      return {{
        x: (e.clientX - rect.left) / rect.width,
        y: (e.clientY - rect.top) / rect.height
      }};
    }}

    canvas.addEventListener('click', (e) => {{
      canvas.focus();
      if (!currentWs || currentMode !== 'diagnostic') return;
      const c = getCoords(e);
      currentWs.send(JSON.stringify({{ type: 'mouse', action: 'click', x: c.x, y: c.y, button: 'left' }}));
    }});

    canvas.addEventListener('keydown', (e) => {{
      if (!currentWs || currentMode !== 'diagnostic') return;
      if (['Tab', 'Backspace', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', ' '].includes(e.key)) {{
        e.preventDefault();
      }}
      if (e.key.length === 1 && !e.ctrlKey && !e.altKey && !e.metaKey) {{
        currentWs.send(JSON.stringify({{ type: 'keyboard', action: 'type', text: e.key }}));
      }} else {{
        currentWs.send(JSON.stringify({{ type: 'keyboard', action: 'press', key: e.key }}));
      }}
    }});

    canvas.addEventListener('wheel', (e) => {{
      if (!currentWs || currentMode !== 'diagnostic') return;
      e.preventDefault();
      currentWs.send(JSON.stringify({{ type: 'scroll', deltaX: e.deltaX, deltaY: e.deltaY }}));
    }});

    function refreshAll() {{
      loadAccounts();
      refreshSessionsAndUploads();
      loadQueueJobs();
      loadScreenshots();
    }}

    refreshAll();
    setInterval(refreshSessionsAndUploads, 4000);
    setInterval(loadQueueJobs, 6000);
  </script>
</body>
</html>"""
    return HTMLResponse(content=html_content)

@app.get("/", response_class=HTMLResponse)
async def home_dashboard():
    """Quick launcher dashboard for creating test sessions and listing channels."""
    return HTMLResponse(content="""<!DOCTYPE html>
<html>
<head>
  <title>YouTube Automation API Dashboard</title>
  <style>
    body { font-family: system-ui, sans-serif; background: #18181b; color: #f4f4f5; padding: 32px; max-width: 800px; margin: 0 auto; }
    h1 { color: #fafafa; }
    .card { background: #27272a; padding: 24px; border-radius: 8px; margin-top: 20px; border: 1px solid #3f3f46; }
    input, button { padding: 10px 14px; border-radius: 6px; font-size: 14px; margin-top: 8px; }
    input { background: #18181b; border: 1px solid #52525b; color: #fff; width: 100%; box-sizing: border-box; }
    button { background: #2563eb; color: #fff; border: none; cursor: pointer; font-weight: 600; width: 100%; margin-top: 16px; }
    button:hover { background: #1d4ed8; }
    .row { display: flex; gap: 12px; }
    .row > div { flex: 1; }
    a.btn-link { display: inline-block; margin-top: 12px; color: #93c5fd; font-weight: 600; text-decoration: none; }
  </style>
</head>
<body>
  <h1>YouTube Automation API</h1>
  <p style="color: #a1a1aa;">Backend server running with multi-tenancy & WebSocket remote browser streaming.</p>
  <a class="btn-link" href="/diagnostic">Open Admin Browser Diagnostic Console &rarr;</a>

  <div class="card">
    <h3>Launch Channel Auth Session</h3>
    <div class="row">
      <div>
        <label>User ID</label>
        <input id="userId" value="user_demo_01" />
      </div>
      <div>
        <label>Account Alias</label>
        <input id="accountAlias" value="main_channel" />
      </div>
    </div>
    <button onclick="startSession()">Start Browser Stream</button>
  </div>

  <script>
    async function startSession() {
      const user_id = document.getElementById('userId').value.trim();
      const account_alias = document.getElementById('accountAlias').value.trim();
      const res = await fetch('/api/v1/auth/session', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_id, account_alias })
      });
      const data = await res.json();
      if (data.session_id) {
        window.location.href = data.embed_url;
      } else {
        alert('Error starting session: ' + JSON.stringify(data));
      }
    }
  </script>
</body>
</html>
""")
