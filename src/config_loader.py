"""Configuration loader, validator, external binary resolver, and SMB/GVFS mapper."""

from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from src.constants import (
    DEFAULT_CTR_KEY_RELPATHS,
    DEFAULT_CTR_SEEDDB_RELPATHS,
    DEFAULT_SWITCH_KEY_RELPATHS,
)
from src.models import (
    ArchivePolicy,
    BinaryPaths,
    ConversionSettings,
    GeneralSettings,
    PathSettings,
    PipelineConfig,
    PostProcessAction,
    SystemConfig,
)
from src.runner import run_capture


class ConfigError(ValueError):
    """Raised when `config.toml` is missing, malformed, or fails validation."""


class MissingConfigError(ConfigError):
    """Raised when the requested configuration file does not exist yet."""


def _format_missing_config_message(cfg_file: Path) -> str:
    """Build a friendly first-time setup guide when a config file has not been created yet."""
    example_candidates = [
        cfg_file.with_name(f"{cfg_file.stem}.example{cfg_file.suffix}"),
        cfg_file.parent / "config.example.toml",
    ]
    example_file = next((cand for cand in example_candidates if cand.is_file()), None)

    lines = [
        f"Configuration file not found: {cfg_file}",
        "",
        "Völundr keeps personal configuration files out of Git.",
    ]
    if example_file is not None:
        example_name = example_file.name
        target_name = cfg_file.name
        lines.extend([
            "First-time setup:",
            f"  1. Copy `{example_name}` to `{target_name}`:",
            f"       Windows (PowerShell): Copy-Item {example_name} {target_name}",
            f"       Linux / macOS:        cp {example_name} {target_name}",
            f"  2. Edit `{target_name}` and set `library_root` (and `watch_dir` if desired).",
            "  3. Run `python main.py doctor` to verify your paths and tools.",
        ])
    else:
        lines.extend([
            "First-time setup:",
            f"  1. Copy `config.example.toml` to `{cfg_file.name}` (or pass `--config <path>`).",
            f"  2. Edit `{cfg_file.name}` and set `library_root` for your OS.",
            "  3. Run `python main.py doctor` to verify your paths and tools.",
        ])
    return "\n".join(lines)


DEFAULT_PATH_LOOKUP_NAMES: dict[str, tuple[str, ...]] = {
    "seven_zip": ("7zz", "7z", "7za"),
    "chdman": ("chdman",),
    "dolphin_tool": ("DolphinTool", "dolphin-tool"),
    "squirrel": ("squirrel",),
    "psxpackager": ("psxpackager",),
    "zarchive": ("zarchive",),
    "cdecrypt": ("cdecrypt", "CDecrypt"),
    "pkg2zip": ("pkg2zip",),
}


@functools.lru_cache(maxsize=32)
def _check_seven_zip_rar_cached(resolved_bin: str, mtime_ns: int) -> bool:
    """Cached probe checking whether `resolved_bin` lists `rar` under `7z i` Formats."""
    _ = mtime_ns
    try:
        proc = run_capture([resolved_bin, "i"], timeout=5.0)
        if proc.returncode != 0:
            return False
        in_formats = False
        stdout = proc.stdout if isinstance(proc.stdout, str) else ""
        for line in stdout.splitlines():
            sline = line.strip()
            if sline.startswith("Formats:"):
                in_formats = True
                continue
            if in_formats and sline.startswith("Codecs:"):
                break
            if in_formats:
                parts = sline.split()
                if any(p.lower() == "rar" for p in parts):
                    return True
        return False
    except Exception:
        return False


def check_seven_zip_supports_rar(seven_zip_bin: Path | str | None) -> bool:
    """Return True if the 7z/7zz binary has RAR decompression support compiled in."""
    if seven_zip_bin is None:
        return False
    bin_path = Path(seven_zip_bin)
    try:
        st = bin_path.stat()
    except OSError:
        return False
    if not bin_path.is_file():
        return False
    return _check_seven_zip_rar_cached(str(bin_path.resolve()), st.st_mtime_ns)


def detect_platform_key(platform: str | None = None) -> str:
    """Normalize `sys.platform` into `'windows'` or `'linux'`."""
    plat = (platform or sys.platform).lower()
    if plat.startswith("win"):
        return "windows"
    if plat.startswith("linux"):
        return "linux"
    # Fallback to POSIX/linux semantics on macOS/BSD if ever invoked there
    return "linux"


def resolve_local_path(raw: str | Path, base_dir: Path) -> Path:
    """Resolve user-configured local paths (`~` expanded; relative anchored to `base_dir`)."""
    expanded = Path(raw).expanduser()
    if expanded.is_absolute():
        return expanded.resolve()
    return (base_dir / expanded).resolve()


def _parse_gvfs_tags(dirname: str) -> dict[str, str]:
    """Parse a GNOME GVFS mount folder name like `smb-share:server=192.0.2.10,share=data,user=alice`."""
    if not dirname.startswith("smb-share:"):
        return {}
    tag_part = dirname.removeprefix("smb-share:")
    tags: dict[str, str] = {}
    for item in tag_part.split(","):
        if "=" in item:
            k, v = item.split("=", 1)
            tags[k.strip().lower()] = v.strip().lower()
    return tags


def _find_matching_gvfs_mount(gvfs_root: Path, host: str, share: str) -> Path | None:
    """Find an active GNOME GVFS SMB mount matching `host` and `share` (case-insensitive)."""
    if not gvfs_root.is_dir():
        return None
    target_host = host.lower()
    target_share = share.lower()
    try:
        for entry in gvfs_root.iterdir():
            tags = _parse_gvfs_tags(entry.name)
            if tags.get("server") == target_host and tags.get("share") == target_share:
                return entry
    except OSError:
        return None
    return None


def parse_smb_uri(uri: str) -> tuple[str, str, tuple[str, ...]]:
    """
    Parse `smb://[user@]host[:port]/share[/sub/path]` into `(host, share, subpath_parts)`.

    Raises `ConfigError` if host or share is missing.
    """
    parsed = urlsplit(uri)
    if parsed.scheme.lower() != "smb" or not parsed.hostname:
        raise ConfigError(f"Invalid SMB URI (missing host): {uri!r}")

    parts = [unquote(p) for p in parsed.path.split("/") if p]
    if not parts:
        raise ConfigError(
            f"Invalid SMB URI (missing share name after host): {uri!r}. "
            "Expected format: smb://<ip-or-host>/<share>/games/library/roms"
        )
    share = parts[0]
    subpath_parts = tuple(parts[1:])
    return parsed.hostname, share, subpath_parts


def resolve_library_root(
    raw_root: str,
    base_dir: Path,
    platform_key: str,
) -> Path:
    """
    Resolve `library_root` across Windows drive/UNC paths and Linux `smb://` URIs.

    - On Linux: Translates `smb://<host>/<share>/<subpath>` to the
      GNOME GVFS FUSE mount directory under `$XDG_RUNTIME_DIR/gvfs/` (or `/run/user/<uid>/gvfs/`).
    - On Windows: Supports mapped drives (`Z:\\...`), UNC paths (`\\\\host\\share\\...`),
      and translates `smb://` URIs to UNC paths automatically.
    """
    cleaned = raw_root.strip()
    if not cleaned:
        raise ConfigError("`library_root` cannot be empty.")

    if cleaned.lower().startswith("smb://"):
        host, share, subpath_parts = parse_smb_uri(cleaned)
        if platform_key == "windows":
            unc_base = Path(f"//{host}/{share}")
            return unc_base.joinpath(*subpath_parts)

        # Linux (Fedora Workstation GNOME GVFS)
        xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
        if xdg_runtime:
            gvfs_root = Path(xdg_runtime) / "gvfs"
        else:
            uid = getattr(os, "getuid", lambda: 1000)()
            gvfs_root = Path("/run/user") / str(uid) / "gvfs"

        mount_dir = _find_matching_gvfs_mount(gvfs_root, host, share)
        if mount_dir is None:
            mount_dir = gvfs_root / f"smb-share:server={host.lower()},share={share.lower()}"
        return mount_dir.joinpath(*subpath_parts)

    expanded = Path(cleaned).expanduser()
    if expanded.is_absolute():
        return expanded
    return (base_dir / expanded).resolve()


def try_mount_smb_if_needed(paths: PathSettings, platform_key: str) -> tuple[Path, str | None]:
    """
    On Linux, if `raw_library_root` is an `smb://` URI that isn't mounted yet and
    `auto_mount_smb` is enabled, attempt `gio mount smb://<host>/<share>` using saved
    GNOME Keyring credentials and return the updated `(library_root, error_message)`.
    """
    if platform_key != "linux" or not paths.raw_library_root.lower().startswith("smb://"):
        return paths.library_root, None

    if paths.library_root.exists():
        return paths.library_root, None

    if not paths.auto_mount_smb:
        return paths.library_root, "SMB share is not mounted and `auto_mount_smb` is disabled."

    gio_bin = shutil.which("gio")
    if not gio_bin:
        return paths.library_root, "`gio` CLI not found to auto-mount `smb://` URI."

    host, share, _ = parse_smb_uri(paths.raw_library_root)
    share_uri = f"smb://{host}/{share}"
    try:
        proc = run_capture([gio_bin, "mount", share_uri], timeout=15.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return paths.library_root, f"`gio mount {share_uri}` failed: {exc}"

    refreshed = resolve_library_root(paths.raw_library_root, paths.project_root, platform_key)
    if refreshed.exists():
        return refreshed, None

    stderr = (proc.stderr or proc.stdout or "").strip()
    if "AlreadyMounted" in stderr or "already mounted" in stderr.lower():
        if refreshed.exists():
            return refreshed, None
    return (
        refreshed,
        stderr or f"Mount succeeded or exited ({proc.returncode}), but path not found: {refreshed}",
    )


def resolve_binary(
    tool_key: str,
    fallback_candidates: list[str],
    base_dir: Path,
) -> Path | None:
    """
    Locate an external CLI binary:
      1. Candidate paths from `config.toml` that point to existing files (e.g. `./tools/...`).
      2. `shutil.which()` (and active `.venv` bin/Scripts dir) against standard tool names.
      3. Fallback bare names or paths from `config.toml`.
    For `seven_zip`, if multiple candidates exist, prioritize one with RAR decompression support.
    """
    venv_bin_dir = Path(sys.executable).resolve().parent

    def _which_or_venv(cmd_name: str) -> Path | None:
        found = shutil.which(cmd_name)
        if found:
            return Path(found).resolve()
        venv_cand = venv_bin_dir / cmd_name
        if venv_cand.is_file():
            return venv_cand.resolve()
        return None

    discovered: list[Path] = []

    # 1. Check explicit file paths in candidates first (e.g. `./tools/7z/7zz`, `./tools/psxpackager/psxpackager`)
    for candidate in fallback_candidates:
        cand_str = candidate.strip()
        if not cand_str:
            continue
        if "/" in cand_str or "\\" in cand_str:
            cand_path = resolve_local_path(cand_str, base_dir)
            if cand_path.is_file() and cand_path not in discovered:
                discovered.append(cand_path)

    # 2. Check standard names on PATH (7zz before 7z so official upstream binary is prioritized)
    for name in DEFAULT_PATH_LOOKUP_NAMES.get(tool_key, ()):
        hit = _which_or_venv(name)
        if hit and hit not in discovered:
            discovered.append(hit)

    # 3. Check any remaining bare command candidates
    for candidate in fallback_candidates:
        cand_str = candidate.strip()
        if not cand_str or "/" in cand_str or "\\" in cand_str:
            continue
        hit = _which_or_venv(cand_str)
        if hit and hit not in discovered:
            discovered.append(hit)

    if not discovered:
        return None

    if tool_key == "seven_zip" and len(discovered) > 1:
        for cand in discovered:
            if check_seven_zip_supports_rar(cand):
                return cand

    return discovered[0]


def resolve_optional_file(raw: str | list[str] | None, base_dir: Path) -> Path | None:
    """Resolve an optional support file (e.g. `keys.txt`, `aes_keys.txt`) from candidate path(s)."""
    if not raw:
        return None
    candidates = [raw] if isinstance(raw, str) else raw
    for cand in candidates:
        cleaned = str(cand).strip()
        if not cleaned:
            continue
        path = resolve_local_path(cleaned, base_dir)
        if path.is_file():
            return path
    return None


def _as_str_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    raise ConfigError(f"Expected string or list of strings for `{field_name}`, got {type(value).__name__}.")


def load_config(
    config_path: str | Path = "config.toml",
    platform_override: str | None = None,
) -> PipelineConfig:
    """Parse, validate, and resolve `config.toml` into an immutable `PipelineConfig`."""
    cfg_file = Path(config_path).expanduser().resolve()
    if not cfg_file.is_file():
        raise MissingConfigError(_format_missing_config_message(cfg_file))

    try:
        with cfg_file.open("rb") as fh:
            raw_data = tomllib.load(fh)
    except FileNotFoundError as exc:
        raise MissingConfigError(_format_missing_config_message(cfg_file)) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML syntax in {cfg_file}: {exc}") from exc

    project_root = cfg_file.parent
    platform_key = detect_platform_key(platform_override)

    # 1. [general]
    gen_raw = raw_data.get("general", {})
    action_raw = str(gen_raw.get("post_process_action", "move")).lower()
    try:
        post_action = PostProcessAction(action_raw)
    except ValueError as exc:
        valid = ", ".join(a.value for a in PostProcessAction)
        raise ConfigError(
            f"Invalid `general.post_process_action` = {action_raw!r}. Must be one of: {valid}"
        ) from exc

    poll_sec = float(gen_raw.get("stability_poll_seconds", 2.0))
    checks_req = int(gen_raw.get("stability_checks_required", 3))
    min_free_gb = float(gen_raw.get("min_free_staging_gb", 5.0))
    if poll_sec <= 0:
        raise ConfigError("`general.stability_poll_seconds` must be > 0.")
    if checks_req < 1:
        raise ConfigError("`general.stability_checks_required` must be >= 1.")
    if min_free_gb < 0:
        raise ConfigError("`general.min_free_staging_gb` must be >= 0.")

    general = GeneralSettings(
        post_process_action=post_action,
        stability_poll_seconds=poll_sec,
        stability_checks_required=checks_req,
        min_free_staging_gb=min_free_gb,
    )

    # 2. [paths] + [paths.<platform>]
    paths_raw = raw_data.get("paths", {})
    plat_paths_raw = paths_raw.get(platform_key, {})

    def _pick_path(key: str, default: str) -> str:
        if isinstance(plat_paths_raw, dict) and key in plat_paths_raw:
            return str(plat_paths_raw[key])
        return str(paths_raw.get(key, default))

    raw_lib_root = _pick_path("library_root", "")
    if not raw_lib_root:
        raise ConfigError(
            f"Missing `paths.{platform_key}.library_root` (or `paths.library_root`) in {cfg_file}."
        )

    auto_mount_smb = bool(
        plat_paths_raw.get("auto_mount_smb", paths_raw.get("auto_mount_smb", True))
        if isinstance(plat_paths_raw, dict)
        else paths_raw.get("auto_mount_smb", True)
    )

    paths = PathSettings(
        project_root=project_root,
        watch_dir=resolve_local_path(_pick_path("watch_dir", "./drop"), project_root),
        staging_dir=resolve_local_path(_pick_path("staging_dir", "./.staging"), project_root),
        processed_dir=resolve_local_path(_pick_path("processed_dir", "./processed"), project_root),
        quarantine_dir=resolve_local_path(_pick_path("quarantine_dir", "./quarantine"), project_root),
        library_root=resolve_library_root(raw_lib_root, project_root, platform_key),
        raw_library_root=raw_lib_root,
        auto_mount_smb=auto_mount_smb,
    )

    # 3. [binaries.<platform>]
    bins_root = raw_data.get("binaries", {})
    plat_bins = bins_root.get(platform_key, {}) if isinstance(bins_root, dict) else {}
    if not isinstance(plat_bins, dict):
        plat_bins = {}

    binaries = BinaryPaths(
        seven_zip=resolve_binary(
            "seven_zip",
            _as_str_list(plat_bins.get("seven_zip"), f"binaries.{platform_key}.seven_zip"),
            project_root,
        ),
        chdman=resolve_binary(
            "chdman",
            _as_str_list(plat_bins.get("chdman"), f"binaries.{platform_key}.chdman"),
            project_root,
        ),
        dolphin_tool=resolve_binary(
            "dolphin_tool",
            _as_str_list(plat_bins.get("dolphin_tool"), f"binaries.{platform_key}.dolphin_tool"),
            project_root,
        ),
        squirrel=resolve_binary(
            "squirrel",
            _as_str_list(plat_bins.get("squirrel"), f"binaries.{platform_key}.squirrel"),
            project_root,
        ),
        squirrel_keys=resolve_optional_file(
            [
                *_as_str_list(plat_bins.get("squirrel_keys"), f"binaries.{platform_key}.squirrel_keys"),
                *DEFAULT_SWITCH_KEY_RELPATHS,
            ],
            project_root,
        ),
        psxpackager=resolve_binary(
            "psxpackager",
            _as_str_list(plat_bins.get("psxpackager"), f"binaries.{platform_key}.psxpackager"),
            project_root,
        ),
        ctr_keys=resolve_optional_file(
            plat_bins.get("ctr_keys") or list(DEFAULT_CTR_KEY_RELPATHS),
            project_root,
        ),
        ctr_seeddb=resolve_optional_file(
            plat_bins.get("ctr_seeddb") or list(DEFAULT_CTR_SEEDDB_RELPATHS),
            project_root,
        ),
        zarchive=resolve_binary(
            "zarchive",
            _as_str_list(plat_bins.get("zarchive"), f"binaries.{platform_key}.zarchive"),
            project_root,
        ),
        cdecrypt=resolve_binary(
            "cdecrypt",
            _as_str_list(plat_bins.get("cdecrypt"), f"binaries.{platform_key}.cdecrypt"),
            project_root,
        ),
        pkg2zip=resolve_binary(
            "pkg2zip",
            _as_str_list(plat_bins.get("pkg2zip"), f"binaries.{platform_key}.pkg2zip"),
            project_root,
        ),
    )

    # 4. [conversion]
    conv_raw = raw_data.get("conversion", {})
    merge_fmt = str(conv_raw.get("switch_merge_format", "xci")).lower()
    if merge_fmt not in ("xci", "nsp"):
        raise ConfigError("`conversion.switch_merge_format` must be 'xci' or 'nsp'.")

    pbp_level = int(conv_raw.get("psx_pbp_compression_level", 5))
    if not (0 <= pbp_level <= 9):
        raise ConfigError("`conversion.psx_pbp_compression_level` must be between 0 and 9.")

    conversion = ConversionSettings(
        psp_to_chd=bool(conv_raw.get("psp_to_chd", True)),
        psp_hunk_size=int(conv_raw.get("psp_hunk_size", 2048)),
        rvz_block_size=int(conv_raw.get("rvz_block_size", 131072)),
        rvz_compression=str(conv_raw.get("rvz_compression", "zstd")),
        rvz_compression_level=int(conv_raw.get("rvz_compression_level", 5)),
        switch_merge_format=merge_fmt,
        decompress_nsz=bool(conv_raw.get("decompress_nsz", True)),
        rename_3ds_to_cci=bool(conv_raw.get("rename_3ds_to_cci", True)),
        cia_games_to_cci=bool(conv_raw.get("cia_games_to_cci", True)),
        cia_subdir=str(conv_raw.get("cia_subdir", "_cia_install")).strip("/\\"),
        generate_m3u=bool(conv_raw.get("generate_m3u", True)),
        multidisc_subdir=str(conv_raw.get("multidisc_subdir", "_discs")).strip("/\\"),
        psx_multidisc_pbp=bool(conv_raw.get("psx_multidisc_pbp", True)),
        psx_pbp_compression_level=pbp_level,
        wiiu_to_wua=bool(conv_raw.get("wiiu_to_wua", True)),
        vita_repack_zip=bool(conv_raw.get("vita_repack_zip", True)),
        vita_addons_subdir=str(conv_raw.get("vita_addons_subdir", "_updates_dlc")).strip("/\\"),
    )

    # 5. [systems]
    systems_raw = raw_data.get("systems", {})
    if not isinstance(systems_raw, dict) or not systems_raw:
        raise ConfigError("`[systems]` table in config.toml cannot be empty.")

    systems: dict[str, SystemConfig] = {}
    for sys_id, entry in systems_raw.items():
        if not isinstance(entry, dict):
            raise ConfigError(f"`[systems.{sys_id}]` must be a TOML table.")
        folder = str(entry.get("folder", sys_id)).strip()
        if not folder:
            raise ConfigError(f"`systems.{sys_id}.folder` cannot be empty.")
        pol_raw = str(entry.get("archive_policy", "extract")).lower()
        try:
            policy = ArchivePolicy(pol_raw)
        except ValueError as exc:
            valid_pols = ", ".join(p.value for p in ArchivePolicy)
            raise ConfigError(
                f"Invalid `systems.{sys_id}.archive_policy` = {pol_raw!r}. Valid: {valid_pols}"
            ) from exc

        systems[sys_id.lower()] = SystemConfig(
            system_id=sys_id.lower(),
            folder=folder,
            archive_policy=policy,
        )

    return PipelineConfig(
        config_path=cfg_file,
        platform_key=platform_key,
        general=general,
        paths=paths,
        binaries=binaries,
        conversion=conversion,
        systems=systems,
    )


def ensure_local_directories(config: PipelineConfig) -> None:
    """Create local pipeline directories (`watch`, `staging`, `processed`, `quarantine`) if absent."""
    for folder in (
        config.paths.watch_dir,
        config.paths.staging_dir,
        config.paths.processed_dir,
        config.paths.quarantine_dir,
    ):
        folder.mkdir(parents=True, exist_ok=True)


def _format_toml_str(value: str, *, prefer_literal: bool = False) -> str:
    """Format a string safely as a TOML literal (`'...'`) or escaped basic string (`"..."`)."""
    cleaned = value.strip()
    if prefer_literal and "'" not in cleaned:
        return f"'{cleaned}'"
    escaped = cleaned.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _replace_toml_key_in_section(
    toml_text: str,
    section_header: str,
    key: str,
    formatted_value: str,
) -> str:
    """
    Replace `key = ...` inside `[section_header]` while preserving all comments and layout.
    If the key is missing from an existing section, inserts it at the end of that section.
    """
    lines = toml_text.splitlines(keepends=True)
    target_hdr = f"[{section_header}]"
    key_rx = re.compile(rf"^{re.escape(key)}\s*=")

    in_section = False
    found_section = False
    replaced = False
    section_end_idx: int | None = None
    out_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_section and not replaced:
                section_end_idx = len(out_lines)
            in_section = stripped == target_hdr
            if in_section:
                found_section = True
        elif in_section and not replaced and key_rx.match(stripped):
            eol = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
            out_lines.append(f"{key} = {formatted_value}{eol}")
            replaced = True
            continue
        out_lines.append(line)

    if not replaced:
        new_line = f"{key} = {formatted_value}\n"
        if found_section:
            insert_at = section_end_idx if section_end_idx is not None else len(out_lines)
            out_lines.insert(insert_at, new_line)
        else:
            if out_lines and not out_lines[-1].endswith("\n"):
                out_lines[-1] = out_lines[-1] + "\n"
            out_lines.append(f"\n{target_hdr}\n{new_line}")

    return "".join(out_lines)


def save_basic_settings(
    config_path: Path,
    platform_key: str,
    *,
    watch_dir: str | None = None,
    library_root: str | None = None,
    post_process_action: str | None = None,
) -> Path:
    """
    Update core settings (`watch_dir`, `library_root`, `post_process_action`) in `config.toml`
    while preserving user comments, validating TOML syntax before replacing atomically.
    """
    cfg_file = Path(config_path).expanduser().resolve()
    if not cfg_file.is_file():
        raise MissingConfigError(_format_missing_config_message(cfg_file))

    text = cfg_file.read_text(encoding="utf-8")

    if post_process_action is not None:
        action = post_process_action.strip().lower()
        try:
            PostProcessAction(action)
        except ValueError as exc:
            valid = ", ".join(a.value for a in PostProcessAction)
            raise ConfigError(
                f"Invalid `post_process_action` = {action!r}. Must be one of: {valid}"
            ) from exc
        text = _replace_toml_key_in_section(
            text, "general", "post_process_action", _format_toml_str(action)
        )

    if watch_dir is not None:
        cleaned_watch = watch_dir.strip()
        if not cleaned_watch:
            raise ConfigError("`watch_dir` cannot be empty.")
        text = _replace_toml_key_in_section(
            text,
            "paths",
            "watch_dir",
            _format_toml_str(cleaned_watch, prefer_literal=(platform_key == "windows" and "\\" in cleaned_watch)),
        )

    if library_root is not None:
        cleaned_lib = library_root.strip()
        if not cleaned_lib:
            raise ConfigError("`library_root` cannot be empty.")
        text = _replace_toml_key_in_section(
            text,
            f"paths.{platform_key}",
            "library_root",
            _format_toml_str(cleaned_lib, prefer_literal=(platform_key == "windows" and not cleaned_lib.lower().startswith("smb://"))),
        )

    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Generated invalid TOML while saving settings: {exc}") from exc

    tmp_file = cfg_file.with_name(f"~{cfg_file.name}.tmp")
    try:
        tmp_file.write_text(text, encoding="utf-8")
        os.replace(tmp_file, cfg_file)
    except OSError as exc:
        tmp_file.unlink(missing_ok=True)
        raise ConfigError(f"Failed writing `{cfg_file}`: {exc}") from exc

    return cfg_file


def save_advanced_settings(
    config_path: Path,
    *,
    general_updates: dict[str, int | float | bool | str] | None = None,
    conversion_updates: dict[str, int | float | bool | str] | None = None,
) -> Path:
    """
    Update advanced `[general]` and `[conversion]` fields in `config.toml` while preserving
    user comments, validating both TOML syntax and full `load_config()` rules before writing.
    """
    cfg_file = Path(config_path).expanduser().resolve()
    if not cfg_file.is_file():
        raise MissingConfigError(_format_missing_config_message(cfg_file))

    def _fmt_toml_val(val: int | float | bool | str) -> str:
        if isinstance(val, bool):
            return "true" if val else "false"
        if isinstance(val, int):
            return str(val)
        if isinstance(val, float):
            return f"{val:.2f}".rstrip("0").rstrip(".") if "." in f"{val:.2f}" else str(val)
        return _format_toml_str(str(val))

    text = cfg_file.read_text(encoding="utf-8")

    for key, val in (general_updates or {}).items():
        text = _replace_toml_key_in_section(text, "general", key, _fmt_toml_val(val))

    for key, val in (conversion_updates or {}).items():
        text = _replace_toml_key_in_section(text, "conversion", key, _fmt_toml_val(val))

    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Generated invalid TOML while saving advanced settings: {exc}") from exc

    tmp_file = cfg_file.with_name(f"~{cfg_file.name}.tmp")
    try:
        tmp_file.write_text(text, encoding="utf-8")
        # Validate full schema rules on temp file before replacing the live config.toml
        load_config(tmp_file)
        os.replace(tmp_file, cfg_file)
    except Exception as exc:
        tmp_file.unlink(missing_ok=True)
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError(f"Failed writing `{cfg_file}`: {exc}") from exc

    return cfg_file
