import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi.testclient import TestClient

from yt_autosub.server import app
from yt_autosub.uploader import wait_for_upload_transfer_complete


@pytest.mark.asyncio
async def test_wait_for_upload_transfer_waits_until_complete():
    page = MagicMock()
    page.is_closed.return_value = False
    # Simulate: 9% -> 51% -> Upload complete
    page.evaluate = AsyncMock(side_effect=[
        {"dialogOpen": True, "hasProgress": True, "labelText": "Đang tải lên 9% ...", "barValue": 9, "isUploadingAttr": True, "hasErrorAttr": False, "errText": ""},
        {"dialogOpen": True, "hasProgress": True, "labelText": "Đang tải lên 51% ...", "barValue": 51, "isUploadingAttr": True, "hasErrorAttr": False, "errText": ""},
        {"dialogOpen": True, "hasProgress": True, "labelText": "Đã tải lên xong ... Đang xử lý", "barValue": 100, "isUploadingAttr": False, "hasErrorAttr": False, "errText": ""},
    ])

    ok, err = await wait_for_upload_transfer_complete(page, account_id="test_acc")
    assert ok is True
    assert err is None
    assert page.evaluate.await_count == 3


@pytest.mark.asyncio
async def test_wait_for_upload_transfer_detects_error():
    page = MagicMock()
    page.is_closed.return_value = False
    page.evaluate = AsyncMock(return_value={
        "dialogOpen": True,
        "hasProgress": True,
        "labelText": "Tải lên không thành công",
        "barValue": 35,
        "isUploadingAttr": False,
        "hasErrorAttr": True,
        "errText": "Daily upload limit reached",
    })

    ok, err = await wait_for_upload_transfer_complete(page, account_id="test_acc")
    assert ok is False
    assert "Daily upload limit reached" in err


def test_diagnostic_endpoints():
    client = TestClient(app)

    page_res = client.get("/diagnostic")
    assert page_res.status_code == 200
    assert "Admin Browser Diagnostic Console" in page_res.text

    acc_res = client.get("/api/v1/diagnostic/accounts")
    assert acc_res.status_code == 200
    assert "accounts" in acc_res.json()

    sess_res = client.get("/api/v1/diagnostic/sessions")
    assert sess_res.status_code == 200
    data = sess_res.json()
    assert "sessions" in data
    assert "active_uploads" in data

    logs_res = client.get("/api/v1/diagnostic/logs")
    assert logs_res.status_code == 200
    assert "screenshots" in logs_res.json()

    # Unknown session should return 404
    scan_404 = client.post("/api/v1/diagnostic/session/nonexistent/scan-interrupted")
    assert scan_404.status_code == 404

    resume_404 = client.post(
        "/api/v1/diagnostic/session/nonexistent/resume-upload",
        json={"row_index": 0, "video_path": "README.md"},
    )
    assert resume_404.status_code == 404


@pytest.mark.asyncio
async def test_scan_interrupted_uploads_parses_filename():
    from yt_autosub.stream_manager import StreamingAuthSession

    sess = StreamingAuthSession("diag_test", "usr_1", "acc1", mode="diagnostic")
    sess.page = MagicMock()
    sess.page.is_closed.return_value = False
    sess.page.evaluate = AsyncMock(return_value=[
        {
            "row_index": 2,
            "title": "¿Y si Freud tenía razón?",
            "text": "Quá trình tải lên bị gián đoạn\nNhấp vào \"Tiếp tục tải lên\" rồi chọn video_1795257522442.mp4 để tải tệp lên",
            "can_resume": True,
            "can_cancel": False,
            "is_interrupted": True,
            "is_uploading": False,
        }
    ])

    items = await sess.scan_interrupted_uploads()
    assert len(items) == 1
    assert items[0]["row_index"] == 2
    assert items[0]["can_resume"] is True
    assert items[0]["expected_filename"] == "video_1795257522442.mp4"


@pytest.mark.asyncio
async def test_queue_validation_error_marks_job_failed():
    import asyncio
    from yt_autosub.queue_manager import UploadQueueManager

    mgr = UploadQueueManager(concurrency=1)
    mgr.start_workers()
    try:
        job = mgr.create_job(
            job_id="bad_file_job",
            user_id="usr_1",
            account_aliases=["acc1"],
            video_path="/tmp/definitely_does_not_exist_123456.mp4",
            title="Missing Video Test",
        )
        await asyncio.wait_for(mgr.queue.join(), timeout=5)
        assert job.status == "failed"
        assert job.tasks[0].status == "failed"
        assert job.tasks[0].error_message is not None
    finally:
        mgr._running = False
        for w in mgr.workers:
            w.cancel()


@pytest.mark.asyncio
async def test_is_video_still_draft_helper():
    from yt_autosub.uploader import _is_video_still_draft

    page = MagicMock()
    page.evaluate = AsyncMock(return_value=True)
    assert await _is_video_still_draft(page, "https://youtu.be/S85e81BD4X4", "El escándalo de la cocaína") is True

    page.evaluate = AsyncMock(return_value=False)
    assert await _is_video_still_draft(page, "https://youtu.be/S85e81BD4X4", "El escándalo de la cocaína") is False


@pytest.mark.asyncio
async def test_find_and_click_publish_anyway_clicks_secondary_not_confirm():
    from yt_autosub.uploader import _find_and_click_publish_anyway

    page = MagicMock()
    btn_mock = MagicMock()
    btn_mock.count = AsyncMock(return_value=1)
    btn_mock.is_visible = AsyncMock(return_value=True)
    btn_mock.inner_text = AsyncMock(return_value="Vẫn xuất bản")
    btn_mock.click = AsyncMock()

    loc_mock = MagicMock()
    loc_mock.first = btn_mock
    page.locator.return_value = loc_mock

    assert await _find_and_click_publish_anyway(page, "acc1") is True
    btn_mock.click.assert_awaited_once()
    selector_used = page.locator.call_args[0][0]
    assert "#confirm-button" not in selector_used


@pytest.mark.asyncio
async def test_find_and_click_publish_anyway_uses_fixed_safe_fallback():
    from yt_autosub.uploader import _find_and_click_publish_anyway

    page = MagicMock()
    btn_mock = MagicMock()
    btn_mock.count = AsyncMock(return_value=0)
    loc_mock = MagicMock()
    loc_mock.first = btn_mock
    page.locator.return_value = loc_mock
    page.evaluate = AsyncMock(return_value={"clicked": "Vẫn xuất bản", "mode": "secondary"})

    assert await _find_and_click_publish_anyway(page, "acc1") is True
    js_arg = page.evaluate.call_args[0][0]
    assert "offsetParent" not in js_arg
    assert "getBoundingClientRect" in js_arg


@pytest.mark.asyncio
async def test_handoff_for_verification_keeps_browser_open_and_marks_job():
    from yt_autosub.queue_manager import UploadQueueManager
    from yt_autosub.stream_manager import session_manager
    from yt_autosub.models import UploadResult
    from unittest.mock import patch

    ctx = MagicMock()
    page = MagicMock()
    page.is_closed.return_value = False
    page.set_viewport_size = AsyncMock()
    page.url = "https://studio.youtube.com/channel/UC123"

    sess = await session_manager.handoff_for_verification("usr_v", "acc1", ctx, page)
    try:
        assert sess.mode == "verify"
        assert sess.status == "needs_verification"
        assert sess.session_id.startswith("verify_")

        # Cleanup watcher tasks without purging/failing
        mgr = UploadQueueManager(concurrency=1)
        mgr.start_workers()
        try:
            mock_res = UploadResult(
                account_id="usr_v_acc1",
                video_path="/tmp/v.mp4",
                title="Verify Test",
                success=False,
                verification_required=True,
                verification_session_id=sess.session_id,
                verification_url=f"/diagnostic?session={sess.session_id}",
                error_message="Google account verification required",
            )
            with patch("yt_autosub.queue_manager.VideoUploadTask") as task_cls, \
                 patch("yt_autosub.queue_manager.upload_video", new=AsyncMock(return_value=mock_res)):
                task_cls.return_value = MagicMock()
                job = mgr.create_job(
                    job_id="verify_job_1",
                    user_id="usr_v",
                    account_aliases=["acc1"],
                    video_path="/tmp/v.mp4",
                    title="Verify Test",
                )
                import asyncio
                await asyncio.wait_for(mgr.queue.join(), timeout=5)
                assert job.status == "needs_verification"
                assert job.tasks[0].status == "needs_verification"
                assert job.tasks[0].verification_session_id == sess.session_id
        finally:
            mgr._running = False
            for w in mgr.workers:
                w.cancel()
    finally:
        sess._running = False
        if sess._stream_task:
            sess._stream_task.cancel()
        if sess._auth_task:
            sess._auth_task.cancel()
        session_manager.sessions.pop(sess.session_id, None)
