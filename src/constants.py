"""Centralized constants, file extension sets, and filename predicates across Völundr."""

from __future__ import annotations

# Supported archive container extensions (`archive_handler.py`)
SUPPORTED_ARCHIVE_EXTENSIONS: frozenset[str] = frozenset({".zip", ".7z", ".rar"})

# CD subchannel / descriptor sidecar extensions grouped with primary disc tracks
CD_SUBCHANNEL_EXTENSIONS: frozenset[str] = frozenset({
    ".sub", ".ccd", ".mds", ".sbi", ".lsd",
})

# Sidecar readme/artwork files occasionally bundled inside Arcade ROM zips.
# Note: `.sub` is intentionally excluded here because in MAME/FBNeo sets `.sub`
# denotes a sub-CPU ROM chip (`ARCADE_CHIP_EXPLICIT_EXTS`).
ARCADE_IGNORABLE_SIDECAR_EXTENSIONS: frozenset[str] = frozenset({
    ".txt", ".nfo", ".diz", ".md", ".htm", ".html", ".url",
    ".desktop", ".lnk", ".website", ".webloc", ".torrent",
    ".jpg", ".jpeg", ".png", ".ini",
})

# Non-ROM promo, document, checksum, and OS metadata sidecar extensions
NON_ROM_SIDECAR_EXTENSIONS: frozenset[str] = frozenset({
    ".txt", ".nfo", ".diz", ".md", ".htm", ".html", ".url",
    ".desktop", ".lnk", ".website", ".webloc", ".torrent",
    ".jpg", ".jpeg", ".png", ".bmp", ".gif", ".ico",
    ".sfv", ".md5", ".sha1", ".par2", ".ini", ".db", ".ds_store", ".json",
})

# Full sidecar set skipped when scanning extracted console directories/archives
IGNORED_SIDECAR_EXTENSIONS: frozenset[str] = (
    NON_ROM_SIDECAR_EXTENSIONS | CD_SUBCHANNEL_EXTENSIONS
)

# OS placeholder and metadata filenames ignored across watcher, switch bundler, and router
IGNORED_FILENAMES: frozenset[str] = frozenset({
    ".gitkeep",
    ".ds_store",
    "thumbs.db",
    "desktop.ini",
})

# Temporary download extensions created by browsers, torrent clients, and Syncthing
TEMP_DOWNLOAD_SUFFIXES: frozenset[str] = frozenset({
    ".crdownload",  # Chromium / Chrome / Edge / Brave
    ".part",        # Firefox / Linux downloaders
    ".tmp",         # Generic / Syncthing temp
    ".opdownload",  # Opera
    ".!qb",         # qBittorrent incomplete
    ".partial",     # Legacy Edge / IE
    ".download",    # WebKit / Safari
})

# Sibling active-download suffixes checked next to 0-byte placeholder files
SIBLING_TEMP_MARKER_SUFFIXES: tuple[str, ...] = (
    ".part", ".crdownload", ".opdownload", ".tmp", ".!qb",
)

# Syncthing temporary file prefixes
SYNCTHING_TEMP_PREFIXES: tuple[str, ...] = ("~syncthing~", ".syncthing.")

# Default relative candidate paths for Nintendo 3DS decryption keys & eShop seed database
DEFAULT_CTR_KEY_RELPATHS: tuple[str, ...] = (
    "./tools/3ds/aes_keys.txt",
    "./tools/3ds/boot9.bin",
    "./tools/3ds/keys.txt",
)

DEFAULT_CTR_BOOT9_RELPATHS: tuple[str, ...] = (
    "./tools/3ds/boot9.bin",
    "./tools/3ds/boot9_prot.bin",
    "~/AppData/Roaming/3ds/boot9.bin",
    "~/.3ds/boot9.bin",
)

DEFAULT_CTR_SEEDDB_RELPATHS: tuple[str, ...] = (
    "./tools/3ds/seeddb.bin",
    "~/AppData/Roaming/Azahar/sysdata/seeddb.bin",
    "~/AppData/Roaming/Citra/sysdata/seeddb.bin",
    "~/AppData/Roaming/3ds/seeddb.bin",
    "~/.local/share/azahar-emu/sysdata/seeddb.bin",
    "~/.local/share/citra-emu/sysdata/seeddb.bin",
    "~/.3ds/seeddb.bin",
)

# Default candidate paths for Nintendo Switch keys (accepts standard `prod.keys` or NSCB `keys.txt`)
DEFAULT_SWITCH_KEY_RELPATHS: tuple[str, ...] = (
    "./tools/nscb/ztools/keys.txt",
    "./tools/nscb/ztools/prod.keys",
    "./tools/nscb/prod.keys",
    "./tools/switch/prod.keys",
    "~/.switch/prod.keys",
)

# Platform subfolder names created by ROMarr/Gamarr (e.g. `drop/switch/Game.xci`).
# When present directly inside `watch_dir`, their children are processed individually
# instead of treating the entire platform folder as one game unit.
WATCH_PLATFORM_BUCKETS: frozenset[str] = frozenset({
    "psx", "ps2", "ps3", "psp", "psvita", "vita", "switch", "switch-2", "gc", "ngc", "wii", "wiiu",
    "3ds", "nds", "n64", "snes", "sfam", "nes", "famicom", "fds", "satellaview", "g-and-w",
    "gba", "gb", "gbc", "virtualboy", "saturn", "dreamcast", "dc", "segacd", "sg1000",
    "pcenginecd", "tg16", "turbografx-cd", "turbografx-16-slash-pc-engine-cd", "turbografx16--1",
    "arcade", "fbneo", "cps1", "cps2", "cps3", "neogeo", "neogeomvs", "neogeoaes", "neo-geo-cd",
    "genesis", "genesis-slash-megadrive", "megadrive", "gamegear", "sms", "sega32", "3do",
    "xbox", "xbox360", "atari2600", "atari5200", "atari7800", "lynx", "colecovision", "vectrex",
    "cdi", "wonderswan-color", "msx", "ngp",
})


def is_syncthing_temp(filename: str) -> bool:
    """Return True if `filename` is a Syncthing temporary file (`~syncthing~*` or `.syncthing.*`)."""
    return filename.lower().startswith(SYNCTHING_TEMP_PREFIXES)
