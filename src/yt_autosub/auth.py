import asyncio
import json
from pathlib import Path
from typing import Optional, Tuple
from loguru import logger
from rich.console import Console
from rich.panel import Panel

from yt_autosub.config import get_session_path, get_profile_dir, resolve_account_paths, ProxyConfig
from yt_autosub.browser import launch_account_browser, close_account_browser

console = Console()

STUDIO_URL = "https://studio.youtube.com"

async def check_is_logged_in(page) -> bool:
    """Check if the page has reached YouTube Studio dashboard."""
    current_url = page.url.lower()
    
    # Direct redirect to sign-in indicates not logged in
    if "accounts.google.com" in current_url or "servicelogin" in current_url:
        return False
        
    # If URL contains /channel/, user is definitely authenticated inside Studio
    if "studio.youtube.com/channel/" in current_url:
        return True

    # Check if we are in studio dashboard
    if "studio.youtube.com" in current_url:
        try:
            selectors = [
                "#avatar-btn",
                "ytcp-app",
                "ytcp-navigation-drawer",
                "#channel-title",
                "button[aria-label*='Create' i]",
                "button[aria-label*='Tạo' i]",
                "button:has-text('Tạo')",
                "button:has-text('Create')",
                "#create-icon"
            ]
            for sel in selectors:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    return True
        except Exception:
            pass
            
    return False

async def interactive_login(
    account_id: str,
    proxy: Optional[ProxyConfig] = None,
    timeout_minutes: int = 10,
) -> bool:
    """
    Launch Camoufox in headful GUI mode for the user to authenticate manually.
    Once authenticated, captures session storage and cookies.
    """
    session_file = get_session_path(account_id)
    profile_dir = get_profile_dir(account_id)

    console.print(Panel.fit(
        f"[bold cyan]Interactive Login Setup[/bold cyan]\n"
        f"Account ID: [yellow]{account_id}[/yellow]\n"
        f"Profile Dir: [dim]{profile_dir}[/dim]\n"
        f"Session File: [dim]{session_file}[/dim]\n\n"
        "Camoufox browser will open now.\n"
        "Please complete Google sign-in and any 2FA/verification in the browser.\n"
        "The script will automatically detect once you are inside YouTube Studio!",
        title="Account Setup"
    ))

    context = await launch_account_browser(
        account_id=account_id,
        headless=False,
        proxy=proxy,
        inject_cookies=True,
    )

    page = context.pages[0] if context.pages else await context.new_page()

    try:
        logger.info(f"[{account_id}] Navigating to {STUDIO_URL}...")
        await page.goto(STUDIO_URL, wait_until="domcontentloaded", timeout=60000)

        console.print("\n[bold green]Browser is open.[/bold green] Waiting for login completion...")
        console.print("[dim](Auto-detecting studio dashboard, or press Ctrl+C to cancel)[/dim]\n")

        # Watcher loop
        poll_interval = 2.0
        max_polls = int((timeout_minutes * 60) / poll_interval)
        logged_in = False

        for i in range(max_polls):
            if await check_is_logged_in(page):
                logged_in = True
                break
            await asyncio.sleep(poll_interval)

        if not logged_in:
            console.print("[yellow]Auto-detection timed out. Checking final state...[/yellow]")
            logged_in = await check_is_logged_in(page)

        if logged_in:
            # Let cookies settle for 2 seconds
            await asyncio.sleep(2)
            
            # Export full storage state (cookies + origins)
            await context.storage_state(path=str(session_file))
            
            with open(session_file, "r", encoding="utf-8") as f:
                state_data = json.load(f)
                cookie_count = len(state_data.get("cookies", []))
                
            console.print(Panel.fit(
                f"[bold green]✓ Login Successful![/bold green]\n"
                f"Exported [bold cyan]{cookie_count}[/bold cyan] cookies to [dim]{session_file}[/dim]\n"
                f"Persistent profile is saved in [dim]{profile_dir}[/dim]",
                title="Success"
            ))
            return True
        else:
            console.print(Panel.fit(
                f"[bold red]✗ Login could not be confirmed.[/bold red]\n"
                f"Final URL: {page.url}",
                title="Failed"
            ))
            return False

    except Exception as e:
        logger.exception(f"[{account_id}] Error during interactive login: {e}")
        return False
    finally:
        logger.info(f"[{account_id}] Closing browser...")
        await close_account_browser(context)

async def test_session(
    account_id: Optional[str] = None,
    user_id: Optional[str] = None,
    account_alias: Optional[str] = None,
    headless: bool = True,
    proxy: Optional[ProxyConfig] = None,
) -> Tuple[bool, str]:
    """
    Test whether a saved session is still valid by navigating to YouTube Studio headless.
    """
    profile_dir, session_file, log_label = resolve_account_paths(
        account_id=account_id,
        user_id=user_id,
        account_alias=account_alias,
    )
    if not session_file.exists():
        return False, f"No session file found at {session_file}. Run 'auth' first."

    context = await launch_account_browser(
        account_id=account_id,
        user_id=user_id,
        account_alias=account_alias,
        headless=headless,
        proxy=proxy,
        inject_cookies=True,
    )
    page = context.pages[0] if context.pages else await context.new_page()

    try:
        logger.info(f"[{log_label}] Testing session at {STUDIO_URL}...")
        await page.goto(STUDIO_URL, wait_until="networkidle", timeout=60000)
        
        # Poll up to 10 seconds for web components to load
        is_logged = False
        for _ in range(10):
            if await check_is_logged_in(page):
                is_logged = True
                break
            await asyncio.sleep(1)

        if is_logged:
            # Save any refreshed cookies back
            await context.storage_state(path=str(session_file))
            return True, f"Session active! Current URL: {page.url}"
        else:
            return False, f"Session invalid/expired. Current URL: {page.url}"
    except Exception as e:
        return False, f"Error verifying session: {e}"
    finally:
        await close_account_browser(context)
