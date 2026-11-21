import asyncio
import json
from pathlib import Path
from typing import Optional, Any, Dict
from playwright.async_api import BrowserContext, Page
from camoufox.async_api import AsyncCamoufox
from loguru import logger

from yt_autosub.config import (
    MAX_CONCURRENT_BROWSERS,
    get_profile_dir,
    get_session_path,
    get_user_profile_dir,
    get_user_session_path,
    resolve_account_paths,
    ProxyConfig,
)

# Global browser slot & per-profile locks (scoped per event loop to stay test-safe)
_loop_semaphores: Dict[int, asyncio.Semaphore] = {}
_loop_profile_locks: Dict[int, Dict[str, asyncio.Lock]] = {}
_active_browser_count: int = 0


def _get_browser_semaphore() -> asyncio.Semaphore:
    loop_id = id(asyncio.get_running_loop())
    sem = _loop_semaphores.get(loop_id)
    if sem is None:
        sem = asyncio.Semaphore(MAX_CONCURRENT_BROWSERS)
        _loop_semaphores[loop_id] = sem
    return sem


def _get_profile_lock(profile_key: str) -> asyncio.Lock:
    loop_id = id(asyncio.get_running_loop())
    locks = _loop_profile_locks.setdefault(loop_id, {})
    lock = locks.get(profile_key)
    if lock is None:
        lock = asyncio.Lock()
        locks[profile_key] = lock
    return lock


def get_browser_pool_status() -> dict:
    return {
        "active_browsers": _active_browser_count,
        "max_concurrent_browsers": MAX_CONCURRENT_BROWSERS,
    }


async def launch_account_browser(
    account_id: Optional[str] = None,
    user_id: Optional[str] = None,
    account_alias: Optional[str] = None,
    headless: bool = False,
    proxy: Optional[ProxyConfig] = None,
    inject_cookies: bool = True,
    **kwargs: Any,
) -> BrowserContext:
    """
    Launch a dedicated Camoufox browser instance with global max-browser concurrency limit
    and per-profile locking to prevent Firefox profile corruption or system freezes.
    """
    global _active_browser_count
    profile_dir, session_file, log_label = resolve_account_paths(
        account_id=account_id,
        user_id=user_id,
        account_alias=account_alias,
    )
    profile_dir.mkdir(parents=True, exist_ok=True)
    profile_key = str(profile_dir.resolve())
    logger.info(f"[{log_label}] Resolved profile={profile_dir.name}, session={session_file.name} (exists={session_file.exists()})")

    profile_lock = _get_profile_lock(profile_key)
    sem = _get_browser_semaphore()

    if profile_lock.locked():
        logger.info(f"[{log_label}] Waiting for existing browser using profile {profile_dir.name} to finish...")
    await profile_lock.acquire()

    sem_acquired = False
    try:
        if _active_browser_count >= MAX_CONCURRENT_BROWSERS:
            logger.info(
                f"[{log_label}] Waiting for browser slot ({_active_browser_count}/{MAX_CONCURRENT_BROWSERS} active)..."
            )
        await sem.acquire()
        sem_acquired = True
        _active_browser_count += 1

        # Safe to clean up stale Firefox lock files now because profile_lock guarantees exclusive ownership
        for lock_name in ("lock", ".parentlock"):
            lock_file = profile_dir / lock_name
            if lock_file.exists():
                try:
                    lock_file.unlink(missing_ok=True)
                except Exception:
                    pass

        launch_opts: dict[str, Any] = {
            "persistent_context": True,
            "user_data_dir": str(profile_dir),
            "headless": headless,
            "enable_cache": True,
        }

        if proxy:
            launch_opts["proxy"] = proxy.to_playwright_proxy()
            logger.info(f"[{log_label}] Routing through proxy: {proxy.server}")

        launch_opts.update(kwargs)

        logger.info(
            f"[{log_label}] Launching Camoufox (headless={headless}, profile={profile_dir}, "
            f"browsers={_active_browser_count}/{MAX_CONCURRENT_BROWSERS})"
        )

        # AsyncCamoufox context manager starts playwright and yields the BrowserContext
        camoufox_cm = AsyncCamoufox(**launch_opts)
        context: BrowserContext = await camoufox_cm.__aenter__()

        # Store references on the context so close_account_browser releases everything cleanly
        context._camoufox_cm = camoufox_cm  # type: ignore
        context._browser_sem = sem  # type: ignore
        context._profile_lock = profile_lock  # type: ignore
        context._locks_released = False  # type: ignore

        # Inject saved cookies if available and requested
        if inject_cookies and session_file.exists():
            try:
                with open(session_file, "r", encoding="utf-8") as f:
                    state_data = json.load(f)
                    cookies = state_data.get("cookies", [])
                    if cookies:
                        sanitized = []
                        for c in cookies:
                            cookie = dict(c)
                            if "sameSite" in cookie and cookie["sameSite"] not in ("Strict", "Lax", "None"):
                                cookie["sameSite"] = "None"
                            if not cookie.get("secure", False) and cookie.get("sameSite") == "None":
                                cookie["sameSite"] = "Lax"
                            sanitized.append(cookie)
                        await context.add_cookies(sanitized)
                        logger.info(f"[{log_label}] Injected {len(sanitized)} cookies from {session_file.name}")
            except Exception as e:
                logger.warning(f"[{log_label}] Could not inject cookies from {session_file}: {e}")

        return context
    except BaseException:
        if sem_acquired:
            _active_browser_count = max(0, _active_browser_count - 1)
            sem.release()
        profile_lock.release()
        raise


async def close_account_browser(context: BrowserContext) -> None:
    """Cleanly close the browser context and release its global browser slot & profile lock."""
    global _active_browser_count
    try:
        if hasattr(context, "_camoufox_cm"):
            await context._camoufox_cm.__aexit__(None, None, None)
        else:
            await context.close()
    except Exception as e:
        logger.debug(f"Error during browser close: {e}")
    finally:
        if not getattr(context, "_locks_released", True):
            context._locks_released = True  # type: ignore
            if hasattr(context, "_browser_sem"):
                _active_browser_count = max(0, _active_browser_count - 1)
                context._browser_sem.release()  # type: ignore
            if hasattr(context, "_profile_lock") and context._profile_lock.locked():  # type: ignore
                context._profile_lock.release()  # type: ignore
