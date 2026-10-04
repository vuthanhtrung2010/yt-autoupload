import pytest
from yt_autosub.models import parse_schedule_datetime, VideoUploadTask, Visibility
from yt_autosub.queue_manager import UploadQueueManager
from fastapi.testclient import TestClient
from yt_autosub.server import app

def test_parse_schedule_datetime():
    # ISO 8601
    date_str, time_str = parse_schedule_datetime("2026-10-25T18:00:00")
    assert date_str == "25/10/2026"
    assert time_str == "18:00"

    # Space separated format
    date_str, time_str = parse_schedule_datetime("2026-10-25 09:30")
    assert date_str == "25/10/2026"
    assert time_str == "09:30"

    # DD/MM/YYYY HH:MM
    date_str, time_str = parse_schedule_datetime("25/10/2026 14:15")
    assert date_str == "25/10/2026"
    assert time_str == "14:15"

    # Invalid datetime
    with pytest.raises(ValueError):
        parse_schedule_datetime("not-a-date")

def test_video_upload_task_validation():
    task = VideoUploadTask(
        user_id="user1",
        account_alias="acc1",
        video_path="README.md",  # existing file
        title="Scheduled Video",
        scheduled_publish_time="2026-10-25 18:00",
        set_as_premiere=True,
    )
    assert task.scheduled_publish_time == "2026-10-25 18:00"
    assert task.set_as_premiere is True

    # Invalid scheduled_publish_time should raise validation error
    with pytest.raises(Exception):
        VideoUploadTask(
            user_id="user1",
            account_alias="acc1",
            video_path="README.md",
            title="Invalid Scheduled Video",
            scheduled_publish_time="bad_time",
        )

def test_queue_schedule():
    manager = UploadQueueManager(concurrency=1)
    job = manager.create_job(
        job_id="sched_job_1",
        user_id="usr_sched",
        account_aliases=["acc1"],
        video_path="/tmp/fake.mp4",
        title="Schedule Test",
        scheduled_publish_time="2026-10-25 18:00",
        set_as_premiere=True,
    )
    assert job.scheduled_publish_time == "2026-10-25 18:00"
    assert job.set_as_premiere is True

def test_api_queue_with_schedule():
    client = TestClient(app)
    payload = {
        "job_id": "api_sched_job_1",
        "user_id": "usr_sched_api",
        "account_aliases": ["acc1"],
        "video_path": "/tmp/sched.mp4",
        "title": "Scheduled API Upload",
        "scheduled_publish_time": "2026-10-25 18:00",
        "set_as_premiere": True,
    }
    res = client.post("/api/v1/queue/upload", json=payload)
    assert res.status_code == 202
    data = res.json()
    assert data["job_id"] == "api_sched_job_1"

    status_res = client.get("/api/v1/queue/jobs/api_sched_job_1")
    assert status_res.status_code == 200
    status_data = status_res.json()
    assert status_data["scheduled_publish_time"] == "2026-10-25 18:00"
    assert status_data["set_as_premiere"] is True
