#!/usr/bin/env python3
"""Völundr — CLI Entry Point."""

from __future__ import annotations

import argparse
from dataclasses import replace as dataclass_replace
from datetime import datetime
from pathlib import Path
import shutil
import signal
import sys
import threading

from src import __version__
from src.archive_handler import (
    ArchiveError,
    StagingJobDir,
    inspect_archive,
    is_archive,
)
from src.config_loader import (
    ConfigError,
    check_seven_zip_supports_rar,
    ensure_local_directories,
    load_config,
    try_mount_smb_if_needed,
)
from src.converter import ConversionError, describe_planned_action
from src.identifier import (
    TruncatedPreviewError,
    identify_directory,
    identify_single_file,
    sanitize_rom_stem,
)
from src.models import (
    ArchiveInspection,
    DetectionMethod,
    IdentificationResult,
    PipelineConfig,
    RoutingOutcome,
)
from src.router import (
    RoutingError,
    _normalize_wiiu_root_titles,
    _stage_and_identify_archive,
    execute_ingestion_batch,
    group_ingestion_batches,
    parse_multidisc_info,
)
from src.runner import clear_module_cache, has_module as _has_module
from src.watcher import PipelineWatcher, purge_stale_staging, scan_watch_dir_once

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.toml"


def _probe_write_access(directory: Path) -> tuple[bool, str]:
    """Verify write + atomic rename capability using a Syncthing-ignored temp filename."""
    if not directory.exists():
        return False, "Directory does not exist or network share is unmounted"
    if not directory.is_dir():
        return False, "Path exists but is not a directory"

    probe_tmp = directory / "~syncthing~volundr_doctor_probe.tmp"
    probe_final = directory / "~syncthing~volundr_doctor_probe2.tmp"
    try:
        probe_tmp.write_bytes(b"volundr")
        probe_tmp.replace(probe_final)
        probe_final.unlink(missing_ok=True)
        return True, "Writable + atomic rename verified"
    except OSError as exc:
        probe_tmp.unlink(missing_ok=True)
        probe_final.unlink(missing_ok=True)
        return False, f"Write/rename failed: {exc}"


def collect_doctor_checks(config: PipelineConfig) -> list[tuple[str, str, str, str]]:
    """Run read-only environment, binary, storage, and library checks and return structured rows."""
    clear_module_cache()
    ensure_local_directories(config)

    # Attempt GNOME GVFS auto-mount if running on Linux with an smb:// URI
    resolved_lib_root, smb_note = try_mount_smb_if_needed(config.paths, config.platform_key)

    checks: list[tuple[str, str, str, str]] = []  # (Category, Item, Status, Detail)

    # 1. Runtime & Python packages
    py_ok = sys.version_info >= (3, 11)
    checks.append((
        "Runtime",
        "Python 3.11+",
        "OK" if py_ok else "FAIL",
        f"{sys.version.split()[0]} ({sys.executable})",
    ))
    for pkg in ("watchdog", "rich"):
        installed = _has_module(pkg)
        checks.append((
            "Python Deps",
            pkg,
            "OK" if installed else "WARN",
            "Installed" if installed else "Missing — run `pip install -r requirements.txt`",
        ))
    pycrypto_ok = _has_module("Crypto") or _has_module("Cryptodome")
    checks.append((
        "Python Deps",
        "pycryptodome",
        "OK" if pycrypto_ok else "WARN",
        "Installed" if pycrypto_ok else "Missing — required for 3DS auto-decryption (`pip install -r requirements.txt`)",
    ))
    pyctr_ok = _has_module("pyctr")
    checks.append((
        "Python Deps",
        "pyctr",
        "OK" if pyctr_ok else "WARN",
        "Installed" if pyctr_ok else "Missing — required for 3DS `boot9.bin` keys (`pip install -r requirements.txt`)",
    ))

    # 2. Local Paths & Staging Free Space
    for label, folder in (
        ("watch_dir", config.paths.watch_dir),
        ("staging_dir", config.paths.staging_dir),
        ("processed_dir", config.paths.processed_dir),
        ("quarantine_dir", config.paths.quarantine_dir),
    ):
        exists = folder.is_dir()
        checks.append((
            "Local Paths",
            label,
            "OK" if exists else "FAIL",
            str(folder),
        ))

    try:
        usage = shutil.disk_usage(config.paths.staging_dir)
        free_gib = usage.free / (1024**3)
        space_ok = free_gib >= config.general.min_free_staging_gb
        checks.append((
            "Storage",
            "Staging Free Space",
            "OK" if space_ok else "WARN",
            f"{free_gib:.1f} GiB free (min threshold: {config.general.min_free_staging_gb:.1f} GiB)",
        ))
    except OSError as exc:
        checks.append(("Storage", "Staging Free Space", "WARN", f"Could not query disk usage: {exc}"))

    # 3. NAS Destination
    nas_ok, nas_detail = _probe_write_access(resolved_lib_root)
    if not nas_ok and smb_note:
        nas_detail = f"{nas_detail} ({smb_note})"
    checks.append((
        "NAS Target",
        f"library_root ({config.platform_key})",
        "OK" if nas_ok else "WARN",
        f"{resolved_lib_root} — {nas_detail}",
    ))

    if resolved_lib_root.is_dir():
        present = [
            cfg.folder
            for cfg in config.systems.values()
            if (resolved_lib_root / cfg.folder).is_dir()
        ]
        checks.append((
            "NAS Target",
            "System Folders",
            "OK",
            f"{len(present)}/{len(config.systems)} present ({', '.join(sorted(present)) or 'none yet'})",
        ))

    # 4. External CLI Binaries
    tool_specs = [
        ("7z (Archives)", config.binaries.seven_zip, "Required for .7z/.rar & fast peeking"),
        ("chdman (PS1/PS2/PSP/Sega)", config.binaries.chdman, "Required for .chd conversion"),
        ("psxpackager (PS1 Multi-Disc)", config.binaries.psxpackager, "Required for multi-disc PS1 .pbp merging"),
        ("DolphinTool (GC/Wii)", config.binaries.dolphin_tool, "Required for .rvz conversion"),
        ("squirrel (Switch NSCB)", config.binaries.squirrel, "Required for Switch XCI merge / NSZ"),
        ("Switch keys (prod.keys)", config.binaries.squirrel_keys, "Required for Switch — place prod.keys (or keys.txt) in tools/nscb/ztools/ or ~/.switch/prod.keys"),
        ("3DS keys (aes_keys/boot9)", config.binaries.ctr_keys, "Optional: required to auto-decrypt encrypted .3ds/.cia ROMs"),
        ("3DS seeddb (eShop 9.6+)", config.binaries.ctr_seeddb, "Optional: place seeddb.bin in tools/3ds/ for post-2015 SeedCrypto eShop .cia -> .cci conversion"),
        ("zarchive (Wii U .wua)", config.binaries.zarchive, "Optional: packs Wii U Loadiine folders into .wua"),
        ("cdecrypt (Wii U NUS)", config.binaries.cdecrypt, "Optional: required to decrypt encrypted Wii U WUP/NUS packages"),
        ("pkg2zip (PS Vita .pkg)", config.binaries.pkg2zip, "Optional: unpacks Sony .pkg + work.bin/zRIF into Vita3K .zip"),
    ]
    for label, path_val, purpose in tool_specs:
        if path_val is not None:
            if label.startswith("7z"):
                if check_seven_zip_supports_rar(path_val):
                    checks.append(("Binaries", label, "OK", f"{path_val} (rar support: yes)"))
                else:
                    checks.append((
                        "Binaries",
                        label,
                        "WARN",
                        f"{path_val} — Lacks RAR support (Fedora distro build excludes unrar). .rar extraction will fail. Place official 7zz in tools/7z/7zz",
                    ))
            elif label.startswith("Switch keys") and path_val.name.lower() != "keys.txt":
                checks.append((
                    "Binaries",
                    label,
                    "OK",
                    f"{path_val} (auto-synced to keys.txt for squirrel)",
                ))
            else:
                checks.append(("Binaries", label, "OK", str(path_val)))
        else:
            checks.append(("Binaries", label, "WARN", f"Not found — {purpose}"))

    psvita_tsv_dir = config.paths.project_root / "tools" / "psvita"
    tsv_files = sorted(psvita_tsv_dir.glob("*.tsv")) if psvita_tsv_dir.is_dir() else []
    if tsv_files:
        checks.append((
            "Binaries",
            "PS Vita TSV DB (zRIF)",
            "OK",
            ", ".join(p.name for p in tsv_files),
        ))
    else:
        checks.append((
            "Binaries",
            "PS Vita TSV DB (zRIF)",
            "WARN",
            "Optional: place PSV_GAMES.tsv / PSV_DLCS.tsv in tools/psvita/ for automatic .pkg zRIF/work.bin license generation",
        ))

    return checks


def run_doctor(config: PipelineConfig) -> int:
    """Run read-only environment, binary, storage, and NAS connectivity diagnostics."""
    checks = collect_doctor_checks(config)
    _render_doctor_report(config, checks)

    has_hard_fail = any(status == "FAIL" for _, _, status, _ in checks)
    return 1 if has_hard_fail else 0


def _render_doctor_report(
    config: PipelineConfig,
    checks: list[tuple[str, str, str, str]],
) -> None:
    """Render diagnostics with `rich` if installed, else clean plain text."""
    if _has_module("rich"):
        from rich.console import Console
        from rich.panel import Panel
        from rich.table import Table

        console = Console()
        console.print(
            Panel.fit(
                f"[bold cyan]Völundr v{__version__}[/bold cyan] — Environment & Config Diagnostics\n"
                f"[dim]Config:[/dim] {config.config_path}\n"
                f"[dim]Active Profile:[/dim] {config.platform_key} | "
                f"[dim]Post-Process Action:[/dim] {config.general.post_process_action.value}",
                border_style="cyan",
            )
        )

        table = Table(show_header=True, header_style="bold magenta", expand=True)
        table.add_column("Category", style="cyan", no_wrap=True)
        table.add_column("Check", style="white", no_wrap=True)
        table.add_column("Status", no_wrap=True)
        table.add_column("Details", overflow="fold")

        badge_map = {
            "OK": "[bold green]OK[/bold green]",
            "WARN": "[bold yellow]WARN[/bold yellow]",
            "FAIL": "[bold red]FAIL[/bold red]",
        }
        for category, item, status, detail in checks:
            table.add_row(category, item, badge_map.get(status, status), detail)

        console.print(table)
        return

    print(f"=== Völundr v{__version__} Diagnostics ===")
    print(f"Config: {config.config_path} (profile={config.platform_key}, post_action={config.general.post_process_action.value})")
    print("-" * 88)
    for category, item, status, detail in checks:
        print(f"[{status:<4}] {category:<12} | {item:<26} | {detail}")
    print("-" * 88)


# ------------------------------------------------------------------------------
# Phase 2: Archive & Binary Header Inspection (`inspect`)
# ------------------------------------------------------------------------------
def _describe_planned_action(
    res: IdentificationResult,
    config: PipelineConfig,
    from_archive: bool,
    archive_ext: str = "",
    psx_multidisc_peers: int = 1,
) -> str:
    """Summarize what Phase 3 (`converter` + `router`) will do with this item."""
    return describe_planned_action(
        res,
        config,
        from_archive,
        archive_ext=archive_ext,
        psx_multidisc_peers=psx_multidisc_peers,
    )


def _inspect_single_target(
    target: Path,
    config: PipelineConfig,
) -> tuple[ArchiveInspection | None, list[tuple[IdentificationResult, bool, str]]]:
    """
    Inspect an archive, standalone ROM/disc image, or folder without modifying user files.

    Returns `(archive_inspection_or_none, [(IdentificationResult, from_archive, archive_ext), ...])`.
    """
    if is_archive(target):
        arc_info = inspect_archive(target, config)
        if arc_info.is_arcade:
            arcade_res = IdentificationResult(
                system_id=arc_info.arcade_system_id,
                method=arc_info.detection_method or DetectionMethod.ARCADE_CHIP_HEURISTIC,
                confidence="high",
                reason=arc_info.reason,
                primary_file=target,
            )
            return arc_info, [(arcade_res, True, target.suffix.lower())]

        # Non-arcade archive: stream lightweight header preview into temp staging & clean up
        with StagingJobDir(config, prefix="_inspect_") as job_dir:
            base, id_results = _stage_and_identify_archive(
                arc_info,
                target,
                config,
                dry_run=True,
                job_dir=job_dir,
            )
            rebound: list[tuple[IdentificationResult, bool, str]] = []
            for r in id_results:
                rel_primary = Path(target.name) / r.primary_file.relative_to(base.resolve())
                rel_comp = tuple(
                    Path(target.name) / c.relative_to(base.resolve()) for c in r.companion_files
                )
                rebound.append((
                    dataclass_replace(
                        r,
                        primary_file=rel_primary,
                        companion_files=rel_comp,
                    ),
                    True,
                    target.suffix.lower(),
                ))
            return arc_info, rebound

    if target.is_dir():
        items: list[tuple[IdentificationResult, bool, str]] = []
        # 1. Inspect loose files/descriptors/bundles inside directory
        for r in _normalize_wiiu_root_titles(
            identify_directory(
                target,
                chdman_bin=config.binaries.chdman,
                project_root=config.paths.project_root,
            ),
            target,
        ):
            items.append((r, False, ""))
        # 2. Also inspect any archives nested inside the directory
        for child_arc in sorted(p for p in target.rglob("*") if p.is_file() and is_archive(p)):
            _, sub_items = _inspect_single_target(child_arc, config)
            items.extend(sub_items)
        return None, items

    # Standalone file
    return None, [
        (
            identify_single_file(
                target,
                chdman_bin=config.binaries.chdman,
                project_root=config.paths.project_root,
            ),
            False,
            "",
        )
    ]


def run_inspect(targets: list[Path], config: PipelineConfig) -> int:
    """CLI handler for `python main.py inspect <paths...>`."""
    ensure_local_directories(config)
    use_rich = _has_module("rich")
    console = None
    if use_rich:
        from rich.console import Console
        from rich.panel import Panel
        from rich.table import Table

        console = Console()

    exit_code = 0
    for raw_target in targets:
        target = raw_target.expanduser().resolve()
        if not target.exists():
            msg = f"Target path not found: {target}"
            if console:
                console.print(f"[bold red][ERROR][/bold red] {msg}")
            else:
                print(f"[ERROR] {msg}", file=sys.stderr)
            exit_code = 1
            continue

        try:
            arc_info, items = _inspect_single_target(target, config)
        except ArchiveError as exc:
            if console:
                console.print(f"[bold red][ARCHIVE ERROR][/bold red] {exc}")
            else:
                print(f"[ARCHIVE ERROR] {exc}", file=sys.stderr)
            exit_code = 1
            continue

        psx_title_counts: dict[str, int] = {}
        for r_chk, _, _ in items:
            if r_chk.system_id == "psx":
                p_chk = parse_multidisc_info(
                    sanitize_rom_stem(r_chk.primary_file.stem, r_chk.companion_files)
                )
                if p_chk is not None:
                    k_chk = p_chk[0].lower()
                    psx_title_counts[k_chk] = psx_title_counts.get(k_chk, 0) + 1

        if console:
            header_lines = [f"[bold cyan]Inspecting:[/bold cyan] {target}"]
            if arc_info is not None:
                mib = arc_info.total_uncompressed_bytes / (1024**2)
                arcade_badge = (
                    f"[bold green]YES ({arc_info.arcade_system_id})[/bold green]"
                    if arc_info.is_arcade
                    else "[dim]No (Console/Disc Archive)[/dim]"
                )
                header_lines.append(
                    f"[dim]Archive Entries:[/dim] {len(arc_info.file_entries)} files "
                    f"({mib:.2f} MiB uncompressed) | [dim]Arcade Set:[/dim] {arcade_badge}"
                )
            console.print(Panel("\n".join(header_lines), border_style="cyan"))

            table = Table(show_header=True, header_style="bold magenta", expand=True)
            table.add_column("Primary Entry", style="white", overflow="fold")
            table.add_column("System ID", style="bold green", no_wrap=True)
            table.add_column("Method", style="cyan", no_wrap=True)
            table.add_column("Reason / Header Details", overflow="fold")
            table.add_column("Planned Pipeline Action", style="yellow", overflow="fold")

            if not items:
                table.add_row("(no ROMs found)", "[yellow]none[/yellow]", "-", "Empty or sidecar-only", "Ignore")
            for res, from_arc, arc_ext in items:
                sys_label = (
                    f"[bold green]{res.system_id}[/bold green]"
                    if res.system_id
                    else "[bold red]UNIDENTIFIED[/bold red]"
                )
                comp_note = f" (+{len(res.companion_files)} companion files)" if res.companion_files else ""
                peers = 1
                if res.system_id == "psx":
                    p_info = parse_multidisc_info(
                        sanitize_rom_stem(res.primary_file.stem, res.companion_files)
                    )
                    if p_info is not None:
                        peers = psx_title_counts.get(p_info[0].lower(), 1)
                action = _describe_planned_action(
                    res, config, from_arc, arc_ext, psx_multidisc_peers=peers
                )
                table.add_row(
                    f"{res.primary_file.name}{comp_note}",
                    sys_label,
                    res.method.value,
                    res.reason,
                    action,
                )
            console.print(table)
        else:
            print(f"=== Inspecting: {target} ===")
            for res, from_arc, arc_ext in items:
                peers = 1
                if res.system_id == "psx":
                    p_info = parse_multidisc_info(
                        sanitize_rom_stem(res.primary_file.stem, res.companion_files)
                    )
                    if p_info is not None:
                        peers = psx_title_counts.get(p_info[0].lower(), 1)
                action = _describe_planned_action(
                    res, config, from_arc, arc_ext, psx_multidisc_peers=peers
                )
                print(
                    f"  - {res.primary_file.name} -> system={res.system_id or 'UNIDENTIFIED'} "
                    f"[{res.method.value}] | {res.reason} | Action: {action}"
                )

    return exit_code


# ------------------------------------------------------------------------------
# Phase 3: Single-Item Conversion & Atomic NAS Routing (`process`)
# ------------------------------------------------------------------------------
def _render_routing_outcome(outcome: RoutingOutcome) -> None:
    is_deferred = outcome.post_action_taken.startswith(("deferred", "skipped"))
    if _has_module("rich"):
        from rich.console import Console
        from rich.markup import escape as rich_escape
        from rich.panel import Panel

        console = Console()
        mode_tag = "[bold yellow][DRY-RUN][/bold yellow] " if outcome.dry_run else ""
        if outcome.additional_sources:
            all_srcs = (outcome.source_path, *outcome.additional_sources)
            lines = [f"{mode_tag}[bold cyan]Source Bundle ({len(all_srcs)} items):[/bold cyan]"]
            for sp in all_srcs:
                lines.append(f"  [dim]•[/dim] {rich_escape(str(sp))}")
        else:
            lines = [f"{mode_tag}[bold cyan]Source:[/bold cyan] {rich_escape(str(outcome.source_path))}"]

        for rf in outcome.routed_files:
            lines.append(f"  [bold green]✔ Routed:[/bold green] {rich_escape(str(rf))}")
        for sf in outcome.superseded_files:
            lines.append(f"  [bold cyan]↻ Replaced on NAS:[/bold cyan] {rich_escape(str(sf))}")
        for m3u in outcome.m3u_playlists:
            lines.append(f"  [bold magenta]♫ Playlist (.m3u):[/bold magenta] {rich_escape(str(m3u))}")
        for qf in outcome.quarantined_files:
            lines.append(f"  [bold yellow]⚠ Quarantined:[/bold yellow] {rich_escape(str(qf))}")
        lines.append(f"  [dim]Post-Process:[/dim] {rich_escape(outcome.post_action_taken)}")
        border = "yellow" if (outcome.quarantined_files or outcome.dry_run or is_deferred) else "green"
        console.print(Panel("\n".join(lines), border_style=border))
        return

    prefix = "[DRY-RUN] " if outcome.dry_run else ""
    print(f"=== {prefix}Processed: {outcome.source_path} ===")
    for sp in outcome.additional_sources:
        print(f"  [BUNDLE+]     {sp}")
    for rf in outcome.routed_files:
        print(f"  [ROUTED]      {rf}")
    for sf in outcome.superseded_files:
        print(f"  [REPLACED]    {sf}")
    for m3u in outcome.m3u_playlists:
        print(f"  [PLAYLIST]    {m3u}")
    for qf in outcome.quarantined_files:
        print(f"  [QUARANTINED] {qf}")
    print(f"  [CLEANUP]     {outcome.post_action_taken}")


def run_process(
    targets: list[Path],
    config: PipelineConfig,
    dry_run: bool = False,
    keep_source: bool = False,
) -> int:
    """CLI handler for `python main.py process <paths...> [--dry-run] [--keep-source]`."""
    if not dry_run:
        purge_stale_staging(config)

    exit_code = 0
    console = None
    if _has_module("rich"):
        from rich.console import Console

        console = Console()

    batches = group_ingestion_batches(targets, config)
    for b_kind, batch in batches:
        try:
            outcome = execute_ingestion_batch(
                b_kind,
                batch,
                config,
                dry_run=dry_run,
                keep_source=keep_source,
            )
            _render_routing_outcome(outcome)
        except (ArchiveError, ConversionError, RoutingError) as exc:
            label = batch[0]
            if console:
                console.print(f"[bold red][INGEST ERROR][/bold red] {label}: {exc}")
            else:
                print(f"[INGEST ERROR] {label}: {exc}", file=sys.stderr)
            exit_code = 1

    return exit_code


# ------------------------------------------------------------------------------
# Phase 4: One-Shot Backlog Scan (`scan`) & Continuous Automated Watcher (`watch`)
# ------------------------------------------------------------------------------
def _timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def run_scan(
    config: PipelineConfig,
    dry_run: bool = False,
    verify_stability: bool = True,
) -> int:
    """CLI handler for `python main.py scan [--dry-run] [--no-wait]`."""
    console = None
    if _has_module("rich"):
        from rich.console import Console

        console = Console()

    def _on_error(path: Path, exc: Exception) -> None:
        if console:
            console.print(f"[bold red][{_timestamp()}] [ERROR][/bold red] {path.name}: {exc}")
        else:
            print(f"[{_timestamp()}] [ERROR] {path.name}: {exc}", file=sys.stderr)

    outcomes, error_count = scan_watch_dir_once(
        config,
        dry_run=dry_run,
        verify_stability=verify_stability,
        on_outcome=_render_routing_outcome,
        on_error=_on_error,
    )

    if not outcomes and error_count == 0:
        msg = f"No actionable ROMs or archives found in `{config.paths.watch_dir}`."
        if console:
            console.print(f"[dim]{msg}[/dim]")
        else:
            print(msg)

    return 1 if error_count > 0 else 0


def run_watch(config: PipelineConfig, scan_existing: bool = True) -> int:
    """CLI handler for `python main.py watch [--no-backlog]`."""
    console = None
    if _has_module("rich"):
        from rich.console import Console
        from rich.panel import Panel

        console = Console()
        poll_total = (
            config.general.stability_poll_seconds * config.general.stability_checks_required
        )
        console.print(
            Panel.fit(
                f"[bold cyan]Völundr v{__version__}[/bold cyan] — Automated Watch Mode Active\n"
                f"[dim]Watching:[/dim]     {config.paths.watch_dir}\n"
                f"[dim]NAS Library:[/dim]  {config.paths.library_root}\n"
                f"[dim]Post-Action:[/dim]  {config.general.post_process_action.value} "
                f"({config.paths.processed_dir})\n"
                f"[dim]Stability:[/dim]    {config.general.stability_checks_required}x "
                f"{config.general.stability_poll_seconds:.1f}s (~{poll_total:.0f}s + exclusive lock)\n"
                f"[dim]Press Ctrl+C to stop cleanly.[/dim]",
                border_style="cyan",
            )
        )
    else:
        print(f"=== Völundr v{__version__} Watch Mode ===")
        print(f"Watching: {config.paths.watch_dir} -> {config.paths.library_root}")
        print("Press Ctrl+C to stop.")

    def _log_event(msg: str) -> None:
        if console:
            from rich.markup import escape as rich_escape

            console.print(f"[dim][{_timestamp()}][/dim] [cyan]•[/cyan] {rich_escape(msg)}")
        else:
            print(f"[{_timestamp()}] {msg}")

    def _on_error(path: Path, exc: Exception) -> None:
        if console:
            from rich.markup import escape as rich_escape

            console.print(
                f"[dim][{_timestamp()}][/dim] [bold red]✖ {rich_escape(path.name)}:[/bold red] {rich_escape(str(exc))}"
            )
        else:
            print(f"[{_timestamp()}] [ERROR] {path.name}: {exc}", file=sys.stderr)

    watcher = PipelineWatcher(
        config,
        on_event_log=_log_event,
        on_outcome=_render_routing_outcome,
        on_error=_on_error,
    )

    shutdown_event = threading.Event()

    def _handle_signal(_signum: int, _frame) -> None:
        shutdown_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_signal)

    watcher.start_background(scan_existing=scan_existing)
    try:
        while not shutdown_event.is_set():
            shutdown_event.wait(timeout=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        _log_event("Shutting down watcher & waiting for active job (if any) to finish...")
        watcher.stop(timeout=30.0)
        _log_event("Pipeline stopped cleanly.")

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="volundr",
        description="Völundr — Universal Automated ROM Ingestion Pipeline",
    )
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to config.toml (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"Völundr {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command")

    # Phase 1: doctor
    doc_parser = subparsers.add_parser(
        "doctor",
        help="Verify configuration, external CLI tools, staging space, and NAS connectivity",
    )
    doc_parser.add_argument(
        "--platform",
        choices=("windows", "linux"),
        default=None,
        help="Override detected OS profile to preview Windows vs Linux path resolution",
    )

    # Phase 2: inspect
    insp_parser = subparsers.add_parser(
        "inspect",
        help="Peek archives & sniff binary magic headers to test console detection (read-only)",
    )
    insp_parser.add_argument(
        "targets",
        nargs="+",
        type=Path,
        help="One or more archive files (.zip/.7z/.rar), ROM/disc files, or folders to inspect",
    )

    # Phase 3: process
    proc_parser = subparsers.add_parser(
        "process",
        help="Extract, convert, and atomically route file(s) or folder(s) to the NAS",
    )
    proc_parser.add_argument(
        "targets",
        nargs="+",
        type=Path,
        help="One or more archive files, ROM/disc files, or folders to ingest",
    )
    proc_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate extraction, conversion, and NAS destination paths without modifying files",
    )
    proc_parser.add_argument(
        "--keep-source",
        action="store_true",
        help="Leave original input file/folder in place after routing (overrides post_process_action)",
    )

    # Phase 4: scan & watch
    scan_parser = subparsers.add_parser(
        "scan",
        help="One-shot sequential ingestion pass over all items currently in watch_dir",
    )
    scan_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be converted and routed without modifying any files",
    )
    scan_parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Skip multi-poll stability delay if files in watch_dir are already complete",
    )

    watch_parser = subparsers.add_parser(
        "watch",
        help="Continuously monitor watch_dir and automatically ingest dropped ROMs/archives",
    )
    watch_parser.add_argument(
        "--no-backlog",
        action="store_true",
        help="Ignore files already sitting in watch_dir at startup (only process newly dropped items)",
    )

    # Desktop UI + System Tray (optional PySide6)
    gui_parser = subparsers.add_parser(
        "gui",
        help="Launch the Völundr desktop window and system tray app (requires PySide6)",
    )
    gui_parser.add_argument(
        "--minimized",
        action="store_true",
        help="Start hidden in the system tray (when a system tray is available)",
    )
    gui_watch_grp = gui_parser.add_mutually_exclusive_group()
    gui_watch_grp.add_argument(
        "--autowatch",
        action="store_true",
        help="Start Watch mode immediately on launch (overrides the saved GUI setting)",
    )
    gui_watch_grp.add_argument(
        "--no-autowatch",
        action="store_true",
        help="Open the window in Paused state (overrides the saved GUI setting)",
    )

    return parser


def run_gui(
    config_path: Path,
    *,
    start_minimized: bool = False,
    auto_watch: bool | None = None,
) -> int:
    """Lazy-import PySide6 and launch the desktop UI & system tray."""
    if not _has_module("PySide6"):
        print(
            "[GUI ERROR] PySide6 is not installed in this Python environment.\n"
            "Install optional desktop GUI dependencies with:\n"
            "  Windows: .\\.venv\\Scripts\\python.exe -m pip install -r requirements-gui.txt\n"
            "  Linux:   ./.venv/bin/pip install -r requirements-gui.txt",
            file=sys.stderr,
        )
        return 1

    from src.gui import launch_gui

    return launch_gui(
        config_path,
        start_minimized=start_minimized,
        auto_watch=auto_watch,
    )


def main(argv: list[str] | None = None) -> int:
    # Ensure clean UTF-8 output on Windows consoles
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = build_parser()
    args = parser.parse_args(argv)

    command = args.command or "doctor"
    platform_override = getattr(args, "platform", None)

    if command == "gui":
        auto_watch_override: bool | None = None
        if args.autowatch:
            auto_watch_override = True
        elif args.no_autowatch:
            auto_watch_override = False
        return run_gui(
            args.config,
            start_minimized=args.minimized,
            auto_watch=auto_watch_override,
        )

    try:
        config = load_config(args.config, platform_override=platform_override)
    except ConfigError as exc:
        print(f"[CONFIG ERROR] {exc}", file=sys.stderr)
        return 2

    if command == "doctor":
        return run_doctor(config)
    if command == "inspect":
        return run_inspect(args.targets, config)
    if command == "process":
        return run_process(
            args.targets,
            config,
            dry_run=args.dry_run,
            keep_source=args.keep_source,
        )
    if command == "scan":
        return run_scan(
            config,
            dry_run=args.dry_run,
            verify_stability=not args.no_wait,
        )
    if command == "watch":
        return run_watch(
            config,
            scan_existing=not args.no_backlog,
        )

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
