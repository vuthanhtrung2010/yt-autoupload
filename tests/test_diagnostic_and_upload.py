import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi.testclient import TestClient

from yt_autosub.server import app
from yt_autosub.uploader import wait_for_upload_transfer_complete


@pytest.mark.asyncio
async def test_wait_for_upload_transfer_waits_until_complete():
    page = MagicMock()
    # Simulate: 9% -> 51% -> Upload complete
    page.evaluate = AsyncMock(side_effect=[
        {"dialogOpen": True, "hasProgress": True, "labelText": "Đang tải lên 9% ...", "barValue": 9, "isUploadingAttr": True, "hasErrorAttr": False, "errText": ""},
        {"dialogOpen": True, "hasProgress": True, "labelText": "Đang tải lên 51% ...", "barValue": 51, "isUploadingAttr": True, "hasErrorAttr": False, "errText": ""},
        {"dialogOpen": True, "hasProgress": True, "labelText": "Đã tải lên xong ... Đang xử lý", "barValue": 100, "isUploadingAttr": False, "hasErrorAttr": False, "errText": ""},
    ])

    ok, err = await wait_for_upload_transfer_complete(page, account_id="test_acc", timeout_seconds=30, stall_timeout_seconds=20)
    assert ok is True
    assert err is None
    assert page.evaluate.await_count == 3


@pytest.mark.asyncio
async def test_wait_for_upload_transfer_detects_error():
    page = MagicMock()
    page.evaluate = AsyncMock(return_value={
        "dialogOpen": True,
        "hasProgress": True,
        "labelText": "Tải lên không thành công",
        "barValue": 35,
        "isUploadingAttr": False,
        "hasErrorAttr": True,
        "errText": "Daily upload limit reached",
    })

    ok, err = await wait_for_upload_transfer_complete(page, account_id="test_acc", timeout_seconds=30)
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
