import asyncio
import base64
import time
from uuid import uuid4
from typing import Dict, Optional, Set
from fastapi import WebSocket
from loguru import logger
from playwright.async_api import BrowserContext, Page

from yt_autosub.config import (
    get_user_profile_dir,
    get_user_session_path,
    get_channel_id,
    ProxyConfig,
)
from yt_autosub.browser import launch_account_browser, close_account_browser
from yt_autosub.auth import check_is_logged_in, STUDIO_URL

VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 720

class StreamingAuthSession:
    def __init__(
        self,
        session_id: str,
        user_id: str,
        account_alias: str,
        proxy: Optional[ProxyConfig] = None,
        mode: str = "auth",
        target_url: Optional[str] = None,
    ):
        self.session_id = session_id
        self.user_id = user_id
        self.account_alias = account_alias
        self.channel_id = get_channel_id(user_id, account_alias)
        self.proxy = proxy
        self.mode = mode  # "auth" or "diagnostic"
        self.target_url = target_url
        self.status = "starting"
        self.error: Optional[str] = None
        self.created_at = time.time()
        self.is_authenticated = False
        self.channel_url: Optional[str] = None

        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.ws_clients: Set[WebSocket] = set()

        self._stream_task: Optional[asyncio.Task] = None
        self._auth_task: Optional[asyncio.Task] = None
        self._action_event = asyncio.Event()
        self._running = True

    async def start(self):
        """Initialize the isolated Camoufox browser and navigate to Studio."""
        try:
            logger.info(f"[{self.session_id}] Launching {self.mode} browser for user={self.user_id}, alias={self.account_alias}")
            self.context = await launch_account_browser(
                user_id=self.user_id,
                account_alias=self.account_alias,
                headless=True,
                proxy=self.proxy,
                inject_cookies=(self.mode == "diagnostic"),
                viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            )
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()

            dest_url = self.target_url or STUDIO_URL
            logger.info(f"[{self.session_id}] Navigating to {dest_url}...")
            await self.page.goto(dest_url, wait_until="domcontentloaded", timeout=60000)

            self.status = "active"
            self._stream_task = asyncio.create_task(self._frame_loop())
            if self.mode == "auth":
                self._auth_task = asyncio.create_task(self._auth_watcher())
        except Exception as e:
            logger.exception(f"[{self.session_id}] Failed to start session: {e}")
            self.status = "error"
            self.error = str(e)
            await self.broadcast({"type": "error", "error": str(e)})
            await self.close()

    def _get_active_page(self) -> Optional[Page]:
        """Return the frontmost non-closed page (handles popup verification tabs)."""
        if self.context:
            open_pages = [p for p in self.context.pages if not p.is_closed()]
            if open_pages:
                return open_pages[-1]
        if self.page and not self.page.is_closed():
            return self.page
        return None

    async def _frame_loop(self):
        """Continuously capture frames from the active page and broadcast them over WebSockets."""
        frame_interval = 0.1  # ~10 fps base, with instant frames on action
        while self._running:
            active_page = self._get_active_page()
            if not active_page:
                break
            try:
                # Capture JPEG frame with moderate compression for low latency
                screenshot_bytes = await active_page.screenshot(type="jpeg", quality=65)
                b64_img = base64.b64encode(screenshot_bytes).decode("ascii")

                msg = {
                    "type": "frame",
                    "data": f"data:image/jpeg;base64,{b64_img}",
                    "status": self.status,
                    "url": active_page.url,
                }
                await self.broadcast(msg)

                # Wait for next tick or an immediate user action event
                try:
                    await asyncio.wait_for(self._action_event.wait(), timeout=frame_interval)
                    self._action_event.clear()
                except asyncio.TimeoutError:
                    pass

            except Exception as e:
                logger.debug(f"[{self.session_id}] Error in frame loop: {e}")
                await asyncio.sleep(0.5)

    async def _auth_watcher(self):
        """Actively watch for successful Google login and Studio dashboard arrival."""
        session_file = get_user_session_path(self.user_id, self.account_alias)
        while self._running and self.page and not self.page.is_closed():
            try:
                if await check_is_logged_in(self.page):
                    logger.info(f"[{self.session_id}] Login detected for {self.user_id}:{self.account_alias}!")
                    await asyncio.sleep(2)
                    
                    # Save storage state & cookies
                    await self.context.storage_state(path=str(session_file))
                    self.is_authenticated = True
                    self.status = "authenticated"
                    self.channel_url = self.page.url

                    await self.broadcast({
                        "type": "authenticated",
                        "user_id": self.user_id,
                        "account_alias": self.account_alias,
                        "channel_id": self.channel_id,
                        "channel_url": self.channel_url,
                    })

                    # Keep browser open for 6 seconds so user sees success on screen
                    await asyncio.sleep(6)
                    await self.close()
                    break

            except Exception as e:
                logger.debug(f"[{self.session_id}] Auth watcher poll error: {e}")

            await asyncio.sleep(1.5)

    async def attach_existing_context(self, context: BrowserContext, page: Page):
        """Attach an already-running browser context (e.g. paused at Google 2FA/identity verification) for live user control."""
        self.context = context
        self.page = page
        self.status = "needs_verification"
        try:
            await self.page.set_viewport_size({"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})
        except Exception:
            pass
        self._stream_task = asyncio.create_task(self._frame_loop())
        self._auth_task = asyncio.create_task(self._verify_watcher())

    async def _verify_watcher(self):
        """Watch a paused upload browser until user completes Google identity verification, then save cookies and keep open."""
        from yt_autosub.config import resolve_account_paths
        _, session_file, _ = resolve_account_paths(
            user_id=self.user_id or None,
            account_alias=self.account_alias,
        )
        session_file.parent.mkdir(parents=True, exist_ok=True)
        saw_cleared = False

        while self._running:
            active_page = self._get_active_page()
            if not active_page:
                break
            try:
                has_verify_prompt = False
                open_pages = [p for p in (self.context.pages if self.context else [active_page]) if not p.is_closed()]
                for p in open_pages:
                    p_has_prompt = await p.evaluate("""() => {
                        const url = window.location.href || '';
                        if (/accounts\\.google\\.com|myaccount\\.google\\.com/i.test(url)) return true;
                        const body = document.body ? (document.body.innerText || '') : '';
                        return /xác minh danh tính|xác thực danh tính|verify your identity|verify it's you|confirm it's you/i.test(body);
                    }""")
                    if p_has_prompt:
                        has_verify_prompt = True
                        break

                studio_page = self.page if (self.page and not self.page.is_closed()) else active_page
                if not has_verify_prompt and await check_is_logged_in(studio_page):
                    if not saw_cleared:
                        saw_cleared = True
                        await asyncio.sleep(2)
                        await self.context.storage_state(path=str(session_file))
                        self.is_authenticated = True
                        self.status = "verified"
                        self.channel_url = studio_page.url
                        logger.info(f"[{self.session_id}] Identity verification completed! Saved refreshed cookies to {session_file}")
                        await self.broadcast({
                            "type": "verified",
                            "user_id": self.user_id or "default",
                            "account_alias": self.account_alias,
                            "channel_id": self.channel_id,
                            "channel_url": self.channel_url,
                        })
                elif has_verify_prompt:
                    saw_cleared = False
            except Exception as e:
                logger.debug(f"[{self.session_id}] Verify watcher poll error: {e}")

            await asyncio.sleep(2)

    async def handle_input(self, data: dict):
        """Translate frontend mouse and keyboard events into Playwright actions on the active page."""
        active_page = self._get_active_page()
        if not active_page:
            return

        msg_type = data.get("type")
        
        try:
            if msg_type == "mouse":
                action = data.get("action")
                # Normalized coordinates (0.0 to 1.0)
                norm_x = float(data.get("x", 0))
                norm_y = float(data.get("y", 0))
                btn = data.get("button", "left")

                actual_x = max(0, min(VIEWPORT_WIDTH, norm_x * VIEWPORT_WIDTH))
                actual_y = max(0, min(VIEWPORT_HEIGHT, norm_y * VIEWPORT_HEIGHT))

                if action == "click":
                    await active_page.mouse.click(actual_x, actual_y, button=btn)
                elif action == "move":
                    await active_page.mouse.move(actual_x, actual_y)
                elif action == "down":
                    await active_page.mouse.down(button=btn)
                elif action == "up":
                    await active_page.mouse.up(button=btn)

            elif msg_type == "keyboard":
                action = data.get("action", "type")
                if action == "type":
                    text = data.get("text", "")
                    await active_page.keyboard.type(text)
                elif action == "press":
                    key = data.get("key", "")
                    await active_page.keyboard.press(key)
                elif action == "down":
                    key = data.get("key", "")
                    await active_page.keyboard.down(key)
                elif action == "up":
                    key = data.get("key", "")
                    await active_page.keyboard.up(key)

            elif msg_type == "scroll":
                dx = float(data.get("deltaX", 0))
                dy = float(data.get("deltaY", 0))
                await active_page.mouse.wheel(dx, dy)

            elif msg_type == "navigate":
                url = str(data.get("url", "")).strip()
                if url:
                    if not url.startswith(("http://", "https://")):
                        url = "https://" + url
                    await active_page.goto(url, wait_until="domcontentloaded", timeout=30000)

            elif msg_type == "reload":
                await active_page.reload(wait_until="domcontentloaded", timeout=30000)

            elif msg_type == "back":
                await active_page.go_back(wait_until="domcontentloaded", timeout=30000)

            elif msg_type == "forward":
                await active_page.go_forward(wait_until="domcontentloaded", timeout=30000)

            # Trigger immediate frame capture
            self._action_event.set()

        except Exception as e:
            logger.debug(f"[{self.session_id}] Error handling input: {e}")

    @staticmethod
    def _find_video_file_on_server(filename: str) -> Optional[str]:
        """Search common project/temp directories for a matching video filename."""
        if not filename:
            return None
        from pathlib import Path
        from yt_autosub.config import BASE_DIR, DATA_DIR
        safe_name = Path(filename).name
        search_roots = [DATA_DIR, BASE_DIR, Path("/tmp")]
        # Also check sibling frontend/upload directories if present
        sibling_fe = BASE_DIR.parent / "yt-autosub-fe"
        if sibling_fe.exists():
            search_roots.append(sibling_fe)
        for root in search_roots:
            if not root.exists():
                continue
            try:
                for match in root.rglob(safe_name):
                    if match.is_file():
                        return str(match)
            except Exception:
                continue
        return None

    async def scan_interrupted_uploads(self) -> list[dict]:
        """Scan YouTube Studio Content table rows for interrupted or in-progress uploads."""
        if not self.page or self.page.is_closed():
            raise RuntimeError("Browser page is not open")

        import re
        rows = await self.page.evaluate("""() => {
            const rowEls = Array.from(document.querySelectorAll('ytcp-video-row'));
            return rowEls.map((row, idx) => {
                const titleEl = row.querySelector('#video-title, a#video-title, .video-title-text');
                const title = titleEl ? titleEl.innerText.trim() : `Row #${idx + 1}`;
                const text = (row.innerText || '').trim();
                const resumeBtn = row.querySelector(
                    '#resume-upload-button, ytcp-button[id*="resume" i], button[aria-label*="Resume" i], button[aria-label*="Tiếp tục" i]'
                ) || Array.from(row.querySelectorAll('ytcp-button, button')).find(
                    b => /tiếp tục tải lên|resume upload/i.test(b.innerText || '')
                );
                const cancelBtn = row.querySelector('#cancel-upload-button') || Array.from(row.querySelectorAll('ytcp-button, button')).find(
                    b => /huỷ tải lên|hủy tải lên|cancel upload/i.test(b.innerText || '')
                );
                const isInterrupted = /bị gián đoạn|interrupted|resume upload|tiếp tục tải lên/i.test(text);
                const isUploading = /đã tải được|uploaded|uploading|đang tải lên/i.test(text) || Boolean(cancelBtn);
                return {
                    row_index: idx,
                    title,
                    text,
                    can_resume: Boolean(resumeBtn),
                    can_cancel: Boolean(cancelBtn),
                    is_interrupted: isInterrupted,
                    is_uploading: isUploading,
                };
            });
        }""")

        results = []
        for r in rows:
            if not (r.get("is_interrupted") or r.get("is_uploading") or r.get("can_resume")):
                continue
            row_text = r.get("text", "")
            fn_match = re.search(r"([A-Za-z0-9_.\-]+\.(?:mp4|mov|mkv|avi|webm|m4v))", row_text, re.IGNORECASE)
            expected_filename = fn_match.group(1) if fn_match else None
            matched_path = self._find_video_file_on_server(expected_filename) if expected_filename else None

            pct_match = re.search(r"(\d+\s*%)", row_text)
            results.append({
                "row_index": r["row_index"],
                "title": r["title"],
                "can_resume": r["can_resume"],
                "can_cancel": r["can_cancel"],
                "is_interrupted": r["is_interrupted"],
                "progress_hint": pct_match.group(1) if pct_match else ("Interrupted" if r["is_interrupted"] else "Uploading"),
                "expected_filename": expected_filename,
                "matched_path": matched_path,
                "snippet": " | ".join(line.strip() for line in row_text.splitlines() if line.strip())[:220],
            })
        return results

    async def resume_interrupted_upload(self, row_index: int, video_path: str) -> dict:
        """Click 'Resume upload' on the specified ytcp-video-row and feed the local video file via file chooser."""
        if not self.page or self.page.is_closed():
            raise RuntimeError("Browser page is not open")

        from pathlib import Path
        file_path = Path(video_path)
        if not file_path.is_file():
            raise FileNotFoundError(f"Video file not found on server: {video_path}")

        row = self.page.locator("ytcp-video-row").nth(row_index)
        if await row.count() == 0:
            raise ValueError(f"Row index {row_index} not found on current page")

        resume_btn = row.locator(
            "#resume-upload-button, ytcp-button[id*='resume' i], "
            "ytcp-button:has-text('Tiếp tục tải lên'), ytcp-button:has-text('Resume upload'), "
            "button:has-text('Tiếp tục tải lên'), button:has-text('Resume upload')"
        ).first

        await resume_btn.wait_for(state="visible", timeout=10000)
        await resume_btn.scroll_into_view_if_needed()

        async with self.page.expect_file_chooser(timeout=15000) as fc_info:
            await resume_btn.click()
        file_chooser = await fc_info.value
        await file_chooser.set_files(str(file_path.resolve()))

        self._action_event.set()
        logger.info(f"[{self.session_id}] Resumed interrupted upload on row {row_index} with {file_path}")
        return {"status": "resumed", "row_index": row_index, "video_path": str(file_path.resolve())}

    async def broadcast(self, message: dict):
        """Send JSON message to all connected WebSocket clients."""
        dead_clients = set()
        for ws in list(self.ws_clients):
            try:
                await ws.send_json(message)
            except Exception:
                dead_clients.add(ws)
        self.ws_clients.difference_update(dead_clients)

    async def close(self):
        """Tear down the browser context and clean up tasks."""
        if not self._running:
            return
        self._running = False
        self.status = "closed" if not self.is_authenticated else "authenticated"

        if self._stream_task:
            self._stream_task.cancel()
        if self._auth_task:
            self._auth_task.cancel()

        if self.context:
            logger.info(f"[{self.session_id}] Closing browser context...")
            await close_account_browser(self.context)
            self.context = None
            self.page = None

        if self.mode == "auth" and not self.is_authenticated:
            import shutil
            from yt_autosub.config import PROFILES_DIR, SESSIONS_DIR, sanitize_identifier
            clean_user = sanitize_identifier(self.user_id)
            clean_alias = sanitize_identifier(self.account_alias)
            profile_dir = PROFILES_DIR / clean_user / clean_alias
            session_file = SESSIONS_DIR / clean_user / f"{clean_alias}.json"
            try:
                if profile_dir.exists():
                    shutil.rmtree(profile_dir, ignore_errors=True)
                if session_file.exists():
                    session_file.unlink(missing_ok=True)
                logger.info(f"[{self.session_id}] Purged unauthenticated profile folder {profile_dir}")
            except Exception as e:
                logger.warning(f"[{self.session_id}] Could not purge profile dir: {e}")

class ActiveUploadSession:
    """Tracks a currently running upload browser so admins can inspect it live."""
    def __init__(self, upload_id: str, account_id: str, title: str, page: Page):
        self.upload_id = upload_id
        self.account_id = account_id
        self.title = title
        self.page = page
        self.progress = "Starting..."
        self.started_at = time.time()

class SessionManager:
    """Registry for active interactive streaming sessions and live upload browsers."""
    def __init__(self):
        self.sessions: Dict[str, StreamingAuthSession] = {}
        self.active_uploads: Dict[str, ActiveUploadSession] = {}

    def create_session(
        self,
        user_id: str,
        account_alias: str,
        proxy: Optional[ProxyConfig] = None,
        mode: str = "auth",
        target_url: Optional[str] = None,
    ) -> StreamingAuthSession:
        prefix = "diag" if mode == "diagnostic" else ("verify" if mode == "verify" else "sess")
        session_id = f"{prefix}_{uuid4().hex[:12]}"
        session = StreamingAuthSession(
            session_id=session_id,
            user_id=user_id,
            account_alias=account_alias,
            proxy=proxy,
            mode=mode,
            target_url=target_url,
        )
        self.sessions[session_id] = session
        return session

    async def handoff_for_verification(
        self,
        user_id: Optional[str],
        account_alias: Optional[str],
        context: BrowserContext,
        page: Page,
        proxy: Optional[ProxyConfig] = None,
    ) -> StreamingAuthSession:
        """Keep an upload browser open for the user to complete Google verification interactively."""
        session = self.create_session(
            user_id=user_id or "",
            account_alias=account_alias or "default",
            proxy=proxy,
            mode="verify",
        )
        await session.attach_existing_context(context, page)
        logger.info(f"[{session.session_id}] Handed off browser to user verification session (browser kept open).")
        return session

    def get_session(self, session_id: str) -> Optional[StreamingAuthSession]:
        return self.sessions.get(session_id)

    def register_upload(self, account_id: str, title: str, page: Page) -> str:
        upload_id = f"upl_{uuid4().hex[:12]}"
        self.active_uploads[upload_id] = ActiveUploadSession(upload_id, account_id, title, page)
        return upload_id

    def update_upload_progress(self, upload_id: str, progress: str) -> None:
        upl = self.active_uploads.get(upload_id)
        if upl:
            upl.progress = progress

    def unregister_upload(self, upload_id: str) -> None:
        self.active_uploads.pop(upload_id, None)

    def get_upload(self, upload_id: str) -> Optional[ActiveUploadSession]:
        return self.active_uploads.get(upload_id)

    async def cleanup_stale_sessions(self, max_age_seconds: int = 1800):
        """Remove closed/finished sessions; never auto-timeout active diagnostic or verify sessions."""
        now = time.time()
        stale_ids = [
            sid for sid, s in self.sessions.items()
            if s.status in ("closed", "authenticated", "error")
            or (s.mode not in ("diagnostic", "verify") and (now - s.created_at > max_age_seconds))
        ]
        for sid in stale_ids:
            s = self.sessions.pop(sid, None)
            if s:
                await s.close()

session_manager = SessionManager()
