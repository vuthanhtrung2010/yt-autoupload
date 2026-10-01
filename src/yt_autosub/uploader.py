import asyncio
from pathlib import Path
from typing import Optional
from loguru import logger
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

from yt_autosub.config import LOGS_DIR, ProxyConfig, sanitize_identifier
from yt_autosub.browser import launch_account_browser, close_account_browser
from yt_autosub.models import VideoUploadTask, UploadResult
from yt_autosub.auth import check_is_logged_in

STUDIO_URL = "https://studio.youtube.com"

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
    timeout_seconds: int = 300,
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

    try:
        # Navigate to Studio
        logger.info(f"[{log_label}] Navigating to YouTube Studio...")
        await page.goto(STUDIO_URL, wait_until="networkidle", timeout=60000)
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

        # Visibility Selection (Language-independent: name="PUBLIC", "UNLISTED", "PRIVATE")
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

        # Publish / Save Button (#done-button)
        logger.info(f"[{account_id}] Clicking publish / save button...")
        done_btn = page.locator("#done-button").first
        await done_btn.wait_for(state="visible", timeout=15000)
        await done_btn.click()

        # Wait for dialog completion
        logger.info(f"[{account_id}] Waiting for upload processing confirmation...")
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
        await close_account_browser(context)
