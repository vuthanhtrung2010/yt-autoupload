import asyncio
from pathlib import Path
from typing import Optional
from loguru import logger
from rich.console import Console
from rich.table import Table

from yt_autosub.config import get_session_path, LOGS_DIR, ProxyConfig
from yt_autosub.browser import launch_account_browser, close_account_browser
from yt_autosub.auth import check_is_logged_in, STUDIO_URL

console = Console()

async def inspect_studio_dom(
    account_id: str,
    headless: bool = True,
    proxy: Optional[ProxyConfig] = None,
    action: str = "overview",
) -> None:
    """
    Inspect YouTube Studio DOM using Playwright to discover exact web structure,
    button selectors, dialogs, and inputs.
    
    Actions:
    - 'overview': Dumps overview of Studio home, navigation, create button.
    - 'open-upload': Clicks the Create -> Upload dialog and inspects modal elements.
    - 'dump-html': Saves full page HTML to data/logs/studio_{account_id}.html.
    """
    session_file = get_session_path(account_id)
    if not session_file.exists():
        console.print(f"[bold red]Error:[/bold red] No session file found at {session_file}. Run 'auth' first.")
        return

    context = await launch_account_browser(
        account_id=account_id,
        headless=headless,
        proxy=proxy,
        inject_cookies=True,
    )
    page = context.pages[0] if context.pages else await context.new_page()

    try:
        console.print(f"[bold cyan]Navigating to {STUDIO_URL}...[/bold cyan]")
        await page.goto(STUDIO_URL, wait_until="networkidle", timeout=60000)
        await asyncio.sleep(3)

        if not await check_is_logged_in(page):
            console.print(f"[bold red]Not logged in![/bold red] Current URL: {page.url}")
            return

        console.print(f"[bold green]Successfully loaded Studio![/bold green] URL: {page.url}")

        screenshot_path = LOGS_DIR / f"inspect_{account_id}_home.png"
        await page.screenshot(path=str(screenshot_path))
        console.print(f"Saved home screenshot: [dim]{screenshot_path}[/dim]")

        # 1. Overview of main control elements
        table = Table(title="YouTube Studio Key Selectors & Status")
        table.add_column("Element / Target", style="cyan")
        table.add_column("Candidate Selector", style="magenta")
        table.add_column("Found?", style="green")
        table.add_column("Details / Text", style="yellow")

        selectors_to_check = [
            ("Create Button", "#create-icon"),
            ("Create Button (Alt 1)", "button[aria-label*='Create' i]"),
            ("Create Button (Alt 2)", "ytcp-button#create-icon"),
            ("Upload Icon (Home)", "#upload-icon"),
            ("Channel Title", "#channel-title"),
            ("Avatar Button", "#avatar-btn"),
            ("Navigation Drawer", "ytcp-navigation-drawer"),
            ("Content Nav Item", "#menu-item-link-content"),
        ]

        for label, sel in selectors_to_check:
            loc = page.locator(sel)
            count = await loc.count()
            if count > 0:
                text = (await loc.first.inner_text()).strip().replace("\n", " ")[:40]
                table.add_row(label, sel, f"✓ (count={count})", text or "[No text]")
            else:
                table.add_row(label, sel, "✗", "-")

        console.print(table)

        if action in ("open-upload", "all"):
            console.print("\n[bold cyan]Attempting to trigger Upload modal...[/bold cyan]")
            
            # Find and click create button
            clicked = False
            for sel in ["#create-icon", "button[aria-label*='Create' i]", "ytcp-button#create-icon"]:
                loc = page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    console.print(f"Clicking create button via: [magenta]{sel}[/magenta]")
                    await loc.click()
                    clicked = True
                    break

            if clicked:
                await asyncio.sleep(2)
                # Check for upload menu items
                menu_items = page.locator("tp-yt-paper-item, ytcp-text-menu-item")
                m_count = await menu_items.count()
                console.print(f"Detected [bold]{m_count}[/bold] dropdown menu items after clicking Create.")
                
                for idx in range(m_count):
                    item = menu_items.nth(idx)
                    txt = (await item.inner_text()).strip().replace("\n", " ")
                    console.print(f"  Menu Item #{idx}: [yellow]{txt}[/yellow]")

                # Click Upload videos item
                upload_item = page.locator("tp-yt-paper-item:has-text('Upload video'), ytcp-text-menu-item:has-text('Upload video')").first
                if await upload_item.count() > 0:
                    console.print("Clicking 'Upload videos' menu item...")
                    await upload_item.click()
                    await asyncio.sleep(3)

                    # Now inspect upload dialog
                    upload_dialog = page.locator("ytcp-uploads-dialog, [role='dialog']:has-text('Upload')")
                    if await upload_dialog.count() > 0:
                        console.print("[bold green]✓ Upload Dialog Opened Successfully![/bold green]")
                        dialog_screenshot = LOGS_DIR / f"inspect_{account_id}_upload_dialog.png"
                        await page.screenshot(path=str(dialog_screenshot))
                        console.print(f"Saved dialog screenshot: [dim]{dialog_screenshot}[/dim]")

                        # Check file input
                        file_input = page.locator("input[type='file']")
                        f_count = await file_input.count()
                        console.print(f"File inputs found in dialog: [bold]{f_count}[/bold]")

        if action in ("dump-html", "all"):
            html_path = LOGS_DIR / f"studio_{account_id}.html"
            content = await page.content()
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(content)
            console.print(f"\n[bold green]Saved full page HTML to [dim]{html_path}[/dim][/bold green]")

    except Exception as e:
        logger.exception(f"Error inspecting DOM: {e}")
    finally:
        await close_account_browser(context)
