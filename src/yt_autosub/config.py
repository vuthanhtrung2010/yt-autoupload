import os
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field

# Base directories
BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
PROFILES_DIR = DATA_DIR / "profiles"
SESSIONS_DIR = DATA_DIR / "sessions"
LOGS_DIR = DATA_DIR / "logs"

def ensure_directories():
    """Ensure all required data directories exist."""
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

def get_profile_dir(account_id: str) -> Path:
    """Get persistent browser profile directory for an account."""
    ensure_directories()
    profile_path = PROFILES_DIR / account_id
    profile_path.mkdir(parents=True, exist_ok=True)
    return profile_path

def get_session_path(account_id: str) -> Path:
    """Get path to exported storage state / cookie JSON for an account."""
    ensure_directories()
    return SESSIONS_DIR / f"{account_id}.json"

class ProxyConfig(BaseModel):
    server: str = Field(..., description="Proxy URL (e.g., http://ip:port or socks5://ip:port)")
    username: Optional[str] = None
    password: Optional[str] = None

    def to_playwright_proxy(self) -> dict:
        proxy_dict = {"server": self.server}
        if self.username:
            proxy_dict["username"] = self.username
        if self.password:
            proxy_dict["password"] = self.password
        return proxy_dict
