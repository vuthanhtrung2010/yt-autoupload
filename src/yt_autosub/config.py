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

def resolve_account_paths(
    account_id: Optional[str] = None,
    user_id: Optional[str] = None,
    account_alias: Optional[str] = None,
) -> tuple[Path, Path, str]:
    """
    Intelligently resolves the profile directory, session file path, and log label.
    Supports:
    1. Direct multi-tenancy: user_id + account_alias -> data/profiles/{user_id}/{account_alias} & data/sessions/{user_id}/{account_alias}.json
       - Falls back to root data/sessions/{account_alias}.json if user-scoped session does not exist yet root does.
    2. Composite account_id: e.g. "usr_123_acc1" or "6AnkfOE3XqE87i8OxBfYYTywysLahTIi_acc1"
       - Automatically splits into (user_id, account_alias) and checks user-scoped paths first, then root paths.
    3. Flat account_id or account_alias: e.g. "acc1"
       - Checks direct root paths data/sessions/{account_id}.json
       - If not in root, scans user directories (data/sessions/*/{account_id}.json) for a match.
    4. Fallback: returns the standard multi-tenant or flat path if no existing session is found.
    """
    ensure_directories()
    clean_user = sanitize_identifier(user_id) if user_id else None
    clean_alias = sanitize_identifier(account_alias) if account_alias else None
    clean_acc = sanitize_identifier(account_id) if account_id else None

    # Case 1: user_id and account_alias both provided
    if clean_user and clean_alias:
        user_sess = SESSIONS_DIR / clean_user / f"{clean_alias}.json"
        user_prof = PROFILES_DIR / clean_user / clean_alias
        log_label = f"{clean_user}:{clean_alias}"

        if user_sess.exists():
            return user_prof, user_sess, log_label

        # Fallback to root alias session (e.g. if authenticated via CLI 'auth <alias>')
        root_sess = SESSIONS_DIR / f"{clean_alias}.json"
        root_prof = PROFILES_DIR / clean_alias
        if root_sess.exists():
            return root_prof, root_sess, log_label

        if user_prof.exists():
            return user_prof, user_sess, log_label

        if root_prof.exists():
            return root_prof, root_sess, log_label

        return user_prof, user_sess, log_label

    # Case 2: account_id provided
    if clean_acc:
        # Check direct root session file
        root_sess = SESSIONS_DIR / f"{clean_acc}.json"
        root_prof = PROFILES_DIR / clean_acc
        if root_sess.exists():
            return root_prof, root_sess, clean_acc

        # Check composite 'user_alias'
        if "_" in clean_acc:
            # Check existing user folders in SESSIONS_DIR
            if SESSIONS_DIR.exists():
                for u_dir in SESSIONS_DIR.iterdir():
                    if u_dir.is_dir() and clean_acc.startswith(f"{u_dir.name}_"):
                        alias = clean_acc[len(u_dir.name) + 1:]
                        cand_sess = u_dir / f"{alias}.json"
                        cand_prof = PROFILES_DIR / u_dir.name / alias
                        if cand_sess.exists():
                            return cand_prof, cand_sess, f"{u_dir.name}:{alias}"

            # Fallback to rsplit by last '_'
            cand_user, cand_alias = clean_acc.rsplit("_", 1)
            cand_sess = SESSIONS_DIR / cand_user / f"{cand_alias}.json"
            cand_prof = PROFILES_DIR / cand_user / cand_alias
            if cand_sess.exists():
                return cand_prof, cand_sess, f"{cand_user}:{cand_alias}"

            root_cand_sess = SESSIONS_DIR / f"{cand_alias}.json"
            root_cand_prof = PROFILES_DIR / cand_alias
            if root_cand_sess.exists():
                return root_cand_prof, root_cand_sess, f"{cand_user}:{cand_alias}"

        # If account_id is a flat alias (e.g. 'acc1'), check if it exists in any user session folder
        if SESSIONS_DIR.exists():
            for u_dir in SESSIONS_DIR.iterdir():
                if u_dir.is_dir():
                    cand_sess = u_dir / f"{clean_acc}.json"
                    cand_prof = PROFILES_DIR / u_dir.name / clean_acc
                    if cand_sess.exists():
                        return cand_prof, cand_sess, f"{u_dir.name}:{clean_acc}"

        # Check direct root profile directory
        if root_prof.exists():
            return root_prof, root_sess, clean_acc

        return root_prof, root_sess, clean_acc

    # Case 3: only account_alias provided
    if clean_alias:
        root_sess = SESSIONS_DIR / f"{clean_alias}.json"
        root_prof = PROFILES_DIR / clean_alias
        if root_sess.exists():
            return root_prof, root_sess, clean_alias

        if SESSIONS_DIR.exists():
            for u_dir in SESSIONS_DIR.iterdir():
                if u_dir.is_dir():
                    cand_sess = u_dir / f"{clean_alias}.json"
                    cand_prof = PROFILES_DIR / u_dir.name / clean_alias
                    if cand_sess.exists():
                        return cand_prof, cand_sess, f"{u_dir.name}:{clean_alias}"

        return root_prof, root_sess, clean_alias

    raise ValueError("Must provide either account_id, (user_id and account_alias), or account_alias")

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
