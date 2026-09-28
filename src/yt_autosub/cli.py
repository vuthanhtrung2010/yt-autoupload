import asyncio
from pathlib import Path
from typing import Optional
import typer
from rich.console import Console
from rich.table import Table

from yt_autosub.config import get_session_path, get_profile_dir, PROFILES_DIR, SESSIONS_DIR, ProxyConfig
from yt_autosub.auth import interactive_login, test_session
from yt_autosub.inspect_dom import inspect_studio_dom

app = typer.Typer(
    name="yt-autosub",
    help="YouTube Multi-Account Automation Tool using Camoufox & Session Injection",
    add_completion=False,
)
console = Console()

def parse_proxy(proxy_str: Optional[str]) -> Optional[ProxyConfig]:
    if not proxy_str:
        return None
    return ProxyConfig(server=proxy_str)

@app.command()
def auth(
    account_id: str = typer.Argument(..., help="Unique ID for the account (e.g. acc1, ch_tech, user01)"),
    proxy: Optional[str] = typer.Option(None, "--proxy", "-p", help="Proxy URL (e.g. http://user:pass@host:port)"),
    timeout: int = typer.Option(10, "--timeout", "-t", help="Timeout in minutes for manual login"),
):
    """
    Launch Camoufox GUI to log into Google / YouTube Studio for the specified account.
    Once logged in, captures cookies and saves persistent profile.
    """
    proxy_cfg = parse_proxy(proxy)
    success = asyncio.run(interactive_login(account_id=account_id, proxy=proxy_cfg, timeout_minutes=timeout))
    if not success:
        raise typer.Exit(code=1)

@app.command()
def test_auth(
    account_id: str = typer.Argument(..., help="Account ID to verify"),
    headful: bool = typer.Option(False, "--headful", help="Run with browser window visible"),
    proxy: Optional[str] = typer.Option(None, "--proxy", "-p", help="Proxy URL"),
):
    """
    Test whether the saved session/cookies for an account are still valid and active.
    """
    proxy_cfg = parse_proxy(proxy)
    valid, message = asyncio.run(test_session(account_id=account_id, headless=not headful, proxy=proxy_cfg))
    if valid:
        console.print(f"[bold green]✓ SUCCESS:[/bold green] {message}")
    else:
        console.print(f"[bold red]✗ FAILED:[/bold red] {message}")
        raise typer.Exit(code=1)

@app.command()
def inspect(
    account_id: str = typer.Argument(..., help="Account ID to use for inspection"),
    action: str = typer.Option("overview", "--action", "-a", help="Action: 'overview', 'open-upload', 'dump-html', or 'all'"),
    headful: bool = typer.Option(False, "--headful", help="Run with browser window visible"),
    proxy: Optional[str] = typer.Option(None, "--proxy", "-p", help="Proxy URL"),
):
    """
    Inspect YouTube Studio DOM live over Playwright to analyze UI elements and selectors.
    """
    proxy_cfg = parse_proxy(proxy)
    asyncio.run(inspect_studio_dom(account_id=account_id, headless=not headful, proxy=proxy_cfg, action=action))

@app.command()
def list_accounts():
    """
    List all configured accounts, persistent profiles, and saved session cookies.
    """
    table = Table(title="Configured YouTube Accounts")
    table.add_column("Account ID", style="cyan")
    table.add_column("Profile Dir", style="blue")
    table.add_column("Session File", style="magenta")
    table.add_column("Has Session?", style="green")

    account_ids = set()
    if PROFILES_DIR.exists():
        for p in PROFILES_DIR.iterdir():
            if p.is_dir():
                account_ids.add(p.name)
    if SESSIONS_DIR.exists():
        for s in SESSIONS_DIR.glob("*.json"):
            account_ids.add(s.stem)

    if not account_ids:
        console.print("[yellow]No accounts found yet. Run 'yt-autosub auth <account_id>' to set one up.[/yellow]")
        return

    for acc_id in sorted(account_ids):
        p_dir = get_profile_dir(acc_id)
        s_file = get_session_path(acc_id)
        has_session = s_file.exists()
        table.add_row(
            acc_id,
            str(p_dir.name),
            str(s_file.name if has_session else "-"),
            "✓ YES" if has_session else "✗ NO",
        )

@app.command()
def upload(
    manifest_path: str = typer.Option("manifest.example.yaml", "--manifest", "-m", help="Path to YAML or JSON upload manifest"),
    concurrency: int = typer.Option(2, "--concurrency", "-c", help="Number of concurrent Camoufox browser instances"),
    headful: bool = typer.Option(False, "--headful", help="Run with browser windows visible"),
):
    """
    Run batch video uploads across accounts defined in the manifest file.
    Uses bounded concurrency to manage memory and resources.
    """
    from yt_autosub.runner import load_manifest, run_batch_upload
    try:
        manifest = load_manifest(manifest_path)
    except Exception as e:
        console.print(f"[bold red]Failed to load manifest:[/bold red] {e}")
        raise typer.Exit(code=1)

    asyncio.run(run_batch_upload(manifest=manifest, concurrency=concurrency, headless=not headful))

@app.command()
def upload_single(
    account_id: str = typer.Argument(..., help="Account ID to upload with"),
    video: str = typer.Option(..., "--video", "-v", help="Path to video file"),
    title: str = typer.Option(..., "--title", "-t", help="Video title"),
    description: str = typer.Option("", "--description", "-d", help="Video description"),
    tags: str = typer.Option("", "--tags", help="Comma-separated tags"),
    thumbnail: Optional[str] = typer.Option(None, "--thumbnail", help="Custom thumbnail image path"),
    visibility: str = typer.Option("PRIVATE", "--visibility", help="Visibility: PRIVATE, UNLISTED, or PUBLIC"),
    headful: bool = typer.Option(False, "--headful", help="Run with browser window visible"),
    proxy: Optional[str] = typer.Option(None, "--proxy", "-p", help="Proxy URL"),
):
    """
    Upload a single video for an account via command line arguments.
    """
    from yt_autosub.models import VideoUploadTask, Visibility
    from yt_autosub.uploader import upload_video

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    try:
        task = VideoUploadTask(
            account_id=account_id,
            video_path=video,
            title=title,
            description=description,
            tags=tag_list,
            thumbnail_path=thumbnail,
            visibility=Visibility(visibility.upper()),
        )
    except Exception as e:
        console.print(f"[bold red]Validation error:[/bold red] {e}")
        raise typer.Exit(code=1)

    proxy_cfg = parse_proxy(proxy)
    res = asyncio.run(upload_video(task=task, headless=not headful, proxy=proxy_cfg))
    if res.success:
        console.print(f"[bold green]✓ SUCCESS:[/bold green] Uploaded '{res.title}' -> {res.video_url or 'Published'}")
    else:
        console.print(f"[bold red]✗ FAILED:[/bold red] {res.error_message}")
        raise typer.Exit(code=1)

def main():
    app()

if __name__ == "__main__":
    main()

