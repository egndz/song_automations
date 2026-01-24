"""Command-line interface for song-automations."""

from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from song_automations import __version__
from song_automations.clients.discogs import DiscogsClient
from song_automations.clients.soundcloud import SoundCloudClient
from song_automations.clients.spotify import SpotifyClient
from song_automations.config import get_settings
from song_automations.reports.missing import generate_missing_report
from song_automations.state.tracker import StateTracker
from song_automations.sync.engine import OperationType, SyncEngine

app = typer.Typer(
    name="song-automations",
    help="Sync Discogs collection folders to Spotify and SoundCloud playlists.",
    no_args_is_help=True,
)
sync_app = typer.Typer(help="Sync playlists to music platforms.")
report_app = typer.Typer(help="Generate reports.")
app.add_typer(sync_app, name="sync")
app.add_typer(report_app, name="report")

console = Console()


def version_callback(value: bool) -> None:
    """Show version and exit."""
    if value:
        console.print(f"song-automations version {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        Optional[bool],
        typer.Option(
            "--version",
            "-v",
            help="Show version and exit.",
            callback=version_callback,
            is_eager=True,
        ),
    ] = None,
) -> None:
    """Song Automations - Sync Discogs folders to playlists."""
    pass


@sync_app.command("spotify")
def sync_spotify(
    folders: Annotated[
        Optional[str],
        typer.Option(
            "--folders",
            "-f",
            help="Comma-separated list of folder names to sync.",
        ),
    ] = None,
    exclude_wantlist: Annotated[
        bool,
        typer.Option(
            "--exclude-wantlist",
            help="Exclude wantlist from sync.",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            "-n",
            help="Show what would be done without making changes.",
        ),
    ] = False,
    min_confidence: Annotated[
        Optional[float],
        typer.Option(
            "--min-confidence",
            help="Minimum confidence threshold (0.0-1.0).",
        ),
    ] = None,
) -> None:
    """Sync Discogs folders to Spotify playlists."""
    settings = get_settings()
    settings.ensure_directories()

    if min_confidence is not None:
        settings.min_confidence = min_confidence

    if not settings.discogs_user_token:
        console.print("[red]Error:[/red] DISCOGS_USER_TOKEN not set in environment.")
        raise typer.Exit(1)

    if not settings.spotify_client_id or not settings.spotify_client_secret:
        console.print(
            "[red]Error:[/red] SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET not set."
        )
        raise typer.Exit(1)

    folder_names = None
    if folders:
        folder_names = [f.strip() for f in folders.split(",")]

    console.print("[bold]Syncing to Spotify...[/bold]")
    if dry_run:
        console.print("[yellow]DRY RUN - No changes will be made[/yellow]\n")

    discogs_client = DiscogsClient(settings)
    spotify_client = SpotifyClient(settings)
    state_tracker = StateTracker(settings.db_path)
    engine = SyncEngine(settings, discogs_client, state_tracker, console)

    result = engine.sync_to_spotify(
        playlist_client=spotify_client,
        include_wantlist=not exclude_wantlist,
        folder_names=folder_names,
        dry_run=dry_run,
    )

    _print_sync_result(result, dry_run)


@sync_app.command("soundcloud")
def sync_soundcloud(
    folders: Annotated[
        Optional[str],
        typer.Option(
            "--folders",
            "-f",
            help="Comma-separated list of folder names to sync.",
        ),
    ] = None,
    exclude_wantlist: Annotated[
        bool,
        typer.Option(
            "--exclude-wantlist",
            help="Exclude wantlist from sync.",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            "-n",
            help="Show what would be done without making changes.",
        ),
    ] = False,
    min_confidence: Annotated[
        Optional[float],
        typer.Option(
            "--min-confidence",
            help="Minimum confidence threshold (0.0-1.0).",
        ),
    ] = None,
) -> None:
    """Sync Discogs folders to SoundCloud playlists."""
    settings = get_settings()
    settings.ensure_directories()

    if min_confidence is not None:
        settings.min_confidence = min_confidence

    if not settings.discogs_user_token:
        console.print("[red]Error:[/red] DISCOGS_USER_TOKEN not set in environment.")
        raise typer.Exit(1)

    if not settings.soundcloud_client_id or not settings.soundcloud_client_secret:
        console.print(
            "[red]Error:[/red] SOUNDCLOUD_CLIENT_ID and SOUNDCLOUD_CLIENT_SECRET not set."
        )
        raise typer.Exit(1)

    folder_names = None
    if folders:
        folder_names = [f.strip() for f in folders.split(",")]

    console.print("[bold]Syncing to SoundCloud...[/bold]")
    if dry_run:
        console.print("[yellow]DRY RUN - No changes will be made[/yellow]\n")

    discogs_client = DiscogsClient(settings)
    soundcloud_client = SoundCloudClient(settings)
    state_tracker = StateTracker(settings.db_path)
    engine = SyncEngine(settings, discogs_client, state_tracker, console)

    result = engine.sync_to_soundcloud(
        playlist_client=soundcloud_client,
        include_wantlist=not exclude_wantlist,
        folder_names=folder_names,
        dry_run=dry_run,
    )

    _print_sync_result(result, dry_run)


@sync_app.command("all")
def sync_all(
    folders: Annotated[
        Optional[str],
        typer.Option(
            "--folders",
            "-f",
            help="Comma-separated list of folder names to sync.",
        ),
    ] = None,
    exclude_wantlist: Annotated[
        bool,
        typer.Option(
            "--exclude-wantlist",
            help="Exclude wantlist from sync.",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            "-n",
            help="Show what would be done without making changes.",
        ),
    ] = False,
) -> None:
    """Sync Discogs folders to both Spotify and SoundCloud."""
    console.print("[bold]Syncing to all platforms...[/bold]\n")

    console.print("[bold cyan]--- Spotify ---[/bold cyan]")
    sync_spotify(
        folders=folders,
        exclude_wantlist=exclude_wantlist,
        dry_run=dry_run,
        min_confidence=None,
    )

    console.print("\n[bold cyan]--- SoundCloud ---[/bold cyan]")
    sync_soundcloud(
        folders=folders,
        exclude_wantlist=exclude_wantlist,
        dry_run=dry_run,
        min_confidence=None,
    )


@app.command("status")
def status() -> None:
    """Show current folder-to-playlist mappings."""
    settings = get_settings()

    if not settings.db_path.exists():
        console.print("[yellow]No sync data found. Run a sync first.[/yellow]")
        return

    state_tracker = StateTracker(settings.db_path)

    table = Table(title="Folder Mappings")
    table.add_column("Folder", style="cyan")
    table.add_column("Platform", style="magenta")
    table.add_column("Playlist", style="green")
    table.add_column("Created", style="dim")

    for dest in ["spotify", "soundcloud"]:
        mappings = state_tracker.get_all_folder_mappings(dest)
        for mapping in mappings:
            table.add_row(
                mapping.discogs_folder_name,
                dest.capitalize(),
                mapping.playlist_name,
                mapping.created_at.strftime("%Y-%m-%d %H:%M"),
            )

    if table.row_count == 0:
        console.print("[yellow]No folder mappings found. Run a sync first.[/yellow]")
    else:
        console.print(table)


@report_app.command("missing")
def report_missing(
    format: Annotated[
        str,
        typer.Option(
            "--format",
            "-f",
            help="Output format: csv or json.",
        ),
    ] = "csv",
    destination: Annotated[
        Optional[str],
        typer.Option(
            "--destination",
            "-d",
            help="Filter by destination: spotify or soundcloud.",
        ),
    ] = None,
    output: Annotated[
        Optional[str],
        typer.Option(
            "--output",
            "-o",
            help="Output file path (default: auto-generated in reports dir).",
        ),
    ] = None,
) -> None:
    """Generate a report of tracks that couldn't be found."""
    settings = get_settings()

    if not settings.db_path.exists():
        console.print("[yellow]No sync data found. Run a sync first.[/yellow]")
        return

    state_tracker = StateTracker(settings.db_path)
    dest = destination if destination in ("spotify", "soundcloud") else None

    output_path = generate_missing_report(
        state_tracker=state_tracker,
        settings=settings,
        format=format,
        destination=dest,
        output_path=output,
    )

    if output_path:
        console.print(f"[green]Report generated:[/green] {output_path}")
    else:
        console.print("[yellow]No missing tracks to report.[/yellow]")


def _print_sync_result(result, dry_run: bool) -> None:
    """Print sync result summary.

    Args:
        result: SyncResult object.
        dry_run: Whether this was a dry run.
    """
    console.print()

    if result.operations:
        table = Table(title="Operations" + (" (DRY RUN)" if dry_run else ""))
        table.add_column("Type", style="cyan")
        table.add_column("Folder", style="magenta")
        table.add_column("Track/Playlist", style="green")
        table.add_column("Confidence", justify="right")
        table.add_column("Status", style="dim")

        for op in result.operations[:50]:
            if op.operation_type == OperationType.CREATE_PLAYLIST:
                table.add_row("Create", op.folder_name, op.playlist_name, "-", "")
            elif op.operation_type == OperationType.DELETE_PLAYLIST:
                table.add_row("Delete", op.folder_name, op.playlist_name, "-", "")
            elif op.operation_type == OperationType.ADD_TRACK:
                status = "[yellow]Review[/yellow]" if op.flagged else "[green]OK[/green]"
                table.add_row(
                    "Add",
                    op.folder_name,
                    f"{op.track_artist} - {op.track_title}"[:50],
                    f"{op.confidence:.0%}",
                    status,
                )
            elif op.operation_type == OperationType.REMOVE_TRACK:
                table.add_row(
                    "Remove",
                    op.folder_name,
                    f"{op.track_artist} - {op.track_title}"[:50],
                    "-",
                    "",
                )

        if len(result.operations) > 50:
            table.add_row("...", f"({len(result.operations) - 50} more)", "", "", "")

        console.print(table)

    summary = Table.grid(padding=1)
    summary.add_column(justify="right")
    summary.add_column()

    summary.add_row("[bold]Summary:[/bold]", "")
    summary.add_row("Playlists created:", str(result.playlists_created))
    summary.add_row("Playlists deleted:", str(result.playlists_deleted))
    summary.add_row("Tracks added:", str(result.tracks_added))
    summary.add_row("Tracks removed:", str(result.tracks_removed))
    summary.add_row("Tracks missing:", f"[red]{result.tracks_missing}[/red]")
    summary.add_row("Tracks flagged:", f"[yellow]{result.tracks_flagged}[/yellow]")

    console.print(summary)

    if result.tracks_missing > 0:
        console.print(
            "\n[dim]Run 'song-automations report missing' to see unfound tracks.[/dim]"
        )


if __name__ == "__main__":
    app()
