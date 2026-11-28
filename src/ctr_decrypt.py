"""Pure-Python streaming Nintendo 3DS decryptor & CIA-to-CCI converter (`.3ds`/`.cia` -> decrypted `.cci`)."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import re
from typing import BinaryIO

from src.constants import (
    DEFAULT_CTR_BOOT9_RELPATHS,
    DEFAULT_CTR_KEY_RELPATHS,
    DEFAULT_CTR_SEEDDB_RELPATHS,
)
from src.models import PipelineConfig

_MASK_128 = (1 << 128) - 1
_DEFAULT_GENERATOR = 0x1FF9E9AAC5FE0408024591DC5D52768A
_CHUNK_SIZE = 4 * 1024 * 1024  # 4 MiB (multiple of 16 bytes for AES-CTR)
_MEDIA_UNIT = 0x200
_NCSD_HEADER_SIZE = 0x4000     # 16 KiB standard CCI header (Partition 0 starts at 0x4000)

_SEC_CRYPTO_KEYSLOTS: dict[int, int] = {
    0x00: 0x2C,  # Original (< 7.x)
    0x01: 0x25,  # 7.x
    0x0A: 0x18,  # New3DS 9.3 (Secure3)
    0x0B: 0x1B,  # New3DS 9.6 (Secure4)
}

_SLOT_KEYX_RE = re.compile(r"^slot(?:0x)?([0-9a-f]{2})keyx$", re.IGNORECASE)
_SLOT_KEY_RE = re.compile(r"^slot(?:0x)?([0-9a-f]{2})key$", re.IGNORECASE)

# Signature type -> total signature block length (4B type + sig + padding to 0x40 boundary)
_CIA_SIG_BLOCK_SIZES: dict[int, int] = {
    0x010000: 0x240,  # RSA-4096 SHA1
    0x010001: 0x140,  # RSA-2048 SHA1
    0x010002: 0x080,  # ECDSA SHA1
    0x010003: 0x240,  # RSA-4096 SHA256
    0x010004: 0x140,  # RSA-2048 SHA256
    0x010005: 0x080,  # ECDSA SHA256
}

_CIA_TITLE_CATEGORIES: dict[int, str] = {
    0x00040000: "game",
    0x00040002: "demo",
    0x0004000E: "update",
    0x0004008C: "dlc",
    0x00048004: "dsiware",
    0x00048005: "dsiware",
    0x0004800F: "dsiware",
    0x00040010: "system",
    0x00040030: "system",
    0x00040130: "system",
    0x00040138: "system",
}


class CtrDecryptionError(RuntimeError):
    """Raised when 3DS keys are missing/invalid or NCCH/CIA decryption fails verification."""


class CtrMissingSeedError(CtrDecryptionError):
    """Raised when a 9.6+ SeedCrypto 3DS title requires `seeddb.bin` (or a seed not present in it)."""


@dataclass(frozen=True, slots=True)
class CiaChunkMeta:
    """Metadata for a single content chunk record inside a `.cia` TMD."""

    content_id: int
    cindex: int
    encrypted: bool
    size: int
    data_offset: int


@dataclass(frozen=True, slots=True)
class CiaHeaderInfo:
    """Parsed metadata from a Nintendo 3DS `.cia` header + TMD (read-only, no keys required)."""

    title_id: str          # 16-char lowercase hex (e.g. "0004000000165f00")
    category: str          # "game", "demo", "update", "dlc", "dsiware", "system", "other"
    content_count: int
    cia_encrypted: bool
    is_convertible_game: bool
    detail: str
    chunks: tuple[CiaChunkMeta, ...] = ()


def _align(val: int, boundary: int) -> int:
    return (val + boundary - 1) & ~(boundary - 1)


def _import_aes():
    """Import AES from pycryptodome (`Crypto`) or pycryptodomex (`Cryptodome`)."""
    try:
        from Crypto.Cipher import AES  # type: ignore[import-not-found]
        return AES
    except ImportError:
        try:
            from Cryptodome.Cipher import AES  # type: ignore[import-not-found]
            return AES
        except ImportError as exc:
            raise CtrDecryptionError(
                "3DS decryption requires `pycryptodome` (`pip install -r requirements.txt`)."
            ) from exc


def _rol128(val: int, bits: int) -> int:
    bits %= 128
    return ((val << bits) | (val >> (128 - bits))) & _MASK_128


def scramble_3ds_key(
    key_x: bytes,
    key_y: bytes,
    generator: int = _DEFAULT_GENERATOR,
) -> bytes:
    """Derive a 128-bit 3DS AES NormalKey from `KeyX`, `KeyY`, and the hardware generator."""
    kx = int.from_bytes(key_x, "big")
    ky = int.from_bytes(key_y, "big")
    normal = _rol128(((_rol128(kx, 2) ^ ky) + generator) & _MASK_128, 87)
    return normal.to_bytes(16, "big")


@dataclass(slots=True)
class CtrKeyStore:
    """Loaded 3DS AES keyslots (`aes_keys.txt` or `boot9.bin`)."""

    source_path: Path
    generator: int = _DEFAULT_GENERATOR
    key_x: dict[int, bytes] = field(default_factory=dict)
    normal_key: dict[int, bytes] = field(default_factory=dict)

    def derive_key(self, slot: int, key_y: bytes) -> bytes:
        if slot in self.normal_key:
            return self.normal_key[slot]
        if slot in self.key_x:
            return scramble_3ds_key(self.key_x[slot], key_y, self.generator)
        raise CtrDecryptionError(
            f"Missing 3DS AES keyslot `0x{slot:02X}` (`slot0x{slot:02X}KeyX`) in `{self.source_path.name}`."
        )


def load_seeddb_map(seeddb_path: Path) -> dict[int, bytes]:
    """
    Parse a Nintendo 3DS `seeddb.bin` into `{program_id_int: 16_byte_seed}`.

    Binary format:
      - `0x00..0x03`: entry count (`uint32 LE`)
      - `0x04..0x0F`: padding
      - `0x10+`:      32-byte entries (`title_id: uint64 LE`, `seed: 16 bytes`, `reserved: 8 bytes`)
    """
    if not seeddb_path.is_file():
        return {}
    try:
        raw = seeddb_path.read_bytes()
    except OSError:
        return {}
    if len(raw) < 0x10:
        return {}
    count = int.from_bytes(raw[0:4], "little")
    seeds: dict[int, bytes] = {}
    max_entries = min(count, (len(raw) - 0x10) // 32)
    for idx in range(max_entries):
        off = 0x10 + idx * 32
        tid = int.from_bytes(raw[off : off + 8], "little")
        seed = raw[off + 8 : off + 24]
        if tid != 0:
            seeds[tid] = seed
    return seeds


def _load_keys_from_boot9(boot9_path: Path) -> CtrKeyStore:
    """Load retail 3DS KeyX slots from an ARM9 `boot9.bin` dump via `pyctr`."""
    try:
        from pyctr.crypto import CryptoEngine  # type: ignore[import-not-found]
    except ImportError as exc:
        raise CtrDecryptionError(
            f"Reading `{boot9_path.name}` requires `pyctr` (`pip install pyctr`), "
            "or provide a text `aes_keys.txt` file instead."
        ) from exc

    try:
        engine = CryptoEngine(boot9=str(boot9_path))
    except Exception as exc:
        raise CtrDecryptionError(f"Failed loading 3DS keys from `{boot9_path.name}`: {exc}") from exc

    store = CtrKeyStore(source_path=boot9_path)
    raw_kx = getattr(engine, "key_x", {}) or {}
    for slot_key, val in raw_kx.items():
        slot_int = int(slot_key)
        if isinstance(val, int):
            store.key_x[slot_int] = val.to_bytes(16, "big")
        elif isinstance(val, (bytes, bytearray)) and len(val) == 16:
            store.key_x[slot_int] = bytes(val)

    if 0x2C not in store.key_x:
        raise CtrDecryptionError(
            f"`{boot9_path.name}` did not yield retail NCCH keyslot 0x2C (invalid or devkit boot9?)."
        )
    return store


def _load_keys_from_text(keys_path: Path) -> CtrKeyStore:
    """Parse Citra/Azahar `aes_keys.txt` (`slot0x2CKeyX=...`, `generator=...`)."""
    store = CtrKeyStore(source_path=keys_path)
    try:
        lines = keys_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise CtrDecryptionError(f"Unable to read `{keys_path}`: {exc}") from exc

    for raw_line in lines:
        line = raw_line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        k, v = (part.strip() for part in line.split("=", 1))
        hex_clean = re.sub(r"\s+", "", v)
        if len(hex_clean) != 32:
            continue
        try:
            key_bytes = bytes.fromhex(hex_clean)
        except ValueError:
            continue

        k_low = k.lower()
        if k_low == "generator":
            store.generator = int.from_bytes(key_bytes, "big")
            continue

        m_kx = _SLOT_KEYX_RE.match(k_low)
        if m_kx:
            store.key_x[int(m_kx.group(1), 16)] = key_bytes
            continue

        m_k = _SLOT_KEY_RE.match(k_low)
        if m_k:
            store.normal_key[int(m_k.group(1), 16)] = key_bytes

    if not store.key_x and not store.normal_key:
        raise CtrDecryptionError(
            f"No valid 3DS AES keyslots found in `{keys_path}` (expected `slot0x2CKeyX=...`)."
        )
    return store


def load_ctr_keys(key_path: Path) -> CtrKeyStore:
    """Load 3DS AES keys from either `aes_keys.txt` or `boot9.bin`."""
    if not key_path.is_file():
        raise CtrDecryptionError(f"3DS key file does not exist: {key_path}")
    if key_path.suffix.lower() == ".bin" or key_path.stat().st_size in (32768, 65536):
        return _load_keys_from_boot9(key_path)
    return _load_keys_from_text(key_path)


def inspect_cia_header(file_path: Path) -> CiaHeaderInfo | None:
    """
    Parse a Nintendo 3DS `.cia` header and TMD without requiring `boot9.bin` or `pyctr`.

    Returns `CiaHeaderInfo` distinguishing convertible Base Games (`00040000...`) / Demos
    (`00040002...`) from install-only Updates (`0004000e...`), DLC (`0004008c...`), and DSiWare.
    """
    try:
        with file_path.open("rb") as fh:
            hdr = fh.read(0x20)
            if len(hdr) < 0x20:
                return None
            header_size = int.from_bytes(hdr[0x00:0x04], "little")
            cert_size = int.from_bytes(hdr[0x08:0x0C], "little")
            tik_size = int.from_bytes(hdr[0x0C:0x10], "little")
            tmd_size = int.from_bytes(hdr[0x10:0x14], "little")
            content_size = int.from_bytes(hdr[0x18:0x20], "little")

            if header_size < 0x20 or header_size > 0x10000 or tmd_size < 0x1E0:
                return None

            tmd_offset = _align(header_size, 0x40) + _align(cert_size, 0x40) + _align(tik_size, 0x40)
            content_offset = tmd_offset + _align(tmd_size, 0x40)

            fh.seek(tmd_offset)
            tmd_bytes = fh.read(tmd_size)
            if len(tmd_bytes) < 0x1E0:
                return None

            sig_type = int.from_bytes(tmd_bytes[0:4], "big")
            sig_block_len = _CIA_SIG_BLOCK_SIZES.get(sig_type)
            if sig_block_len is None or len(tmd_bytes) < sig_block_len + 0xC4:
                return None

            body = tmd_bytes[sig_block_len:]
            tid_int = int.from_bytes(body[0x4C:0x54], "big")
            tid_hex = f"{tid_int:016x}"
            tid_high = tid_int >> 32
            content_count = int.from_bytes(body[0x9E:0xA0], "big")

            category = _CIA_TITLE_CATEGORIES.get(tid_high, "other")
            chunks: list[CiaChunkMeta] = []
            chunks_base = 0x9C4
            cur_off = content_offset
            for i in range(content_count):
                rec_off = chunks_base + i * 0x30
                if rec_off + 0x30 > len(body):
                    break
                cid = int.from_bytes(body[rec_off : rec_off + 4], "big")
                cindex = int.from_bytes(body[rec_off + 4 : rec_off + 6], "big")
                ctype = int.from_bytes(body[rec_off + 6 : rec_off + 8], "big")
                csize = int.from_bytes(body[rec_off + 8 : rec_off + 16], "big")
                chunks.append(
                    CiaChunkMeta(
                        content_id=cid,
                        cindex=cindex,
                        encrypted=bool(ctype & 0x0001),
                        size=csize,
                        data_offset=cur_off,
                    )
                )
                cur_off += csize

            if not chunks and content_size == 0:
                return None

            cia_encrypted = any(c.encrypted for c in chunks)
            has_primary_part = any(c.cindex == 0 and c.size >= 0x200 for c in chunks)
            is_convertible = category in ("game", "demo") and has_primary_part

            cat_labels = {
                "game": "Base Game",
                "demo": "Demo",
                "update": "Update",
                "dlc": "DLC",
                "dsiware": "DSiWare",
                "system": "System Title",
                "other": "Installable Title",
            }
            enc_label = "CIA-encrypted" if cia_encrypted else "CIA-decrypted"
            detail = f"3DS {cat_labels.get(category, 'Title')} `.cia` [{tid_hex}] ({len(chunks)} parts, {enc_label})"
            return CiaHeaderInfo(
                title_id=tid_hex,
                category=category,
                content_count=len(chunks) or content_count,
                cia_encrypted=cia_encrypted,
                is_convertible_game=is_convertible,
                detail=detail,
                chunks=tuple(chunks),
            )
    except OSError:
        return None


def inspect_3ds_header(file_path: Path) -> tuple[bool, bool, str]:
    """
    Inspect a Nintendo 3DS NCSD/NCCH binary header.

    Returns `(is_encrypted, is_zerokey, detail_reason)`.
    """
    try:
        with file_path.open("rb") as fh:
            head = fh.read(0x200)
            if len(head) < 0x190:
                return False, False, "3DS extension match (header shorter than NCSD)"

            # Case 1: NCSD cartridge container (`NCSD` at 0x100)
            if head[0x100:0x104] == b"NCSD":
                part0_mu = int.from_bytes(head[0x120:0x124], "little")
                fh.seek(part0_mu * _MEDIA_UNIT)
                ncch0 = fh.read(0x200)
                if len(ncch0) < 0x190 or ncch0[0x100:0x104] != b"NCCH":
                    return (
                        False,
                        False,
                        "Verified NCSD header (Partition 0 NCCH unreadable or non-standard)",
                    )

                partitions: list[int] = [part0_mu * _MEDIA_UNIT]
                for idx in range(1, 8):
                    off_mu = int.from_bytes(head[0x120 + idx * 8 : 0x124 + idx * 8], "little")
                    sz_mu = int.from_bytes(head[0x124 + idx * 8 : 0x128 + idx * 8], "little")
                    if sz_mu > 0 and (off_mu * _MEDIA_UNIT) not in partitions:
                        partitions.append(off_mu * _MEDIA_UNIT)

                found_encrypted = False
                all_zerokey = True
                for off in partitions:
                    fh.seek(off)
                    hdr = fh.read(0x200)
                    if len(hdr) < 0x190 or hdr[0x100:0x104] != b"NCCH":
                        continue
                    flags7 = hdr[0x18F]
                    if flags7 & 0x04:  # NoCrypto
                        continue
                    found_encrypted = True
                    pid = int.from_bytes(hdr[0x108:0x110], "little")
                    if not ((flags7 & 0x01) and (pid & (0x10 << 32)) == 0):
                        all_zerokey = False

                if not found_encrypted:
                    return False, False, "Verified NCSD+NCCH header (`NoCrypto` bit set — Decrypted)"
                if all_zerokey:
                    return True, True, "Verified NCSD+NCCH header (`FixedCryptoKey` ZeroKey — Encrypted)"
                return True, False, "Verified NCSD+NCCH header (`NoCrypto` bit NOT set — Encrypted)"

            # Case 2: Standalone NCCH container (`NCCH` at 0x100)
            if head[0x100:0x104] == b"NCCH":
                flags7 = head[0x18F]
                no_crypto = bool(flags7 & 0x04)
                if no_crypto:
                    return False, False, "Verified standalone NCCH header (Decrypted)"
                pid = int.from_bytes(head[0x108:0x110], "little")
                is_zk = bool((flags7 & 0x01) and (pid & (0x10 << 32)) == 0)
                return True, is_zk, "Verified standalone NCCH header (Encrypted)"
    except OSError:
        pass
    return False, False, "Matched 3DS extension"


def _uses_zerokey_only(file_path: Path) -> bool:
    """Return True if all encrypted NCCH partitions in `file_path` use ZeroKey (`FixedCryptoKey`)."""
    is_enc, is_zk, _ = inspect_3ds_header(file_path)
    return is_enc and is_zk


def _resolve_candidate_path(raw: str, project_root: Path) -> Path:
    p = Path(raw).expanduser()
    if p.is_absolute():
        return p.resolve()
    return (project_root / p).resolve()


def _resolve_ctr_keys_path(config: PipelineConfig) -> Path | None:
    """Resolve `ctr_keys`, checking `tools/3ds/` dynamically if added after watcher startup."""
    keys = config.binaries.ctr_keys
    if keys is not None and keys.is_file():
        return keys
    project_root = config.config_path.parent
    for rel in DEFAULT_CTR_KEY_RELPATHS:
        cand = _resolve_candidate_path(rel, project_root)
        if cand.is_file():
            return cand
    return None


def _resolve_boot9_path(config: PipelineConfig) -> Path | None:
    """Resolve ARM9 `boot9.bin` specifically (required for decrypting CIA Ticket title keys)."""
    keys = config.binaries.ctr_keys
    if keys is not None and keys.is_file():
        if keys.suffix.lower() == ".bin" or keys.stat().st_size in (32768, 65536):
            return keys
    project_root = config.config_path.parent
    for rel in DEFAULT_CTR_BOOT9_RELPATHS:
        cand = _resolve_candidate_path(rel, project_root)
        if cand.is_file() and cand.stat().st_size in (32768, 65536):
            return cand
    return None


def _resolve_seeddb_path(config: PipelineConfig) -> Path | None:
    """Resolve optional `seeddb.bin` (required for 9.6+ SeedCrypto eShop games)."""
    seeddb = config.binaries.ctr_seeddb
    if seeddb is not None and seeddb.is_file():
        return seeddb
    project_root = config.config_path.parent
    for rel in DEFAULT_CTR_SEEDDB_RELPATHS:
        cand = _resolve_candidate_path(rel, project_root)
        if cand.is_file():
            return cand
    return None


def can_decrypt_3ds(file_path: Path, config: PipelineConfig) -> bool:
    """Return True if `file_path` can be decrypted (either `ctr_keys` exists or ZeroKey-only)."""
    if _resolve_ctr_keys_path(config) is not None:
        return True
    return _uses_zerokey_only(file_path)


def can_convert_cia_game_to_cci(file_path: Path, config: PipelineConfig) -> tuple[bool, str]:
    """
    Check whether `file_path` is a Base Game / Demo `.cia` that can be converted to `.cci`
    using the keys & Python modules available in the active environment.
    """
    info = inspect_cia_header(file_path)
    if info is None:
        return False, "Unreadable or non-standard `.cia` header"
    if not info.is_convertible_game:
        return False, f"`.cia` category `{info.category}` stays in `{config.conversion.cia_subdir}`"

    if info.cia_encrypted:
        if _resolve_boot9_path(config) is None:
            return False, "Encrypted `.cia` requires `boot9.bin` in `tools/3ds/`"
        from src.runner import has_module

        if not has_module("pyctr"):
            return False, "Encrypted `.cia` requires `pyctr` (`pip install -r requirements.txt`)"
        if not (has_module("Crypto") or has_module("Cryptodome")):
            return False, "Encrypted `.cia` requires `pycryptodome` (`pip install -r requirements.txt`)"
    return True, f"Ready to convert {info.category} `.cia` [{info.title_id}] -> decrypted `.cci`"


def _copy_span(fin: BinaryIO, fout: BinaryIO, length: int) -> None:
    remaining = length
    while remaining > 0:
        buf = fin.read(min(_CHUNK_SIZE, remaining))
        if not buf:
            raise CtrDecryptionError("Unexpected EOF while copying 3DS container bytes.")
        fout.write(buf)
        remaining -= len(buf)


def _decrypt_span(fin: BinaryIO, fout: BinaryIO, length: int, key: bytes, iv_int: int, aes_mod) -> None:
    cipher = aes_mod.new(key, aes_mod.MODE_CTR, nonce=b"", initial_value=iv_int.to_bytes(16, "big"))
    remaining = length
    while remaining > 0:
        buf = fin.read(min(_CHUNK_SIZE, remaining))
        if not buf:
            raise CtrDecryptionError("Unexpected EOF while decrypting 3DS NCCH section.")
        fout.write(cipher.decrypt(buf))
        remaining -= len(buf)


def _ncch_section_iv(hdr: bytes, ncch_ver: int, section_type: int, rel_offset: int) -> int:
    """Compute initial 128-bit big-endian CTR counter for an NCCH section."""
    if ncch_ver == 1:
        base_iv = hdr[0x108:0x110] + b"\x00\x00\x00\x00" + rel_offset.to_bytes(4, "big")
        return int.from_bytes(base_iv, "big")
    # NCCH v0 / v2: reversed 8-byte PartitionID + type byte (1=ExHeader, 2=ExeFS, 3=RomFS) + 7 zeros
    base_iv = hdr[0x108:0x110][::-1] + bytes([section_type]) + (b"\x00" * 7)
    return int.from_bytes(base_iv, "big")


def _parse_exefs_code_span(exefs_hdr: bytes) -> tuple[int, int] | None:
    """Locate `.code` offset/size (relative to ExeFS start) inside decrypted 0x200 ExeFS header."""
    for idx in range(10):
        entry = exefs_hdr[idx * 16 : (idx + 1) * 16]
        name = entry[0:8].split(b"\x00", 1)[0]
        if name == b".code":
            rel_off = int.from_bytes(entry[8:12], "little")
            size = int.from_bytes(entry[12:16], "little")
            if size == 0:
                return None
            # ExeFS files start after the 0x200-byte header and are padded to 0x200 units
            start = 0x200 + rel_off
            padded_len = ((size + 0x1FF) // 0x200) * 0x200
            return start, start + padded_len
    return None


def _resolve_partition_keys(
    hdr: bytes,
    keys: CtrKeyStore | None,
    seeds: dict[int, bytes] | None = None,
) -> tuple[bytes, bytes]:
    """Return `(primary_key, secondary_key)` for an encrypted NCCH partition (including 9.6+ SeedCrypto)."""
    flags3 = hdr[0x18B]
    flags7 = hdr[0x18F]
    pid = int.from_bytes(hdr[0x108:0x110], "little")
    program_id_bytes = hdr[0x118:0x120]
    program_id = int.from_bytes(program_id_bytes, "little")

    if flags7 & 0x01:  # FixedCryptoKey
        if (pid & (0x10 << 32)) == 0:
            zero = b"\x00" * 16
            return zero, zero
        raise CtrDecryptionError("System FixedCryptoKey (non-zero) is not supported.")

    if keys is None:
        raise CtrDecryptionError(
            "Encrypted 3DS ROM requires `aes_keys.txt` or `boot9.bin` in `tools/3ds/`."
        )

    key_y = hdr[0x000:0x010]
    primary_key = keys.derive_key(0x2C, key_y)

    uses_seed = bool(flags7 & 0x20)
    if flags3 == 0x00 and not uses_seed:
        return primary_key, primary_key

    sec_slot = _SEC_CRYPTO_KEYSLOTS.get(flags3)
    if sec_slot is None:
        raise CtrDecryptionError(f"Unsupported 3DS NCCH secondary crypto flag: 0x{flags3:02X}.")

    sec_key_y = key_y
    if uses_seed:
        seed = (seeds or {}).get(program_id)
        if seed is None:
            raise CtrMissingSeedError(
                f"3DS SeedCrypto title `{program_id:016x}` requires `seeddb.bin` in `tools/3ds/`."
            )
        # NCCH SeedCrypto verify hash lives at offset 0x114..0x118 (SHA-256(seed + program_id_le)[:4])
        expected_hash = hdr[0x114:0x118]
        if expected_hash != b"\x00\x00\x00\x00":
            actual_hash = hashlib.sha256(seed + program_id_bytes).digest()[:4]
            if actual_hash != expected_hash:
                raise CtrDecryptionError(
                    f"3DS seed verification mismatch for title `{program_id:016x}` in `seeddb.bin`."
                )
        sec_key_y = hashlib.sha256(key_y + seed).digest()[:16]

    secondary_key = keys.derive_key(sec_slot, sec_key_y)
    return primary_key, secondary_key


def _decrypt_ncch_stream(
    fin: BinaryIO,
    fout: BinaryIO,
    part_len: int,
    *,
    out_part_offset: int,
    keys: CtrKeyStore | None,
    seeds: dict[int, bytes] | None,
    aes_mod,
    romfs_verify_offsets: list[int],
) -> bytes:
    """
    Stream-decrypt a single NCCH partition of `part_len` bytes from `fin` into `fout`,
    patching `NoCrypto` (`0x04`) and returning the written 0x200-byte NCCH header.
    """
    hdr = bytearray(fin.read(0x200))
    if len(hdr) < 0x200 or hdr[0x100:0x104] != b"NCCH":
        fout.write(hdr)
        rem = part_len - len(hdr)
        if rem > 0:
            _copy_span(fin, fout, rem)
        return bytes(hdr)

    flags7 = hdr[0x18F]
    if flags7 & 0x04:  # Partition already decrypted
        fout.write(hdr)
        if part_len > 0x200:
            _copy_span(fin, fout, part_len - 0x200)
        return bytes(hdr)

    primary_key, secondary_key = _resolve_partition_keys(bytes(hdr), keys, seeds)
    ncch_ver = int.from_bytes(hdr[0x112:0x114], "little")
    exhdr_size = int.from_bytes(hdr[0x180:0x184], "little")
    exefs_off = int.from_bytes(hdr[0x1A0:0x1A4], "little") * _MEDIA_UNIT
    exefs_len = int.from_bytes(hdr[0x1A4:0x1A8], "little") * _MEDIA_UNIT
    romfs_off = int.from_bytes(hdr[0x1B0:0x1B4], "little") * _MEDIA_UNIT
    romfs_len = int.from_bytes(hdr[0x1B4:0x1B8], "little") * _MEDIA_UNIT

    # Patch NCCH header: clear secondary crypto byte & Fixed/Seed flags, set NoCrypto (0x04)
    orig_hdr = bytes(hdr)
    hdr[0x18B] = 0x00
    hdr[0x18F] = (flags7 & ~0x21) | 0x04
    fout.write(hdr)
    part_cursor = 0x200

    # 1. Extended Header (always 0x800 bytes on disk when exhdr_size > 0)
    if exhdr_size > 0 and part_cursor + 0x800 <= part_len:
        iv_exh = _ncch_section_iv(orig_hdr, ncch_ver, 1, part_cursor)
        _decrypt_span(fin, fout, 0x800, primary_key, iv_exh, aes_mod)
        part_cursor += 0x800

    # 2. ExeFS
    if exefs_len > 0 and exefs_off >= part_cursor and exefs_off + exefs_len <= part_len:
        if exefs_off > part_cursor:
            gap = exefs_off - part_cursor
            _copy_span(fin, fout, gap)
            part_cursor += gap

        exefs_iv = _ncch_section_iv(orig_hdr, ncch_ver, 2, exefs_off)
        if primary_key == secondary_key:
            _decrypt_span(fin, fout, exefs_len, primary_key, exefs_iv, aes_mod)
            part_cursor += exefs_len
        else:
            # 7.x+ / SeedCrypto split ExeFS: header & icons use primary_key; `.code` uses secondary_key
            raw_exefs_hdr = fin.read(0x200)
            if len(raw_exefs_hdr) < 0x200:
                raise CtrDecryptionError("Unexpected EOF reading 3DS ExeFS header.")
            hdr_cipher = aes_mod.new(
                primary_key,
                aes_mod.MODE_CTR,
                nonce=b"",
                initial_value=exefs_iv.to_bytes(16, "big"),
            )
            dec_exefs_hdr = hdr_cipher.decrypt(raw_exefs_hdr)
            fout.write(dec_exefs_hdr)
            part_cursor += 0x200

            exefs_pos = 0x200
            code_span = _parse_exefs_code_span(dec_exefs_hdr)
            if code_span is not None and code_span[1] <= exefs_len:
                c_start, c_end = code_span
                if c_start > exefs_pos:
                    _decrypt_span(
                        fin, fout, c_start - exefs_pos,
                        primary_key, exefs_iv + (exefs_pos // 16), aes_mod,
                    )
                    exefs_pos = c_start
                _decrypt_span(
                    fin, fout, c_end - c_start,
                    secondary_key, exefs_iv + (c_start // 16), aes_mod,
                )
                exefs_pos = c_end
            if exefs_pos < exefs_len:
                _decrypt_span(
                    fin, fout, exefs_len - exefs_pos,
                    primary_key, exefs_iv + (exefs_pos // 16), aes_mod,
                )
            part_cursor += exefs_len - 0x200

    # 3. RomFS
    if romfs_len > 0 and romfs_off >= part_cursor and romfs_off < part_len:
        if romfs_off > part_cursor:
            gap = romfs_off - part_cursor
            _copy_span(fin, fout, gap)
            part_cursor += gap

        actual_romfs_len = min(romfs_len, part_len - romfs_off)
        romfs_verify_offsets.append(out_part_offset + romfs_off)
        romfs_iv = _ncch_section_iv(orig_hdr, ncch_ver, 3, romfs_off)
        _decrypt_span(fin, fout, actual_romfs_len, secondary_key, romfs_iv, aes_mod)
        part_cursor += actual_romfs_len

    # Trailing partition padding
    if part_cursor < part_len:
        _copy_span(fin, fout, part_len - part_cursor)

    return bytes(hdr)


def _verify_romfs_magic(cci_path: Path, romfs_verify_offsets: list[int]) -> None:
    if not romfs_verify_offsets:
        return
    with cci_path.open("rb") as vfh:
        for romfs_abs_off in romfs_verify_offsets:
            vfh.seek(romfs_abs_off)
            if vfh.read(4) != b"IVFC":
                raise CtrDecryptionError(
                    "3DS decryption verification failed (RomFS `IVFC` magic mismatch — check your 3DS keys/seeddb.bin)."
                )


def decrypt_3ds_to_cci(
    input_path: Path,
    job_dir: Path,
    config: PipelineConfig,
    clean_stem: str | None = None,
) -> Path:
    """
    Decrypt an encrypted `.3ds` (NCSD/NCCH) ROM into a staging `.cci` ready for Azahar.

    Streams in 4 MiB chunks, patches `NoCrypto` (`0x04`) in each NCCH header, supports
    7.x/9.3/9.6/SeedCrypto, and verifies RomFS `IVFC` magic bytes after decryption.
    """
    aes_mod = _import_aes()
    key_path = _resolve_ctr_keys_path(config)
    keys = load_ctr_keys(key_path) if key_path is not None else None
    seeddb_path = _resolve_seeddb_path(config)
    seeds = load_seeddb_map(seeddb_path) if seeddb_path is not None else {}

    file_size = input_path.stat().st_size
    with input_path.open("rb") as fh:
        head = fh.read(0x200)
        if len(head) < 0x200:
            raise CtrDecryptionError(f"File too small to be a 3DS image: {input_path.name}")

        raw_parts: list[tuple[int, int]] = []
        if head[0x100:0x104] == b"NCSD":
            for idx in range(8):
                off_mu = int.from_bytes(head[0x120 + idx * 8 : 0x124 + idx * 8], "little")
                sz_mu = int.from_bytes(head[0x124 + idx * 8 : 0x128 + idx * 8], "little")
                if sz_mu > 0:
                    off_b = off_mu * _MEDIA_UNIT
                    sz_b = sz_mu * _MEDIA_UNIT
                    if off_b + 0x200 <= file_size:
                        raw_parts.append((off_b, min(sz_b, file_size - off_b)))
        elif head[0x100:0x104] == b"NCCH":
            raw_parts.append((0, file_size))
        else:
            raise CtrDecryptionError(f"Unsupported 3DS container (expected NCSD or NCCH): {input_path.name}")

    raw_parts.sort(key=lambda item: item[0])

    job_dir.mkdir(parents=True, exist_ok=True)
    stem = clean_stem or input_path.stem
    out_cci = job_dir / f"{stem}.cci"
    if out_cci.resolve() == input_path.resolve():
        out_cci = job_dir / f"{stem}_decrypted.cci"

    romfs_verify_offsets: list[int] = []
    cursor = 0

    try:
        with input_path.open("rb") as fin, out_cci.open("wb") as fout:
            for part_off, part_len in raw_parts:
                if part_off > cursor:
                    _copy_span(fin, fout, part_off - cursor)
                    cursor = part_off
                elif part_off < cursor:
                    continue

                _decrypt_ncch_stream(
                    fin,
                    fout,
                    part_len,
                    out_part_offset=part_off,
                    keys=keys,
                    seeds=seeds,
                    aes_mod=aes_mod,
                    romfs_verify_offsets=romfs_verify_offsets,
                )
                cursor += part_len

            # Trailing NCSD padding after last partition
            if cursor < file_size:
                _copy_span(fin, fout, file_size - cursor)

        _verify_romfs_magic(out_cci, romfs_verify_offsets)
        return out_cci
    except Exception:
        out_cci.unlink(missing_ok=True)
        raise


def _build_ncsd_header(
    *,
    title_id_int: int,
    total_size_bytes: int,
    part_entries: dict[int, tuple[int, int, int]],  # cindex -> (offset_bytes, size_bytes, program_id_int)
    part0_ncch_hdr: bytes,
) -> bytes:
    """Construct a spec-compliant 16 KiB (`0x4000`) decrypted NCSD cartridge header for `.cci`."""
    hdr = bytearray(_NCSD_HEADER_SIZE)

    # 0x100..0x103: Magic "NCSD"
    hdr[0x100:0x104] = b"NCSD"
    # 0x104..0x107: Total CCI image size in 0x200-byte media units
    total_mu = total_size_bytes // _MEDIA_UNIT
    hdr[0x104:0x108] = total_mu.to_bytes(4, "little")
    # 0x108..0x10F: Media ID (Partition 0 Title ID)
    hdr[0x108:0x110] = title_id_int.to_bytes(8, "little")

    # 0x120..0x15F: Partition table (8 slots x (offset_mu, size_mu))
    # 0x190..0x1CF: Partition Title IDs (8 x uint64 LE)
    for cindex, (off_b, sz_b, prog_id) in sorted(part_entries.items()):
        if not (0 <= cindex < 8):
            continue
        off_mu = off_b // _MEDIA_UNIT
        sz_mu = sz_b // _MEDIA_UNIT
        tbl_off = 0x120 + cindex * 8
        hdr[tbl_off : tbl_off + 4] = off_mu.to_bytes(4, "little")
        hdr[tbl_off + 4 : tbl_off + 8] = sz_mu.to_bytes(4, "little")

        tid_off = 0x190 + cindex * 8
        hdr[tid_off : tid_off + 8] = prog_id.to_bytes(8, "little")

    # 0x188..0x18F: Partition flags (flags[4]=1 inner device, flags[5]=1 card1, flags[6]=0 -> 0x200 media unit)
    hdr[0x18C] = 0x01
    hdr[0x18D] = 0x01
    hdr[0x18E] = 0x00  # Media unit size = 0x200 * 2^0 = 512 bytes

    # 0x200..0x3FF: Card Info Header (card1 writable address + used size)
    hdr[0x200:0x204] = (0xFFFFFFFF).to_bytes(4, "little")
    hdr[0x300:0x304] = min(total_size_bytes, 0xFFFFFFFF).to_bytes(4, "little")

    # 0x1000..0x11FF: Copy of Partition 0 NCCH header (standard CCI Card Info layout)
    if len(part0_ncch_hdr) >= 0x200:
        hdr[0x1000:0x1200] = part0_ncch_hdr[:0x200]

    return bytes(hdr)


def convert_cia_to_cci(
    input_path: Path,
    job_dir: Path,
    config: PipelineConfig,
    clean_stem: str | None = None,
) -> Path:
    """
    Convert a Nintendo 3DS Base Game / Demo `.cia` into a standalone, fully decrypted `.cci`
    (NCSD container) ready for Azahar and frontend scanners like Cocoon FE.

    Supports both CIA-encrypted (requires `boot9.bin` + `pyctr`) and CIA-decrypted packages,
    as well as 7.x/9.3/9.6 NCCH crypto and 9.6+ SeedCrypto (`tools/3ds/seeddb.bin`).
    """
    info = inspect_cia_header(input_path)
    if info is None or not info.is_convertible_game:
        cat = info.category if info is not None else "unknown"
        raise CtrDecryptionError(
            f"`{input_path.name}` is not a convertible 3DS Base Game `.cia` (category: {cat})."
        )

    ncch_chunks = [c for c in info.chunks if 0 <= c.cindex < 8 and c.size >= 0x200]
    ncch_chunks.sort(key=lambda c: c.cindex)
    if not ncch_chunks or ncch_chunks[0].cindex != 0:
        raise CtrDecryptionError(f"`{input_path.name}` is missing primary NCCH Partition 0.")

    aes_mod = _import_aes()
    boot9_path = _resolve_boot9_path(config)
    seeddb_path = _resolve_seeddb_path(config)
    seeds = load_seeddb_map(seeddb_path) if seeddb_path is not None else {}

    # Prefer boot9 for NCCH keys too when present, otherwise fall back to aes_keys.txt
    key_path = boot9_path or _resolve_ctr_keys_path(config)
    keys = load_ctr_keys(key_path) if key_path is not None else None

    cia_reader = None
    if info.cia_encrypted:
        if boot9_path is None:
            raise CtrDecryptionError(
                f"Encrypted 3DS `.cia` `{input_path.name}` requires `boot9.bin` in `tools/3ds/`."
            )
        try:
            from pyctr.crypto import CryptoEngine, load_seeddb  # type: ignore[import-not-found]
            from pyctr.crypto.seeddb import MissingSeedError  # type: ignore[import-not-found]
            from pyctr.type.cia import CIAReader  # type: ignore[import-not-found]
        except ImportError as exc:
            raise CtrDecryptionError(
                "Converting encrypted `.cia` games requires `pyctr` (`pip install -r requirements.txt`)."
            ) from exc

        if seeddb_path is not None:
            load_seeddb(str(seeddb_path))

        engine = CryptoEngine(boot9=str(boot9_path))
        try:
            cia_reader = CIAReader(input_path, crypto=engine)
        except MissingSeedError as exc:
            raise CtrMissingSeedError(
                f"3DS SeedCrypto eShop game `{input_path.name}` [{info.title_id}] requires `seeddb.bin` in `tools/3ds/`."
            ) from exc
        except Exception as exc:
            raise CtrDecryptionError(f"Failed opening `.cia` `{input_path.name}`: {exc}") from exc

    job_dir.mkdir(parents=True, exist_ok=True)
    stem = clean_stem or input_path.stem
    out_cci = job_dir / f"{stem}.cci"
    if out_cci.resolve() == input_path.resolve():
        out_cci = job_dir / f"{stem}_converted.cci"

    romfs_verify_offsets: list[int] = []
    part_entries: dict[int, tuple[int, int, int]] = {}
    part0_hdr = b""

    try:
        with out_cci.open("wb") as fout:
            # Reserve 16 KiB for the NCSD header (filled in after all partitions are streamed)
            fout.write(b"\x00" * _NCSD_HEADER_SIZE)
            cur_out_offset = _NCSD_HEADER_SIZE

            raw_fh: BinaryIO | None = None if cia_reader is not None else input_path.open("rb")
            try:
                for chunk in ncch_chunks:
                    if cia_reader is not None:
                        src_stream = cia_reader.open_raw_section(chunk.cindex)
                    else:
                        assert raw_fh is not None
                        raw_fh.seek(chunk.data_offset)
                        src_stream = raw_fh

                    try:
                        dec_hdr = _decrypt_ncch_stream(
                            src_stream,
                            fout,
                            chunk.size,
                            out_part_offset=cur_out_offset,
                            keys=keys,
                            seeds=seeds,
                            aes_mod=aes_mod,
                            romfs_verify_offsets=romfs_verify_offsets,
                        )
                    finally:
                        if cia_reader is not None and hasattr(src_stream, "close"):
                            src_stream.close()

                    if len(dec_hdr) < 0x200 or dec_hdr[0x100:0x104] != b"NCCH":
                        raise CtrDecryptionError(
                            f"CIA content index {chunk.cindex} in `{input_path.name}` did not yield a valid NCCH header."
                        )

                    # Pad partition to 0x200 media-unit boundary
                    aligned_size = _align(chunk.size, _MEDIA_UNIT)
                    pad_len = aligned_size - chunk.size
                    if pad_len > 0:
                        fout.write(b"\x00" * pad_len)

                    prog_id = int.from_bytes(dec_hdr[0x118:0x120], "little")
                    part_entries[chunk.cindex] = (cur_out_offset, aligned_size, prog_id)
                    if chunk.cindex == 0:
                        part0_hdr = dec_hdr
                    cur_out_offset += aligned_size
            finally:
                if raw_fh is not None:
                    raw_fh.close()
                if cia_reader is not None:
                    cia_reader.close()

            # Write finalized 16 KiB NCSD header at offset 0
            ncsd_header = _build_ncsd_header(
                title_id_int=int(info.title_id, 16),
                total_size_bytes=cur_out_offset,
                part_entries=part_entries,
                part0_ncch_hdr=part0_hdr,
            )
            fout.seek(0)
            fout.write(ncsd_header)

        # Cryptographic & structural verification
        _verify_romfs_magic(out_cci, romfs_verify_offsets)
        is_still_enc, _, _ = inspect_3ds_header(out_cci)
        if is_still_enc:
            raise CtrDecryptionError(
                f"Converted `.cci` `{out_cci.name}` still reports encrypted NCCH flags."
            )

        try:
            from pyctr.type.cci import CCIReader  # type: ignore[import-not-found]

            with out_cci.open("rb") as cci_fh:
                with CCIReader(cci_fh) as cci_check:
                    if 0 not in cci_check.contents:
                        raise CtrDecryptionError(
                            f"CCI verification failed: Partition 0 missing in `{out_cci.name}`."
                        )
        except ImportError:
            pass

        return out_cci
    except Exception:
        out_cci.unlink(missing_ok=True)
        raise
