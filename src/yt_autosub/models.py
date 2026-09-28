from enum import Enum
from pathlib import Path
from typing import Optional, List
from pydantic import BaseModel, Field, field_validator

class Visibility(str, Enum):
    PRIVATE = "PRIVATE"
    UNLISTED = "UNLISTED"
    PUBLIC = "PUBLIC"

class VideoUploadTask(BaseModel):
    account_id: str = Field(..., description="Unique account ID matching data/profiles and data/sessions")
    video_path: str = Field(..., description="Path to video file (.mp4, .mov, etc.)")
    title: str = Field(..., description="Video title (up to 100 characters)")
    description: str = Field(default="", description="Video description text")
    tags: List[str] = Field(default_factory=list, description="List of search tags")
    thumbnail_path: Optional[str] = Field(default=None, description="Path to custom thumbnail image")
    visibility: Visibility = Field(default=Visibility.PRIVATE, description="Visibility: PRIVATE, UNLISTED, or PUBLIC")

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

class UploadManifest(BaseModel):
    uploads: List[VideoUploadTask] = Field(default_factory=list, description="List of upload tasks")

class UploadResult(BaseModel):
    account_id: str
    video_path: str
    title: str
    success: bool
    video_url: Optional[str] = None
    error_message: Optional[str] = None
