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
    timeout_seconds: int = 1800,
    stall_timeout_seconds: int = 240,
) -> Tuple[bool, Optional[str]]:
    """
    Wait inside ytcp-uploads-dialog until the video file transfer reaches 100%
    (upload complete / checks / processing started) or an error occurs.
    Prevents closing the browser mid-upload.
    """
    start_ts = time.monotonic()
    last_progress_ts = start_ts
    last_label = ""

    while (time.monotonic() - start_ts) < timeout_seconds:
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

        if label_text and label_text != last_label:
            logger.info(f"[{account_id}] Upload progress: {label_text}")
            if upload_id:
                session_manager.update_upload_progress(upload_id, label_text)
            last_label = label_text
            last_progress_ts = time.monotonic()

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

        if (time.monotonic() - last_progress_ts) > stall_timeout_seconds:
            return False, f"Upload stalled for {stall_timeout_seconds}s at: '{label_text}'"

        await asyncio.sleep(3)

    return False, f"Upload timed out after {timeout_seconds}s (last status: '{last_label}')"

async def dismiss_popovers(page: Page) -> None:
    """Dismiss any floating onboarding tooltips or guide cards without canceling dialogs."""
    try:
        # If an accidental 'Cancel upload' confirmation dialog popped up, dismiss it
        cancel_dialog = page.locator("ytcp-confirmation-dialog, [role='dialog']:has-text('Hủy')").first
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

async def upload_video(
    task: VideoUploadTask,
    headless: bool = True,
    proxy: Optional[ProxyConfig] = None,
    timeout_seconds: int = 1800,
) -> UploadResult:
    """
    Perform complete browser automation to upload a video to YouTube Studio.
    Completely language-agnostic using stable element IDs and attributes.
    """
    account_id = task.account_id or (f"{task.user_id}_{task.account_alias}" if task.user_id else task.account_alias) or "default"
    log_label = f"{task.user_id}:{task.account_alias}" if (task.user_id and task.account_alias) else account_id
    logger.info(f"[{log_label}] Starting upload: '{task.title}' -> {task.video_path}")

    context = await launch_account_browser(
        account_id=task.account_id,
        user_id=task.user_id,
        account_alias=task.account_alias,
        headless=headless,
        proxy=proxy,
        inject_cookies=True,
    )
    page = context.pages[0] if context.pages else await context.new_page()
    upload_id = session_manager.register_upload(account_id, task.title, page)

    try:
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
            vis_radio = page.locator(f"[name='{task.visibility.value}']").first
            await vis_radio.wait_for(state="visible", timeout=15000)
            await vis_radio.scroll_into_view_if_needed()
            await vis_radio.click()

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

        # Publish / Save Button (#done-button)
        logger.info(f"[{account_id}] Clicking publish / save button...")
        done_btn = page.locator("#done-button").first
        await done_btn.wait_for(state="visible", timeout=15000)
        await done_btn.click()

        # Wait for confirmation dialog (e.g. Video published/scheduled) or modal close
        logger.info(f"[{account_id}] Waiting for publish/schedule confirmation...")
        confirm_dialog = page.locator("ytcp-video-share-dialog, ytcp-uploads-still-processing-dialog").first
        try:
            await confirm_dialog.wait_for(state="visible", timeout=20000)
        except Exception:
            await asyncio.sleep(5)

        # Dismiss final dialog if present
        await dismiss_popovers(page)

        logger.info(f"[{account_id}] Successfully uploaded: {task.title} (URL: {video_url or 'N/A'})")
        return UploadResult(
            account_id=account_id,
            video_path=task.video_path,
            title=task.title,
            success=True,
            video_url=video_url,
        )

    except Exception as e:
        logger.exception(f"[{account_id}] Failed upload: {e}")
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
            error_message=str(e),
        )
    finally:
        session_manager.unregister_upload(upload_id)
        await close_account_browser(context)
