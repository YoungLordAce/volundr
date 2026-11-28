"""Shared domain models, enums, and configuration dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class PostProcessAction(StrEnum):
    """Action applied to source items in `watch_dir` after verified NAS routing."""

    MOVE = "move"
    DELETE = "delete"


class ArchivePolicy(StrEnum):
    """How archives for a given target system should be handled."""

    PASSTHROUGH = "passthrough"  # Never extract (Arcade sets: FBNeo, CPS1/2/3, Neo Geo)
    EXTRACT = "extract"          # Always extract contents before conversion/routing
    KEEP_ZIP = "keep_zip"        # Keep single-ROM .zip intact; extract .7z/.rar


class DetectionMethod(StrEnum):
    """How a ROM, disc image, or archive was matched to a `system_id`."""

    ARCADE_CHIP_HEURISTIC = "arcade_chip_heuristic"
    ARCADE_SHORTNAME = "arcade_shortname"
    UNAMBIGUOUS_EXTENSION = "unambiguous_extension"
    BINARY_MAGIC = "binary_magic"
    ISO9660_INSPECTION = "iso9660_inspection"
    DESCRIPTOR_TRACK_SNIFF = "descriptor_track_sniff"
    UNIDENTIFIED = "unidentified"


@dataclass(frozen=True, slots=True)
class ArchiveEntry:
    """Metadata for a single item inside a `.zip`, `.7z`, or `.rar` archive."""

    path: str  # Normalized forward-slash internal path
    size: int  # Uncompressed size in bytes
    is_dir: bool = False

    @property
    def filename(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def suffix(self) -> str:
        name = self.filename
        dot = name.rfind(".")
        return name[dot:].lower() if dot > 0 else ""


@dataclass(frozen=True, slots=True)
class ArchiveInspection:
    """In-memory peek result for an archive before deciding whether to extract."""

    archive_path: Path
    entries: tuple[ArchiveEntry, ...]
    total_uncompressed_bytes: int
    is_arcade: bool
    arcade_system_id: str | None = None
    detection_method: DetectionMethod | None = None
    reason: str = ""

    @property
    def file_entries(self) -> tuple[ArchiveEntry, ...]:
        return tuple(e for e in self.entries if not e.is_dir)


@dataclass(frozen=True, slots=True)
class IdentificationResult:
    """Classification output for a single logical game unit (ROM, disc set, or bundle)."""

    system_id: str | None
    method: DetectionMethod
    confidence: str  # "high", "medium", "low"
    reason: str
    primary_file: Path
    companion_files: tuple[Path, ...] = ()
    disc_media_type: str | None = None  # "cd" or "dvd" (used by PS2 converter)
    is_encrypted_3ds: bool = False
    is_zerokey_3ds: bool = False
    is_installable_cia: bool = False
    cia_title_id: str | None = None
    cia_category: str | None = None     # "game", "demo", "update", "dlc", "dsiware", "system", "other"
    is_compressed_switch: bool = False  # .nsz / .xcz
    is_multifile_switch: bool = False   # Base + Update + DLC bundle
    is_encrypted_wiiu: bool = False     # Encrypted WUP/NUS folder (title.tmd + *.app)
    wiiu_title_id: str | None = None
    wiiu_title_version: int | None = None
    wiiu_clean_title: str | None = None
    vita_title_id: str | None = None
    vita_app_version: str | None = None
    vita_category: str | None = None
    vita_clean_title: str | None = None
    vita_content_id: str | None = None
    vita_zrif: str | None = None

    @property
    def is_identified(self) -> bool:
        return self.system_id is not None


@dataclass(frozen=True, slots=True)
class ConversionArtifact:
    """Output produced by `converter.py` inside `.staging/<job_uuid>/` (or passthrough)."""

    system_id: str
    output_files: tuple[Path, ...]
    converted: bool
    tool_used: str | None = None
    is_installable_cia: bool = False
    is_vita_addon: bool = False
    is_directory: bool = False
    summary: str = ""


@dataclass(frozen=True, slots=True)
class RoutingOutcome:
    """Final ledger of an ingested job after atomic NAS routing and cleanup."""

    source_path: Path
    routed_files: tuple[Path, ...] = ()
    m3u_playlists: tuple[Path, ...] = ()
    quarantined_files: tuple[Path, ...] = ()
    post_action_taken: str = ""
    dry_run: bool = False
    additional_sources: tuple[Path, ...] = ()
    superseded_files: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class SystemConfig:
    """Routing and archive behavior for a single console/platform."""

    system_id: str
    folder: str
    archive_policy: ArchivePolicy


@dataclass(frozen=True, slots=True)
class BinaryPaths:
    """Resolved external CLI executable paths for the active host OS."""

    seven_zip: Path | None
    chdman: Path | None
    dolphin_tool: Path | None
    squirrel: Path | None
    squirrel_keys: Path | None
    psxpackager: Path | None = None
    ctr_keys: Path | None = None
    ctr_seeddb: Path | None = None
    zarchive: Path | None = None
    cdecrypt: Path | None = None
    pkg2zip: Path | None = None


@dataclass(frozen=True, slots=True)
class ConversionSettings:
    """Parameters controlling disc/ROM compression and format normalization."""

    psp_to_chd: bool = True
    psp_hunk_size: int = 2048
    rvz_block_size: int = 131072
    rvz_compression: str = "zstd"
    rvz_compression_level: int = 5
    switch_merge_format: str = "xci"
    decompress_nsz: bool = True
    rename_3ds_to_cci: bool = True
    cia_games_to_cci: bool = True
    cia_subdir: str = "_cia_install"
    generate_m3u: bool = True
    multidisc_subdir: str = "_discs"
    psx_multidisc_pbp: bool = True
    psx_pbp_compression_level: int = 5
    wiiu_to_wua: bool = True
    vita_repack_zip: bool = True
    vita_addons_subdir: str = "_updates_dlc"


@dataclass(frozen=True, slots=True)
class GeneralSettings:
    """Top-level pipeline behavior and safety thresholds."""

    post_process_action: PostProcessAction = PostProcessAction.MOVE
    stability_poll_seconds: float = 2.0
    stability_checks_required: int = 3
    min_free_staging_gb: float = 5.0


@dataclass(frozen=True, slots=True)
class PathSettings:
    """Resolved filesystem paths for the current host OS."""

    project_root: Path
    watch_dir: Path
    staging_dir: Path
    processed_dir: Path
    quarantine_dir: Path
    library_root: Path
    raw_library_root: str
    auto_mount_smb: bool = True


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    """Validated root configuration object for Völundr."""

    config_path: Path
    platform_key: str  # "windows" or "linux"
    general: GeneralSettings
    paths: PathSettings
    binaries: BinaryPaths
    conversion: ConversionSettings
    systems: dict[str, SystemConfig] = field(default_factory=dict)

    def system_dest_dir(self, system_id: str) -> Path:
        """Return the destination folder inside `library_root` for `system_id`."""
        sys_cfg = self.systems.get(system_id)
        if sys_cfg is None:
            raise KeyError(f"Unknown system_id: {system_id!r}")
        return self.paths.library_root / sys_cfg.folder
