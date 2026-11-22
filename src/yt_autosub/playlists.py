import asyncio
import re
from typing import List, Dict, Optional
from loguru import logger

from yt_autosub.auth import check_is_logged_in, STUDIO_URL
from yt_autosub.browser import launch_account_browser, close_account_browser
from yt_autosub.config import ProxyConfig


async def list_channel_playlists(
    user_id: str,
    account_alias: str,
    headless: bool = True,
    proxy: Optional[ProxyConfig] = None,
) -> List[Dict[str, str]]:
    """
    Open YouTube Studio Playlists page for a connected channel and extract playlist IDs & titles.
    """
    log_label = f"{user_id}:{account_alias}"
    logger.info(f"[{log_label}] Fetching YouTube Studio playlists...")

    context = None
    try:
        context = await asyncio.wait_for(
            launch_account_browser(
                user_id=user_id,
                account_alias=account_alias,
                headless=headless,
                proxy=proxy,
                inject_cookies=True,
            ),
            timeout=60,
        )
        page = context.pages[0] if context.pages else await context.new_page()

        await page.goto(STUDIO_URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(2.5)

        if not await check_is_logged_in(page):
            raise RuntimeError("Channel session is not authenticated in YouTube Studio")

        current_url = page.url
        match = re.search(r"studio\.youtube\.com/channel/([^/?#]+)", current_url)
        if match:
            channel_id = match.group(1)
            playlists_url = f"https://studio.youtube.com/channel/{channel_id}/playlists"
            await page.goto(playlists_url, wait_until="domcontentloaded", timeout=60000)
        else:
            nav_link = page.locator(
                "a[href*='/playlists'], #menu-item-link-playlists, #menu-item-2"
            ).first
            if await nav_link.count() > 0:
                await nav_link.click()
            else:
                raise RuntimeError("Could not locate Playlists navigation in YouTube Studio")

        # Wait for playlist rows or empty state
        try:
            await page.locator(
                "ytcp-playlist-row, ytcp-playlists-table, #row-container, ytcp-playlist-section, .empty-state"
            ).first.wait_for(state="attached", timeout=15000)
        except Exception:
            pass

        await asyncio.sleep(2.5)

        raw_playlists = await page.evaluate(
            """() => {
                const seen = new Set();
                const results = [];

                const addItem = (id, title) => {
                    const cleanTitle = (title || '').replace(/\\s+/g, ' ').trim();
                    if (!cleanTitle) return;
                    const key = cleanTitle.toLowerCase();
                    if (seen.has(key)) return;
                    seen.add(key);
                    results.push({ id: id || cleanTitle, title: cleanTitle });
                };

                const extractId = (href) => {
                    if (!href) return '';
                    const m1 = href.match(/\\/playlist\\/([^/?#]+)/);
                    if (m1) return m1[1];
                    const m2 = href.match(/[?&]list=([^&#]+)/);
                    if (m2) return m2[1];
                    return '';
                };

                const rows = document.querySelectorAll(
                    'ytcp-playlist-row, ytcp-playlists-table #row-container, ytcp-playlist-list-row'
                );
                for (const row of rows) {
                    const link = row.querySelector('a[href*="/playlist/"], a[href*="list="]');
                    const titleEl = row.querySelector(
                        '#playlist-title, a#playlist-title, .playlist-title-text, #video-title, a[href*="/playlist/"], h3'
                    );
                    const title = titleEl ? titleEl.innerText : (link ? link.innerText : '');
                    const id = extractId(link ? link.getAttribute('href') : '');
                    addItem(id, title);
                }

                if (results.length === 0) {
                    const links = document.querySelectorAll('a[href*="/playlist/"], a[href*="list="]');
                    for (const a of links) {
                        const txt = (a.innerText || '').trim();
                        if (txt && txt.length < 200) {
                            addItem(extractId(a.getAttribute('href')), txt);
                        }
                    }
                }

                return results;
            }"""
        )

        logger.info(f"[{log_label}] Found {len(raw_playlists)} playlist(s).")
        return raw_playlists
    finally:
        if context:
            await close_account_browser(context)
