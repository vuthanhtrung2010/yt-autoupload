import asyncio
from pathlib import Path
from typing import Optional, List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Header, Query, Depends, status
from fastapi.responses import HTMLResponse, JSONResponse
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
    return {"user_id": user_id, "channels": channels}

@app.post("/api/v1/upload", dependencies=[Depends(verify_api_key)])
async def upload_video_endpoint(req: DirectUploadRequest):
    """Trigger automated video upload for a user's authenticated channel."""
    task = VideoUploadTask(
        account_id=get_channel_id(req.user_id, req.account_alias),
        video_path=req.video_path,
        title=req.title,
        description=req.description,
        tags=req.tags,
        thumbnail_path=req.thumbnail_path,
        visibility=req.visibility,
    )

    # Note: upload_video can launch with user_id and account_alias directly
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
  </style>
</head>
<body>
  <h1>YouTube Automation API</h1>
  <p style="color: #a1a1aa;">Backend server running with multi-tenancy & WebSocket remote browser streaming.</p>

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
