import json
from pathlib import Path
from typing import Optional, Any
from playwright.async_api import BrowserContext, Page
from camoufox.async_api import AsyncCamoufox
from loguru import logger

from yt_autosub.config import get_profile_dir, get_session_path, ProxyConfig

async def launch_account_browser(
    account_id: str,
    headless: bool = False,
    proxy: Optional[ProxyConfig] = None,
    inject_cookies: bool = True,
    **kwargs: Any,
) -> BrowserContext:
    """
    Launch a dedicated Camoufox browser instance for a specific account.
    Uses a persistent profile directory to preserve browser fingerprints,
    device tokens, and session data.
    """
    profile_dir = get_profile_dir(account_id)
    session_file = get_session_path(account_id)

    # Clean up stale Firefox lock files if any
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
        logger.info(f"[{account_id}] Routing through proxy: {proxy.server}")

    launch_opts.update(kwargs)

    logger.info(f"[{account_id}] Launching Camoufox (headless={headless}, profile={profile_dir})")
    
    # AsyncCamoufox context manager starts playwright and yields the BrowserContext
    camoufox_cm = AsyncCamoufox(**launch_opts)
    context: BrowserContext = await camoufox_cm.__aenter__()
    
    # Store the context manager reference on the context so it can be cleanly closed
    context._camoufox_cm = camoufox_cm  # type: ignore

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
                    logger.info(f"[{account_id}] Injected {len(sanitized)} cookies from {session_file.name}")
        except Exception as e:
            logger.warning(f"[{account_id}] Could not inject cookies from {session_file}: {e}")

    return context

async def close_account_browser(context: BrowserContext) -> None:
    """Cleanly close the browser context and underlying Camoufox/Playwright process."""
    try:
        if hasattr(context, "_camoufox_cm"):
            await context._camoufox_cm.__aexit__(None, None, None)
        else:
            await context.close()
    except Exception as e:
        logger.debug(f"Error during browser close: {e}")
