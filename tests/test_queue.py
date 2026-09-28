import pytest
import asyncio
from fastapi.testclient import TestClient
from yt_autosub.queue_manager import UploadQueueManager, QueueJob, QueueChannelTask
from yt_autosub.server import app

@pytest.mark.asyncio
async def test_queue_enqueue_and_get():
    manager = UploadQueueManager(concurrency=1)
    job = manager.create_job(
        job_id="test_job_1",
        user_id="usr_test",
        account_aliases=["acc1", "acc2"],
        video_path="/tmp/fake.mp4",
        title="Test Title",
        description="Test Desc",
        tags=["test"],
        thumbnail_path=None,
        visibility="PRIVATE",
    )
    assert job.job_id == "test_job_1"
    assert job.status == "queued"
    assert len(job.tasks) == 2
    assert job.tasks[0].account_alias == "acc1"
    assert job.tasks[1].account_alias == "acc2"
    
    retrieved = manager.get_job("test_job_1")
    assert retrieved is not None
    assert retrieved.job_id == "test_job_1"

def test_api_queue_endpoints():
    client = TestClient(app)
    payload = {
        "job_id": "test_api_job_123",
        "user_id": "usr_999",
        "account_aliases": ["channel_a"],
        "video_path": "/tmp/test_video.mp4",
        "title": "API Test Video",
        "description": "Uploaded via test",
        "tags": ["test"],
        "thumbnail_path": None,
        "visibility": "PRIVATE"
    }
    res = client.post("/api/v1/queue/upload", json=payload)
    assert res.status_code == 202
    data = res.json()
    assert data["job_id"] == "test_api_job_123"
    assert data["status"] == "queued"
    assert data["total_channels"] == 1

    status_res = client.get("/api/v1/queue/jobs/test_api_job_123")
    assert status_res.status_code == 200
    status_data = status_res.json()
    assert status_data["job_id"] == "test_api_job_123"
    assert len(status_data["tasks"]) == 1
    assert status_data["tasks"][0]["account_alias"] == "channel_a"
