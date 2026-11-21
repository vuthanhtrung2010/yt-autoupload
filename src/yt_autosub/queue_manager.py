import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Dict, Optional, Any
from loguru import logger
from yt_autosub.models import VideoUploadTask, Visibility
from yt_autosub.config import get_channel_id, ProxyConfig, MAX_QUEUE_WORKERS
from yt_autosub.uploader import upload_video

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

@dataclass
class QueueChannelTask:
    account_alias: str
    status: str = "queued" # queued, uploading, completed, failed
    video_url: Optional[str] = None
    error_message: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

@dataclass
class QueueJob:
    job_id: str
    user_id: str
    video_path: str
    title: str
    description: str
    tags: List[str]
    thumbnail_path: Optional[str]
    visibility: str
    scheduled_publish_time: Optional[str] = None
    set_as_premiere: bool = False
    proxy: Optional[ProxyConfig] = None
    status: str = "queued" # queued, processing, completed, partial_failed, failed
    tasks: List[QueueChannelTask] = field(default_factory=list)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "user_id": self.user_id,
            "status": self.status,
            "title": self.title,
            "visibility": self.visibility,
            "scheduled_publish_time": self.scheduled_publish_time,
            "set_as_premiere": self.set_as_premiere,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "tasks": [
                {
                    "account_alias": t.account_alias,
                    "status": t.status,
                    "video_url": t.video_url,
                    "error_message": t.error_message,
                    "started_at": t.started_at.isoformat() if t.started_at else None,
                    "completed_at": t.completed_at.isoformat() if t.completed_at else None,
                }
                for t in self.tasks
            ]
        }

class UploadQueueManager:
    _instance = None

    def __init__(self, concurrency: Optional[int] = None):
        self.concurrency = max(1, concurrency if concurrency is not None else MAX_QUEUE_WORKERS)
        self.queue: asyncio.Queue = asyncio.Queue()
        self.jobs: Dict[str, QueueJob] = {}
        self.workers: List[asyncio.Task] = []
        self._running = False

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = UploadQueueManager()
        return cls._instance

    def start_workers(self):
        if not self._running:
            self._running = True
            for i in range(self.concurrency):
                task = asyncio.create_task(self._worker_loop(i))
                self.workers.append(task)
            logger.info(f"Started {self.concurrency} upload queue worker(s).")

    def create_job(
        self,
        job_id: str,
        user_id: str,
        account_aliases: List[str],
        video_path: str,
        title: str,
        description: str = "",
        tags: Optional[List[str]] = None,
        thumbnail_path: Optional[str] = None,
        visibility: str = "PRIVATE",
        scheduled_publish_time: Optional[str] = None,
        set_as_premiere: bool = False,
        proxy: Optional[ProxyConfig] = None,
    ) -> QueueJob:
        tasks = [QueueChannelTask(account_alias=alias) for alias in account_aliases]
        job = QueueJob(
            job_id=job_id,
            user_id=user_id,
            video_path=video_path,
            title=title,
            description=description,
            tags=tags or [],
            thumbnail_path=thumbnail_path,
            visibility=visibility,
            scheduled_publish_time=scheduled_publish_time,
            set_as_premiere=set_as_premiere,
            proxy=proxy,
            tasks=tasks,
        )
        self.jobs[job_id] = job
        for channel_task in tasks:
            self.queue.put_nowait((job, channel_task))
        return job

    def get_job(self, job_id: str) -> Optional[QueueJob]:
        return self.jobs.get(job_id)

    def delete_job(self, job_id: str) -> bool:
        return self.jobs.pop(job_id, None) is not None

    def list_jobs(self, user_id: Optional[str] = None) -> List[QueueJob]:
        if user_id:
            return [j for j in self.jobs.values() if j.user_id == user_id]
        return list(self.jobs.values())

    async def _worker_loop(self, worker_id: int):
        logger.info(f"Upload worker {worker_id} started.")
        while self._running:
            try:
                job, channel_task = await self.queue.get()
                if job.job_id not in self.jobs:
                    self.queue.task_done()
                    continue

                job.status = "processing"
                job.updated_at = utc_now()
                channel_task.status = "uploading"
                channel_task.started_at = utc_now()

                try:
                    vis = Visibility.PRIVATE
                    try:
                        vis = Visibility(job.visibility.upper())
                    except Exception:
                        pass

                    task = VideoUploadTask(
                        user_id=job.user_id,
                        account_alias=channel_task.account_alias,
                        account_id=get_channel_id(job.user_id, channel_task.account_alias),
                        video_path=job.video_path,
                        title=job.title,
                        description=job.description,
                        tags=job.tags,
                        thumbnail_path=job.thumbnail_path,
                        visibility=vis,
                        scheduled_publish_time=job.scheduled_publish_time,
                        set_as_premiere=job.set_as_premiere,
                    )

                    result = await upload_video(task=task, headless=True, proxy=job.proxy)
                    if result.success:
                        channel_task.status = "completed"
                        channel_task.video_url = result.video_url
                    else:
                        channel_task.status = "failed"
                        channel_task.error_message = result.error_message or "Upload failed"
                except asyncio.CancelledError:
                    logger.warning(f"Worker {worker_id} cancelled while uploading for {channel_task.account_alias}")
                    channel_task.status = "failed"
                    channel_task.error_message = "Upload cancelled or interrupted"
                    raise
                except Exception as e:
                    logger.exception(f"Worker {worker_id} error uploading for {channel_task.account_alias}: {e}")
                    channel_task.status = "failed"
                    channel_task.error_message = str(e)
                finally:
                    if channel_task.status == "uploading":
                        channel_task.status = "failed"
                        channel_task.error_message = channel_task.error_message or "Upload terminated unexpectedly"
                    channel_task.completed_at = utc_now()
                    self.queue.task_done()
                    self._update_job_status(job)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker error in loop: {e}")

    def _update_job_status(self, job: QueueJob):
        statuses = [t.status for t in job.tasks]
        if not statuses:
            job.status = "failed"
        elif all(s in ("completed", "failed") for s in statuses):
            if all(s == "completed" for s in statuses):
                job.status = "completed"
            elif any(s == "completed" for s in statuses):
                job.status = "partial_failed"
            else:
                job.status = "failed"
        elif any(s == "uploading" for s in statuses):
            job.status = "processing"
        job.updated_at = utc_now()
