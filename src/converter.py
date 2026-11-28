"""Subprocess wrappers and format optimization for chdman, DolphinTool, and NSCB squirrel."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import sys
import uuid
import zipfile

from src.constants import CD_SUBCHANNEL_EXTENSIONS
from src.ctr_decrypt import (
    CtrDecryptionError,
    CtrMissingSeedError,
    can_convert_cia_game_to_cci,
    can_decrypt_3ds,
    convert_cia_to_cci,
    decrypt_3ds_to_cci,
)
from src.identifier import (
    SectorReader,
    _parse_iso9660_root_entries,
    decode_zrif_to_work_bin,
    lookup_vita_tsv_zrif,
    parse_multidisc_info,
    sanitize_rom_stem,
)
from src.models import (
    ArchivePolicy,
    ConversionArtifact,
    IdentificationResult,
    PipelineConfig,
)
from src.runner import run_psxpackager, run_streaming

_CUE_FILE_LINE_RE = re.compile(r"""^(\s*FILE\s+)(?:"([^"]+)"|'([^']+)'|(\S+))(\s+\S+.*)$""", re.IGNORECASE)
_PSX_BOOT_SERIAL_RE = re.compile(
    rb"BOOT\s*=\s*cdrom:[\\/]?([A-Za-z]{4})[_\-]?(\d{3,5})\.?(\d{2})?",
    re.IGNORECASE,
)


def _format_psx_serial_match(m: re.Match[bytes]) -> str:
    prefix = m.group(1).decode("ascii", errors="ignore").upper()
    p1 = m.group(2).decode("ascii", errors="ignore")
    p2 = m.group(3).decode("ascii", errors="ignore") if m.group(3) else ""
    return f"{prefix}-{p1}{p2}"


def extract_psx_disc_serial(disc_primary_file: Path, companion_files: tuple[Path, ...] = ()) -> str | None:
    """
    Extract PS1/PS2 normalized serial (e.g. 'SLES-02558') from the disc's ISO9660 `SYSTEM.CNF`
    via `SectorReader` (falling back to a raw initial-sector scan for non-standard layouts).
    """
    targets = [disc_primary_file, *companion_files]
    for t in targets:
        if t.suffix.lower() not in (".bin", ".iso", ".img") or not t.is_file():
            continue
        try:
            file_sz = t.stat().st_size
            with t.open("rb") as fh:
                reader = SectorReader(fh, actual_file_size=file_sz, logical_file_size=file_sz)
                if reader.has_iso9660_pvd:
                    pvd = reader.read_sector(16)
                    if len(pvd) >= 190 and pvd[:6] == b"\x01CD001":
                        root_rec = pvd[156:190]
                        root_lba = int.from_bytes(root_rec[2:6], "little")
                        root_len = min(int.from_bytes(root_rec[10:14], "little"), 65536)
                        if root_lba > 0 and root_len > 0:
                            root_entries = _parse_iso9660_root_entries(
                                reader.read_logical_bytes(root_lba, root_len)
                            )
                            if "SYSTEM.CNF" in root_entries:
                                cnf_lba, cnf_len, _ = root_entries["SYSTEM.CNF"]
                                if cnf_lba > 0 and cnf_len > 0:
                                    cnf_bytes = reader.read_logical_bytes(cnf_lba, min(cnf_len, 4096))
                                    m = _PSX_BOOT_SERIAL_RE.search(cnf_bytes)
                                    if m:
                                        return _format_psx_serial_match(m)
                fh.seek(0)
                head = fh.read(500 * 2352)
                m = _PSX_BOOT_SERIAL_RE.search(head)
                if m:
                    return _format_psx_serial_match(m)
        except Exception:
            continue
    return None


class ConversionError(RuntimeError):
    """Raised when an external conversion binary is missing, exits non-zero, or yields 0 bytes."""


class MissingToolError(ConversionError):
    """Raised when a required external CLI binary (`chdman`, `DolphinTool`, `squirrel`) is missing."""


CD_CHD_SYSTEMS: frozenset[str] = frozenset({
    "psx", "saturn", "dreamcast", "segacd", "pcenginecd",
})

DOLPHIN_CONVERTIBLE_EXTS: frozenset[str] = frozenset({
    ".iso", ".gcm", ".wbfs", ".ciso", ".gcz", ".wia",
})


def _run_streaming_subprocess(
    cmd: list[str],
    status_label: str,
    cwd: Path | None = None,
    abort_substrings: tuple[str, ...] = (),
) -> None:
    """Delegate streaming subprocess execution to the unified process runner (`src/runner.py`)."""
    run_streaming(
        cmd,
        status_label=status_label,
        cwd=cwd,
        abort_substrings=abort_substrings,
        error_cls=ConversionError,
    )


def _verify_nonzero_output(path: Path, tool_name: str) -> None:
    """Ensure converted output file exists and is strictly > 0 bytes."""
    if not path.is_file():
        raise ConversionError(f"`{tool_name}` exited 0, but expected output file is missing: {path}")
    if path.stat().st_size <= 0:
        path.unlink(missing_ok=True)
        raise ConversionError(f"`{tool_name}` produced an empty 0-byte file: {path.name}")


def _unique_staging_output(job_dir: Path, stem: str, ext: str) -> Path:
    """Return `<job_dir>/<stem><ext>`, avoiding accidental overwrite of input with same name."""
    candidate = job_dir / f"{stem}{ext}"
    if not candidate.exists():
        return candidate
    idx = 1
    while True:
        alt = job_dir / f"{stem}_conv{idx}{ext}"
        if not alt.exists():
            return alt
        idx += 1


def _resolve_squirrel_invocation(config: PipelineConfig) -> tuple[list[str], Path | None]:
    """
    Build base command prefix and working directory for NSC_Builder `squirrel` (or Linux `nsz`).

    Note: `NSCB/ztools/squirrel.exe` looks up `ztools/keys.txt` relative to its parent `nscb` root!
    """
    sq = config.binaries.squirrel
    if sq is None:
        raise MissingToolError(
            "Switch conversion requires `squirrel` (NSC_Builder) or `nsz`, which was not found."
        )

    cwd: Path | None = None
    if sq.parent.name.lower() == "ztools":
        cwd = sq.parent.parent

    if sq.suffix.lower() == ".py":
        return [sys.executable, str(sq)], cwd
    return [str(sq)], cwd


def _cue_relative_track_ref(track_path: Path, cue_dir: Path, job_dir: Path) -> str:
    """
    Return a `FILE "..."` reference relative to `cue_dir` (because `chdman` always prepends the
    `.cue` file's parent directory to `FILE` entries rather than accepting Windows drive paths).
    """
    resolved_track = track_path.resolve()
    resolved_cue_dir = cue_dir.resolve()
    if resolved_track.parent == resolved_cue_dir:
        return resolved_track.name
    try:
        return os.path.relpath(resolved_track, resolved_cue_dir)
    except ValueError:
        # Cross-drive fallback on Windows (e.g. source on D:\ while `.staging` is on C:\)
        staged_track = _unique_staging_output(job_dir, resolved_track.stem, resolved_track.suffix)
        shutil.copy2(resolved_track, staged_track)
        return os.path.relpath(staged_track.resolve(), resolved_cue_dir)


def _prepare_cd_input_for_chdman(
    input_path: Path,
    companion_files: tuple[Path, ...],
    job_dir: Path,
    clean_stem: str,
) -> Path:
    """
    Ensure `chdman createcd` always receives a valid descriptor (`.cue`, `.gdi`, `.iso`):
      1. If `input_path` is a `.cue` with a broken/mismatched `FILE "..."` reference (e.g. modified
         by a ROM-site watermarker or case-mismatched on Linux), write a healed `.cue` in `job_dir`.
      2. If `input_path` is a raw cue-less `.bin`/`.img`/`.raw`/`.mdf`, synthesize a `.cue` in
         `job_dir` matching its sector geometry (`MODE2/2352`, `MODE1/2352`, etc.)—because passing
         a raw `.bin` directly to `chdman createcd` causes `Input tracks: 0` / infinite `nan%` loop!
    """
    job_dir.mkdir(parents=True, exist_ok=True)
    ext = input_path.suffix.lower()

    if ext == ".cue":
        try:
            lines = input_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return input_path

        file_indices: list[int] = []
        needs_repair = False
        for idx, line in enumerate(lines):
            m = _CUE_FILE_LINE_RE.match(line)
            if not m:
                continue
            file_indices.append(idx)
            raw_ref = (m.group(2) or m.group(3) or m.group(4) or "").replace("\\", "/").rsplit("/", 1)[-1]
            if not raw_ref or not (input_path.parent / raw_ref).is_file():
                needs_repair = True

        if not needs_repair and file_indices:
            return input_path

        track_companions = [
            c for c in companion_files
            if c.suffix.lower() not in CD_SUBCHANNEL_EXTENSIONS
        ]
        if file_indices and len(file_indices) == len(track_companions):
            first_track_dir = track_companions[0].resolve().parent
            cue_home = (
                first_track_dir
                if first_track_dir.is_relative_to(job_dir.resolve())
                else job_dir
            )
            healed_cue = _unique_staging_output(cue_home, f"_healed_{clean_stem}", ".cue")
            repaired_lines = list(lines)
            for track_idx, line_idx in enumerate(file_indices):
                m = _CUE_FILE_LINE_RE.match(repaired_lines[line_idx])
                assert m is not None
                prefix, suffix = m.group(1), m.group(5)
                rel_ref = _cue_relative_track_ref(track_companions[track_idx], cue_home, job_dir)
                repaired_lines[line_idx] = f'{prefix}"{rel_ref}"{suffix}'

            healed_cue.write_text("\n".join(repaired_lines) + "\n", encoding="utf-8")
            return healed_cue

        return input_path

    if ext in (".bin", ".img", ".raw", ".mdf"):
        track_mode = "MODE2/2352"
        try:
            file_sz = input_path.stat().st_size
            with input_path.open("rb") as fh:
                sr = SectorReader(fh, actual_file_size=file_sz, logical_file_size=file_sz)
            if sr.sector_size == 2352:
                track_mode = "MODE2/2352" if sr.data_offset == 24 else "MODE1/2352"
            elif sr.sector_size == 2336:
                track_mode = "MODE2/2336"
            elif sr.sector_size == 2048:
                track_mode = "MODE1/2048"
        except OSError:
            pass

        track_parent = input_path.resolve().parent
        cue_home = track_parent if track_parent.is_relative_to(job_dir.resolve()) else job_dir
        synth_cue = _unique_staging_output(cue_home, f"_synth_{clean_stem}", ".cue")
        rel_ref = _cue_relative_track_ref(input_path, cue_home, job_dir)
        synth_cue.write_text(
            f'FILE "{rel_ref}" BINARY\n'
            f"  TRACK 01 {track_mode}\n"
            f"    INDEX 01 00:00:00\n",
            encoding="utf-8",
        )
        return synth_cue

    return input_path


def _convert_chd(
    subcommand: str,
    input_path: Path,
    job_dir: Path,
    config: PipelineConfig,
    companion_files: tuple[Path, ...] = (),
    hunk_size: int | None = None,
) -> Path:
    chdman = config.binaries.chdman
    if chdman is None:
        raise MissingToolError(
            f"Cannot convert `{input_path.name}` to `.chd`: `chdman` binary not found."
        )

    clean_stem = sanitize_rom_stem(input_path.stem, companion_files)
    effective_input = (
        _prepare_cd_input_for_chdman(input_path, companion_files, job_dir, clean_stem)
        if subcommand == "createcd"
        else input_path
    )

    out_chd = _unique_staging_output(job_dir, clean_stem, ".chd")
    cmd = [str(chdman), subcommand]
    if hunk_size is not None:
        cmd.extend(["-hs", str(hunk_size)])
    cmd.extend(["-i", str(effective_input), "-o", str(out_chd), "-f"])

    _run_streaming_subprocess(
        cmd,
        status_label=f"chdman {subcommand}: {input_path.name} -> {out_chd.name}",
        abort_substrings=("nan% complete", "Input tracks: 0"),
    )
    _verify_nonzero_output(out_chd, "chdman")
    return out_chd


def _run_psxpackager_subprocess(
    cmd: list[str],
    work_dir: Path,
    status_label: str,
) -> deque[str]:
    """Delegate `psxpackager` execution to the unified process runner (`src/runner.py`)."""
    return run_psxpackager(
        cmd,
        work_dir=work_dir,
        status_label=status_label,
        error_cls=ConversionError,
    )


def _prepare_psx_disc_for_pbp(
    item: IdentificationResult,
    disc_dir: Path,
    config: PipelineConfig,
) -> Path:
    """
    Prepare a single PS1 disc entry inside `disc_dir` for `psxpackager`:
      - `.chd` -> extracted to `.cue` + `.bin` via `chdman extractcd`
      - `.cue` / cue-less `.bin`/`.img`/`.raw`/`.mdf` -> healed or synthesized `.cue`
      - `.iso` -> passed directly
    """
    disc_dir.mkdir(parents=True, exist_ok=True)
    primary = item.primary_file
    ext = primary.suffix.lower()
    clean_stem = sanitize_rom_stem(primary.stem, item.companion_files)

    if ext == ".chd":
        chdman = config.binaries.chdman
        if chdman is None:
            raise MissingToolError(
                f"Extracting `{primary.name}` for `.pbp` merging requires `chdman`, which was not found."
            )
        out_cue = disc_dir / f"{clean_stem}.cue"
        out_bin = disc_dir / f"{clean_stem}.bin"
        _run_streaming_subprocess(
            [
                str(chdman),
                "extractcd",
                "-i",
                str(primary),
                "-o",
                str(out_cue),
                "-ob",
                str(out_bin),
                "-f",
            ],
            status_label=f"chdman extractcd: {primary.name} -> {out_cue.name}",
        )
        _verify_nonzero_output(out_cue, "chdman extractcd")
        _verify_nonzero_output(out_bin, "chdman extractcd")
        return out_cue

    if ext in (".cue", ".bin", ".img", ".raw", ".mdf"):
        return _prepare_cd_input_for_chdman(primary, item.companion_files, disc_dir, clean_stem)

    return primary


def _find_psx_sbi_sidecar(
    item: IdentificationResult,
    include_parent_dir: bool = False,
) -> Path | None:
    """Locate a companion `.sbi` LibCrypt sidecar for `item`, if present."""
    candidates = [item.primary_file, *item.companion_files]
    if include_parent_dir:
        p_dir = item.primary_file.resolve().parent
        if p_dir.is_dir():
            candidates.extend(
                f for f in p_dir.iterdir()
                if f.is_file() and f.suffix.lower() in (".sbi", ".lsd")
            )
    for c in candidates:
        sbi = c if c.suffix.lower() == ".sbi" else c.with_suffix(".sbi")
        if sbi.is_file():
            return sbi
    return None


def _stage_psx_sbi_sidecars(
    item: IdentificationResult,
    job_dir: Path,
    stems_to_stage: tuple[str, ...],
    include_parent_dir: bool = False,
) -> list[Path]:
    """
    Stage `.sbi` LibCrypt sidecars into `job_dir` under both the disc's serial
    (e.g. `SLES-02558.sbi`) and `stems_to_stage` for DuckStation compatibility.
    """
    sbi = _find_psx_sbi_sidecar(item, include_parent_dir=include_parent_dir)
    if sbi is None:
        return []

    staged_sbis: list[Path] = []
    serial = extract_psx_disc_serial(item.primary_file, item.companion_files)
    all_stems = (*( (serial,) if serial else () ), *stems_to_stage)
    for stem in all_stems:
        out_sbi = _unique_staging_output(job_dir, stem, ".sbi")
        shutil.copy2(sbi, out_sbi)
        staged_sbis.append(out_sbi)
    return staged_sbis


def merge_psx_multidisc_pbp(
    disc_entries: list[tuple[int, IdentificationResult]],
    base_title: str,
    job_dir: Path,
    config: PipelineConfig,
) -> ConversionArtifact:
    """
    Merge 2+ PlayStation 1 discs into a single `<base_title>.pbp` inside `job_dir`
    using `psxpackager` so standalone DuckStation + Cocoon FE launch with zero `.m3u`
    Android Scoped Storage issues.
    """
    if len(disc_entries) < 2:
        raise ConversionError("`merge_psx_multidisc_pbp` requires at least 2 PS1 discs.")

    packager = config.binaries.psxpackager
    if packager is None:
        raise MissingToolError(
            f"Multi-disc PS1 `.pbp` merging for `{base_title}` requires `psxpackager`. "
            "Place `psxpackager` in `tools/psxpackager/` or install it on PATH."
        )

    ordered = sorted(
        disc_entries,
        key=lambda pair: (
            pair[0],
            sanitize_rom_stem(pair[1].primary_file.stem, pair[1].companion_files).lower(),
        ),
    )

    job_dir.mkdir(parents=True, exist_ok=True)
    work_dir = job_dir / f"_psx_pbp_{uuid.uuid4().hex[:8]}"
    out_dir = work_dir / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        prepared_descriptors: list[Path] = []
        for idx, (_disc_num, disc_item) in enumerate(ordered, start=1):
            disc_sub = work_dir / f"disc_{idx:02d}"
            desc = _prepare_psx_disc_for_pbp(disc_item, disc_sub, config)
            prepared_descriptors.append(desc.resolve())

        m3u_path = work_dir / f"{base_title}.m3u"
        m3u_path.write_text(
            "\n".join(str(p) for p in prepared_descriptors) + "\n",
            encoding="utf-8",
        )

        level = config.conversion.psx_pbp_compression_level
        cmd = [
            str(packager),
            "-i",
            str(m3u_path.resolve()),
            "-o",
            str(out_dir.resolve()),
            "-l",
            str(level),
            "-x",
            "-g",
        ]
        log_tail = _run_psxpackager_subprocess(
            cmd,
            work_dir=work_dir,
            status_label=f"PSXPackager ({len(ordered)} discs -> {base_title}.pbp, level={level})",
        )

        produced = sorted(
            p for p in out_dir.iterdir()
            if p.is_file() and p.suffix.lower() == ".pbp"
        )
        if not produced:
            recent = "\n  ".join(log_tail) if log_tail else "(no log output)"
            raise ConversionError(
                f"`psxpackager` exited 0, but produced no `.pbp` file for `{base_title}`:\n  {recent}"
            )

        raw_pbp = produced[0]
        _verify_nonzero_output(raw_pbp, "psxpackager")
        with raw_pbp.open("rb") as fh:
            magic = fh.read(4)
        if magic != b"\x00PBP":
            raise ConversionError(
                f"`psxpackager` output `{raw_pbp.name}` has invalid PBP header: {magic!r}"
            )

        final_pbp = _unique_staging_output(job_dir, base_title, ".pbp")
        shutil.move(str(raw_pbp), str(final_pbp))
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    # Collect and stage companion .sbi files across discs for DuckStation LibCrypt support
    staged_sbis: list[Path] = []
    seen_disc_nums: set[int] = set()
    for disc_num, disc_item in ordered:
        if disc_num in seen_disc_nums:
            continue
        disc_stems = (
            f"{base_title} (Disc {disc_num})",
            f"{base_title}_{disc_num}",
            *((base_title,) if disc_num == 1 else ()),
        )
        disc_sbis = _stage_psx_sbi_sidecars(
            disc_item,
            job_dir,
            stems_to_stage=disc_stems,
            include_parent_dir=True,
        )
        if disc_sbis:
            seen_disc_nums.add(disc_num)
            staged_sbis.extend(disc_sbis)

    return ConversionArtifact(
        system_id="psx",
        output_files=(final_pbp, *staged_sbis),
        converted=True,
        tool_used="psxpackager",
        summary=f"Merged {len(ordered)} PS1 discs -> `{final_pbp.name}` (`psxpackager -l {level}`)",
    )


_GC_DISC_SIZE = 1_459_978_240     # 0x57058000 (GameCube Mini-DVD)
_WII_SL_DISC_SIZE = 4_699_979_776  # 0x118240000 (Wii Single-Layer DVD)
_WII_DL_DISC_SIZE = 8_511_160_320  # 0x1FB4E0000 (Wii Dual-Layer DVD)
_GC_MAGIC = b"\xc2\x33\x9f\x3d"
_WII_MAGIC = b"\x5d\x1c\x9e\xa3"
_DOLPHIN_UNPACK_CHUNK = 4 * 1024 * 1024


def _unpack_ciso_to_sparse_iso(ciso_path: Path, out_iso: Path, system_id: str | None = None) -> None:
    """
    Unpack a GameCube/Wii `.ciso` container into a plain sparse `.iso` with exact disc size.

    Dolphin's `CISOBlobReader` reports `DataSizeType::UpperBound` rather than `DataSizeType::Accurate`,
    which triggers an assertion crash (`WIABlob.cpp:1747`, exit `-4` / SIGILL) in Fedora's
    `dolphin-emu-tool` 2503a when converting `.ciso` directly to `.rvz` or `.iso`.
    """
    with ciso_path.open("rb") as fin:
        header = fin.read(0x8000)
        if len(header) < 0x8000 or header[:4] != b"CISO":
            raise ConversionError(f"Invalid CISO header in `{ciso_path.name}`.")

        block_size = int.from_bytes(header[4:8], "little")
        if block_size <= 0 or block_size > 64 * 1024 * 1024:
            raise ConversionError(f"Invalid CISO block size ({block_size}) in `{ciso_path.name}`.")

        block_map = header[8:]
        used_indices = [idx for idx, flag in enumerate(block_map) if flag == 1]
        if not used_indices:
            raise ConversionError(f"CISO image `{ciso_path.name}` contains no allocated data blocks.")

        # Peek at the first 0x20 bytes of block 0 (if present) to distinguish GC vs Wii
        is_wii = system_id == "wii"
        if block_map[0] == 1:
            disc_head = fin.read(0x20)
            fin.seek(0x8000)
            if len(disc_head) >= 0x20:
                if disc_head[0x18:0x1C] == _WII_MAGIC:
                    is_wii = True
                elif disc_head[0x1C:0x20] == _GC_MAGIC:
                    is_wii = False

        last_idx = used_indices[-1]
        if is_wii:
            if (last_idx + 1) * block_size <= _WII_SL_DISC_SIZE:
                target_size = _WII_SL_DISC_SIZE
            elif (last_idx + 1) * block_size <= _WII_DL_DISC_SIZE:
                target_size = _WII_DL_DISC_SIZE
            else:
                target_size = (last_idx + 1) * block_size
        else:
            # Standard GC Mini-DVD ends inside block 696 (`0x57058000`); truncate block-alignment padding
            if last_idx * block_size < _GC_DISC_SIZE:
                target_size = _GC_DISC_SIZE
            else:
                target_size = (last_idx + 1) * block_size

        with out_iso.open("wb") as fout:
            fout.truncate(target_size)
            for idx in used_indices:
                disc_off = idx * block_size
                if disc_off >= target_size:
                    break
                to_write = min(block_size, target_size - disc_off)
                fout.seek(disc_off)
                rem = to_write
                eof_hit = False
                while rem > 0:
                    chunk = fin.read(min(_DOLPHIN_UNPACK_CHUNK, rem))
                    if not chunk:
                        eof_hit = True
                        break
                    fout.write(chunk)
                    rem -= len(chunk)
                if eof_hit:
                    break
                if to_write < block_size:
                    fin.seek(block_size - to_write, os.SEEK_CUR)


def _unpack_wbfs_to_sparse_iso(wbfs_path: Path, out_iso: Path) -> None:
    """
    Unpack a Wii `.wbfs` (including optional FAT32-split `.wbf1`, `.wbf2`, ...) container
    into a plain sparse `.iso` with exact `DataSizeType::Accurate` disc size for `dolphin-tool`.
    """
    parts: list[Path] = [wbfs_path]
    for idx in range(1, 10):
        cand = wbfs_path.with_suffix(f".wbf{idx}")
        if cand.is_file():
            parts.append(cand)
        else:
            break

    part_sizes = [p.stat().st_size for p in parts]
    part_offsets: list[tuple[int, int, Path]] = []
    running = 0
    for p, sz in zip(parts, part_sizes):
        part_offsets.append((running, sz, p))
        running += sz

    def _read_stream_into(offset: int, length: int, fout) -> None:
        rem = length
        pos = offset
        for start, sz, p_path in part_offsets:
            if pos >= start + sz:
                continue
            local_off = pos - start
            avail = min(rem, sz - local_off)
            if avail <= 0:
                continue
            with p_path.open("rb") as pf:
                pf.seek(local_off)
                sub_rem = avail
                while sub_rem > 0:
                    chunk = pf.read(min(_DOLPHIN_UNPACK_CHUNK, sub_rem))
                    if not chunk:
                        return
                    fout.write(chunk)
                    sub_rem -= len(chunk)
            pos += avail
            rem -= avail
            if rem <= 0:
                return

    with wbfs_path.open("rb") as fin:
        hdr = fin.read(512)
        if len(hdr) < 16 or hdr[:4] != b"WBFS":
            raise ConversionError(f"Invalid WBFS header in `{wbfs_path.name}`.")
        hd_sec_sz = 1 << hdr[8]
        wbfs_sec_sz = 1 << hdr[9]
        if hd_sec_sz < 512 or wbfs_sec_sz < 0x8000 or wbfs_sec_sz > 64 * 1024 * 1024:
            raise ConversionError(f"Unsupported WBFS sector geometry in `{wbfs_path.name}`.")
        if hd_sec_sz > len(hdr):
            hdr += fin.read(hd_sec_sz - len(hdr))

        disc_table = hdr[12:hd_sec_sz]
        slot = next((i for i, b in enumerate(disc_table) if b != 0), None)
        if slot is None:
            raise ConversionError(f"WBFS container `{wbfs_path.name}` contains no active disc slot.")

        wii_sec_per_wbfs = wbfs_sec_sz // 0x8000
        blocks_per_disc = (143_432 * 2) // wii_sec_per_wbfs
        disc_info_sz = ((0x100 + blocks_per_disc * 2 + hd_sec_sz - 1) // hd_sec_sz) * hd_sec_sz
        fin.seek(hd_sec_sz + slot * disc_info_sz)
        disc_info = fin.read(disc_info_sz)
        if len(disc_info) < 0x100 + blocks_per_disc * 2:
            raise ConversionError(f"Truncated WBFS disc table in `{wbfs_path.name}`.")

        is_gc = disc_info[0x1C:0x20] == _GC_MAGIC and disc_info[0x18:0x1C] != _WII_MAGIC
        wlba = [
            int.from_bytes(disc_info[0x100 + i * 2 : 0x100 + (i + 1) * 2], "big")
            for i in range(blocks_per_disc)
        ]
        used_blocks = [(i, lba) for i, lba in enumerate(wlba) if lba != 0]
        if not used_blocks:
            raise ConversionError(f"WBFS container `{wbfs_path.name}` has an empty block map.")

        last_idx = used_blocks[-1][0]
        if is_gc:
            target_size = _GC_DISC_SIZE if last_idx * wbfs_sec_sz < _GC_DISC_SIZE else (last_idx + 1) * wbfs_sec_sz
        elif (last_idx + 1) * wbfs_sec_sz <= _WII_SL_DISC_SIZE:
            target_size = _WII_SL_DISC_SIZE
        elif (last_idx + 1) * wbfs_sec_sz <= _WII_DL_DISC_SIZE:
            target_size = _WII_DL_DISC_SIZE
        else:
            target_size = (last_idx + 1) * wbfs_sec_sz

    with out_iso.open("wb") as fout:
        fout.truncate(target_size)
        for idx, lba in used_blocks:
            disc_off = idx * wbfs_sec_sz
            if disc_off >= target_size:
                break
            to_write = min(wbfs_sec_sz, target_size - disc_off)
            fout.seek(disc_off)
            _read_stream_into(lba * wbfs_sec_sz, to_write, fout)


def _prepare_dolphin_input_for_rvz(
    input_path: Path,
    job_dir: Path,
    system_id: str | None = None,
) -> tuple[Path, Path | None]:
    """
    If `input_path` is a `.ciso` or `.wbfs` container (whose `DataSizeType::UpperBound` triggers
    an assertion abort in Fedora's `dolphin-tool` 2503a), unpack it into a temporary sparse `.iso`
    inside `job_dir`. Returns `(effective_input_path, temp_iso_to_delete_or_none)`.
    """
    try:
        with input_path.open("rb") as fh:
            magic = fh.read(4)
    except OSError:
        magic = b""

    ext = input_path.suffix.lower()
    if magic == b"CISO" or ext == ".ciso":
        job_dir.mkdir(parents=True, exist_ok=True)
        temp_iso = job_dir / f"_dolphin_unpacked_{uuid.uuid4().hex[:8]}.iso"
        try:
            _unpack_ciso_to_sparse_iso(input_path, temp_iso, system_id=system_id)
        except Exception:
            temp_iso.unlink(missing_ok=True)
            raise
        return temp_iso, temp_iso

    if magic == b"WBFS" or ext == ".wbfs":
        job_dir.mkdir(parents=True, exist_ok=True)
        temp_iso = job_dir / f"_dolphin_unpacked_{uuid.uuid4().hex[:8]}.iso"
        try:
            _unpack_wbfs_to_sparse_iso(input_path, temp_iso)
        except Exception:
            temp_iso.unlink(missing_ok=True)
            raise
        return temp_iso, temp_iso

    return input_path, None


def _convert_rvz(
    input_path: Path,
    job_dir: Path,
    config: PipelineConfig,
    companion_files: tuple[Path, ...] = (),
    system_id: str | None = None,
) -> Path:
    dolphin = config.binaries.dolphin_tool
    if dolphin is None:
        raise MissingToolError(
            f"Cannot convert `{input_path.name}` to `.rvz`: `DolphinTool` binary not found. "
            "Place `DolphinTool.exe` in `tools/dolphin/` or install Dolphin on PATH."
        )

    clean_stem = sanitize_rom_stem(input_path.stem, companion_files)
    out_rvz = _unique_staging_output(job_dir, clean_stem, ".rvz")
    conv = config.conversion
    effective_input, temp_iso = _prepare_dolphin_input_for_rvz(input_path, job_dir, system_id=system_id)
    try:
        cmd = [
            str(dolphin),
            "convert",
            "-i",
            str(effective_input),
            "-o",
            str(out_rvz),
            "-f",
            "rvz",
            "-b",
            str(conv.rvz_block_size),
            "-c",
            conv.rvz_compression,
            "-l",
            str(conv.rvz_compression_level),
        ]
        _run_streaming_subprocess(
            cmd,
            status_label=f"DolphinTool RVZ: {input_path.name} -> {out_rvz.name}",
        )
        _verify_nonzero_output(out_rvz, "DolphinTool")
    finally:
        if temp_iso is not None:
            temp_iso.unlink(missing_ok=True)
    return out_rvz


def ensure_squirrel_keys_txt(config: PipelineConfig) -> Path:
    """
    Ensure NSC_Builder (`squirrel`) has a readable `keys.txt` beside its executable/script.

    Users commonly supply standard Switch `prod.keys` (e.g. in `tools/nscb/ztools/prod.keys` or
    `~/.switch/prod.keys`) rather than NSCB's `keys.txt`. When `prod.keys` is present (or newer
    than an existing `keys.txt`), automatically copy it to `<squirrel_dir>/keys.txt` without
    modifying or deleting the original `prod.keys`.
    """
    if config.binaries.squirrel is None:
        raise MissingToolError("Switch conversion requires `squirrel` (NSC_Builder).")
    if config.binaries.squirrel_keys is None or not config.binaries.squirrel_keys.is_file():
        raise MissingToolError(
            "Switch crypto requires `prod.keys` or `keys.txt` "
            "(place `prod.keys` in `tools/nscb/ztools/` or `~/.switch/prod.keys`)."
        )

    expected_keys_txt = config.binaries.squirrel.parent / "keys.txt"
    source_keys = config.binaries.squirrel_keys

    # If `keys.txt` already exists, check whether a sibling `prod.keys` has been updated more recently
    if expected_keys_txt.is_file() and source_keys.resolve() == expected_keys_txt.resolve():
        newer_candidates = (
            expected_keys_txt.parent / "prod.keys",
            config.paths.project_root / "tools" / "nscb" / "ztools" / "prod.keys",
            config.paths.project_root / "tools" / "nscb" / "prod.keys",
            config.paths.project_root / "tools" / "switch" / "prod.keys",
        )
        cur_mtime = expected_keys_txt.stat().st_mtime_ns
        for cand in newer_candidates:
            try:
                if cand.is_file() and cand.stat().st_mtime_ns > cur_mtime:
                    source_keys = cand
                    cur_mtime = cand.stat().st_mtime_ns
            except OSError:
                continue

    try:
        if expected_keys_txt.is_file() and source_keys.resolve() == expected_keys_txt.resolve():
            return expected_keys_txt
    except OSError:
        pass

    # Skip redundant write if `expected_keys_txt` already matches `source_keys` size & mtime
    try:
        if expected_keys_txt.is_file():
            src_st = source_keys.stat()
            dst_st = expected_keys_txt.stat()
            if dst_st.st_size == src_st.st_size and dst_st.st_mtime_ns >= src_st.st_mtime_ns:
                return expected_keys_txt
    except OSError:
        pass

    try:
        # In Docker, `/opt/nscb/ztools/keys.txt` may be a symlink pointing into read-only `/app/tools`;
        # replace a dangling or mismatched symlink cleanly with a real copy in writable `/opt/nscb/ztools/`.
        if expected_keys_txt.is_symlink():
            expected_keys_txt.unlink(missing_ok=True)
        expected_keys_txt.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_keys, expected_keys_txt)
    except OSError as exc:
        raise ConversionError(
            f"Could not sync Switch keys from `{source_keys}` to `{expected_keys_txt}`: {exc}"
        ) from exc

    return expected_keys_txt


def _merge_switch_bundle(
    item: IdentificationResult,
    job_dir: Path,
    config: PipelineConfig,
) -> tuple[Path, ...]:
    base_cmd, sq_cwd = _resolve_squirrel_invocation(config)
    sq_name = Path(base_cmd[-1]).stem.lower()
    if sq_name == "nsz":
        raise MissingToolError(
            "Multi-file Switch `.xci` merging requires NSC_Builder (`squirrel`), not standalone `nsz`."
        )
    ensure_squirrel_keys_txt(config)

    out_dir = job_dir / "_switch_merged"
    out_dir.mkdir(parents=True, exist_ok=True)
    mlist_file = job_dir / "mlist.txt"
    all_inputs = [item.primary_file, *item.companion_files]
    mlist_file.write_text(
        "\n".join(str(p.resolve()) for p in all_inputs) + "\n",
        encoding="utf-8",
    )

    target_fmt = "xci" if config.conversion.switch_merge_format == "xci" else "cnsp"
    cmd = [
        *base_cmd,
        "-b", "65536",
        "-pv", "false",
        "-kp", "false",
        "--RSVcap", "268435656",
        "-fat", "exfat",
        "-fx", "files",
        "-ND", "true",
        "-t", target_fmt,
        "-o", str(out_dir.resolve()),
        "-tfile", str(mlist_file.resolve()),
        "-roma", "true",
        "-dmul", "calculate",
    ]
    _run_streaming_subprocess(
        cmd,
        status_label=f"Squirrel Multi-Merge ({len(all_inputs)} files -> .{config.conversion.switch_merge_format})",
        cwd=sq_cwd,
    )

    produced = sorted(
        p for p in out_dir.iterdir()
        if p.is_file() and p.suffix.lower() in (".xci", ".nsp")
    )
    if not produced:
        raise ConversionError("`squirrel` exited 0, but produced no `.xci`/`.nsp` in output folder.")
    for p in produced:
        _verify_nonzero_output(p, "squirrel")
    return tuple(produced)


def _decompress_switch_package(
    input_path: Path,
    job_dir: Path,
    config: PipelineConfig,
) -> Path:
    base_cmd, sq_cwd = _resolve_squirrel_invocation(config)
    sq_name = Path(base_cmd[-1]).stem.lower()

    out_dir = job_dir / "_switch_decomp"
    out_dir.mkdir(parents=True, exist_ok=True)

    if sq_name == "nsz":
        cmd = [*base_cmd, "-D", "-o", str(out_dir.resolve()), str(input_path.resolve())]
    else:
        ensure_squirrel_keys_txt(config)
        cmd = [*base_cmd, "-dcpr", str(input_path.resolve()), "-o", str(out_dir.resolve())]

    _run_streaming_subprocess(
        cmd,
        status_label=f"Decompressing Switch package: {input_path.name}",
        cwd=sq_cwd,
    )

    produced = sorted(
        p for p in out_dir.iterdir()
        if p.is_file() and p.suffix.lower() in (".nsp", ".xci")
    )
    if not produced:
        raise ConversionError(f"Decompression produced no `.nsp`/`.xci` output for `{input_path.name}`.")
    _verify_nonzero_output(produced[0], sq_name)
    return produced[0]


def _link_or_copy_file(src_file: Path, dst_file: Path) -> None:
    """Create a hardlink on the same volume when possible, falling back to copying bytes."""
    dst_file.parent.mkdir(parents=True, exist_ok=True)
    if dst_file.exists():
        dst_file.unlink()
    try:
        os.link(src_file, dst_file)
    except OSError:
        shutil.copy2(src_file, dst_file)


def _stage_clean_loadiine_tree(title_root: Path, files: tuple[Path, ...], dest_root: Path) -> None:
    """Populate `dest_root` with clean Loadiine subfolders (`code`, `content`, `meta`)."""
    if dest_root.exists():
        shutil.rmtree(dest_root, ignore_errors=True)
    dest_root.mkdir(parents=True, exist_ok=True)
    for f in files:
        if not f.is_file():
            continue
        try:
            rel = f.resolve().relative_to(title_root.resolve())
        except ValueError:
            continue
        _link_or_copy_file(f, dest_root / rel)


def _convert_wiiu_item(
    item: IdentificationResult,
    staging_job_dir: Path,
    config: PipelineConfig,
) -> ConversionArtifact:
    """Convert or normalize a Wii U single file, Loadiine folder, or encrypted NUS package."""
    primary = item.primary_file
    clean_title = item.wiiu_clean_title or sanitize_rom_stem(primary.stem)

    # Single-file Wii U image (`.wua`, `.wux`, `.wud`, `.wuhb`)
    if not item.is_encrypted_wiiu and primary.suffix.lower() != ".rpx":
        return ConversionArtifact(
            system_id="wiiu",
            output_files=(primary,),
            converted=False,
            summary=f"Wii U image ready ({primary.name})",
        )

    # Encrypted-only WUP/NUS package (`title.tmd` + `title.tik` + `*.app`)
    title_id = item.wiiu_title_id
    title_ver = item.wiiu_title_version if item.wiiu_title_version is not None else 0
    if item.is_encrypted_wiiu:
        cdecrypt_bin = config.binaries.cdecrypt
        if cdecrypt_bin is None:
            raise MissingToolError(
                "`cdecrypt` binary not found. Place `cdecrypt` in `tools/wiiu/` to decrypt encrypted Wii U NUS packages."
            )
        nus_dir = primary.parent
        dec_dir = staging_job_dir / f"_wiiu_dec_{uuid.uuid4().hex[:6]}"
        dec_dir.mkdir(parents=True, exist_ok=True)
        tik_file = next(
            (c for c in item.companion_files if c.name.lower() == "title.tik"),
            None,
        )
        cmd = (
            [str(cdecrypt_bin), str(primary), str(tik_file), str(dec_dir)]
            if tik_file is not None
            else [str(cdecrypt_bin), str(nus_dir), str(dec_dir)]
        )
        _run_streaming_subprocess(cmd, f"cdecrypt: {clean_title}", cwd=nus_dir)
        code_rpx = sorted((dec_dir / "code").glob("*.rpx")) if (dec_dir / "code").is_dir() else []
        if not code_rpx:
            raise ConversionError(f"`cdecrypt` produced no `code/*.rpx` inside `{dec_dir}`.")
        title_root = dec_dir
        loadiine_files = tuple(sorted(p for p in dec_dir.rglob("*") if p.is_file()))
    else:
        # Unpacked Loadiine/hybrid tree: `primary` is `<title_root>/code/<game>.rpx`
        title_root = primary.parent.parent
        loadiine_files = (primary, *item.companion_files)

    # Option A: Pack into compressed `.wua` if `zarchive` binary is available and enabled
    zarchive_bin = config.binaries.zarchive
    if config.conversion.wiiu_to_wua and zarchive_bin is not None and title_id:
        pack_stage = staging_job_dir / f"_wua_stage_{uuid.uuid4().hex[:6]}"
        slot_name = f"{title_id.lower()}_v{title_ver}"
        _stage_clean_loadiine_tree(title_root, loadiine_files, pack_stage / slot_name)
        out_wua = staging_job_dir / f"{clean_title}.wua"
        out_wua.unlink(missing_ok=True)
        cmd = [str(zarchive_bin), str(pack_stage), str(out_wua)]
        _run_streaming_subprocess(cmd, f"zarchive -> {out_wua.name}", cwd=staging_job_dir)
        _verify_nonzero_output(out_wua, "zarchive")
        return ConversionArtifact(
            system_id="wiiu",
            output_files=(out_wua,),
            converted=True,
            tool_used="zarchive",
            summary=f"Packed Wii U Loadiine title -> `{out_wua.name}`",
        )

    # Option B: Fallback clean Loadiine folder (`{code, content, meta}`, stripped of `.app`/`.h3`)
    out_dir = staging_job_dir / clean_title
    _stage_clean_loadiine_tree(title_root, loadiine_files, out_dir)
    return ConversionArtifact(
        system_id="wiiu",
        output_files=(out_dir,),
        converted=True,
        is_directory=True,
        summary=f"Prepared clean Wii U Cemu folder `{clean_title}/` ({len(loadiine_files)} files)",
    )


def _format_vita_clean_game_title(item: IdentificationResult, fallback_stem: str) -> str:
    """Return the clean display title for a PS Vita game (used for `.dpt` tag and stem prefix)."""
    base = item.vita_clean_title or fallback_stem
    if item.vita_title_id:
        base = re.sub(
            rf"[\s_\-]*[\(\[]{re.escape(item.vita_title_id)}[\)\]]",
            "",
            base,
            flags=re.IGNORECASE,
        ).strip()
    if not base:
        base = f"Vita_{item.vita_title_id}" if item.vita_title_id else fallback_stem
    return base


def _format_vita_output_stem(item: IdentificationResult, fallback_stem: str) -> str:
    """Format a standardized PS Vita output stem: `<Title> [<TitleID>] [v<Ver>]`."""
    base = _format_vita_clean_game_title(item, fallback_stem)

    tid_tag = f" [{item.vita_title_id}]" if item.vita_title_id else ""
    if item.vita_category == "gp":
        ver_tag = f" [Update v{item.vita_app_version}]" if item.vita_app_version else " [Update]"
    elif item.vita_category == "ac":
        ver_tag = " [DLC]"
    elif item.vita_app_version:
        ver_tag = f" [v{item.vita_app_version}]"
    else:
        ver_tag = ""

    return f"{base}{tid_tag}{ver_tag}".strip()


def _plan_vita_dpt_shortcut(
    item: IdentificationResult,
    fallback_stem: str,
    job_dir: Path,
) -> tuple[Path, ...]:
    """Predict the Cocoon FE `.dpt` tag shortcut path (`<Title>.dpt`) for a PS Vita base game."""
    if item.vita_category in ("gp", "ac") or not item.vita_title_id:
        return ()
    clean_title = _format_vita_clean_game_title(item, fallback_stem)
    return (job_dir / f"{clean_title}.dpt",)


def _write_vita_dpt_shortcut(
    item: IdentificationResult,
    fallback_stem: str,
    job_dir: Path,
) -> tuple[Path, ...]:
    """
    Generate a Cocoon FE `.dpt` tag shortcut (`[vita_game_id] <TITLE_ID>`) for PS Vita base games.

    Cocoon FE scans `<Title>.dpt` in `roms/psvita/` to display the game title, scrape box art,
    and launch Vita3K directly by Title ID (`https://cocoon-shell.com/wiki/tags/`).
    Standalone Updates (`gp`) and DLC (`ac`) do not generate `.dpt` files.
    """
    if item.vita_category in ("gp", "ac") or not item.vita_title_id:
        return ()
    clean_title = _format_vita_clean_game_title(item, fallback_stem)
    job_dir.mkdir(parents=True, exist_ok=True)
    dpt_path = job_dir / f"{clean_title}.dpt"
    dpt_path.write_text(f"[vita_game_id] {item.vita_title_id}\n", encoding="utf-8")
    return (dpt_path,)


def _stage_clean_vita_tree(
    vita_files: tuple[Path, ...],
    primary_tid: str | None,
    dest_root: Path,
    project_root: Path | None = None,
) -> int:
    """
    Stage a clean Vita3K NoNpDrm / MaiDump package tree inside `dest_root`:
      - Single package root: `<dest_root>/<TITLE_ID>/sce_sys/param.sfo`, etc.
      - Multi-package bundle (Base + Update + DLC): preserves relative package root layout
        (`app/<TITLE_ID>/...`, `patch/<TITLE_ID>/...`, `addcont/<TITLE_ID>/...`).
    """
    dest_root.mkdir(parents=True, exist_ok=True)
    staging_parent = dest_root.parent.resolve()

    sfo_files = [
        f for f in vita_files
        if f.name.lower() == "param.sfo" and f.parent.name.lower() == "sce_sys"
    ]
    pkg_roots = sorted(
        {f.parent.parent.resolve() for f in sfo_files},
        key=lambda p: (len(p.parts), str(p).lower()),
    )

    if not pkg_roots:
        return 0

    if len(pkg_roots) == 1:
        single_root = pkg_roots[0]
        top_folder = primary_tid or single_root.name or "VITA_APP"
        root_map = [(single_root, Path(top_folder))]
    else:
        common = Path(os.path.commonpath([str(r) for r in pkg_roots])).resolve()
        # Match deepest roots first when assigning files
        root_map = []
        for idx, r in enumerate(sorted(pkg_roots, key=lambda p: len(p.parts), reverse=True)):
            rel_prefix = r.relative_to(common) if r != common else Path(f"{primary_tid or 'pkg'}_{idx}")
            root_map.append((r, rel_prefix))

    staged_count = 0
    for f in vita_files:
        src_f = f.resolve()
        if not src_f.is_file():
            continue
        matched_pair = next(((r, pref) for r, pref in root_map if src_f.is_relative_to(r)), None)
        if matched_pair is None:
            continue
        pkg_root, rel_prefix = matched_pair
        rel_inside = src_f.relative_to(pkg_root)
        dst_f = dest_root / rel_prefix / rel_inside
        dst_f.parent.mkdir(parents=True, exist_ok=True)
        if src_f.is_relative_to(staging_parent):
            os.replace(src_f, dst_f)
        else:
            _link_or_copy_file(src_f, dst_f)
        staged_count += 1

    # If the primary package root lacks `sce_sys/package/work.bin`, inject it from `PSV_GAMES.tsv` if available
    if primary_tid and root_map:
        _, first_prefix = root_map[0]
        work_bin_dst = dest_root / first_prefix / "sce_sys" / "package" / "work.bin"
        if not work_bin_dst.is_file():
            tsv_zrif, _, _ = lookup_vita_tsv_zrif(None, primary_tid, project_root=project_root)
            if tsv_zrif:
                try:
                    rif_bytes = decode_zrif_to_work_bin(tsv_zrif)
                    work_bin_dst.parent.mkdir(parents=True, exist_ok=True)
                    work_bin_dst.write_bytes(rif_bytes)
                    staged_count += 1
                except Exception:
                    pass

    return staged_count


def _pack_directory_to_zip(
    src_dir: Path,
    out_zip: Path,
    config: PipelineConfig,
) -> str:
    """Pack `src_dir` contents into `out_zip` via `7z` (if available) or Python's `zipfile`."""
    out_zip.unlink(missing_ok=True)
    seven_zip = config.binaries.seven_zip
    if seven_zip is not None:
        cmd = [str(seven_zip), "a", "-tzip", "-mx=1", str(out_zip.resolve()), "."]
        _run_streaming_subprocess(cmd, status_label=f"7z zip -> {out_zip.name}", cwd=src_dir)
        _verify_nonzero_output(out_zip, "7z")
        return "7z"

    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        for file_path in sorted(p for p in src_dir.rglob("*") if p.is_file()):
            arcname = file_path.relative_to(src_dir).as_posix()
            zf.write(file_path, arcname=arcname)
    _verify_nonzero_output(out_zip, "zipfile")
    return "zipfile"


def _convert_psvita_item(
    item: IdentificationResult,
    job_dir: Path,
    config: PipelineConfig,
) -> ConversionArtifact:
    """
    Convert or package a PlayStation Vita item for Vita3K:
      1. `.vpk` -> passthrough as-is.
      2. `.pkg` -> unpack into Vita3K `.zip` via `pkg2zip` (embedding `.zrif` or `work.bin` if present),
         or route `.pkg` (+ license sidecar) as-is if `pkg2zip` is not configured.
      3. `sce_sys/param.sfo` (NoNpDrm / MaiDump / Vitamin tree) -> repack into a clean Vita3K-installable
         `<Title> [<TitleID>] [v<Ver>].zip` (or clean folder when `vita_repack_zip = false`).
    """
    primary = item.primary_file
    ext = primary.suffix.lower()
    is_addon = item.vita_category in ("gp", "ac")

    if ext == ".dpt":
        return ConversionArtifact(
            system_id="psvita",
            output_files=(primary,),
            converted=False,
            is_vita_addon=False,
            summary=f"Cocoon FE `.dpt` Vita tag ready ({primary.name})",
        )

    fallback_stem = sanitize_rom_stem(
        primary.parent.parent.name if primary.name.lower() == "param.sfo" else primary.stem,
        item.companion_files,
    )
    staging_job_dir = job_dir.resolve()
    staging_job_dir.mkdir(parents=True, exist_ok=True)
    dpt_outputs = _write_vita_dpt_shortcut(item, fallback_stem, staging_job_dir)

    if ext == ".vpk":
        return ConversionArtifact(
            system_id="psvita",
            output_files=(primary, *dpt_outputs),
            converted=bool(dpt_outputs),
            is_vita_addon=is_addon,
            summary=f"PS Vita `.vpk` ready ({primary.name})",
        )

    vita_stem = _format_vita_output_stem(item, fallback_stem)

    if ext == ".pkg":
        pkg2zip_bin = config.binaries.pkg2zip
        zrif_str = item.vita_zrif or ""
        work_bin_path: Path | None = None
        for comp in item.companion_files:
            if comp.name.lower() == "work.bin":
                work_bin_path = comp
            elif comp.suffix.lower() in (".zrif", ".txt"):
                try:
                    cand = comp.read_text(encoding="utf-8", errors="ignore").strip()
                    if cand:
                        zrif_str = cand.splitlines()[0].strip()
                except OSError:
                    pass

        if pkg2zip_bin is None:
            norm_pkg = _unique_staging_output(staging_job_dir, vita_stem, ".pkg")
            if primary.resolve().is_relative_to(staging_job_dir):
                os.replace(primary, norm_pkg)
            else:
                _link_or_copy_file(primary, norm_pkg)

            extra_outputs: list[Path] = list(item.companion_files)
            if zrif_str and item.vita_category != "gp":
                if work_bin_path is None:
                    try:
                        rif_bytes = decode_zrif_to_work_bin(zrif_str)
                        gen_work_bin = staging_job_dir / f"{vita_stem}_work.bin"
                        gen_work_bin.write_bytes(rif_bytes)
                        extra_outputs.append(gen_work_bin)
                    except Exception:
                        pass
                if not any(
                    c.suffix.lower() == ".zrif"
                    or c.name.lower() == "zrif.txt"
                    or c.name.lower().endswith(".zrif.txt")
                    for c in extra_outputs
                ):
                    gen_zrif = staging_job_dir / f"{vita_stem}.zrif.txt"
                    gen_zrif.write_text(f"{zrif_str}\n", encoding="utf-8")
                    extra_outputs.append(gen_zrif)

            return ConversionArtifact(
                system_id="psvita",
                output_files=(norm_pkg, *dpt_outputs, *extra_outputs),
                converted=(norm_pkg.name != primary.name or bool(dpt_outputs) or bool(extra_outputs)),
                is_vita_addon=is_addon,
                summary=f"PS Vita `.pkg` ready ({norm_pkg.name})",
            )

        pkg_work = staging_job_dir / f"_pkg2zip_{uuid.uuid4().hex[:6]}"
        pkg_work.mkdir(parents=True, exist_ok=True)
        cmd = [str(pkg2zip_bin), str(primary.resolve())]
        if zrif_str and item.vita_category != "gp":
            cmd.append(zrif_str)

        _run_streaming_subprocess(
            cmd,
            status_label=f"pkg2zip -> {primary.name}",
            cwd=pkg_work,
        )
        created_zips = sorted(pkg_work.glob("*.zip"))
        if not created_zips:
            raise ConversionError(f"`pkg2zip` exited 0, but produced no `.zip` output in `{pkg_work}`.")

        raw_zip = created_zips[0]
        # Ensure `sce_sys/package/work.bin` is present inside the resulting `.zip` whenever a license exists
        if (work_bin_path is not None and work_bin_path.is_file()) or (zrif_str and item.vita_category != "gp"):
            with zipfile.ZipFile(raw_zip, "a", compression=zipfile.ZIP_DEFLATED) as zf:
                names = [n.replace("\\", "/").lstrip("/") for n in zf.namelist()]
                if not any(n.lower().endswith("sce_sys/package/work.bin") for n in names):
                    sfo_entry = next((n for n in names if n.lower().endswith("sce_sys/param.sfo")), "")
                    prefix_dir = sfo_entry[: -len("sce_sys/param.sfo")] if sfo_entry else ""
                    if work_bin_path is not None and work_bin_path.is_file():
                        zf.write(work_bin_path, arcname=f"{prefix_dir}sce_sys/package/work.bin")
                    elif zrif_str:
                        try:
                            zf.writestr(f"{prefix_dir}sce_sys/package/work.bin", decode_zrif_to_work_bin(zrif_str))
                        except Exception:
                            pass

        out_zip = _unique_staging_output(staging_job_dir, vita_stem, ".zip")
        shutil.move(str(raw_zip), str(out_zip))
        shutil.rmtree(pkg_work, ignore_errors=True)
        _verify_nonzero_output(out_zip, "pkg2zip")
        return ConversionArtifact(
            system_id="psvita",
            output_files=(out_zip, *dpt_outputs),
            converted=True,
            tool_used="pkg2zip",
            is_vita_addon=is_addon,
            summary=f"Unpacked PS Vita `.pkg` -> `{out_zip.name}` (`pkg2zip`)",
        )

    # Case 3: Unpacked NoNpDrm / MaiDump / Vitamin tree (`primary` is `sce_sys/param.sfo`)
    vita_files = (primary, *item.companion_files)
    if config.conversion.vita_repack_zip:
        pack_stage = staging_job_dir / f"_vita_stage_{uuid.uuid4().hex[:6]}"
        staged_count = _stage_clean_vita_tree(
            vita_files,
            item.vita_title_id,
            pack_stage,
            project_root=config.paths.project_root,
        )
        if staged_count == 0:
            raise ConversionError(f"Failed to stage any PS Vita package files from `{primary.parent.parent}`.")
        out_zip = _unique_staging_output(staging_job_dir, vita_stem, ".zip")
        tool_used = _pack_directory_to_zip(pack_stage, out_zip, config)
        shutil.rmtree(pack_stage, ignore_errors=True)
        return ConversionArtifact(
            system_id="psvita",
            output_files=(out_zip, *dpt_outputs),
            converted=True,
            tool_used=tool_used,
            is_vita_addon=is_addon,
            summary=f"Repacked PS Vita package -> `{out_zip.name}` ({staged_count} files)",
        )

    out_dir = staging_job_dir / vita_stem
    staged_count = _stage_clean_vita_tree(
        vita_files,
        item.vita_title_id,
        out_dir,
        project_root=config.paths.project_root,
    )
    return ConversionArtifact(
        system_id="psvita",
        output_files=(out_dir, *dpt_outputs),
        converted=True,
        is_vita_addon=is_addon,
        is_directory=True,
        summary=f"Prepared clean PS Vita folder `{vita_stem}/` ({staged_count} files)",
    )


# ------------------------------------------------------------------------------
# Conversion Strategy Registry (`plan()` + `execute()`)
# ------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ConversionPlan:
    """Unified dry-run artifact prediction and `inspect` action description."""

    artifact: ConversionArtifact
    action_description: str
    requires_quarantine: bool = False


class ConversionStrategy:
    """Base strategy for console-specific conversion planning and execution."""

    def matches(self, item: IdentificationResult, config: PipelineConfig) -> bool:
        raise NotImplementedError

    def plan(
        self,
        item: IdentificationResult,
        job_dir: Path,
        config: PipelineConfig,
        from_archive: bool = False,
        archive_ext: str = "",
        psx_multidisc_peers: int = 1,
    ) -> ConversionPlan:
        raise NotImplementedError

    def execute(
        self,
        item: IdentificationResult,
        job_dir: Path,
        config: PipelineConfig,
    ) -> ConversionArtifact:
        raise NotImplementedError


def _archive_prefix(item: IdentificationResult, config: PipelineConfig, from_archive: bool) -> str:
    if not item.system_id or item.system_id not in config.systems:
        return ""
    policy = config.systems[item.system_id].archive_policy
    return "Extract -> " if from_archive and policy == ArchivePolicy.EXTRACT else ""


class ArcadePassthroughStrategy(ConversionStrategy):
    """Strategy for Arcade sets (`cps1`, `cps2`, `cps3`, `neogeo`, `fbneo`) with `passthrough` policy."""

    def matches(self, item: IdentificationResult, config: PipelineConfig) -> bool:
        return bool(
            item.system_id
            and item.system_id in config.systems
            and config.systems[item.system_id].archive_policy == ArchivePolicy.PASSTHROUGH
        )

    def plan(
        self,
        item: IdentificationResult,
        job_dir: Path,
        config: PipelineConfig,
        from_archive: bool = False,
        archive_ext: str = "",
        psx_multidisc_peers: int = 1,
    ) -> ConversionPlan:
        assert item.system_id is not None
        dest_dir = config.system_dest_dir(item.system_id)
        art = ConversionArtifact(
            system_id=item.system_id,
            output_files=(item.primary_file,),
            converted=False,
            summary=f"Arcade passthrough ({item.primary_file.name})",
        )
        return ConversionPlan(
            artifact=art,
            action_description=f"DO NOT EXTRACT -> Route archive as-is to {dest_dir}",
        )

    def execute(
        self,
        item: IdentificationResult,
        job_dir: Path,
        config: PipelineConfig,
    ) -> ConversionArtifact:
        return self.plan(item, job_dir, config).artifact


class CdChdStrategy(ConversionStrategy):
    """Strategy for CD-ROM CHD systems (`psx`, `saturn`, `dreamcast`, `segacd`, `pcenginecd`)."""

    def matches(self, item: IdentificationResult, config: PipelineConfig) -> bool:
        return bool(item.system_id in CD_CHD_SYSTEMS)

    def plan(
        self,
        item: IdentificationResult,
        job_dir: Path,
        config: PipelineConfig,
        from_archive: bool = False,
        archive_ext: str = "",
        psx_multidisc_peers: int = 1,
    ) -> ConversionPlan:
        assert item.system_id is not None
        sys_id = item.system_id
        primary = item.primary_file
        ext = primary.suffix.lower()
        clean_stem = sanitize_rom_stem(primary.stem, item.companion_files)
        dest_dir = config.system_dest_dir(sys_id)
        prefix = _archive_prefix(item, config, from_archive)

        if ext in (".chd", ".pbp", ".m3u"):
            out = primary
        else:
            out = job_dir / f"{clean_stem}.chd"

        if (
            sys_id == "psx"
            and config.conversion.psx_multidisc_pbp
            and ext not in (".pbp", ".m3u")
            and psx_multidisc_peers >= 2
        ):
            parsed = parse_multidisc_info(clean_stem)
            if parsed is not None:
                desc = f"{prefix}psxpackager merge ({psx_multidisc_peers} discs -> {parsed[0]}.pbp) -> {dest_dir}"
                return ConversionPlan(
                    artifact=ConversionArtifact(system_id=sys_id, output_files=(out,), converted=(out != primary)),
                    action_description=desc,
                )

        if ext in (".chd", ".pbp"):
            desc = f"{prefix}Route {ext} as-is -> {dest_dir}"
        else:
            desc = f"{prefix}chdman createcd (.chd) -> {dest_dir}"

        return ConversionPlan(
            artifact=ConversionArtifact(system_id=sys_id, output_files=(out,), converted=(out != primary)),
            action_description=desc,
        )

    def execute(
        self,
        item: IdentificationResult,
        job_dir: Path,
        config: PipelineConfig,
    ) -> ConversionArtifact:
        assert item.system_id is not None
        sys_id = item.system_id
        primary = item.primary_file
        ext = primary.suffix.lower()
        if ext in (".chd", ".pbp", ".m3u"):
            return ConversionArtifact(
                system_id=sys_id,
                output_files=(primary,),
                converted=False,
                summary=f"Already optimal format ({ext})",
            )
        out_chd = _convert_chd(
            "createcd",
            primary,
            job_dir,
            config,
            companion_files=item.companion_files,
        )
        staged_sbis: list[Path] = []
        if sys_id == "psx":
            clean_stem = sanitize_rom_stem(primary.stem, item.companion_files)
            staged_sbis = _stage_psx_sbi_sidecars(
                item,
                job_dir,
                stems_to_stage=(clean_stem,),
                include_parent_dir=False,
            )

        return ConversionArtifact(
            system_id=sys_id,
            output_files=(out_chd, *staged_sbis),
            converted=True,
            tool_used="chdman",
            summary=f"Converted `{primary.name}` -> `{out_chd.name}` (`createcd`)",
        )


class Ps2ChdStrategy(ConversionStrategy):
    """Strategy for PlayStation 2 (`ps2`: DVD `.iso` -> `createdvd`, <750MB CD `.cue/.bin` -> `createcd`)."""

    def matches(self, item: IdentificationResult, config: PipelineConfig) -> bool:
        return item.system_id == "ps2"

    def plan(
        self,
        item: IdentificationResult,
        job_dir: Path,
        config: PipelineConfig,
        from_archive: bool = False,
        archive_ext: str = "",
        psx_multidisc_peers: int = 1,
    ) -> ConversionPlan:
        primary = item.primary_file
        ext = primary.suffix.lower()
        clean_stem = sanitize_rom_stem(primary.stem, item.companion_files)
        dest_dir = config.system_dest_dir("ps2")
        prefix = _archive_prefix(item, config, from_archive)

        if ext == ".chd":
            out = primary
            desc = f"{prefix}Route .chd as-is -> {dest_dir}"
        else:
            out = job_dir / f"{clean_stem}.chd"
            subcmd = "createcd" if item.disc_media_type == "cd" else "createdvd"
            desc = f"{prefix}chdman {subcmd} (.chd) -> {dest_dir}"

        return ConversionPlan(
            artifact=ConversionArtifact(system_id="ps2", output_files=(out,), converted=(out != primary)),
            action_description=desc,
        )

    def execute(
        self,
        item: IdentificationResult,
        job_dir: Path,
        config: PipelineConfig,
    ) -> ConversionArtifact:
        primary = item.primary_file
        if primary.suffix.lower() == ".chd":
            return ConversionArtifact(
                system_id="ps2",
                output_files=(primary,),
                converted=False,
                summary="Already `.chd`",
            )
        subcmd = "createcd" if item.disc_media_type == "cd" else "createdvd"
        out_chd = _convert_chd(
            subcommand=subcmd,
            input_path=primary,
            job_dir=job_dir,
            config=config,
            companion_files=item.companion_files,
        )
        return ConversionArtifact(
            system_id="ps2",
            output_files=(out_chd,),
            converted=True,
            tool_used="chdman",
            summary=f"Converted `{primary.name}` -> `{out_chd.name}` (`{subcmd}`)",
        )


class PspChdStrategy(ConversionStrategy):
    """Strategy for PlayStation Portable (`psp`: `.iso` -> `chdman createdvd -hs 2048`)."""

    def matches(self, item: IdentificationResult, config: PipelineConfig) -> bool:
        return item.system_id == "psp"

    def plan(
        self,
        item: IdentificationResult,
        job_dir: Path,
        config: PipelineConfig,
        from_archive: bool = False,
        archive_ext: str = "",
        psx_multidisc_peers: int = 1,
    ) -> ConversionPlan:
        primary = item.primary_file
        ext = primary.suffix.lower()
        clean_stem = sanitize_rom_stem(primary.stem, item.companion_files)
        dest_dir = config.system_dest_dir("psp")
        prefix = _archive_prefix(item, config, from_archive)

        if ext == ".iso" and config.conversion.psp_to_chd:
            out = job_dir / f"{clean_stem}.chd"
        else:
            out = primary

        if ext in (".chd", ".cso", ".pbp") or not config.conversion.psp_to_chd:
            desc = f"{prefix}Route {ext} as-is -> {dest_dir}"
        else:
            desc = f"{prefix}chdman createdvd -hs {config.conversion.psp_hunk_size} (.chd) -> {dest_dir}"

        return ConversionPlan(
            artifact=ConversionArtifact(system_id="psp", output_files=(out,), converted=(out != primary)),
            action_description=desc,
        )

    def execute(
        self,
        item: IdentificationResult,
        job_dir: Path,
        config: PipelineConfig,
    ) -> ConversionArtifact:
        primary = item.primary_file
        ext = primary.suffix.lower()
        if ext == ".iso" and config.conversion.psp_to_chd:
            out_chd = _convert_chd(
                subcommand="createdvd",
                input_path=primary,
                job_dir=job_dir,
                config=config,
                companion_files=item.companion_files,
                hunk_size=config.conversion.psp_hunk_size,
            )
            return ConversionArtifact(
                system_id="psp",
                output_files=(out_chd,),
                converted=True,
                tool_used="chdman",
                summary=(
                    f"Converted `{primary.name}` -> `{out_chd.name}` "
                    f"(`createdvd -hs {config.conversion.psp_hunk_size}`)"
                ),
            )
        return ConversionArtifact(
            system_id="psp",
            output_files=(primary,),
            converted=False,
            summary=f"PSP passthrough ({ext})",
        )


class DolphinRvzStrategy(ConversionStrategy):
    """Strategy for GameCube & Wii (`gc`, `wii`: `.iso/.gcm/.wbfs/.ciso/.gcz` -> `.rvz`)."""

    def matches(self, item: IdentificationResult, config: PipelineConfig) -> bool:
        return item.system_id in ("gc", "wii")

    def plan(
        self,
        item: IdentificationResult,
        job_dir: Path,
        config: PipelineConfig,
        from_archive: bool = False,
        archive_ext: str = "",
        psx_multidisc_peers: int = 1,
    ) -> ConversionPlan:
        assert item.system_id is not None
        sys_id = item.system_id
        primary = item.primary_file
        ext = primary.suffix.lower()
        clean_stem = sanitize_rom_stem(primary.stem, item.companion_files)
        dest_dir = config.system_dest_dir(sys_id)
        prefix = _archive_prefix(item, config, from_archive)

        if ext in DOLPHIN_CONVERTIBLE_EXTS:
            out = job_dir / f"{clean_stem}.rvz"
        else:
            out = primary

        if ext == ".rvz":
            desc = f"{prefix}Route .rvz as-is -> {dest_dir}"
        elif ext not in DOLPHIN_CONVERTIBLE_EXTS:
            desc = f"{prefix}Route {ext} -> {dest_dir}"
        else:
            desc = f"{prefix}DolphinTool convert (.rvz) -> {dest_dir}"

        return ConversionPlan(
            artifact=ConversionArtifact(system_id=sys_id, output_files=(out,), converted=(out != primary)),
            action_description=desc,
        )

    def execute(
        self,
        item: IdentificationResult,
        job_dir: Path,
        config: PipelineConfig,
    ) -> ConversionArtifact:
        assert item.system_id is not None
        sys_id = item.system_id
        primary = item.primary_file
        ext = primary.suffix.lower()
        if ext in DOLPHIN_CONVERTIBLE_EXTS:
            out_rvz = _convert_rvz(
                primary,
                job_dir,
                config,
                companion_files=item.companion_files,
                system_id=sys_id,
            )
            return ConversionArtifact(
                system_id=sys_id,
                output_files=(out_rvz,),
                converted=True,
                tool_used="DolphinTool",
                summary=f"Converted `{primary.name}` -> `{out_rvz.name}` (`rvz zstd-5`)",
            )
        return ConversionArtifact(
            system_id=sys_id,
            output_files=(primary,),
            converted=False,
            summary=f"{sys_id.upper()} passthrough ({ext})",
        )


class SwitchStrategy(ConversionStrategy):
    """Strategy for Nintendo Switch (`switch`: Multi-file `.xci` merge OR `.nsz/.xcz` decompression)."""

    def matches(self, item: IdentificationResult, config: PipelineConfig) -> bool:
        return item.system_id == "switch"

    def plan(
        self,
        item: IdentificationResult,
        job_dir: Path,
        config: PipelineConfig,
        from_archive: bool = False,
        archive_ext: str = "",
        psx_multidisc_peers: int = 1,
    ) -> ConversionPlan:
        primary = item.primary_file
        ext = primary.suffix.lower()
        clean_stem = sanitize_rom_stem(primary.stem, item.companion_files)
        dest_dir = config.system_dest_dir("switch")
        prefix = _archive_prefix(item, config, from_archive)

        if item.is_multifile_switch:
            fmt = config.conversion.switch_merge_format
            out = job_dir / f"{clean_stem} [Merged].{fmt}"
            total = 1 + len(item.companion_files)
            desc = f"{prefix}Merge {total} files via squirrel (.{fmt}) -> {dest_dir}"
        elif item.is_compressed_switch and config.conversion.decompress_nsz:
            out_ext = ".nsp" if ext == ".nsz" else ".xci"
            out = job_dir / f"{clean_stem}{out_ext}"
            desc = f"{prefix}Decompress {ext} -> {out_ext} via squirrel -> {dest_dir}"
        else:
            out = primary
            desc = f"{prefix}Route {ext} as-is -> {dest_dir}"

        return ConversionPlan(
            artifact=ConversionArtifact(system_id="switch", output_files=(out,), converted=(out != primary)),
            action_description=desc,
        )

    def execute(
        self,
        item: IdentificationResult,
        job_dir: Path,
        config: PipelineConfig,
    ) -> ConversionArtifact:
        primary = item.primary_file
        if item.is_multifile_switch:
            merged = _merge_switch_bundle(item, job_dir, config)
            return ConversionArtifact(
                system_id="switch",
                output_files=merged,
                converted=True,
                tool_used="squirrel",
                summary=f"Merged {1 + len(item.companion_files)} Switch files -> `{merged[0].name}`",
            )
        if item.is_compressed_switch and config.conversion.decompress_nsz:
            decomp = _decompress_switch_package(primary, job_dir, config)
            return ConversionArtifact(
                system_id="switch",
                output_files=(decomp,),
                converted=True,
                tool_used="squirrel",
                summary=f"Decompressed `{primary.name}` -> `{decomp.name}`",
            )
        return ConversionArtifact(
            system_id="switch",
            output_files=(primary,),
            converted=False,
            summary=f"Switch passthrough ({primary.name})",
        )


class Ctr3dsStrategy(ConversionStrategy):
    """Strategy for Nintendo 3DS (`3ds`: Decrypt if needed & normalize `.3ds` -> `.cci`; `.cia` -> `_cia_install`)."""

    def matches(self, item: IdentificationResult, config: PipelineConfig) -> bool:
        return item.system_id == "3ds"

    def plan(
        self,
        item: IdentificationResult,
        job_dir: Path,
        config: PipelineConfig,
        from_archive: bool = False,
        archive_ext: str = "",
        psx_multidisc_peers: int = 1,
    ) -> ConversionPlan:
        primary = item.primary_file
        ext = primary.suffix.lower()
        clean_stem = sanitize_rom_stem(primary.stem, item.companion_files)
        dest_dir = config.system_dest_dir("3ds")
        prefix = _archive_prefix(item, config, from_archive)

        if item.is_encrypted_3ds:
            out = job_dir / f"{clean_stem}.cci"
            can_dec = (
                item.is_zerokey_3ds
                or can_decrypt_3ds(primary, config)
                or config.binaries.ctr_keys is not None
            )
            if can_dec:
                enc_prefix = "Extract -> " if from_archive else ""
                desc = f"{enc_prefix}Decrypt .3ds -> .cci -> {dest_dir}"
                return ConversionPlan(
                    artifact=ConversionArtifact(system_id="3ds", output_files=(out,), converted=True),
                    action_description=desc,
                )
            desc = (
                f"Quarantine (Encrypted 3DS ROM — add aes_keys.txt/boot9.bin to tools/3ds/) -> "
                f"{config.paths.quarantine_dir}"
            )
            return ConversionPlan(
                artifact=ConversionArtifact(system_id="3ds", output_files=(out,), converted=True),
                action_description=desc,
                requires_quarantine=True,
            )

        if item.is_installable_cia:
            if config.conversion.cia_games_to_cci and item.cia_category in ("game", "demo"):
                can_conv, conv_note = can_convert_cia_game_to_cci(primary, config)
                if can_conv:
                    out = job_dir / f"{clean_stem}.cci"
                    return ConversionPlan(
                        artifact=ConversionArtifact(
                            system_id="3ds",
                            output_files=(out,),
                            converted=True,
                            is_installable_cia=False,
                        ),
                        action_description=f"{prefix}Convert Base Game .cia -> decrypted .cci -> {dest_dir}",
                    )
                target = (dest_dir / config.conversion.cia_subdir) if config.conversion.cia_subdir else dest_dir
                return ConversionPlan(
                    artifact=ConversionArtifact(
                        system_id="3ds",
                        output_files=(primary,),
                        converted=False,
                        is_installable_cia=True,
                    ),
                    action_description=f"{prefix}Route .cia -> {target} ({conv_note})",
                )
            target = (dest_dir / config.conversion.cia_subdir) if config.conversion.cia_subdir else dest_dir
            return ConversionPlan(
                artifact=ConversionArtifact(
                    system_id="3ds",
                    output_files=(primary,),
                    converted=False,
                    is_installable_cia=True,
                ),
                action_description=f"{prefix}Route .cia -> {target}",
            )

        if ext == ".3ds" and config.conversion.rename_3ds_to_cci:
            out = job_dir / f"{clean_stem}.cci"
            return ConversionPlan(
                artifact=ConversionArtifact(system_id="3ds", output_files=(out,), converted=True),
                action_description=f"{prefix}Rename .3ds -> .cci -> {dest_dir}",
            )

        return ConversionPlan(
            artifact=ConversionArtifact(system_id="3ds", output_files=(primary,), converted=False),
            action_description=f"{prefix}Route {ext} -> {dest_dir}",
        )

    def execute(
        self,
        item: IdentificationResult,
        job_dir: Path,
        config: PipelineConfig,
    ) -> ConversionArtifact:
        primary = item.primary_file
        ext = primary.suffix.lower()
        if item.is_installable_cia:
            if config.conversion.cia_games_to_cci and item.cia_category in ("game", "demo"):
                can_conv, conv_note = can_convert_cia_game_to_cci(primary, config)
                if can_conv:
                    clean_stem = sanitize_rom_stem(primary.stem)
                    try:
                        out_cci = convert_cia_to_cci(primary, job_dir, config, clean_stem=clean_stem)
                    except CtrMissingSeedError as exc:
                        # Gracefully fall back to `_cia_install/` if a 9.6+ SeedCrypto game lacks `seeddb.bin`
                        return ConversionArtifact(
                            system_id="3ds",
                            output_files=(primary,),
                            converted=False,
                            is_installable_cia=True,
                            summary=f"Routed `.cia` to `{config.conversion.cia_subdir}` ({exc})",
                        )
                    except CtrDecryptionError as exc:
                        raise ConversionError(str(exc)) from exc
                    _verify_nonzero_output(out_cci, "ctr_cia_to_cci")
                    return ConversionArtifact(
                        system_id="3ds",
                        output_files=(out_cci,),
                        converted=True,
                        tool_used="ctr_cia_to_cci",
                        is_installable_cia=False,
                        summary=f"Converted Base Game `.cia` `{primary.name}` -> `{out_cci.name}` for Azahar/Cocoon",
                    )
                return ConversionArtifact(
                    system_id="3ds",
                    output_files=(primary,),
                    converted=False,
                    is_installable_cia=True,
                    summary=f"Installable 3DS `.cia` ({primary.name} — {conv_note})",
                )
            return ConversionArtifact(
                system_id="3ds",
                output_files=(primary,),
                converted=False,
                is_installable_cia=True,
                summary=f"Installable 3DS `.cia` ({primary.name})",
            )
        if item.is_encrypted_3ds:
            clean_stem = sanitize_rom_stem(primary.stem)
            try:
                out_cci = decrypt_3ds_to_cci(primary, job_dir, config, clean_stem=clean_stem)
            except CtrDecryptionError as exc:
                raise ConversionError(str(exc)) from exc
            if ext != ".3ds" and not config.conversion.rename_3ds_to_cci and ext != ".cci":
                alt = _unique_staging_output(job_dir, clean_stem, ext)
                out_cci.rename(alt)
                out_cci = alt
            _verify_nonzero_output(out_cci, "ctr_decrypt")
            return ConversionArtifact(
                system_id="3ds",
                output_files=(out_cci,),
                converted=True,
                tool_used="ctr_decrypt",
                summary=f"Decrypted `{primary.name}` -> `{out_cci.name}` for Azahar",
            )
        if ext == ".3ds" and config.conversion.rename_3ds_to_cci:
            job_dir.mkdir(parents=True, exist_ok=True)
            out_cci = _unique_staging_output(job_dir, primary.stem, ".cci")
            if primary.resolve().is_relative_to(job_dir.resolve()):
                primary.rename(out_cci)
            else:
                _link_or_copy_file(primary, out_cci)
            _verify_nonzero_output(out_cci, "3ds-to-cci")
            return ConversionArtifact(
                system_id="3ds",
                output_files=(out_cci,),
                converted=True,
                summary=f"Normalized `{primary.name}` -> `{out_cci.name}` for Azahar",
            )
        return ConversionArtifact(
            system_id="3ds",
            output_files=(primary,),
            converted=False,
            summary=f"3DS passthrough ({primary.name})",
        )


class WiiUStrategy(ConversionStrategy):
    """Strategy for Nintendo Wii U (`wiiu`: `.wua`/`.wux` passthrough, `cdecrypt` NUS, or `zarchive` `.wua` / Loadiine)."""

    def matches(self, item: IdentificationResult, config: PipelineConfig) -> bool:
        return item.system_id == "wiiu"

    def plan(
        self,
        item: IdentificationResult,
        job_dir: Path,
        config: PipelineConfig,
        from_archive: bool = False,
        archive_ext: str = "",
        psx_multidisc_peers: int = 1,
    ) -> ConversionPlan:
        primary = item.primary_file
        ext = primary.suffix.lower()
        clean_stem = sanitize_rom_stem(primary.stem, item.companion_files)
        dest_dir = config.system_dest_dir("wiiu")
        prefix = _archive_prefix(item, config, from_archive)

        if item.is_encrypted_wiiu and config.binaries.cdecrypt is None:
            wiiu_name = item.wiiu_clean_title or clean_stem
            return ConversionPlan(
                artifact=ConversionArtifact(
                    system_id="wiiu",
                    output_files=(job_dir / wiiu_name,),
                    converted=True,
                    is_directory=True,
                ),
                action_description=(
                    f"Quarantine (Encrypted Wii U WUP/NUS — add cdecrypt to tools/wiiu/) -> "
                    f"{config.paths.quarantine_dir}"
                ),
                requires_quarantine=True,
            )

        if not item.is_encrypted_wiiu and ext != ".rpx":
            return ConversionPlan(
                artifact=ConversionArtifact(system_id="wiiu", output_files=(primary,), converted=False),
                action_description=f"{prefix}Route {ext} as-is -> {dest_dir}",
            )

        wiiu_name = item.wiiu_clean_title or clean_stem
        dec_step = "cdecrypt NUS -> " if item.is_encrypted_wiiu else ""
        if config.conversion.wiiu_to_wua and config.binaries.zarchive is not None and item.wiiu_title_id:
            out = job_dir / f"{wiiu_name}.wua"
            return ConversionPlan(
                artifact=ConversionArtifact(system_id="wiiu", output_files=(out,), converted=True),
                action_description=f"{prefix}{dec_step}zarchive pack ({wiiu_name}.wua) -> {dest_dir}",
            )

        out = job_dir / wiiu_name
        return ConversionPlan(
            artifact=ConversionArtifact(
                system_id="wiiu",
                output_files=(out,),
                converted=True,
                is_directory=True,
            ),
            action_description=f"{prefix}{dec_step}Route clean Loadiine folder ({wiiu_name}/) -> {dest_dir}",
        )

    def execute(
        self,
        item: IdentificationResult,
        job_dir: Path,
        config: PipelineConfig,
    ) -> ConversionArtifact:
        return _convert_wiiu_item(item, job_dir, config)


class PsVitaStrategy(ConversionStrategy):
    """Strategy for PlayStation Vita (`psvita`: `.vpk` passthrough, `.pkg` -> `pkg2zip`, or `sce_sys/param.sfo` -> Vita3K `.zip`)."""

    def matches(self, item: IdentificationResult, config: PipelineConfig) -> bool:
        return item.system_id == "psvita"

    def plan(
        self,
        item: IdentificationResult,
        job_dir: Path,
        config: PipelineConfig,
        from_archive: bool = False,
        archive_ext: str = "",
        psx_multidisc_peers: int = 1,
    ) -> ConversionPlan:
        primary = item.primary_file
        ext = primary.suffix.lower()
        base_dest_dir = config.system_dest_dir("psvita")
        is_addon = item.vita_category in ("gp", "ac")
        dest_dir = (
            base_dest_dir / config.conversion.vita_addons_subdir
            if is_addon and config.conversion.vita_addons_subdir
            else base_dest_dir
        )
        prefix = _archive_prefix(item, config, from_archive)

        if ext == ".dpt":
            return ConversionPlan(
                artifact=ConversionArtifact(
                    system_id="psvita",
                    output_files=(primary,),
                    converted=False,
                    is_vita_addon=False,
                    summary=f"Cocoon FE `.dpt` Vita tag ready ({primary.name})",
                ),
                action_description=f"{prefix}Route .dpt tag as-is -> {base_dest_dir}",
            )

        fallback_stem = sanitize_rom_stem(
            primary.parent.parent.name if primary.name.lower() == "param.sfo" else primary.stem,
            item.companion_files,
        )
        dpt_planned = _plan_vita_dpt_shortcut(item, fallback_stem, job_dir)
        dpt_note = f" + {dpt_planned[0].name}" if dpt_planned else ""

        if ext == ".vpk":
            return ConversionPlan(
                artifact=ConversionArtifact(
                    system_id="psvita",
                    output_files=(primary, *dpt_planned),
                    converted=bool(dpt_planned),
                    is_vita_addon=is_addon,
                    summary=f"PS Vita `.vpk` ready ({primary.name})",
                ),
                action_description=f"{prefix}Route .vpk as-is{dpt_note} -> {dest_dir}",
            )

        vita_stem = _format_vita_output_stem(item, fallback_stem)

        if ext == ".pkg":
            has_lic = bool(item.companion_files or item.vita_zrif)
            lic_note = " + zRIF/license" if has_lic else ""
            if config.binaries.pkg2zip is not None:
                out = job_dir / f"{vita_stem}.zip"
                return ConversionPlan(
                    artifact=ConversionArtifact(
                        system_id="psvita",
                        output_files=(out, *dpt_planned),
                        converted=True,
                        tool_used="pkg2zip",
                        is_vita_addon=is_addon,
                    ),
                    action_description=f"{prefix}pkg2zip unpack .pkg{lic_note} ({out.name}{dpt_note}) -> {dest_dir}",
                )
            out_pkg = job_dir / f"{vita_stem}.pkg"
            extra_planned: list[Path] = list(item.companion_files)
            if item.vita_zrif and item.vita_category != "gp":
                if not any(c.name.lower() == "work.bin" for c in extra_planned):
                    extra_planned.append(job_dir / f"{vita_stem}_work.bin")
                if not any(
                    c.suffix.lower() == ".zrif"
                    or c.name.lower() == "zrif.txt"
                    or c.name.lower().endswith(".zrif.txt")
                    for c in extra_planned
                ):
                    extra_planned.append(job_dir / f"{vita_stem}.zrif.txt")
            return ConversionPlan(
                artifact=ConversionArtifact(
                    system_id="psvita",
                    output_files=(out_pkg, *dpt_planned, *extra_planned),
                    converted=(out_pkg.name != primary.name or bool(dpt_planned) or bool(extra_planned)),
                    is_vita_addon=is_addon,
                ),
                action_description=f"{prefix}Route .pkg{lic_note} ({out_pkg.name}{dpt_note}) -> {dest_dir}",
            )

        # Unpacked NoNpDrm / MaiDump / Vitamin tree (`sce_sys/param.sfo`)
        if config.conversion.vita_repack_zip:
            out = job_dir / f"{vita_stem}.zip"
            return ConversionPlan(
                artifact=ConversionArtifact(
                    system_id="psvita",
                    output_files=(out, *dpt_planned),
                    converted=True,
                    is_vita_addon=is_addon,
                ),
                action_description=f"{prefix}Repack Vita3K .zip ({out.name}{dpt_note}) -> {dest_dir}",
            )

        out = job_dir / vita_stem
        return ConversionPlan(
            artifact=ConversionArtifact(
                system_id="psvita",
                output_files=(out, *dpt_planned),
                converted=True,
                is_vita_addon=is_addon,
                is_directory=True,
            ),
            action_description=f"{prefix}Route clean Vita folder ({vita_stem}/{dpt_note}) -> {dest_dir}",
        )

    def execute(
        self,
        item: IdentificationResult,
        job_dir: Path,
        config: PipelineConfig,
    ) -> ConversionArtifact:
        return _convert_psvita_item(item, job_dir, config)


class CartridgeDefaultStrategy(ConversionStrategy):
    """Fallback strategy for cartridge and other systems (`gba`, `nds`, `n64`, `snes`, `nes`, etc.)."""

    def matches(self, item: IdentificationResult, config: PipelineConfig) -> bool:
        return bool(item.system_id and item.system_id in config.systems)

    def plan(
        self,
        item: IdentificationResult,
        job_dir: Path,
        config: PipelineConfig,
        from_archive: bool = False,
        archive_ext: str = "",
        psx_multidisc_peers: int = 1,
    ) -> ConversionPlan:
        assert item.system_id is not None
        sys_id = item.system_id
        sys_cfg = config.systems[sys_id]
        primary = item.primary_file
        ext = primary.suffix.lower()
        dest_dir = config.system_dest_dir(sys_id)
        prefix = _archive_prefix(item, config, from_archive)

        if from_archive and sys_cfg.archive_policy == ArchivePolicy.KEEP_ZIP and archive_ext == ".zip":
            desc = f"Keep .zip intact -> {dest_dir}"
        else:
            desc = f"{prefix}Route {ext} -> {dest_dir}"

        return ConversionPlan(
            artifact=ConversionArtifact(
                system_id=sys_id,
                output_files=(primary,),
                converted=False,
                summary=f"Cartridge ROM ready ({primary.name})",
            ),
            action_description=desc,
        )

    def execute(
        self,
        item: IdentificationResult,
        job_dir: Path,
        config: PipelineConfig,
    ) -> ConversionArtifact:
        return self.plan(item, job_dir, config).artifact


CONVERSION_STRATEGIES: tuple[ConversionStrategy, ...] = (
    ArcadePassthroughStrategy(),
    CdChdStrategy(),
    Ps2ChdStrategy(),
    PspChdStrategy(),
    DolphinRvzStrategy(),
    SwitchStrategy(),
    Ctr3dsStrategy(),
    WiiUStrategy(),
    PsVitaStrategy(),
    CartridgeDefaultStrategy(),
)


def _select_strategy(item: IdentificationResult, config: PipelineConfig) -> ConversionStrategy:
    for strategy in CONVERSION_STRATEGIES:
        if strategy.matches(item, config):
            return strategy
    raise ConversionError(f"Cannot convert unidentified/unconfigured system: {item.system_id!r}")


def plan_conversion(
    item: IdentificationResult,
    job_dir: Path,
    config: PipelineConfig,
    from_archive: bool = False,
    archive_ext: str = "",
    psx_multidisc_peers: int = 1,
) -> ConversionPlan:
    """Return the unified `ConversionPlan` (`artifact` + `action_description`) for `item`."""
    if not item.system_id or item.system_id not in config.systems:
        return ConversionPlan(
            artifact=ConversionArtifact(
                system_id=item.system_id or "unknown",
                output_files=(item.primary_file,),
                converted=False,
            ),
            action_description=f"Quarantine -> {config.paths.quarantine_dir}",
            requires_quarantine=True,
        )
    strategy = _select_strategy(item, config)
    return strategy.plan(
        item,
        job_dir,
        config,
        from_archive=from_archive,
        archive_ext=archive_ext,
        psx_multidisc_peers=psx_multidisc_peers,
    )


def build_dry_run_artifact(
    item: IdentificationResult,
    job_dir: Path,
    config: PipelineConfig,
) -> ConversionArtifact:
    """Predict output filenames for `--dry-run` via the unified `ConversionStrategy` registry."""
    return plan_conversion(item, job_dir, config).artifact


def describe_planned_action(
    res: IdentificationResult,
    config: PipelineConfig,
    from_archive: bool,
    archive_ext: str = "",
    psx_multidisc_peers: int = 1,
) -> str:
    """Summarize what `converter` + `router` will do with `res` via the `ConversionStrategy` registry."""
    return plan_conversion(
        res,
        config.paths.staging_dir,
        config,
        from_archive=from_archive,
        archive_ext=archive_ext,
        psx_multidisc_peers=psx_multidisc_peers,
    ).action_description


def convert_identified_item(
    item: IdentificationResult,
    job_dir: Path,
    config: PipelineConfig,
) -> ConversionArtifact:
    """
    Convert or normalize a single `IdentificationResult` inside `job_dir` (`.staging/<uuid>/`).

    Source files outside `job_dir` are never modified in-place.
    """
    strategy = _select_strategy(item, config)
    return strategy.execute(item, job_dir, config)
