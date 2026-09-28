import os
import re
from pathlib import Path
from typing import Optional
from pydantic import BaseModel, Field

# Base directories
BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
PROFILES_DIR = DATA_DIR / "profiles"
SESSIONS_DIR = DATA_DIR / "sessions"
LOGS_DIR = DATA_DIR / "logs"

# Server & Security Configuration
API_SECRET_KEY = os.getenv("YT_AUTOSUB_API_KEY", "")
SERVER_HOST = os.getenv("YT_AUTOSUB_HOST", "127.0.0.1")
SERVER_PORT = int(os.getenv("YT_AUTOSUB_PORT", "8000"))

def sanitize_identifier(text: str) -> str:
    """Sanitize user ID and account alias to avoid directory traversal or invalid chars."""
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", text)

def ensure_directories():
    """Ensure all required data directories exist."""
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

def get_channel_id(user_id: str, account_alias: str) -> str:
    """Generate unique composite channel ID for multi-tenant isolation."""
    clean_user = sanitize_identifier(user_id)
    clean_alias = sanitize_identifier(account_alias)
    return f"{clean_user}_{clean_alias}"

def get_user_profile_dir(user_id: str, account_alias: str) -> Path:
    """Get persistent browser profile directory scoped by user and account alias."""
    ensure_directories()
    clean_user = sanitize_identifier(user_id)
    clean_alias = sanitize_identifier(account_alias)
    p = PROFILES_DIR / clean_user / clean_alias
    p.mkdir(parents=True, exist_ok=True)
    return p

def get_user_session_path(user_id: str, account_alias: str) -> Path:
    """Get path to exported storage state / cookie JSON scoped by user."""
    ensure_directories()
    clean_user = sanitize_identifier(user_id)
    clean_alias = sanitize_identifier(account_alias)
    user_sess_dir = SESSIONS_DIR / clean_user
    user_sess_dir.mkdir(parents=True, exist_ok=True)
    return user_sess_dir / f"{clean_alias}.json"

def get_profile_dir(account_id: str) -> Path:
    """Legacy helper: get persistent browser profile directory for flat account_id."""
    ensure_directories()
    clean_id = sanitize_identifier(account_id)
    profile_path = PROFILES_DIR / clean_id
    profile_path.mkdir(parents=True, exist_ok=True)
    return profile_path

def get_session_path(account_id: str) -> Path:
    """Legacy helper: get session file path for flat account_id."""
    ensure_directories()
    clean_id = sanitize_identifier(account_id)
    return SESSIONS_DIR / f"{clean_id}.json"

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
