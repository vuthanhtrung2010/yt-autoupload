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
