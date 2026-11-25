import asyncio
import re
import time
from pathlib import Path
from typing import Optional, Tuple
from loguru import logger
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from yt_autosub.config import LOGS_DIR, ProxyConfig, sanitize_identifier
from yt_autosub.browser import launch_account_browser, close_account_browser
from yt_autosub.models import VideoUploadTask, UploadResult
from yt_autosub.auth import check_is_logged_in
from yt_autosub.stream_manager import session_manager

STUDIO_URL = "https://studio.youtube.com"

_PROCESSING_OR_DONE_RE = re.compile(
    r"(upload complete|checks? complete|checking|processing|processed|"
    r"đã tải lên|tải lên hoàn tất|quá trình kiểm tra|đang kiểm tra|đang xử lý|đã xử lý|"
    r"comprobación|procesando|subida completa)",
    re.IGNORECASE,
)
_UPLOAD_ERROR_RE = re.compile(
    r"(upload failed|failed to upload|processing abandoned|daily limit|duplicate|"
    r"tải lên không thành công|lỗi tải lên|bị huỷ|vượt quá giới hạn)",
    re.IGNORECASE,
)


async def wait_for_upload_transfer_complete(
    page: Page,
    account_id: str,
    upload_id: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Wait inside ytcp-uploads-dialog until the video file transfer reaches 100%
    (upload complete / checks / processing started) or an explicit error occurs.
    Never artificially times out when timeout_seconds is None so large/slow uploads
    always complete instead of being aborted mid-transfer.
    """
    start_ts = time.monotonic()
    last_log_ts = start_ts
    last_label = ""

    while timeout_seconds is None or (time.monotonic() - start_ts) < timeout_seconds:
        if page.is_closed():
            return False, "Browser page was closed or killed unexpectedly"

        try:
            state = await page.evaluate("""() => {
                const dialog = document.querySelector('ytcp-uploads-dialog');
                if (!dialog) return { dialogOpen: false };

                const errEl = dialog.querySelector('.error-short, #error-message, ytcp-error-banner, .error-details');
                const errText = errEl && errEl.offsetParent !== null ? errEl.innerText.trim() : '';

                const progComp = dialog.querySelector('ytcp-video-upload-progress');
                if (!progComp) return { dialogOpen: true, hasProgress: false, errText };

                const labelEl = progComp.querySelector('.progress-label');
                const labelText = labelEl ? labelEl.innerText.trim() : progComp.innerText.trim();

                const bar = progComp.querySelector('tp-yt-paper-progress');
                const barValue = bar ? bar.getAttribute('value') : null;
                const isUploadingAttr = progComp.hasAttribute('uploading');
                const hasErrorAttr = progComp.hasAttribute('error');

                return {
                    dialogOpen: true,
                    hasProgress: true,
                    labelText,
                    barValue: barValue !== null ? Number(barValue) : null,
                    isUploadingAttr,
                    hasErrorAttr,
                    errText
                };
            }""")
        except Exception as e:
            return False, f"Browser page error while checking upload progress: {e}"

        if not state.get("dialogOpen"):
            return True, None

        err_text = state.get("errText") or ""
        label_text = state.get("labelText") or ""
        has_error = bool(state.get("hasErrorAttr")) or bool(err_text) or bool(_UPLOAD_ERROR_RE.search(label_text))

        if has_error:
            return False, f"YouTube reported upload error: {err_text or label_text}"

        now = time.monotonic()
        if label_text and (label_text != last_label or (now - last_log_ts) >= 60):
            logger.info(f"[{account_id}] Upload progress: {label_text}")
            if upload_id:
                session_manager.update_upload_progress(upload_id, label_text)
            last_label = label_text
            last_log_ts = now

        # Determine if file upload transfer is still in progress
        pct_match = re.search(r"(\d+)\s*%", label_text)
        pct = int(pct_match.group(1)) if pct_match else None
        is_processing_or_done = bool(_PROCESSING_OR_DONE_RE.search(label_text))
        is_uploading_attr = bool(state.get("isUploadingAttr"))
        bar_val = state.get("barValue")

        # File upload is done once processing/checks begin or 100% is reached without active uploading attr
        if is_processing_or_done and not is_uploading_attr:
            logger.info(f"[{account_id}] Video file transfer complete ({label_text}).")
            return True, None

        if pct is not None and pct >= 100 and not is_uploading_attr:
            logger.info(f"[{account_id}] Video file transfer reached 100% ({label_text}).")
            return True, None

        if not state.get("hasProgress"):
            # No progress widget means upload finished or wasn't needed
            return True, None

        if bar_val is not None and bar_val >= 100 and not is_uploading_attr and pct is None:
            return True, None

        await asyncio.sleep(3)

    return False, f"Upload timed out after {timeout_seconds}s (last status: '{last_label}')"

async def dismiss_popovers(page: Page) -> None:
    """Dismiss any floating onboarding tooltips or guide cards without canceling dialogs."""
    try:
        # If an accidental 'Cancel upload' confirmation dialog popped up, dismiss it
        cancel_dialog = page.locator(
            "[role='dialog']:has-text('Hủy tải lên'), [role='dialog']:has-text('Huỷ tải lên'), "
            "[role='dialog']:has-text('Cancel upload')"
        ).first
        if await cancel_dialog.count() > 0 and await cancel_dialog.is_visible():
            close_btn = cancel_dialog.locator("button:has-text('Đóng'), button:has-text('Cancel'), #secondary-action-button, button:has-text('Dismiss')").first
            if await close_btn.count() > 0 and await close_btn.is_visible():
                await close_btn.click()
                await asyncio.sleep(0.5)

        # Target specific popover / tip buttons
        popover_btns = page.locator(
            "ytcp-bubble-hint button, ytcp-popover button, "
            "ytcp-button#dismiss-button, button[aria-label*='Dismiss' i]"
        )
        count = await popover_btns.count()
        for idx in range(min(count, 5)):
            btn = popover_btns.nth(idx)
            if await btn.is_visible():
                await btn.click()
                await asyncio.sleep(0.3)
    except Exception:
        pass

async def trigger_upload_dialog(page: Page) -> bool:
    """Language-agnostic trigger for YouTube Studio upload dialog with proper element waiting."""
    await dismiss_popovers(page)
    
    # Wait for any of the primary upload triggers to mount and become visible
    trigger = page.locator(
        "#upload-icon, button#create-icon, ytcp-button#create-icon, "
        "button[aria-label*='Create' i], button[aria-label*='Tạo' i], "
        "ytcp-button:has(#upload-icon), button:has-text('Tải video lên'), button:has-text('Upload video')"
    ).first

    try:
        await trigger.wait_for(state="visible", timeout=30000)
    except Exception as e:
        logger.warning(f"Timeout waiting for upload trigger: {e}")
        return False

    logger.debug("Clicking upload trigger...")
    await trigger.click()
    await asyncio.sleep(1.5)

    # In case a dropdown opened (e.g. from Create button), click the first item (Upload video)
    menu_item = page.locator("tp-yt-paper-item, ytcp-text-menu-item").first
    if await menu_item.count() > 0 and await menu_item.is_visible():
        logger.debug("Clicking upload item in dropdown...")
        await menu_item.click()
        await asyncio.sleep(1.5)

    # Confirm dialog or file input is ready
    file_input = page.locator("input[type='file']").first
    try:
        await file_input.wait_for(state="attached", timeout=20000)
        return True
    except Exception:
        return False


async def select_playlists(page: Page, account_id: str, playlists: list[str]) -> None:
    """Open YouTube Studio's Playlists selector inside upload details and select matching playlists."""
    wanted = [p.strip() for p in playlists if p and p.strip()]
    if not wanted:
        return

    logger.info(f"[{account_id}] Selecting playlists: {wanted}")
    trigger = page.locator(
        "ytcp-video-metadata-playlists ytcp-text-dropdown-trigger, "
        "ytcp-video-metadata-playlists ytcp-dropdown-trigger, "
        "ytcp-video-metadata-playlists #playlists-dropdown, "
        "ytcp-video-metadata-playlists [role='button']"
    ).first

    if await trigger.count() == 0:
        logger.warning(f"[{account_id}] Playlist dropdown trigger not found in upload dialog")
        return

    await trigger.scroll_into_view_if_needed()
    await trigger.click()

    dialog = page.locator("ytcp-playlist-dialog").first
    try:
        await dialog.wait_for(state="visible", timeout=10000)
    except Exception as e:
        logger.warning(f"[{account_id}] Playlist dialog did not open: {e}")
        return

    await asyncio.sleep(0.8)

    async def _try_check_playlist(target_name: str) -> bool:
        target_lower = target_name.lower()
        items = dialog.locator("ytcp-playlist-item, li, ytcp-checkbox-lit, [role='checkbox']")
        count = await items.count()
        for i in range(count):
            item = items.nth(i)
            try:
                text = (await item.inner_text()).strip()
            except Exception:
                continue
            lines = [ln.strip().lower() for ln in text.splitlines() if ln.strip()]
            if not lines:
                continue
            if lines[0] == target_lower or text.lower() == target_lower:
                checkbox = item.locator("ytcp-checkbox-lit, [role='checkbox'], #checkbox").first
                target_el = checkbox if await checkbox.count() > 0 else item
                checked_attr = await target_el.get_attribute("aria-checked")
                if checked_attr != "true":
                    await target_el.scroll_into_view_if_needed()
                    await target_el.click()
                    await asyncio.sleep(0.3)
                return True
        return False

    search_input = dialog.locator("#search-input input, input[type='search'], input").first
    has_search = await search_input.count() > 0 and await search_input.is_visible()

    for name in wanted:
        found = await _try_check_playlist(name)
        if not found and has_search:
            try:
                await search_input.click(force=True)
                await search_input.fill(name)
                await asyncio.sleep(0.7)
                found = await _try_check_playlist(name)
                await search_input.fill("")
                await asyncio.sleep(0.4)
            except Exception:
                pass
        if not found:
            logger.warning(f"[{account_id}] Playlist '{name}' not found on this channel; skipping")

    done_btn = dialog.locator(
        ".done-button, ytcp-button.done-button, button:has-text('Done'), button:has-text('Xong')"
    ).first
    if await done_btn.count() > 0:
        await done_btn.click()
        await asyncio.sleep(0.6)

async def _ensure_visibility_selected(page: Page, account_id: str, visibility_value: str) -> bool:
    """Select visibility radio inside Step 4 and verify Polymer actually marked it checked."""
    vis_radio = page.locator(
        f"tp-yt-paper-radio-button[name='{visibility_value}'], [name='{visibility_value}']"
    ).first
    for attempt in range(4):
        await vis_radio.wait_for(state="visible", timeout=15000)
        await vis_radio.scroll_into_view_if_needed()
        is_checked = await vis_radio.evaluate(
            "el => el.getAttribute('aria-checked') === 'true' || el.hasAttribute('checked')"
        )
        if is_checked:
            return True
        logger.info(f"[{account_id}] Selecting visibility '{visibility_value}' (attempt {attempt + 1}/4)...")
        # Prefer clicking the inner radio container/label so Polymer tap handler reliably fires
        inner_target = vis_radio.locator("#radioContainer, #radioLabel").first
        if await inner_target.count() > 0 and await inner_target.is_visible():
            await inner_target.click()
        else:
            await vis_radio.click()
        await asyncio.sleep(0.8)

    return await vis_radio.evaluate(
        "el => el.getAttribute('aria-checked') === 'true' || el.hasAttribute('checked')"
    )


async def _is_video_still_draft(page: Page, video_url: Optional[str], title: str) -> bool:
    """Check Studio Content rows to confirm the uploaded video did NOT remain a Draft."""
    video_id = ""
    if video_url:
        m = re.search(r"(?:youtu\.be/|v=|/video/)([A-Za-z0-9_\-]{6,})", video_url)
        if m:
            video_id = m.group(1)

    try:
        return await page.evaluate(
            """([videoId, titleText]) => {
                const rows = Array.from(document.querySelectorAll('ytcp-video-row'));
                for (const row of rows) {
                    const rowText = (row.innerText || '').trim();
                    const link = videoId ? row.querySelector(`a[href*="${videoId}"]`) : null;
                    const titleEl = row.querySelector('#video-title, a#video-title');
                    const rowTitle = titleEl ? titleEl.innerText.trim() : '';
                    const matches = Boolean(link) || (titleText && rowTitle === titleText.trim());
                    if (!matches) continue;

                    const draftBtn = row.querySelector(
                        '#edit-draft-button, ytcp-button[id*="draft" i]'
                    ) || Array.from(row.querySelectorAll('ytcp-button, button')).find(
                        b => /chỉnh sửa bản nháp|edit draft/i.test(b.innerText || '')
                    );
                    if (draftBtn) return true;
                    if (/\\b(bản nháp|draft)\\b/i.test(rowText) && !/\\b(công khai|public|riêng tư|private|không công khai|unlisted|đã lên lịch|scheduled)\\b/i.test(rowText)) {
                        return true;
                    }
                    return false;
                }
                return false;
            }""",
            [video_id, title],
        )
    except Exception:
        return False


async def _check_identity_verification(page: Page) -> Optional[str]:
    """
    Detect Google's 'Verify your identity' / 'Xác minh danh tính của bạn' / re-auth prompt
    that blocks YouTube Studio uploads with a backdrop until user interacts.
    Returns prompt text if detected, else None.
    """
    if not page or page.is_closed():
        return None
    try:
        return await page.evaluate("""() => {
            const url = window.location.href || '';
            if (/accounts\\.google\\.com|myaccount\\.google\\.com/i.test(url)) {
                return 'Redirected to Google identity verification: ' + url;
            }
            const re = /xác minh danh tính|xác thực danh tính|verify your identity|verify it's you|confirm it's you|verifica tu identidad|vérifier votre identité/i;
            const dialogs = Array.from(document.querySelectorAll('ytcp-dialog, tp-yt-paper-dialog, [role="dialog"]'));
            for (const d of dialogs) {
                const st = getComputedStyle(d);
                const r = d.getBoundingClientRect();
                if (st.display === 'none' || st.visibility === 'hidden' || r.width === 0 || r.height === 0) continue;
                const txt = (d.innerText || '').trim();
                if (re.test(txt)) return txt.slice(0, 200);
            }
            return null;
        }""")
    except Exception:
        return None


async def _handoff_verification_result(
    task: VideoUploadTask,
    account_id: str,
    context,
    page: Page,
    proxy: Optional[ProxyConfig],
    prompt_text: str,
    video_url: Optional[str] = None,
) -> UploadResult:
    """Keep the browser open inside session_manager so the user can complete Google verification."""
    screenshot_path = LOGS_DIR / f"verify_required_{sanitize_identifier(account_id)}.png"
    try:
        await page.screenshot(path=str(screenshot_path))
    except Exception:
        pass

    sess = await session_manager.handoff_for_verification(
        user_id=task.user_id,
        account_alias=task.account_alias,
        context=context,
        page=page,
        proxy=proxy,
    )
    verify_url = f"/diagnostic?session={sess.session_id}"
    return UploadResult(
        account_id=account_id,
        video_path=task.video_path,
        title=task.title,
        success=False,
        video_url=video_url,
        verification_required=True,
        verification_session_id=sess.session_id,
        verification_url=verify_url,
        error_message=(
            f"Google account verification required ({prompt_text.splitlines()[0] if prompt_text else 'Verify identity'}). "
            f"Browser kept open — open {verify_url} (or /stream/{sess.session_id}) to verify."
        ),
    )


async def _find_and_click_publish_anyway(page: Page, account_id: str) -> bool:
    """
    Detect YouTube Studio's 'We're still checking your content' popup and click
    the 'Publish anyway' ('Vẫn xuất bản') action — NEVER '#confirm-button' ('Got it' / 'Đã hiểu'),
    because '#confirm-button' aborts publishing and keeps the video as a Draft/Private.
    """
    # 1. Explicit text / ID match across common Studio languages (prefer inner <button> when nested)
    explicit_pub_anyway = page.locator(
        "#publish-anyway-button, [id*='publish-anyway' i], "
        "button:has-text('Vẫn xuất bản'), button:has-text('Publish anyway'), "
        "button:has-text('Publicar de todos modos'), button:has-text('Publicar mesmo assim'), "
        "ytcp-button:has-text('Vẫn xuất bản'), ytcp-button:has-text('Publish anyway'), "
        "ytcp-button:has-text('Publicar de todos modos'), ytcp-button:has-text('Publicar mesmo assim'), "
        "ytcp-button:has-text('Publier quand même'), ytcp-button:has-text('Trotzdem veröffentlichen')"
    ).first
    if await explicit_pub_anyway.count() > 0 and await explicit_pub_anyway.is_visible():
        btn_text = (await explicit_pub_anyway.inner_text()).strip()
        logger.info(f"[{account_id}] Confirming still-checking prompt by clicking '{btn_text}'...")
        await explicit_pub_anyway.click(force=True)
        return True

    # 2. Language-agnostic + position:fixed-safe + Shadow-DOM-aware fallback:
    #    Note: position:fixed modals always have offsetParent === null, so we check getBoundingClientRect()!
    clicked_info = await page.evaluate("""() => {
        function isVisibleEl(el) {
            if (!el) return false;
            const st = getComputedStyle(el);
            if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
        }
        function collectAll(root, selector, out = []) {
            if (!root) return out;
            if (root.querySelectorAll) {
                for (const el of root.querySelectorAll(selector)) out.push(el);
            }
            const walk = root.querySelectorAll ? root.querySelectorAll('*') : [];
            for (const node of walk) {
                if (node.shadowRoot) collectAll(node.shadowRoot, selector, out);
            }
            return out;
        }
        const dialogs = collectAll(document, 'ytcp-prechecks-warning-dialog, ytcp-confirmation-dialog, tp-yt-paper-dialog, ytcp-dialog, [role="dialog"]');
        const pubRe = /vẫn xuất bản|publish anyway|publicar de todos modos|publicar mesmo assim|publier quand même|trotzdem veröffentlichen/i;

        for (const dlg of dialogs) {
            if (!isVisibleEl(dlg)) continue;
            const tag = (dlg.tagName || '').toLowerCase();
            if (tag === 'ytcp-uploads-dialog' || tag === 'ytcp-video-share-dialog' || tag === 'ytcp-uploads-still-processing-dialog') {
                continue;
            }
            const dlgText = (dlg.innerText || '').trim();
            // Skip if this is the main upload dialog
            if (dlg.querySelector && dlg.querySelector('ytcp-video-upload-progress')) continue;

            // Search buttons inside dialog (including shadow roots)
            const btns = collectAll(dlg, 'ytcp-button, button, [role="button"]');
            for (const b of btns) {
                if (!isVisibleEl(b)) continue;
                const txt = (b.innerText || b.getAttribute('aria-label') || '').trim();
                if (pubRe.test(txt)) {
                    b.click();
                    return { clicked: txt, mode: 'text' };
                }
            }
            // Fallback: secondary left button (#secondary-action-button) when #confirm-button is present
            const sec = dlg.querySelector('#secondary-action-button');
            const conf = dlg.querySelector('#confirm-button');
            if (sec && conf && isVisibleEl(sec)) {
                const innerBtn = sec.querySelector('button') || sec;
                innerBtn.click();
                return { clicked: (sec.innerText || 'secondary-action-button').trim(), mode: 'secondary' };
            }
        }
        return null;
    }""")
    if clicked_info:
        logger.info(f"[{account_id}] Confirming still-checking prompt ({clicked_info.get('mode')}): '{clicked_info.get('clicked')}'...")
        return True

    return False


async def _click_publish_and_wait_confirm(page: Page, account_id: str) -> Tuple[bool, Optional[str]]:
    """
    Click #done-button and wait for YouTube's publish RPC & confirmation dialog
    (ytcp-video-share-dialog or ytcp-uploads-still-processing-dialog).
    Never exits early solely because ytcp-uploads-dialog hid while the publish RPC is still in flight.
    """
    done_btn = page.locator("#done-button").first
    await done_btn.wait_for(state="visible", timeout=15000)

    is_enabled = False
    for _ in range(30):
        is_disabled = await done_btn.evaluate(
            "el => el.hasAttribute('disabled') || el.getAttribute('aria-disabled') === 'true'"
        )
        if not is_disabled:
            is_enabled = True
            break
        await asyncio.sleep(1)

    if not is_enabled:
        return False, "#done-button remained disabled (visibility or required checks incomplete)"

    for attempt in range(3):
        # Clear any still-open "Publish anyway" prompt before attempting another click on #done-button
        if not await _find_and_click_publish_anyway(page, account_id):
            logger.info(f"[{account_id}] Clicking publish / save button (attempt {attempt + 1}/3)...")
            await done_btn.scroll_into_view_if_needed()
            await done_btn.click()

        dialog_gone_seconds = 0
        # Wait up to 35s for YouTube's publish RPC to finish and show share/processing confirmation
        for _ in range(35):
            await asyncio.sleep(1)

            # Check for "Checks still running / Publish anyway" warning BEFORE checking confirm dialogs
            if await _find_and_click_publish_anyway(page, account_id):
                dialog_gone_seconds = 0
                await asyncio.sleep(2)
                continue

            confirm_dialog = page.locator(
                "ytcp-video-share-dialog, ytcp-uploads-still-processing-dialog"
            ).first
            if await confirm_dialog.count() > 0 and await confirm_dialog.is_visible():
                # Ensure this isn't a prechecks warning dialog containing Publish anyway
                has_pub_anyway = await confirm_dialog.locator(
                    "ytcp-button:has-text('Vẫn xuất bản'), ytcp-button:has-text('Publish anyway')"
                ).count()
                if has_pub_anyway > 0:
                    continue
                logger.info(f"[{account_id}] Publish confirmation dialog appeared.")
                # Close the share/confirmation dialog cleanly so Content table updates and trailing RPCs settle
                close_confirm = confirm_dialog.locator("#close-button, ytcp-button#close-button").first
                if await close_confirm.count() > 0 and await close_confirm.is_visible():
                    try:
                        await close_confirm.click()
                    except Exception:
                        pass
                await asyncio.sleep(2)
                return True, None

            upload_dialog = page.locator("ytcp-uploads-dialog").first
            if await upload_dialog.count() == 0 or not await upload_dialog.is_visible():
                # Do NOT exit immediately at 1s — YouTube hides ytcp-uploads-dialog for ~3-4s
                # while the publish RPC is in flight before ytcp-video-share-dialog mounts!
                dialog_gone_seconds += 1
                if dialog_gone_seconds >= 8:
                    logger.info(f"[{account_id}] Upload dialog closed and settled after {dialog_gone_seconds}s.")
                    return True, None
            else:
                dialog_gone_seconds = 0
                err_banner = upload_dialog.locator(".error-short, #error-message, ytcp-error-banner").first
                if await err_banner.count() > 0 and await err_banner.is_visible():
                    txt = (await err_banner.inner_text()).strip()
                    if txt:
                        return False, f"YouTube blocked publish: {txt}"

    return False, "Video remained in draft: publish/save confirmation was not detected"


async def upload_video(
    task: VideoUploadTask,
    headless: bool = True,
    proxy: Optional[ProxyConfig] = None,
    timeout_seconds: Optional[int] = None,
) -> UploadResult:
    """
    Perform complete browser automation to upload a video to YouTube Studio.
    Completely language-agnostic using stable element IDs and attributes.
    """
    account_id = task.account_id or (f"{task.user_id}_{task.account_alias}" if task.user_id else task.account_alias) or "default"
    log_label = f"{task.user_id}:{task.account_alias}" if (task.user_id and task.account_alias) else account_id
    logger.info(f"[{log_label}] Starting upload: '{task.title}' -> {task.video_path}")

    context = None
    page = None
    upload_id = None
    handed_off_for_verification = False
    video_url: Optional[str] = None

    try:
        context = await asyncio.wait_for(
            launch_account_browser(
                account_id=task.account_id,
                user_id=task.user_id,
                account_alias=task.account_alias,
                headless=headless,
                proxy=proxy,
                inject_cookies=True,
            ),
            timeout=60,
        )
        page = context.pages[0] if context.pages else await context.new_page()
        upload_id = session_manager.register_upload(account_id, task.title, page)

        # Navigate to Studio
        logger.info(f"[{log_label}] Navigating to YouTube Studio...")
        await page.goto(STUDIO_URL, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(2)

        # Verify authentication
        if not await check_is_logged_in(page):
            screenshot_path = LOGS_DIR / f"error_auth_{sanitize_identifier(account_id)}.png"
            await page.screenshot(path=str(screenshot_path))
            return UploadResult(
                account_id=account_id,
                video_path=task.video_path,
                title=task.title,
                success=False,
                error_message=f"Session not authenticated. Check {screenshot_path}",
            )

        # Trigger Upload Dialog
        opened = await trigger_upload_dialog(page)
        if not opened:
            screenshot_path = LOGS_DIR / f"error_trigger_{sanitize_identifier(account_id)}.png"
            await page.screenshot(path=str(screenshot_path))
            return UploadResult(
                account_id=account_id,
                video_path=task.video_path,
                title=task.title,
                success=False,
                error_message=f"Could not open upload dialog. Check {screenshot_path}",
            )

        # Wait for file input inside dialog
        logger.info(f"[{account_id}] Attaching video file...")
        file_input = page.locator("input[type='file']").first
        await file_input.wait_for(state="attached", timeout=20000)
        await file_input.set_input_files(task.video_path)

        # Wait for details form to appear
        logger.info(f"[{account_id}] Waiting for metadata form...")
        title_box = page.locator("#title-textarea #textbox").first
        await title_box.wait_for(state="visible", timeout=30000)
        await dismiss_popovers(page)

        # Fill Title
        logger.info(f"[{account_id}] Setting title: {task.title}")
        await title_box.click(force=True)
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
        await page.keyboard.type(task.title)

        # Fill Description
        if task.description:
            logger.info(f"[{account_id}] Setting description...")
            desc_box = page.locator("#description-textarea #textbox").first
            if await desc_box.count() > 0:
                await desc_box.click(force=True)
                await page.keyboard.type(task.description)

        # Upload Custom Thumbnail if provided
        if task.thumbnail_path:
            logger.info(f"[{account_id}] Uploading thumbnail: {task.thumbnail_path}")
            thumb_input = page.locator("input#file-loader[type='file'], input[type='file'][accept*='image']").first
            if await thumb_input.count() > 0:
                await thumb_input.set_input_files(task.thumbnail_path)

        # Select Playlists if provided
        if task.playlists:
            await select_playlists(page, account_id, task.playlists)

        # Check early if Google popped an identity verification modal (backdrop would block clicks)
        verify_prompt = await _check_identity_verification(page)
        if verify_prompt:
            handed_off_for_verification = True
            return await _handoff_verification_result(task, account_id, context, page, proxy, verify_prompt)

        # COPPA Audience Selection (Language-independent: name="VIDEO_MADE_FOR_KIDS_NOT_MFK")
        logger.info(f"[{account_id}] Selecting 'Not made for kids'...")
        not_for_kids = page.locator("[name='VIDEO_MADE_FOR_KIDS_NOT_MFK']").first
        await not_for_kids.scroll_into_view_if_needed()
        await not_for_kids.click()

        # Handle Tags (Advanced Options)
        if task.tags:
            logger.info(f"[{account_id}] Adding tags: {task.tags}")
            toggle_btn = page.locator("#toggle-button, ytcp-button#toggle-button").first
            if await toggle_btn.count() > 0:
                await toggle_btn.scroll_into_view_if_needed()
                await toggle_btn.click(force=True)
                await asyncio.sleep(1)

                tags_input = page.locator("#tags-container input, #text-input[aria-label*='tag' i]").first
                if await tags_input.count() > 0:
                    await tags_input.scroll_into_view_if_needed()
                    await tags_input.click(force=True)
                    await page.keyboard.type(",".join(task.tags) + ",")

        await dismiss_popovers(page)

        # Stepper: Advance through Video Elements -> Checks -> Visibility
        next_btn = page.locator("#next-button").first
        for step_idx in range(3):
            await next_btn.wait_for(state="visible", timeout=15000)
            logger.debug(f"[{account_id}] Advancing stepper step {step_idx + 1}...")
            await next_btn.click()
            await asyncio.sleep(1.5)
            await dismiss_popovers(page)

        # Visibility or Schedule Configuration
        if task.scheduled_publish_time:
            from yt_autosub.models import parse_schedule_datetime
            target_date, target_time = parse_schedule_datetime(task.scheduled_publish_time)
            logger.info(f"[{account_id}] Scheduling video release for {target_date} at {target_time} (premiere={task.set_as_premiere})...")

            # 1. Expand Native YouTube Studio Schedule card
            sched_card = page.locator("#second-container, [id*='schedule-toggle']").first
            await sched_card.wait_for(state="visible", timeout=15000)
            await sched_card.scroll_into_view_if_needed()
            await sched_card.click()
            await asyncio.sleep(1.5)

            # 2. Open datepicker dialog
            dp_trigger = page.locator("#datepicker-trigger").first
            await dp_trigger.wait_for(state="visible", timeout=15000)
            await dp_trigger.scroll_into_view_if_needed()
            await dp_trigger.click()
            await asyncio.sleep(1)

            # 3. Fill Date into ytcp-date-picker input
            dp_input = page.locator("ytcp-date-picker input").first
            await dp_input.wait_for(state="visible", timeout=10000)
            await dp_input.click(force=True)
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
            await page.keyboard.type(target_date)
            await page.keyboard.press("Enter")
            await asyncio.sleep(1)

            # 4. Fill Time into timepicker input
            tp_input = page.locator("ytcp-datetime-picker tp-yt-paper-input input, ytcp-time-of-day-picker input").first
            await tp_input.wait_for(state="visible", timeout=10000)
            await tp_input.click(force=True)
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
            await page.keyboard.type(target_time)
            await page.keyboard.press("Enter")
            await asyncio.sleep(1)

            # 5. Handle Premiere checkbox if requested
            if task.set_as_premiere:
                logger.info(f"[{account_id}] Enabling Premiere for scheduled release...")
                prem_checkbox = page.locator("#schedule-type-checkbox, ytcp-checkbox-lit#schedule-type-checkbox").first
                if await prem_checkbox.count() > 0:
                    aria_checked = await prem_checkbox.locator("#checkbox").first.get_attribute("aria-checked")
                    if aria_checked != "true":
                        await prem_checkbox.click()
                        await asyncio.sleep(0.5)

            logger.info(f"[{account_id}] Release schedule successfully set to {target_date} {target_time}")
        else:
            # Instant Visibility Selection (Language-independent: name="PUBLIC", "UNLISTED", "PRIVATE")
            logger.info(f"[{account_id}] Setting visibility to {task.visibility.value}...")
            await _ensure_visibility_selected(page, account_id, task.visibility.value)

        # Capture Video Link if present
        video_url = None
        try:
            link_el = page.locator("a.ytcp-video-info, a[href*='youtu.be']").first
            if await link_el.count() > 0:
                video_url = await link_el.get_attribute("href")
        except Exception:
            pass

        # Wait for the actual video file upload transfer to finish (100%) BEFORE clicking Save/Publish,
        # so closing the browser afterwards never aborts an in-progress upload!
        logger.info(f"[{account_id}] Ensuring video file transfer completes before publishing...")
        transfer_ok, transfer_err = await wait_for_upload_transfer_complete(
            page,
            account_id=account_id,
            upload_id=upload_id,
            timeout_seconds=timeout_seconds,
        )
        if not transfer_ok:
            screenshot_path = LOGS_DIR / f"upload_error_{sanitize_identifier(account_id)}.png"
            try:
                await page.screenshot(path=str(screenshot_path))
                logger.info(f"Saved failure screenshot to {screenshot_path}")
            except Exception:
                pass
            return UploadResult(
                account_id=account_id,
                video_path=task.video_path,
                title=task.title,
                success=False,
                video_url=video_url,
                error_message=transfer_err or "Video file upload did not complete",
            )

        # Re-verify instant visibility radio is actually checked (fail explicitly if not checked)
        if not task.scheduled_publish_time:
            vis_ok = await _ensure_visibility_selected(page, account_id, task.visibility.value)
            if not vis_ok:
                screenshot_path = LOGS_DIR / f"upload_visibility_error_{sanitize_identifier(account_id)}.png"
                try:
                    await page.screenshot(path=str(screenshot_path))
                    logger.info(f"Saved visibility-failure screenshot to {screenshot_path}")
                except Exception:
                    pass
                return UploadResult(
                    account_id=account_id,
                    video_path=task.video_path,
                    title=task.title,
                    success=False,
                    video_url=video_url,
                    error_message=f"Could not select visibility '{task.visibility.value}' on Step 4",
                )

        # Only dismiss non-confirmation bubble hints before clicking #done-button
        try:
            bubble_btns = page.locator("ytcp-bubble-hint button, ytcp-popover button")
            for idx in range(min(await bubble_btns.count(), 3)):
                btn = bubble_btns.nth(idx)
                if await btn.is_visible():
                    await btn.click()
                    await asyncio.sleep(0.3)
        except Exception:
            pass

        # Click #done-button and wait for YouTube's publish RPC & confirmation dialog
        published_confirmed, publish_err = await _click_publish_and_wait_confirm(page, account_id)

        # Ground-truth check on Studio Content: ensure the video row is NOT still a Draft!
        if published_confirmed and await _is_video_still_draft(page, video_url, task.title):
            logger.warning(f"[{account_id}] Video '{task.title}' is still marked as Draft on Studio Content! Retrying draft publish once...")
            recovered = False
            try:
                draft_btn = page.locator(
                    "ytcp-video-row:has(#video-title:text-is(" + repr(task.title) + ")) #edit-draft-button"
                ).first
                if await draft_btn.count() > 0 and await draft_btn.is_visible():
                    await draft_btn.click()
                    await asyncio.sleep(2)
                    nb = page.locator("#next-button").first
                    for _ in range(3):
                        if await nb.count() > 0 and await nb.is_visible():
                            await nb.click()
                            await asyncio.sleep(1.5)
                    if not task.scheduled_publish_time:
                        await _ensure_visibility_selected(page, account_id, task.visibility.value)
                    recovered, publish_err = await _click_publish_and_wait_confirm(page, account_id)
                    if recovered and await _is_video_still_draft(page, video_url, task.title):
                        recovered = False
                        publish_err = "Video remained in draft even after retry"
            except Exception as rec_err:
                publish_err = f"Draft recovery failed: {rec_err}"
            published_confirmed = recovered

        if not published_confirmed:
            screenshot_path = LOGS_DIR / f"upload_draft_error_{sanitize_identifier(account_id)}.png"
            try:
                await page.screenshot(path=str(screenshot_path))
                logger.info(f"Saved draft-failure screenshot to {screenshot_path}")
            except Exception:
                pass
            return UploadResult(
                account_id=account_id,
                video_path=task.video_path,
                title=task.title,
                success=False,
                video_url=video_url,
                error_message=publish_err or "Video remained in draft: publish/save confirmation was not detected",
            )

        # Dismiss final dialog if present
        await dismiss_popovers(page)

        logger.info(f"[{account_id}] Successfully published/scheduled: {task.title} (URL: {video_url or 'N/A'})")
        return UploadResult(
            account_id=account_id,
            video_path=task.video_path,
            title=task.title,
            success=True,
            video_url=video_url,
        )

    except asyncio.TimeoutError:
        if page and context:
            verify_prompt = await _check_identity_verification(page)
            if verify_prompt:
                handed_off_for_verification = True
                return await _handoff_verification_result(task, account_id, context, page, proxy, verify_prompt, video_url=video_url)
        logger.error(f"[{account_id}] Upload timed out waiting for browser operation")
        return UploadResult(
            account_id=account_id,
            video_path=task.video_path,
            title=task.title,
            success=False,
            video_url=video_url,
            error_message="Browser operation timed out",
        )
    except Exception as e:
        if page and context:
            verify_prompt = await _check_identity_verification(page)
            if verify_prompt:
                handed_off_for_verification = True
                return await _handoff_verification_result(task, account_id, context, page, proxy, verify_prompt, video_url=video_url)
        logger.exception(f"[{account_id}] Failed upload: {e}")
        if page and not page.is_closed():
            screenshot_path = LOGS_DIR / f"upload_error_{sanitize_identifier(account_id)}.png"
            try:
                await page.screenshot(path=str(screenshot_path))
                logger.info(f"Saved failure screenshot to {screenshot_path}")
            except Exception:
                pass
        return UploadResult(
            account_id=account_id,
            video_path=task.video_path,
            title=task.title,
            success=False,
            video_url=video_url,
            error_message=str(e),
        )
    finally:
        if upload_id:
            session_manager.unregister_upload(upload_id)
        if context and not handed_off_for_verification:
            await close_account_browser(context)
