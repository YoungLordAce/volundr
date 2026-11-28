"""Console identification via unambiguous extensions, descriptor parsing, and binary magic sniffing."""

from __future__ import annotations

import base64
import functools
import re
import shlex
from pathlib import Path
import subprocess
import tempfile
from typing import BinaryIO
import xml.etree.ElementTree as ET
import zipfile
import zlib

from src.archive_handler import is_archive
from src.constants import IGNORED_FILENAMES, IGNORED_SIDECAR_EXTENSIONS
from src.ctr_decrypt import inspect_3ds_header, inspect_cia_header
from src.models import DetectionMethod, IdentificationResult
from src.runner import run_capture
from src.switch_bundler import (
    classify_switch_title_id,
    extract_switch_title_ids_from_file,
    switch_package_sort_key,
)

# Unambiguous single-system file extensions
UNAMBIGUOUS_EXTENSION_MAP: dict[str, str] = {
    # Nintendo handhelds
    ".gba": "gba",
    ".agb": "gba",
    ".nds": "nds",
    ".dsi": "nds",
    ".ids": "nds",
    ".3ds": "3ds",
    ".cci": "3ds",
    ".zcci": "3ds",
    ".3dsx": "3ds",
    ".cia": "3ds",
    # Nintendo consoles
    ".nes": "nes",
    ".fds": "nes",
    ".unf": "nes",
    ".unif": "nes",
    ".sfc": "snes",
    ".smc": "snes",
    ".fig": "snes",
    ".swc": "snes",
    ".n64": "n64",
    ".z64": "n64",
    ".v64": "n64",
    ".ndd": "n64",
    ".gcm": "gc",
    ".wbfs": "wii",
    ".wad": "wii",
    # Nintendo Wii U
    ".wua": "wiiu",
    ".wux": "wiiu",
    ".wud": "wiiu",
    ".wuhb": "wiiu",
    # Nintendo Switch
    ".nsp": "switch",
    ".xci": "switch",
    ".nsz": "switch",
    ".xcz": "switch",
    # Sony PSP & PS Vita
    ".cso": "psp",
    ".vpk": "psvita",
    ".dpt": "psvita",
    # Sega Dreamcast descriptor
    ".gdi": "dreamcast",
}

SWITCH_EXTENSIONS: frozenset[str] = frozenset({".nsp", ".xci", ".nsz", ".xcz"})
COMPRESSED_SWITCH_EXTENSIONS: frozenset[str] = frozenset({".nsz", ".xcz"})

# Extensions requiring binary magic / header inspection
AMBIGUOUS_DISC_EXTENSIONS: frozenset[str] = frozenset({
    ".iso", ".bin", ".img", ".mdf", ".raw", ".ciso", ".rvz", ".wia", ".gcz", ".pbp", ".chd", ".pkg",
})

# Binary Magic Constants
GC_MAGIC = b"\xc2\x33\x9f\x3d"     # Offset 0x1C (GameCube)
WII_MAGIC = b"\x5d\x1c\x9e\xa3"    # Offset 0x18 (Wii)
CD_SYNC_PATTERN = b"\x00\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\x00"

_BOOT2_RE = re.compile(rb"\bBOOT2\s*=", re.IGNORECASE)
_BOOT1_RE = re.compile(rb"\bBOOT\s*=", re.IGNORECASE)
_CUE_FILE_RE = re.compile(r"""^\s*FILE\s+(?:"([^"]+)"|'([^']+)'|(\S+))\s+(\S+)""", re.IGNORECASE)

# ROM-site domain watermarks, e.g. `(axekin.com)`, `[romsite.net]`, or leading `axekin.com Gear Solid`
_PROMO_TAG_RE = re.compile(
    r"[\s_]*[\(\[]\s*(?:www\.)?[a-z0-9\-]{3,}\.(?:com|net|org|gg|io|me|info|xyz|to|site|online|roms)\s*[\)\]]",
    re.IGNORECASE,
)
_LEADING_DOMAIN_RE = re.compile(
    r"^(?:www\.)?[a-z0-9\-]{3,}\.(?:com|net|org|gg|io|me|info|xyz|to)\b[\s_\-]*",
    re.IGNORECASE,
)
_TAG_SPLIT_RE = re.compile(r"\s*[\(\[]")


def _split_title_and_tags(stem: str) -> tuple[str, str]:
    """Split `'Metal Gear Solid (Europe) (Disc 1)'` -> `('Metal Gear Solid', '(Europe) (Disc 1)')`."""
    m = _TAG_SPLIT_RE.search(stem)
    if not m:
        return stem.strip(), ""
    return stem[: m.start()].strip(), stem[m.start() :].strip()


def sanitize_rom_stem(raw_stem: str, companion_paths: tuple[Path, ...] = ()) -> str:
    """
    Clean ROM-site promo watermarks and repair domain-clobbered `.cue` stems:
      - `'Gran Turismo (axekin.com)'` -> `'Gran Turismo'`
      - `'axekin.com Gear Solid (Europe) (Disc 1)'` + companion `'Metal Gear Solid (axekin.com) (Disc 1).bin'`
        -> `'Metal Gear Solid (Europe) (Disc 1)'`
    """
    cleaned = _PROMO_TAG_RE.sub("", raw_stem)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -_")

    lead_match = _LEADING_DOMAIN_RE.match(cleaned)
    if lead_match:
        remainder = cleaned[lead_match.end() :].strip()
        rem_title, rem_tags = _split_title_and_tags(remainder)

        donor_candidates: list[str] = [c.stem for c in companion_paths]
        if companion_paths:
            donor_candidates.append(companion_paths[0].parent.name)

        reconstructed: str | None = None
        for raw_donor in donor_candidates:
            donor_clean = _PROMO_TAG_RE.sub("", raw_donor)
            donor_clean = re.sub(r"\s{2,}", " ", donor_clean).strip(" -_")
            if _LEADING_DOMAIN_RE.match(donor_clean):
                continue
            donor_title, _ = _split_title_and_tags(donor_clean)
            if not donor_title:
                continue
            if rem_title and donor_title.lower().endswith(rem_title.lower()):
                reconstructed = f"{donor_title} {rem_tags}".strip()
                break
            if reconstructed is None:
                reconstructed = f"{donor_title} {rem_tags}".strip()

        if reconstructed:
            cleaned = reconstructed
        elif remainder:
            cleaned = remainder

    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned or raw_stem


# Matches `(Disc 1)`, `(Disk 02)`, `[CD 1]`, `- Disc 1 of 4`, etc.
_MULTIDISC_TOKEN_RE = re.compile(
    r"[\s\-_]*(?:\(\s*(?:disc|disk|cd)\s*0*(\d+)(?:\s*of\s*\d+)?\s*\)"
    r"|\[\s*(?:disc|disk|cd)\s*0*(\d+)(?:\s*of\s*\d+)?\s*\]"
    r"|(?:^|[\s\-_])(?:disc|disk|cd)\s*0*(\d+)(?:\s*of\s*\d+)?(?=$|[\s\-_\(\[]))",
    re.IGNORECASE,
)


def parse_multidisc_info(stem: str) -> tuple[str, int] | None:
    """
    If `stem` contains a multi-disc marker (`(Disc 1)`, `CD2`, etc.), return
    `(clean_base_title, disc_number)`. Otherwise return `None`.
    """
    match = _MULTIDISC_TOKEN_RE.search(stem)
    if not match:
        return None

    num_str = match.group(1) or match.group(2) or match.group(3)
    if not num_str:
        return None
    disc_num = int(num_str)

    # Strip the matched disc token while preserving surrounding region/revision tags
    cleaned = (stem[: match.start()] + " " + stem[match.end() :]).strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -_")
    if not cleaned:
        return None
    return cleaned, disc_num


class TruncatedPreviewError(RuntimeError):
    """Raised when an archive preview slice is shorter than a required high-LBA seek."""


class SectorReader:
    """
    Abstraction over cooked 2048-byte `.iso` images and raw 2352/2336-byte `.bin` tracks.

    Automatically detects sector size and user-data payload offset so callers can
    read logical 2048-byte sectors by LBA regardless of Mode 1 vs Mode 2 XA framing.
    """

    def __init__(self, fh: BinaryIO, actual_file_size: int, logical_file_size: int) -> None:
        self.fh = fh
        self.actual_file_size = actual_file_size
        self.logical_file_size = logical_file_size
        self.sector_size, self.data_offset, self.has_iso9660_pvd = self._detect_geometry()

    def _read_at(self, offset: int, length: int) -> bytes:
        if offset < 0:
            return b""
        if offset >= self.actual_file_size:
            if self.logical_file_size > self.actual_file_size and offset < self.logical_file_size:
                raise TruncatedPreviewError(
                    f"Seek offset {offset} exceeds preview buffer ({self.actual_file_size} bytes)"
                )
            return b""
        self.fh.seek(offset)
        return self.fh.read(length)

    def _detect_geometry(self) -> tuple[int, int, bool]:
        # 1. Check Sector 16 for ISO9660 Primary Volume Descriptor (`\x01CD001`)
        candidates = (
            (2048, 0),   # Standard cooked .iso (0x8000)
            (2352, 24),  # Raw CD Mode 2 XA Form 1 .bin (0x9318 — PS1 / PS2 CD)
            (2352, 16),  # Raw CD Mode 1 .bin (0x9310 — Saturn / Sega CD / Dreamcast)
            (2448, 24),  # CHD raw CD frame (2352B sector + 96B subchannel, Mode 2 XA)
            (2448, 16),  # CHD raw CD frame (2352B sector + 96B subchannel, Mode 1)
            (2448, 0),   # CHD cooked Mode 1 CD frame (2048B data + padding + 96B subchannel)
            (2336, 8),   # Sectorless-sync Mode 2 .bin
        )
        for sec_size, data_off in candidates:
            pvd_pos = 16 * sec_size + data_off
            sig = self._read_at(pvd_pos, 6)
            if sig == b"\x01CD001":
                return sec_size, data_off, True

        # 2. Check Sector 0 for raw 2352-byte CD sync header even if non-ISO9660
        head = self._read_at(0, 16)
        if len(head) == 16 and head[:12] == CD_SYNC_PATTERN:
            mode = head[15]
            return 2352, (24 if mode == 2 else 16), False

        return 2048, 0, False

    def read_raw(self, offset: int, length: int) -> bytes:
        return self._read_at(offset, length)

    def read_sector(self, lba: int) -> bytes:
        pos = lba * self.sector_size + self.data_offset
        return self._read_at(pos, 2048)

    def read_logical_bytes(self, start_lba: int, byte_length: int) -> bytes:
        chunks: list[bytes] = []
        remaining = byte_length
        lba = start_lba
        while remaining > 0:
            sec = self.read_sector(lba)
            if not sec:
                break
            take = min(len(sec), remaining)
            chunks.append(sec[:take])
            remaining -= take
            lba += 1
        return b"".join(chunks)


def _shares_meaningful_title_words(stem_a: str, stem_b: str) -> bool:
    """Return True if two ROM stems share at least one >=3-char title word (ignoring tags/domains)."""
    clean_a, _ = _split_title_and_tags(sanitize_rom_stem(stem_a))
    clean_b, _ = _split_title_and_tags(sanitize_rom_stem(stem_b))
    words_a = {w for w in re.findall(r"[a-z0-9]{3,}", clean_a.lower())}
    words_b = {w for w in re.findall(r"[a-z0-9]{3,}", clean_b.lower())}
    return bool(words_a & words_b)


def _find_case_insensitive_file(directory: Path, target_name: str) -> Path | None:
    """
    Resolve track filename inside `directory`:
      1. Exact file match (`directory / clean_name`).
      2. Case-insensitive match (for Linux compatibility when cues use Windows casing).
      3. Sanitized stem match (recovering from ROM-site watermark scripts, tag reordering, and double spaces).
      4. Track-number + title-sharing match (e.g. `(Track 01)` in both stems).
    """
    clean_name = target_name.replace("\\", "/").rsplit("/", 1)[-1]
    direct = directory / clean_name
    if direct.is_file():
        return direct
    lower_goal = clean_name.lower()
    try:
        children = [p for p in directory.iterdir() if p.is_file()]
    except OSError:
        return None

    # Pass 1: Case-insensitive match
    for child in children:
        if child.name.lower() == lower_goal:
            return child

    # Pass 2: Sanitized stem match (removes promo tags like (axekin.com), collapses spaces)
    target_path = Path(clean_name)
    target_ext = target_path.suffix.lower()
    clean_target_stem = sanitize_rom_stem(target_path.stem).lower()
    for child in children:
        if child.suffix.lower() == target_ext:
            if sanitize_rom_stem(child.stem).lower() == clean_target_stem:
                return child

    # Pass 3: Track number + title match
    track_m = re.search(r"\btrack\s*0*(\d+)\b", clean_name, re.IGNORECASE)
    if track_m:
        target_track_num = int(track_m.group(1))
        for child in children:
            if child.suffix.lower() == target_ext:
                child_track_m = re.search(r"\btrack\s*0*(\d+)\b", child.name, re.IGNORECASE)
                if child_track_m and int(child_track_m.group(1)) == target_track_num:
                    if _shares_meaningful_title_words(target_path.stem, child.stem):
                        return child

    return None


def parse_cue_tracks(cue_path: Path) -> list[Path]:
    """
    Parse `FILE "..."` directives from a `.cue` sheet and return existing track paths in order.

    Includes automatic recovery for single-track and multi-track `.cue` files whose internal `FILE`
    names were desynced from actual `.bin` filenames on disk by ROM-site watermark scripts.
    """
    tracks: list[Path] = []
    seen: set[Path] = set()
    declared_names: list[str] = []
    try:
        text = cue_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return tracks

    for line in text.splitlines():
        match = _CUE_FILE_RE.match(line)
        if not match:
            continue
        raw_name = match.group(1) or match.group(2) or match.group(3)
        if not raw_name:
            continue
        declared_names.append(raw_name)
        resolved = _find_case_insensitive_file(cue_path.parent, raw_name)
        if resolved is not None and resolved not in seen:
            seen.add(resolved)
            tracks.append(resolved)

    # Fallback 1: Desynced single-FILE `.cue` sheets (e.g. `.cue` says `(axekin.com)` while `.bin` says `(Europe)`)
    if len(declared_names) == 1 and not tracks:
        cue_dir = cue_path.parent
        # 1. Exact stem match (`<cue_stem>.bin` / `.img` / `.raw`)
        for track_ext in (".bin", ".img", ".raw"):
            same_stem = _find_case_insensitive_file(cue_dir, f"{cue_path.stem}{track_ext}")
            if same_stem is not None:
                return [same_stem]

        # 2. Sole binary track in the same folder that shares title words with the `.cue`
        try:
            bin_siblings = [
                p for p in cue_dir.iterdir()
                if p.is_file() and p.suffix.lower() in (".bin", ".img", ".raw")
            ]
        except OSError:
            bin_siblings = []

        if len(bin_siblings) == 1 and _shares_meaningful_title_words(cue_path.stem, bin_siblings[0].stem):
            return [bin_siblings[0]]

    # Fallback 2: Desynced multi-track `.cue` sheets where count matches available binary tracks
    if declared_names and len(tracks) < len(declared_names):
        cue_dir = cue_path.parent
        try:
            bin_siblings = [
                p for p in cue_dir.iterdir()
                if p.is_file() and p.suffix.lower() in (".bin", ".img", ".raw", ".iso")
            ]
        except OSError:
            bin_siblings = []

        if len(bin_siblings) == len(declared_names):
            if all(_shares_meaningful_title_words(cue_path.stem, b.stem) for b in bin_siblings):
                def _track_sort_key(p: Path) -> tuple[int, str]:
                    m = re.search(r"\btrack\s*0*(\d+)\b", p.name, re.IGNORECASE)
                    if m:
                        return (int(m.group(1)), p.name.lower())
                    return (9999, p.name.lower())

                return sorted(bin_siblings, key=_track_sort_key)

    return tracks


def parse_gdi_tracks(gdi_path: Path) -> list[Path]:
    """Parse track filenames from a Sega Dreamcast `.gdi` descriptor."""
    tracks: list[Path] = []
    seen: set[Path] = set()
    try:
        lines = gdi_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return tracks

    for idx, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line or (idx == 0 and line.isdigit()):
            continue
        try:
            parts = shlex.split(line)
        except ValueError:
            parts = line.split()
        # Format: <track_num> <lba> <type> <sector_size> <filename> <offset>
        if len(parts) >= 5:
            track_name = parts[4]
            resolved = _find_case_insensitive_file(gdi_path.parent, track_name)
            if resolved is not None and resolved not in seen:
                seen.add(resolved)
                tracks.append(resolved)
    return tracks


def _check_3ds_encryption(file_path: Path) -> tuple[bool, str]:
    """
    Inspect Nintendo 3DS NCSD/NCCH binary header to check if the image is still encrypted.

    Returns `(is_encrypted, detail_reason)`.
    """
    is_enc, _, detail = inspect_3ds_header(file_path)
    return is_enc, detail


def _sniff_rvz_or_wia(fh: BinaryIO) -> tuple[str | None, str]:
    """Identify already-compressed Dolphin `.rvz` / `.wia` files (`gc` vs `wii`)."""
    fh.seek(0)
    header = fh.read(0xD8)
    if len(header) < 0x78 or header[:4] not in (b"RVZ\x01", b"WIA\x01"):
        return None, ""
    fmt = header[:3].decode("ascii")
    # `wia_disc_t` begins at 0x48: uint32_be disc_type (1 = GC, 2 = Wii), dhead[128] at 0x58
    disc_type = int.from_bytes(header[0x48:0x4C], "big")
    dhead = header[0x58:0xD8]
    if dhead[0x18:0x1C] == WII_MAGIC or disc_type == 2:
        return "wii", f"{fmt} container header (`disc_type=Wii`)"
    if dhead[0x1C:0x20] == GC_MAGIC or disc_type == 1:
        return "gc", f"{fmt} container header (`disc_type=GameCube`)"
    return None, ""


def _sniff_ciso(fh: BinaryIO) -> tuple[str | None, str]:
    """Disambiguate `.ciso` between GameCube/Wii CISO (0x8000 header) and PSP CISO."""
    fh.seek(0)
    head = fh.read(0x8040)
    if len(head) < 32 or head[:4] != b"CISO":
        return None, ""
    if len(head) >= 0x8020:
        if head[0x8018:0x801C] == WII_MAGIC:
            return "wii", "CISO container with Wii magic `0x5D1C9EA3` at `0x8018`"
        if head[0x801C:0x8020] == GC_MAGIC:
            return "gc", "CISO container with GameCube magic `0xC2339F3D` at `0x801C`"
    return "psp", "PSP CISO compressed UMD header"


def _sniff_pbp(fh: BinaryIO) -> tuple[str | None, str]:
    """Disambiguate Sony `.pbp` EBOOTs between PS1 Classics (`psx`) and native PSP (`psp`)."""
    fh.seek(0)
    head = fh.read(40)
    if len(head) < 40 or head[:4] != b"\x00PBP":
        return None, ""
    psar_offset = int.from_bytes(head[36:40], "little")
    if psar_offset > 0:
        fh.seek(psar_offset)
        psar_sig = fh.read(16)
        if psar_sig.startswith((b"PSISOIMG0000", b"PSTITLEIMG0000")):
            return "psx", f"Sony PBP EBOOT containing PS1 `{psar_sig[:12].decode('ascii', 'ignore')}`"
    return "psp", "Sony PBP native PSP EBOOT"


def _parse_iso9660_root_entries(dir_bytes: bytes) -> dict[str, tuple[int, int, int]]:
    """
    Parse ISO9660 directory records into `{UPPER_NAME: (extent_lba, data_length, file_flags)}`.
    """
    entries: dict[str, tuple[int, int, int]] = {}
    pos = 0
    total = len(dir_bytes)
    while pos < total:
        rec_len = dir_bytes[pos]
        if rec_len == 0:
            # Directory records do not cross 2048-byte sector boundaries; advance to next sector
            next_sector = ((pos // 2048) + 1) * 2048
            if next_sector <= pos:
                break
            pos = next_sector
            continue
        if pos + rec_len > total or rec_len < 34:
            break

        rec = dir_bytes[pos : pos + rec_len]
        extent_lba = int.from_bytes(rec[2:6], "little")
        data_len = int.from_bytes(rec[10:14], "little")
        flags = rec[25]
        name_len = rec[32]
        raw_name = rec[33 : 33 + name_len]

        if raw_name not in (b"\x00", b"\x01"):
            name = raw_name.decode("latin-1", errors="ignore").split(";", 1)[0].strip().upper()
            if name:
                entries[name] = (extent_lba, data_len, flags)

        pos += rec_len
    return entries


def _inspect_iso9660(
    reader: SectorReader,
    effective_size: int,
    via_cue: bool,
    file_suffix: str,
) -> tuple[str | None, DetectionMethod, str, str | None]:
    """
    Inspect ISO9660 Primary Volume Descriptor, Root Directory, and `SYSTEM.CNF`
    to accurately distinguish `psp`, `ps2`, and `psx`.

    Returns `(system_id, method, reason, disc_media_type)`.
    """
    pvd = reader.read_sector(16)
    if len(pvd) < 2048 or pvd[:6] != b"\x01CD001":
        return None, DetectionMethod.UNIDENTIFIED, "", None

    sys_ident = pvd[8:40].decode("latin-1", errors="ignore").strip().upper()
    vol_ident = pvd[40:72].decode("latin-1", errors="ignore").strip()

    # 1. PSP Check via PVD System Identifier
    if "PSP GAME" in sys_ident:
        return (
            "psp",
            DetectionMethod.ISO9660_INSPECTION,
            f"ISO9660 PVD System Identifier = '{sys_ident}' (Vol='{vol_ident}')",
            "dvd",
        )

    # 2. Parse Root Directory Record (PVD offset 156..190)
    root_rec = pvd[156:190]
    root_lba = int.from_bytes(root_rec[2:6], "little")
    root_len = min(int.from_bytes(root_rec[10:14], "little"), 65536)

    root_entries: dict[str, tuple[int, int, int]] = {}
    if root_lba > 0 and root_len > 0:
        root_bytes = reader.read_logical_bytes(root_lba, root_len)
        root_entries = _parse_iso9660_root_entries(root_bytes)

    if "PSP_GAME" in root_entries or "UMD_DATA.BIN" in root_entries:
        return (
            "psp",
            DetectionMethod.ISO9660_INSPECTION,
            f"ISO9660 root directory contains PSP structure (`PSP_GAME` / `UMD_DATA.BIN`)",
            "dvd",
        )

    # 3. Inspect `SYSTEM.CNF` if present in root directory
    if "SYSTEM.CNF" in root_entries:
        cnf_lba, cnf_len, _ = root_entries["SYSTEM.CNF"]
        if cnf_lba > 0 and cnf_len > 0:
            system_cnf_bytes = reader.read_logical_bytes(cnf_lba, min(cnf_len, 4096))
            if _BOOT2_RE.search(system_cnf_bytes):
                is_cd = (via_cue or reader.sector_size == 2352) and effective_size < 750 * 1024 * 1024
                media = "cd" if is_cd else "dvd"
                return (
                    "ps2",
                    DetectionMethod.ISO9660_INSPECTION,
                    f"ISO9660 `SYSTEM.CNF` contains `BOOT2` (PS2 {media.upper()}, {effective_size / (1024**2):.0f} MiB)",
                    media,
                )
            if _BOOT1_RE.search(system_cnf_bytes):
                return (
                    "psx",
                    DetectionMethod.ISO9660_INSPECTION,
                    "ISO9660 `SYSTEM.CNF` contains `BOOT` (without `BOOT2` -> PlayStation 1)",
                    "cd",
                )

    # Fallback scan of buffered initial sectors (17..64) if directory record was non-standard
    max_sec = min(64, reader.actual_file_size // reader.sector_size)
    initial_window = b"".join(reader.read_sector(s) for s in range(17, max(17, max_sec)))
    if b"PSP_GAME" in initial_window:
        return (
            "psp",
            DetectionMethod.ISO9660_INSPECTION,
            "Found `PSP_GAME` directory record in initial ISO9660 sectors",
            "dvd",
        )

    if _BOOT2_RE.search(initial_window):
        is_cd = (via_cue or reader.sector_size == 2352) and effective_size < 750 * 1024 * 1024
        media = "cd" if is_cd else "dvd"
        return (
            "ps2",
            DetectionMethod.ISO9660_INSPECTION,
            f"ISO9660 `SYSTEM.CNF` contains `BOOT2` (PS2 {media.upper()}, {effective_size / (1024**2):.0f} MiB)",
            media,
        )

    if _BOOT1_RE.search(initial_window):
        return (
            "psx",
            DetectionMethod.ISO9660_INSPECTION,
            "ISO9660 `SYSTEM.CNF` contains `BOOT` (without `BOOT2` -> PlayStation 1)",
            "cd",
        )

    # 4. PlayStation PVD + UDF / Size / Structure Fallbacks (Spec Section 4C)
    sec4 = reader.read_sector(4)
    has_sony_license = b"Sony Computer Entertainment" in sec4 or b"Licensed  by" in sec4
    if "PLAYSTATION" in sys_ident or has_sony_license:
        has_udf = any(sig in initial_window for sig in (b"NSR02", b"NSR03", b"BEA01"))
        if (file_suffix == ".iso" and effective_size > 800 * 1024 * 1024) or has_udf:
            return (
                "ps2",
                DetectionMethod.ISO9660_INSPECTION,
                f"ISO9660 PVD='PLAYSTATION' + DVD size/UDF ({effective_size / (1024**2):.0f} MiB) -> ps2",
                "dvd",
            )
        if via_cue or reader.sector_size == 2352 or file_suffix in (".bin", ".img", ".iso"):
            return (
                "psx",
                DetectionMethod.ISO9660_INSPECTION,
                f"ISO9660 PVD='PLAYSTATION' CD image ({effective_size / (1024**2):.0f} MiB) -> psx",
                "cd",
            )

    return None, DetectionMethod.UNIDENTIFIED, "", None


def _sniff_chd(
    chd_path: Path,
    chdman_bin: Path | None,
) -> tuple[str | None, DetectionMethod, str, str | None]:
    """
    Inspect a pre-compressed `.chd` container:
      1. Read CHD v5 header (`MComprHD`) to obtain uncompressed `logicalbytes`.
      2. If `chdman` is available, extract only the first 4 MiB of raw sectors via
         `chdman extractraw -ib` into a temp buffer and pass to `sniff_disc_image()`.
    """
    try:
        with chd_path.open("rb") as fh:
            hdr = fh.read(64)
    except OSError:
        return None, DetectionMethod.UNIDENTIFIED, "", None

    if len(hdr) < 64 or hdr[:8] != b"MComprHD":
        return None, DetectionMethod.UNIDENTIFIED, "Invalid CHD header signature", None

    version = int.from_bytes(hdr[12:16], "big")
    logical_bytes = int.from_bytes(hdr[32:40], "big") if version == 5 else chd_path.stat().st_size

    if chdman_bin is None or not chdman_bin.is_file():
        return (
            None,
            DetectionMethod.UNIDENTIFIED,
            "`chdman` binary required to inspect internal sectors of `.chd` files",
            None,
        )

    slice_bytes = min(logical_bytes, 4 * 1024 * 1024)
    with tempfile.TemporaryDirectory() as tmp:
        raw_slice = Path(tmp) / "chd_header_slice.bin"
        cmd = [
            str(chdman_bin),
            "extractraw",
            "-i",
            str(chd_path),
            "-o",
            str(raw_slice),
            "-ib",
            str(slice_bytes),
            "-f",
        ]
        try:
            proc = run_capture(cmd, timeout=30.0)
        except (OSError, subprocess.TimeoutExpired):
            return None, DetectionMethod.UNIDENTIFIED, "Timed out running `chdman extractraw`", None

        if proc.returncode != 0 or not raw_slice.is_file():
            return None, DetectionMethod.UNIDENTIFIED, "`chdman extractraw` could not read CHD header", None

        sys_id, method, reason, media = sniff_disc_image(raw_slice, logical_size=logical_bytes)
        if sys_id:
            return sys_id, method, f"Inside `.chd`: {reason}", media

    return None, DetectionMethod.UNIDENTIFIED, f"Unrecognized disc payload inside `{chd_path.name}`", None


def sniff_disc_image(
    track_path: Path,
    logical_size: int | None = None,
    via_cue: bool = False,
    chdman_bin: Path | None = None,
) -> tuple[str | None, DetectionMethod, str, str | None]:
    """
    Binary magic-byte and ISO9660 volume sniffer for ambiguous disc images.

    Returns `(system_id, detection_method, reason, disc_media_type)`.
    """
    ext = track_path.suffix.lower()
    if ext == ".chd":
        return _sniff_chd(track_path, chdman_bin)

    actual_size = track_path.stat().st_size
    effective_size = logical_size if logical_size is not None else actual_size

    with track_path.open("rb") as fh:
        # Container-specific headers first (.rvz, .wia, .ciso, .pbp, .wbfs)
        if ext in (".rvz", ".wia"):
            sys_id, reason = _sniff_rvz_or_wia(fh)
            if sys_id:
                return sys_id, DetectionMethod.BINARY_MAGIC, reason, "dvd"

        if ext == ".ciso":
            sys_id, reason = _sniff_ciso(fh)
            if sys_id:
                return sys_id, DetectionMethod.BINARY_MAGIC, reason, "dvd"

        if ext == ".pbp":
            sys_id, reason = _sniff_pbp(fh)
            if sys_id:
                return sys_id, DetectionMethod.BINARY_MAGIC, reason, "cd"

        reader = SectorReader(fh, actual_size, effective_size)
        raw0 = reader.read_raw(0, 64)
        sec0 = reader.read_sector(0)

        # 1. Wii WBFS or Raw Disc Magic (`0x5D1C9EA3` at offset 0x18)
        if raw0[:4] == b"WBFS":
            return "wii", DetectionMethod.BINARY_MAGIC, "WBFS header magic at offset `0x00`", "dvd"
        if len(raw0) >= 0x1C and raw0[0x18:0x1C] == WII_MAGIC:
            return "wii", DetectionMethod.BINARY_MAGIC, "Wii magic `0x5D1C9EA3` at offset `0x18`", "dvd"

        # 2. GameCube Raw Disc Magic (`0xC2339F3D` at offset 0x1C)
        if len(raw0) >= 0x20 and raw0[0x1C:0x20] == GC_MAGIC:
            return "gc", DetectionMethod.BINARY_MAGIC, "GameCube magic `0xC2339F3D` at offset `0x1C`", "dvd"

        # 3. Sega Saturn / Dreamcast / Sega CD at offset 0x00 or 0x10
        header_slices = (raw0[:32], raw0[0x10:0x30], sec0[:32])
        if any(s.startswith(b"SEGA SEGASATURN") for s in header_slices):
            return (
                "saturn",
                DetectionMethod.BINARY_MAGIC,
                "ASCII magic `'SEGA SEGASATURN'` in sector 0",
                "cd",
            )
        if any(s.startswith(b"SEGA SEGAKATANA") for s in header_slices):
            return (
                "dreamcast",
                DetectionMethod.BINARY_MAGIC,
                "ASCII magic `'SEGA SEGAKATANA'` in sector 0",
                "cd",
            )
        if any(s.startswith((b"SEGADISCSYSTEM", b"SEGABOOTDISC")) for s in header_slices):
            return (
                "segacd",
                DetectionMethod.BINARY_MAGIC,
                "ASCII magic `'SEGADISCSYSTEM'` / `'SEGABOOTDISC'` in sector 0",
                "cd",
            )

        # 4. PC Engine CD / TurboGrafx-CD IPL signature in Sector 0 or Sector 1
        sec1 = reader.read_sector(1)
        if b"PC Engine CD-ROM SYSTEM" in sec0 or b"PC Engine CD-ROM SYSTEM" in sec1:
            return (
                "pcenginecd",
                DetectionMethod.BINARY_MAGIC,
                "NEC IPL signature `'PC Engine CD-ROM SYSTEM'` in boot sector",
                "cd",
            )

        # 5. PSP vs PS1 vs PS2 via ISO9660 PVD & `SYSTEM.CNF`
        if reader.has_iso9660_pvd:
            sys_id, method, reason, media = _inspect_iso9660(
                reader,
                effective_size=effective_size,
                via_cue=via_cue,
                file_suffix=ext,
            )
            if sys_id is not None:
                return sys_id, method, reason, media

    return (
        None,
        DetectionMethod.UNIDENTIFIED,
        f"No known console binary signature matched `{track_path.name}`",
        None,
    )


def identify_single_file(
    file_path: Path,
    logical_size: int | None = None,
    chdman_bin: Path | None = None,
    project_root: Path | None = None,
) -> IdentificationResult:
    """Identify a standalone file (unambiguous extension, `.cue`/`.gdi`, or binary disc image)."""
    ext = file_path.suffix.lower()

    # 1. `.gdi` Dreamcast descriptor
    if ext == ".gdi":
        tracks = parse_gdi_tracks(file_path)
        return IdentificationResult(
            system_id="dreamcast",
            method=DetectionMethod.DESCRIPTOR_TRACK_SNIFF,
            confidence="high",
            reason=f"Sega Dreamcast `.gdi` descriptor ({len(tracks)} track files grouped)",
            primary_file=file_path,
            companion_files=tuple(tracks),
            disc_media_type="cd",
        )

    # 2. `.cue` CD descriptor -> parse Track 01 and sniff binary header!
    if ext == ".cue":
        tracks = parse_cue_tracks(file_path)
        if not tracks:
            return IdentificationResult(
                system_id=None,
                method=DetectionMethod.UNIDENTIFIED,
                confidence="low",
                reason=f"`.cue` sheet `{file_path.name}` references no existing track files",
                primary_file=file_path,
            )
        track01 = tracks[0]
        sys_id, _, reason, media = sniff_disc_image(
            track01,
            logical_size=logical_size,
            via_cue=True,
        )

        # Also find and claim companion .sbi / .sub / .ccd subchannel files alongside .cue or in its directory
        parent_dir = file_path.resolve().parent
        sbi_in_dir = (
            [f.resolve() for f in parent_dir.iterdir() if f.is_file() and f.suffix.lower() in (".sbi", ".lsd")]
            if parent_dir.is_dir()
            else []
        )
        sbi_candidates = [
            file_path.with_suffix(".sbi"),
            file_path.with_suffix(".lsd"),
            *(t.with_suffix(".sbi") for t in tracks),
            *(t.with_suffix(".lsd") for t in tracks),
            *sbi_in_dir,
        ]
        extra_companions = [s.resolve() for s in sbi_candidates if s.is_file()]
        all_companions = tuple(dict.fromkeys((*tracks, *extra_companions)))

        return IdentificationResult(
            system_id=sys_id,
            method=DetectionMethod.DESCRIPTOR_TRACK_SNIFF if sys_id else DetectionMethod.UNIDENTIFIED,
            confidence="high" if sys_id else "low",
            reason=f"Via `{file_path.name}` -> `{track01.name}`: {reason}",
            primary_file=file_path,
            companion_files=all_companions,
            disc_media_type=media or "cd",
        )

    # 3. Unambiguous Console / Handheld Extensions
    if ext in UNAMBIGUOUS_EXTENSION_MAP:
        sys_id = UNAMBIGUOUS_EXTENSION_MAP[ext]
        is_enc_3ds = False
        is_zk_3ds = False
        is_cia = ext == ".cia"
        cia_tid: str | None = None
        cia_cat: str | None = None
        is_comp_sw = ext in COMPRESSED_SWITCH_EXTENSIONS
        vita_tid: str | None = None
        vita_ver: str | None = None
        vita_cat: str | None = None
        vita_title: str | None = None
        reason = f"Unambiguous extension `{ext}` -> {sys_id}"

        if sys_id == "3ds" and ext in (".3ds", ".cci"):
            is_enc_3ds, is_zk_3ds, enc_detail = inspect_3ds_header(file_path)
            reason = f"`{ext}` -> 3ds ({enc_detail})"
        elif is_cia:
            cia_info = inspect_cia_header(file_path)
            if cia_info is not None:
                cia_tid = cia_info.title_id
                cia_cat = cia_info.category
                reason = cia_info.detail
            else:
                reason = "`.cia` installable 3DS package (update/DLC/title)"
        elif is_comp_sw:
            reason = f"Compressed Switch package `{ext}` (requires decompression for Eden)"
        elif ext == ".vpk":
            vita_tid, vita_ver, vita_cat, vita_title = _inspect_vpk_package(file_path)
            if vita_tid:
                ver_tag = f" v{vita_ver}" if vita_ver else ""
                title_tag = f" `{vita_title}`" if vita_title else ""
                reason = f"PS Vita `.vpk` package{title_tag} [{vita_tid}{ver_tag}]"
        elif ext == ".dpt":
            vita_tid, vita_title = _inspect_vita_dpt_file(file_path)
            if vita_tid:
                reason = f"Cocoon FE `.dpt` Vita tag `{vita_title or file_path.stem}` [{vita_tid}]"

        return IdentificationResult(
            system_id=sys_id,
            method=DetectionMethod.UNAMBIGUOUS_EXTENSION,
            confidence="high",
            reason=reason,
            primary_file=file_path,
            is_encrypted_3ds=is_enc_3ds,
            is_zerokey_3ds=is_zk_3ds,
            is_installable_cia=is_cia,
            cia_title_id=cia_tid,
            cia_category=cia_cat,
            is_compressed_switch=is_comp_sw,
            vita_title_id=vita_tid,
            vita_app_version=vita_ver,
            vita_category=vita_cat,
            vita_clean_title=vita_title,
        )

    # 3b. Sony `.pkg` package -> Binary Magic (`\x7fPKG` + Content ID at offset 0x30 + embedded `\x00PSF`)
    if ext == ".pkg":
        pkg_info = _inspect_sony_pkg_header(file_path, project_root=project_root)
        if pkg_info is not None:
            v_tid, v_ver, v_cat, v_title, v_cid, v_zrif, pkg_reason = pkg_info
            companions = _find_pkg_license_companions(file_path)
            return IdentificationResult(
                system_id="psvita",
                method=DetectionMethod.BINARY_MAGIC,
                confidence="high",
                reason=pkg_reason,
                primary_file=file_path,
                companion_files=companions,
                vita_title_id=v_tid,
                vita_app_version=v_ver,
                vita_category=v_cat,
                vita_clean_title=v_title,
                vita_content_id=v_cid,
                vita_zrif=v_zrif,
            )
        return IdentificationResult(
            system_id=None,
            method=DetectionMethod.UNIDENTIFIED,
            confidence="low",
            reason=f"`.pkg` file `{file_path.name}` is not a recognized PS Vita package",
            primary_file=file_path,
        )

    # 4. Ambiguous Disc / Container Extensions -> Binary Sniffing
    if ext in AMBIGUOUS_DISC_EXTENSIONS:
        sys_id, method, reason, media = sniff_disc_image(
            file_path,
            logical_size=logical_size,
            chdman_bin=chdman_bin,
        )
        return IdentificationResult(
            system_id=sys_id,
            method=method,
            confidence="high" if sys_id else "low",
            reason=reason,
            primary_file=file_path,
            disc_media_type=media,
        )

    return IdentificationResult(
        system_id=None,
        method=DetectionMethod.UNIDENTIFIED,
        confidence="low",
        reason=f"Unrecognized file extension `{ext or '(none)'}`",
        primary_file=file_path,
    )


_WIIU_NUS_ROOT_FILES: frozenset[str] = frozenset({
    "title.tmd", "title.tik", "title.cert", "title.fst",
})


def _sanitize_wiiu_title(raw_title: str) -> str:
    """Normalize a Wii U `meta.xml` title string into a clean cross-platform filename."""
    cleaned = raw_title.replace(":", " -")
    cleaned = re.sub(r"[\r\n\t]+", " ", cleaned)
    cleaned = re.sub(r'[<>:"/\\|?*]', "", cleaned)
    return re.sub(r"\s{2,}", " ", cleaned).strip(" .-_")


def _parse_wiiu_meta_xml(meta_xml: Path) -> tuple[str | None, int | None, str | None]:
    """Extract `(title_id_hex, title_version, clean_title)` from Wii U `meta/meta.xml`."""
    if not meta_xml.is_file():
        return None, None, None
    try:
        root = ET.parse(meta_xml).getroot()
    except Exception:
        return None, None, None

    tid_text = (root.findtext("title_id") or "").strip().upper()
    title_id = tid_text if re.fullmatch(r"[0-9A-F]{16}", tid_text) else None

    ver_text = (root.findtext("title_version") or "").strip()
    title_ver: int | None = int(ver_text) if ver_text.isdigit() else None

    raw_name = (
        root.findtext("longname_en")
        or root.findtext("shortname_en")
        or root.findtext("longname_ja")
        or ""
    ).strip()
    clean_title = _sanitize_wiiu_title(raw_name) if raw_name else None
    return title_id, title_ver, clean_title or None


def _parse_wiiu_tmd_header(tmd_path: Path) -> tuple[str | None, int | None]:
    """Read Title ID and Title Version from a big-endian Wii U `title.tmd` binary header."""
    try:
        data = tmd_path.read_bytes()
        if len(data) < 0x1DE:
            return None, None
        title_id = data[0x18C:0x194].hex().upper()
        title_ver = int.from_bytes(data[0x1DC:0x1DE], "big")
        return title_id, title_ver
    except OSError:
        return None, None


def _is_wiiu_encrypted_nus_blob(file_path: Path, title_root: Path) -> bool:
    """Return True for encrypted WUP/NUS files sitting at `title_root` alongside Loadiine data."""
    if file_path.parent != title_root:
        return False
    return file_path.suffix.lower() in (".app", ".h3") or file_path.name.lower() in _WIIU_NUS_ROOT_FILES


_VITA_TITLE_ID_RE = re.compile(r"\b(PCS[A-I]\d{5}|VLJM\d{5}|VLJS\d{5}|VLAS\d{5}|VCAS\d{5}|VCJS\d{5})\b", re.IGNORECASE)


def parse_sfo_bytes(data: bytes) -> dict[str, str | int]:
    """
    Parse a Sony `param.sfo` (`\\x00PSF` v1.1) binary table into a `{key: value}` dict.

    Supports UTF-8 special (`0x0004`), UTF-8 null-terminated (`0x0204`), and `uint32` (`0x0404`).
    """
    if len(data) < 20 or data[:4] != b"\x00PSF":
        return {}
    key_table_off = int.from_bytes(data[8:12], "little")
    data_table_off = int.from_bytes(data[12:16], "little")
    num_entries = int.from_bytes(data[16:20], "little")
    if num_entries <= 0 or num_entries > 512:
        return {}

    parsed: dict[str, str | int] = {}
    for i in range(num_entries):
        entry_off = 20 + (i * 16)
        if entry_off + 16 > len(data):
            break
        key_rel = int.from_bytes(data[entry_off : entry_off + 2], "little")
        param_fmt = int.from_bytes(data[entry_off + 2 : entry_off + 4], "little")
        param_len = int.from_bytes(data[entry_off + 4 : entry_off + 8], "little")
        data_rel = int.from_bytes(data[entry_off + 12 : entry_off + 16], "little")

        k_start = key_table_off + key_rel
        if k_start >= len(data):
            continue
        k_end = data.find(b"\x00", k_start)
        if k_end == -1:
            k_end = len(data)
        key = data[k_start:k_end].decode("utf-8", errors="ignore").strip()
        if not key:
            continue

        v_start = data_table_off + data_rel
        v_end = min(v_start + param_len, len(data))
        if v_start >= len(data) or v_end < v_start:
            continue
        raw_val = data[v_start:v_end]

        if param_fmt in (0x0004, 0x0204):
            val_str = raw_val.rstrip(b"\x00").decode("utf-8", errors="ignore").strip()
            parsed[key] = val_str
        elif param_fmt == 0x0404 and len(raw_val) >= 4:
            parsed[key] = int.from_bytes(raw_val[:4], "little")

    return parsed


def _extract_vita_sfo_metadata(
    sfo_bytes: bytes,
) -> tuple[str | None, str | None, str | None, str | None]:
    """
    Extract `(title_id, app_version, category, clean_title)` from a PS Vita `sce_sys/param.sfo`.
    """
    fields = parse_sfo_bytes(sfo_bytes)
    if not fields:
        return None, None, None, None

    raw_tid = str(fields.get("TITLE_ID", "")).strip().upper()
    title_id = raw_tid if re.fullmatch(r"[A-Z]{4}\d{5}", raw_tid) else None

    raw_ver = str(fields.get("APP_VER") or fields.get("VERSION") or "").strip()
    app_ver = raw_ver if raw_ver else None

    raw_cat = str(fields.get("CATEGORY", "")).strip().lower()
    category = raw_cat if raw_cat else None

    raw_title = str(fields.get("TITLE") or fields.get("STITLE") or "").strip()
    clean_title = _sanitize_wiiu_title(raw_title) if raw_title else None

    return title_id, app_ver, category, clean_title or None


def _inspect_vpk_package(
    file_path: Path,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Peek inside a `.vpk` (ZIP archive) for `sce_sys/param.sfo` metadata."""
    try:
        if not file_path.is_file() or not zipfile.is_zipfile(file_path):
            return None, None, None, None
        with zipfile.ZipFile(file_path, "r") as zf:
            for name in zf.namelist():
                norm = name.replace("\\", "/").lstrip("/")
                if norm.lower() == "sce_sys/param.sfo" or norm.lower().endswith("/sce_sys/param.sfo"):
                    sfo_bytes = zf.read(name)
                    return _extract_vita_sfo_metadata(sfo_bytes)
    except Exception:
        pass
    return None, None, None, None


_VITA_DPT_TAG_RE = re.compile(r"\[\s*vita_game_id\s*\]\s*([A-Z]{4}\d{5})", re.IGNORECASE)


def _inspect_vita_dpt_file(file_path: Path) -> tuple[str | None, str | None]:
    """Inspect a Cocoon FE `.dpt` tag file (`[vita_game_id] PCSE00120`) for its Title ID."""
    try:
        text = file_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None, None
    m = _VITA_DPT_TAG_RE.search(text) or _VITA_TITLE_ID_RE.search(text)
    tid = m.group(1).upper() if m else None
    clean_title = sanitize_rom_stem(file_path.stem) or None
    return tid, clean_title


def _find_pkg_license_companions(pkg_path: Path) -> tuple[Path, ...]:
    """Find any companion `work.bin` or `.zrif` license files next to a Sony `.pkg`."""
    parent = pkg_path.resolve().parent
    if not parent.is_dir():
        return ()
    candidates = [
        parent / "work.bin",
        pkg_path.with_suffix(".zrif"),
        parent / f"{pkg_path.name}.zrif",
        parent / "zrif.txt",
    ]
    found: list[Path] = []
    for c in candidates:
        if c.is_file() and c.resolve() != pkg_path.resolve():
            found.append(c.resolve())
    return tuple(dict.fromkeys(found))


_ZRIF_DICT: bytes = zlib.decompress(
    base64.b64decode(
        b"eNpjYBgFo2AU0AsYAIElGt8MRJiDCAsw3xhEmIAIU4N4AwNdRxcXZ3+/EJCAkW6Ac7C7ARwYgviuQAaIdoPSzlDaBUo7QmknIM3ACIZM78+u7kx3VWYEAGJ9HV0="
    )
)


def decode_zrif_to_work_bin(zrif: str) -> bytes:
    """Decompress a NoPayStation `zRIF` base64 string into a 512-byte Sony RIF (`work.bin`)."""
    raw = base64.b64decode(zrif.strip().encode("ascii"))
    decomp = zlib.decompressobj(wbits=10, zdict=_ZRIF_DICT)
    return decomp.decompress(raw) + decomp.flush()


def encode_work_bin_to_zrif(rif_bytes: bytes) -> str:
    """Compress a 512-byte Sony RIF (`work.bin`) into a NoPayStation `zRIF` base64 string."""
    comp = zlib.compressobj(level=9, wbits=10, memLevel=8, zdict=_ZRIF_DICT)
    compressed = comp.compress(rif_bytes) + comp.flush()
    if len(compressed) % 3 != 0:
        compressed += b"\x00" * (3 - (len(compressed) % 3))
    return base64.b64encode(compressed).decode("ascii")


@functools.lru_cache(maxsize=16)
def _parse_vita_tsv_file(
    tsv_path_str: str,
    mtime_ns: int,
    file_size: int,
) -> tuple[dict[str, tuple[str, str]], dict[str, tuple[str, str]]]:
    """
    Parse a NoPayStation TSV (`PSV_GAMES.tsv`, `PSV_DLCS.tsv`) into:
      - `by_content_id`: `{CONTENT_ID_UPPER: (zrif, official_name)}`
      - `by_title_id`:   `{TITLE_ID_UPPER: (zrif, official_name)}`
    """
    _ = (mtime_ns, file_size)
    by_cid: dict[str, tuple[str, str]] = {}
    by_tid: dict[str, tuple[str, str]] = {}
    tsv_path = Path(tsv_path_str)
    try:
        lines = tsv_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return by_cid, by_tid

    if not lines:
        return by_cid, by_tid

    headers = [h.strip().lower() for h in lines[0].split("\t")]
    tid_idx = headers.index("title id") if "title id" in headers else 0
    name_idx = headers.index("name") if "name" in headers else 2
    zrif_idx = headers.index("zrif") if "zrif" in headers else -1
    cid_idx = headers.index("content id") if "content id" in headers else -1
    is_dlc_tsv = "dlc" in tsv_path.stem.lower()

    req_cols = [c for c in (tid_idx, name_idx, zrif_idx, cid_idx) if c >= 0]
    max_col = max(req_cols) if req_cols else 0
    for line in lines[1:]:
        cols = line.split("\t")
        if len(cols) <= max_col:
            continue
        tid = cols[tid_idx].strip().upper() if tid_idx >= 0 else ""
        name = cols[name_idx].strip() if name_idx >= 0 else ""
        zrif = cols[zrif_idx].strip() if zrif_idx >= 0 else ""
        cid = cols[cid_idx].strip().upper() if cid_idx >= 0 else ""

        valid_zrif = zrif if (zrif and zrif.upper() not in ("MISSING", "NOT REQUIRED", "N/A")) else ""
        if cid:
            if cid not in by_cid or (valid_zrif and not by_cid[cid][0]):
                by_cid[cid] = (valid_zrif, name)
        # Only index `by_tid` for Base Game TSVs (`PSV_GAMES.tsv`) so DLC TitleIDs don't shadow Base game zRIFs
        if tid and not is_dlc_tsv:
            if tid not in by_tid or (valid_zrif and not by_tid[tid][0]):
                by_tid[tid] = (valid_zrif, name)

    return by_cid, by_tid


def lookup_vita_tsv_zrif(
    content_id: str | None,
    title_id: str | None,
    extra_dirs: tuple[Path, ...] = (),
    project_root: Path | None = None,
) -> tuple[str | None, str | None, str | None]:
    """
    Look up `(zrif, official_title, tsv_filename)` for a PS Vita `content_id` or `title_id`
    across `tools/psvita/*.tsv` (`PSV_GAMES.tsv`, `PSV_DLCS.tsv`, `PSV_UPDATES.tsv`).
    """
    base_root = project_root if project_root is not None else Path(__file__).resolve().parent.parent
    default_psvita_dir = base_root / "tools" / "psvita"
    search_dirs: list[Path] = []
    for d in (*extra_dirs, default_psvita_dir):
        if d.is_dir() and d not in search_dirs:
            search_dirs.append(d)

    cid_key = content_id.strip().upper() if content_id else ""
    tid_key = title_id.strip().upper() if title_id else ""
    fallback_title: str | None = None

    loaded_tables: list[tuple[str, dict[str, tuple[str, str]], dict[str, tuple[str, str]]]] = []
    for folder in search_dirs:
        try:
            # Prioritize `PSV_GAMES.tsv` first, then `PSV_DLCS.tsv`, then `PSV_UPDATES.tsv`
            tsv_files = sorted(
                (p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".tsv"),
                key=lambda p: (0 if "game" in p.stem.lower() else (1 if "dlc" in p.stem.lower() else 2), p.name.lower()),
            )
        except OSError:
            continue
        for tsv_p in tsv_files:
            try:
                st = tsv_p.stat()
            except OSError:
                continue
            by_cid, by_tid = _parse_vita_tsv_file(str(tsv_p.resolve()), st.st_mtime_ns, st.st_size)
            loaded_tables.append((tsv_p.name, by_cid, by_tid))

    # Pass 1: Exact Content ID match (`EP1063-PCSB00408_00-DRAGONSCROWN0001`) across all TSVs
    if cid_key:
        for tsv_name, by_cid, _ in loaded_tables:
            if cid_key in by_cid:
                zrif, name = by_cid[cid_key]
                if zrif:
                    return zrif, (name or None), tsv_name
                if name and not fallback_title:
                    fallback_title = name

    # Pass 2: Title ID match (`PCSB00408`) across non-DLC TSVs (`PSV_GAMES.tsv`)
    if tid_key:
        for tsv_name, _, by_tid in loaded_tables:
            if tid_key in by_tid:
                zrif, name = by_tid[tid_key]
                if zrif:
                    return zrif, (name or None), tsv_name
                if name and not fallback_title:
                    fallback_title = name

    return None, fallback_title, None


def _inspect_sony_pkg_header(
    file_path: Path,
    project_root: Path | None = None,
) -> tuple[str | None, str | None, str | None, str | None, str | None, str | None, str] | None:
    """
    Inspect a Sony `.pkg` file for magic `\\x7fPKG` at offset `0x00`, PS Vita Content ID
    at offset `0x30..0x54` (e.g. `UP0000-PCSE00120_00-PERSONA4GOLDEN00`), any embedded
    plaintext `\\x00PSF` (`param.sfo`) table inside the first 64 KiB header, and any matching
    `zRIF` license in `tools/psvita/*.tsv`.
    """
    try:
        with file_path.open("rb") as fh:
            head = fh.read(65536)
    except OSError:
        return None

    if len(head) < 0x54 or head[:4] != b"\x7fPKG":
        return None

    content_id = head[0x30:0x54].split(b"\x00", 1)[0].decode("ascii", errors="ignore").strip()
    m = _VITA_TITLE_ID_RE.search(content_id) or _VITA_TITLE_ID_RE.search(file_path.stem)
    has_work_bin = bool(_find_pkg_license_companions(file_path))

    # Also check for an embedded `\x00PSF` (`param.sfo`) table in the PKG header
    sfo_tid: str | None = None
    sfo_ver: str | None = None
    sfo_cat: str | None = None
    sfo_title: str | None = None
    psf_off = head.find(b"\x00PSF")
    if psf_off != -1:
        sfo_tid, sfo_ver, sfo_cat, sfo_title = _extract_vita_sfo_metadata(head[psf_off:])

    # Offset 0x06..0x08 big-endian pkg_type: 0x0002 = PSP/PS Vita
    pkg_type = int.from_bytes(head[0x06:0x08], "big")
    if m is None and sfo_tid is None and not (pkg_type == 2 and has_work_bin):
        return None

    title_id = sfo_tid or (m.group(1).upper() if m else None)
    clean_stem = sanitize_rom_stem(file_path.stem)

    # Fallback filename heuristics if the PKG header did not embed a plaintext SFO table
    if not sfo_cat:
        upd_m = re.search(r"[\(\[]\s*(?:update|patch)\s*v?(\d+(?:\.\d+)?)?\s*[\)\]]", clean_stem, re.IGNORECASE)
        if upd_m:
            sfo_cat = "gp"
            if upd_m.group(1) and not sfo_ver:
                sfo_ver = upd_m.group(1)
        elif re.search(r"[\(\[]\s*dlc\b", clean_stem, re.IGNORECASE):
            sfo_cat = "ac"

    # Look up zRIF & official title from `tools/psvita/*.tsv` (e.g. `PSV_GAMES.tsv`) for non-update packages
    tsv_zrif: str | None = None
    tsv_title: str | None = None
    tsv_source: str | None = None
    if sfo_cat != "gp":
        tsv_zrif, tsv_raw_title, tsv_source = lookup_vita_tsv_zrif(
            content_id or None,
            title_id,
            extra_dirs=(file_path.resolve().parent,),
            project_root=project_root,
        )
        if tsv_raw_title:
            tsv_title = _sanitize_wiiu_title(tsv_raw_title)

    # Strip trailing TitleID or [UPDATE ...] bracket from fallback stem so we don't duplicate it
    if title_id:
        clean_stem = re.sub(rf"[\s_\-]*[\(\[]{re.escape(title_id)}[\)\]]", "", clean_stem, flags=re.IGNORECASE).strip()
    clean_stem = re.sub(r"[\s_\-]*[\(\[]\s*(?:update|patch)\b[^\)\]]*[\)\]]", "", clean_stem, flags=re.IGNORECASE).strip()

    clean_title = sfo_title or tsv_title or clean_stem or (f"Vita_{title_id}" if title_id else None)
    cat_labels = {"gd": "Base Game", "gp": f"Update v{sfo_ver}" if sfo_ver else "Update", "ac": "DLC"}
    cat_note = f", {cat_labels[sfo_cat]}" if sfo_cat in cat_labels else ""
    cid_note = f"Content ID `{content_id}`" if content_id else f"Title ID `{title_id}`"
    if has_work_bin:
        lic_note = " + license sidecar"
    elif tsv_zrif and tsv_source:
        lic_note = f" + zRIF from `{tsv_source}`"
    else:
        lic_note = ""
    return (
        title_id,
        sfo_ver,
        sfo_cat,
        clean_title,
        content_id or None,
        tsv_zrif,
        f"Sony PS Vita `.pkg` ({cid_note}{cat_note}{lic_note})",
    )


def _detect_psvita_trees(
    root_resolved: Path,
    all_files: list[Path],
    claimed: set[Path],
    project_root: Path | None = None,
) -> list[IdentificationResult]:
    """
    Pass 0c: Detect unpacked PS Vita NoNpDrm / MaiDump / Vitamin folders (`sce_sys/param.sfo`)
    and Sony `.pkg` packages (+ companion `work.bin`/`.zrif`).
    """
    results: list[IdentificationResult] = []

    # 1. Detect `sce_sys/param.sfo` package trees
    sfo_files = [
        f for f in all_files
        if f not in claimed
        and f.name.lower() == "param.sfo"
        and f.parent.name.lower() == "sce_sys"
    ]
    if sfo_files:
        # Sort shallowest package roots first
        sfo_files.sort(key=lambda p: (len(p.parts), str(p).lower()))
        grouped_by_tid: dict[str, list[tuple[Path, Path, str | None, str | None, str | None, str | None]]] = {}

        for sfo_path in sfo_files:
            pkg_root = sfo_path.parent.parent
            try:
                sfo_bytes = sfo_path.read_bytes()
            except OSError:
                sfo_bytes = b""
            tid, app_ver, category, clean_title = _extract_vita_sfo_metadata(sfo_bytes)
            # Fallback to parent folder name if `param.sfo` was a 0-byte placeholder or lacked TITLE_ID
            if not tid:
                m_dir = _VITA_TITLE_ID_RE.search(pkg_root.name) or _VITA_TITLE_ID_RE.search(str(pkg_root))
                if m_dir:
                    tid = m_dir.group(1).upper()
            group_key = tid or str(pkg_root).lower()
            grouped_by_tid.setdefault(group_key, []).append(
                (sfo_path, pkg_root, tid, app_ver, category, clean_title)
            )

        for _gkey, entries in grouped_by_tid.items():
            # Prioritize Base game (`gd`) as primary entry when Base + Patch (`gp`) + DLC (`ac`) coexist
            entries.sort(key=lambda item: (0 if item[4] == "gd" else (1 if item[4] == "gp" else 2), str(item[0]).lower()))
            primary_sfo, primary_root, primary_tid, primary_ver, primary_cat, primary_title = entries[0]

            #Pick highest version if an update (`gp`) is bundled alongside Base (`gd`)
            best_ver = primary_ver
            for _, _, _, e_ver, e_cat, _ in entries:
                if e_cat == "gp" and e_ver:
                    best_ver = e_ver

            pkg_roots = [e[1] for e in entries]
            pkg_root_set = set(pkg_roots)
            tree_files: list[Path] = []
            for f in all_files:
                if f in claimed:
                    continue
                if any(f.is_relative_to(r) for r in pkg_roots):
                    claimed.add(f)
                    # Strip OS metadata junk anywhere in tree and ROM-site watermark sidecars at package root
                    if f.name.lower() in IGNORED_FILENAMES or (
                        f.parent in pkg_root_set and f.suffix.lower() in IGNORED_SIDECAR_EXTENSIONS
                    ):
                        continue
                    tree_files.append(f)

            # Also claim any non-ROM sidecars / OS metadata sitting outside `pkg_roots` so they aren't quarantined
            for f in all_files:
                if f not in claimed and (
                    f.suffix.lower() in IGNORED_SIDECAR_EXTENSIONS
                    or f.name.lower() in IGNORED_FILENAMES
                ):
                    claimed.add(f)

            if not primary_title:
                for _, r_dir, _, _, _, t_cand in entries:
                    if t_cand:
                        primary_title = t_cand
                        break
                    if (
                        r_dir != root_resolved
                        and not re.match(r"^(?:job_|_inspect_|src_|app$|patch$|addcont$)", r_dir.name, re.IGNORECASE)
                        and not _VITA_TITLE_ID_RE.fullmatch(r_dir.name)
                    ):
                        primary_title = sanitize_rom_stem(r_dir.name)
                        break

            cat_labels = {
                "gd": "Base Game",
                "gp": "Update/Patch",
                "ac": "DLC",
            }
            if len(entries) > 1:
                pkg_summary = f"{len(entries)} Vita packages (Base/Update/DLC)"
            else:
                pkg_summary = f"Vita {cat_labels.get(primary_cat or '', 'NoNpDrm')} folder"
            tid_note = f" [{primary_tid}]" if primary_tid else ""
            ver_note = f" v{best_ver}" if best_ver else ""
            title_note = f" `{primary_title}`" if primary_title else ""

            results.append(
                IdentificationResult(
                    system_id="psvita",
                    method=DetectionMethod.BINARY_MAGIC if primary_tid else DetectionMethod.UNAMBIGUOUS_EXTENSION,
                    confidence="high",
                    reason=f"PS Vita `sce_sys/param.sfo` ({pkg_summary}{title_note}{tid_note}{ver_note}, {len(tree_files)} files)",
                    primary_file=primary_sfo,
                    companion_files=tuple(f for f in tree_files if f != primary_sfo),
                    vita_title_id=primary_tid,
                    vita_app_version=best_ver,
                    vita_category=primary_cat if len(entries) == 1 else "bundle",
                    vita_clean_title=primary_title,
                )
            )

    # 2. Detect `.pkg` files and claim their companion `work.bin` / `.zrif` before Pass 3
    pkg_files = [f for f in all_files if f not in claimed and f.suffix.lower() == ".pkg"]
    for pkg_path in pkg_files:
        pkg_res = identify_single_file(pkg_path, project_root=project_root)
        if pkg_res.system_id == "psvita":
            claimed.add(pkg_path)
            for comp in pkg_res.companion_files:
                claimed.add(comp.resolve())
            results.append(pkg_res)

    return results


def _detect_wiiu_loadiine_trees(
    root_resolved: Path,
    candidate_dirs: list[Path],
    all_files: list[Path],
    claimed: set[Path],
) -> list[IdentificationResult]:
    """Pass 0a: Detect unpacked Wii U Loadiine/Cemu folders (`code/*.rpx` + optional `content/` + `meta/`)."""
    results: list[IdentificationResult] = []
    for cand_dir in candidate_dirs:
        code_dir = cand_dir / "code"
        if not code_dir.is_dir():
            continue
        rpx_files = sorted(
            f.resolve() for f in code_dir.iterdir()
            if f.is_file() and f.suffix.lower() == ".rpx" and f.resolve() not in claimed
        )
        if not rpx_files:
            continue

        tree_files = [f for f in all_files if f.is_relative_to(cand_dir)]
        claimed.update(tree_files)
        decrypted_files = [f for f in tree_files if not _is_wiiu_encrypted_nus_blob(f, cand_dir)]
        skipped_nus_count = len(tree_files) - len(decrypted_files)

        tid, tver, xml_title = _parse_wiiu_meta_xml(cand_dir / "meta" / "meta.xml")
        if xml_title:
            clean_title = xml_title
        elif cand_dir != root_resolved and not re.match(r"^(?:job_|_inspect_|src_)", cand_dir.name):
            clean_title = sanitize_rom_stem(cand_dir.name)
        else:
            clean_title = None

        extra_note = (
            f"; skipped {skipped_nus_count} duplicate encrypted WUP/NUS files"
            if skipped_nus_count > 0
            else ""
        )
        results.append(
            IdentificationResult(
                system_id="wiiu",
                method=DetectionMethod.UNAMBIGUOUS_EXTENSION,
                confidence="high",
                reason=(
                    f"Wii U Loadiine title folder (`{rpx_files[0].name}`, "
                    f"{len(decrypted_files)} files{extra_note})"
                ),
                primary_file=rpx_files[0],
                companion_files=tuple(f for f in decrypted_files if f != rpx_files[0]),
                wiiu_title_id=tid,
                wiiu_title_version=tver,
                wiiu_clean_title=clean_title,
            )
        )
    return results


def _detect_wiiu_encrypted_nus_trees(
    root_resolved: Path,
    candidate_dirs: list[Path],
    all_files: list[Path],
    claimed: set[Path],
) -> list[IdentificationResult]:
    """Pass 0b: Detect encrypted-only Wii U WUP/NUS folders (`title.tmd` + `title.tik`/`*.app`)."""
    results: list[IdentificationResult] = []
    for cand_dir in candidate_dirs:
        tmd_candidates = [
            f for f in all_files
            if f.parent == cand_dir and f.name.lower() == "title.tmd" and f not in claimed
        ]
        if not tmd_candidates:
            continue
        dir_files = [f for f in all_files if f.is_relative_to(cand_dir) and f not in claimed]
        has_wup_payload = any(
            f.parent == cand_dir and (f.suffix.lower() == ".app" or f.name.lower() == "title.tik")
            for f in dir_files
        )
        if not has_wup_payload:
            continue

        tmd_path = tmd_candidates[0]
        claimed.update(dir_files)
        tid, tver = _parse_wiiu_tmd_header(tmd_path)
        fallback_name = (
            sanitize_rom_stem(cand_dir.name)
            if cand_dir != root_resolved and not re.match(r"^(?:job_|_inspect_|src_)", cand_dir.name)
            else (f"WiiU_{tid}" if tid else None)
        )
        results.append(
            IdentificationResult(
                system_id="wiiu",
                method=DetectionMethod.BINARY_MAGIC,
                confidence="high",
                reason=f"Encrypted Wii U WUP/NUS package (`{tmd_path.name}`, {len(dir_files)} files)",
                primary_file=tmd_path,
                companion_files=tuple(f for f in dir_files if f != tmd_path),
                is_encrypted_wiiu=True,
                wiiu_title_id=tid,
                wiiu_title_version=tver,
                wiiu_clean_title=fallback_name,
            )
        )
    return results


def _detect_switch_family_bundles(
    all_files: list[Path],
    claimed: set[Path],
) -> list[IdentificationResult]:
    """Pass 1: Group multi-file Nintendo Switch bundles by TitleID family (or parent folder if untagged)."""
    results: list[IdentificationResult] = []
    switch_files = [p for p in all_files if p.suffix.lower() in SWITCH_EXTENSIONS and p not in claimed]
    switch_groups: dict[tuple[str, str], list[Path]] = {}
    for sw_path in switch_files:
        tids = extract_switch_title_ids_from_file(sw_path)
        if tids:
            fam_id, _ = classify_switch_title_id(tids[0])
            group_key = ("tid", fam_id)
        else:
            group_key = ("dir", str(sw_path.parent).lower())
        switch_groups.setdefault(group_key, []).append(sw_path)

    for (key_kind, key_val), group in switch_groups.items():
        if len(group) < 2:
            continue
        ordered = sorted(group, key=switch_package_sort_key)
        has_compressed = any(p.suffix.lower() in COMPRESSED_SWITCH_EXTENSIONS for p in ordered)
        primary = ordered[0]
        companions = tuple(ordered[1:])
        claimed.update(ordered)
        fam_label = f" [{key_val}]" if key_kind == "tid" else ""
        results.append(
            IdentificationResult(
                system_id="switch",
                method=DetectionMethod.UNAMBIGUOUS_EXTENSION,
                confidence="high",
                reason=(
                    f"Multi-file Switch bundle{fam_label} ({len(ordered)} files: Base/Update/DLC) "
                    "ready for single `.xci` merge via squirrel"
                ),
                primary_file=primary,
                companion_files=companions,
                is_compressed_switch=has_compressed,
                is_multifile_switch=True,
            )
        )
    return results


def _detect_disc_descriptors(
    all_files: list[Path],
    claimed: set[Path],
    sizes: dict[Path, int],
) -> list[IdentificationResult]:
    """Pass 2: Process `.gdi` and `.cue` descriptors first so they claim their `.bin`/`.raw` tracks."""
    results: list[IdentificationResult] = []
    descriptors = [p for p in all_files if p.suffix.lower() in (".gdi", ".cue") and p not in claimed]
    for desc_path in descriptors:
        tracks = parse_gdi_tracks(desc_path) if desc_path.suffix.lower() == ".gdi" else parse_cue_tracks(desc_path)
        track01_size = sizes.get(tracks[0].resolve()) if tracks else None
        res = identify_single_file(desc_path, logical_size=track01_size)
        claimed.add(desc_path)
        for trk in res.companion_files:
            claimed.add(trk.resolve())
        results.append(res)
    return results


def _detect_standalone_files(
    all_files: list[Path],
    claimed: set[Path],
    sizes: dict[Path, int],
    chdman_bin: Path | None,
    project_root: Path | None = None,
) -> list[IdentificationResult]:
    """Pass 3: Identify remaining standalone files, skipping harmless sidecars."""
    results: list[IdentificationResult] = []
    for file_path in all_files:
        if file_path in claimed:
            continue
        if (
            file_path.suffix.lower() in IGNORED_SIDECAR_EXTENSIONS
            or file_path.name.lower() in IGNORED_FILENAMES
        ):
            continue
        res = identify_single_file(
            file_path,
            logical_size=sizes.get(file_path),
            chdman_bin=chdman_bin,
            project_root=project_root,
        )
        claimed.add(file_path)
        results.append(res)
    return results


def identify_directory(
    directory: Path,
    logical_sizes: dict[Path, int] | None = None,
    chdman_bin: Path | None = None,
    project_root: Path | None = None,
) -> list[IdentificationResult]:
    """
    Identify all logical game units inside an extracted staging directory or dropped folder:
      - Pass 0a/0b: Wii U Loadiine `{code, content, meta}` or encrypted WUP/NUS trees.
      - Pass 1: Multi-file Switch bundles (Base + Update + DLC) grouped by TitleID family.
      - Pass 2: `.cue` and `.gdi` descriptors grouped with all of their referenced `.bin`/`.raw` tracks.
      - Pass 3: Standalone ROMs/disc images (skipping harmless sidecar files).
    """
    sizes = logical_sizes or {}
    all_files = sorted(
        (p.resolve() for p in directory.rglob("*") if p.is_file() and not is_archive(p)),
        key=lambda p: str(p).lower(),
    )

    claimed: set[Path] = set()
    root_resolved = directory.resolve()
    candidate_dirs = sorted(
        {root_resolved, *(p.parent for p in all_files)},
        key=lambda d: (len(d.parts), str(d).lower()),
    )

    results: list[IdentificationResult] = []
    results.extend(_detect_wiiu_loadiine_trees(root_resolved, candidate_dirs, all_files, claimed))
    results.extend(_detect_wiiu_encrypted_nus_trees(root_resolved, candidate_dirs, all_files, claimed))
    results.extend(_detect_psvita_trees(root_resolved, all_files, claimed, project_root=project_root))
    results.extend(_detect_switch_family_bundles(all_files, claimed))
    results.extend(_detect_disc_descriptors(all_files, claimed, sizes))
    results.extend(_detect_standalone_files(all_files, claimed, sizes, chdman_bin, project_root=project_root))
    return results
