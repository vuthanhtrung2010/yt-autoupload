import asyncio
from pathlib import Path
from typing import List, Optional
import yaml
import json
from loguru import logger
from rich.console import Console
from rich.table import Table

from yt_autosub.models import UploadManifest, VideoUploadTask, UploadResult
from yt_autosub.uploader import upload_video
from yt_autosub.config import ProxyConfig

console = Console()

def load_manifest(manifest_path: str | Path) -> UploadManifest:
    """Load and validate an upload manifest from YAML or JSON."""
    path = Path(manifest_path).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Manifest file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        if path.suffix in (".yaml", ".yml"):
            data = yaml.safe_load(f)
        elif path.suffix == ".json":
            data = json.load(f)
        else:
            # Try YAML parser as fallback for all text
            data = yaml.safe_load(f)

    return UploadManifest(**data)

async def run_batch_upload(
    manifest: UploadManifest,
    concurrency: int = 2,
    headless: bool = True,
    proxy_map: Optional[dict[str, ProxyConfig]] = None,
) -> List[UploadResult]:
    """
    Execute batch video uploads across accounts with bounded concurrency.
    Each account uses its own isolated Camoufox instance.
    """
    semaphore = asyncio.Semaphore(concurrency)
    results: List[UploadResult] = []

    async def _worker(task: VideoUploadTask) -> UploadResult:
        async with semaphore:
            proxy = proxy_map.get(task.account_id) if proxy_map else None
            return await upload_video(
                task=task,
                headless=headless,
                proxy=proxy,
            )

    tasks = [_worker(task) for task in manifest.uploads]
    results = await asyncio.gather(*tasks)

    # Print summary table
    table = Table(title=f"Batch Upload Summary ({len(results)} tasks)")
    table.add_column("Account", style="cyan")
    table.add_column("Title", style="magenta")
    table.add_column("Status", style="bold")
    table.add_column("Video URL / Error", style="green")

    success_count = 0
    for r in results:
        if r.success:
            success_count += 1
            table.add_row(
                r.account_id,
                r.title[:30],
                "[green]✓ SUCCESS[/green]",
                r.video_url or "[dim]Published[/dim]",
            )
        else:
            table.add_row(
                r.account_id,
                r.title[:30],
                "[red]✗ FAILED[/red]",
                f"[red]{r.error_message[:40]}[/red]",
            )

    console.print("\n")
    console.print(table)
    console.print(f"\n[bold]Completed: [green]{success_count}[/green] / {len(results)} succeeded.[/bold]\n")

    return results
