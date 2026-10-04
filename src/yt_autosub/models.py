from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional, List, Any, Tuple
from pydantic import BaseModel, Field, field_validator

def parse_schedule_datetime(val: str) -> Tuple[str, str]:
    """
    Parse a datetime string into (date_str, time_str) for YouTube Studio.
    date_str is formatted as 'DD/MM/YYYY'.
    time_str is formatted as 'HH:MM'.
    """
    clean = val.strip()
    dt = None
    try:
        dt = datetime.fromisoformat(clean.replace("Z", "+00:00"))
    except Exception:
        pass
    if dt is None:
        formats = [
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d %H:%M",
            "%d/%m/%Y %H:%M:%S",
            "%d/%m/%Y %H:%M",
            "%d-%m-%Y %H:%M:%S",
            "%d-%m-%Y %H:%M",
            "%m/%d/%Y %H:%M",
        ]
        for fmt in formats:
            try:
                dt = datetime.strptime(clean, fmt)
                break
            except Exception:
                continue
    if dt is None:
        raise ValueError(
            f"Could not parse scheduled datetime '{val}'. Expected ISO 8601 (e.g. '2026-10-25T18:00:00') "
            f"or format like 'YYYY-MM-DD HH:MM'."
        )
    return dt.strftime("%d/%m/%Y"), dt.strftime("%H:%M")

class Visibility(str, Enum):
    PRIVATE = "PRIVATE"
    UNLISTED = "UNLISTED"
    PUBLIC = "PUBLIC"

class VideoUploadTask(BaseModel):
    account_id: Optional[str] = Field(default=None, description="Unique account ID matching data/profiles and data/sessions")
    user_id: Optional[str] = Field(default=None, description="User ID for multi-tenant isolation")
    account_alias: Optional[str] = Field(default=None, description="Account identifier within tenant")
    video_path: str = Field(..., description="Path to video file (.mp4, .mov, etc.)")
    title: str = Field(..., description="Video title (up to 100 characters)")
    description: str = Field(default="", description="Video description text")
    tags: List[str] = Field(default_factory=list, description="List of search tags")
    thumbnail_path: Optional[str] = Field(default=None, description="Path to custom thumbnail image")
    visibility: Visibility = Field(default=Visibility.PRIVATE, description="Visibility: PRIVATE, UNLISTED, or PUBLIC")
    scheduled_publish_time: Optional[str] = Field(
        default=None,
        description="ISO 8601 or standard datetime string (e.g. '2026-10-25T18:00:00' or '2026-10-25 18:00') to schedule video release"
    )
    set_as_premiere: bool = Field(
        default=False,
        description="Whether to set the scheduled video as a Premiere (Công chiếu)"
    )

    def model_post_init(self, __context: Any) -> None:
        if not self.account_id:
            if self.user_id and self.account_alias:
                from yt_autosub.config import get_channel_id
                self.account_id = get_channel_id(self.user_id, self.account_alias)
            elif self.account_alias:
                self.account_id = self.account_alias
            else:
                self.account_id = "default"

    @field_validator("title")
    @classmethod
    def validate_title(cls, v: str) -> str:
        if len(v.strip()) == 0:
            raise ValueError("Title cannot be empty")
        return v[:100]  # Cap at YouTube's 100 char limit

    @field_validator("video_path")
    @classmethod
    def validate_video_exists(cls, v: str) -> str:
        path = Path(v).expanduser().resolve()
        if not path.exists():
            raise ValueError(f"Video file not found: {v}")
        return str(path)

    @field_validator("thumbnail_path")
    @classmethod
    def validate_thumbnail(cls, v: Optional[str]) -> Optional[str]:
        if v:
            path = Path(v).expanduser().resolve()
            if not path.exists():
                raise ValueError(f"Thumbnail file not found: {v}")
            return str(path)
        return None

    @field_validator("scheduled_publish_time")
    @classmethod
    def validate_scheduled_publish_time(cls, v: Optional[str]) -> Optional[str]:
        if v:
            parse_schedule_datetime(v)
            return v.strip()
        return None

class UploadManifest(BaseModel):
    uploads: List[VideoUploadTask] = Field(default_factory=list, description="List of upload tasks")

class UploadResult(BaseModel):
    account_id: str
    video_path: str
    title: str
    success: bool
    video_url: Optional[str] = None
    error_message: Optional[str] = None
