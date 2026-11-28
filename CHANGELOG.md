# Changelog

All notable changes to Völundr are documented in this file.
Versioning follows [Semantic Versioning](https://semver.org/) (`MAJOR.MINOR.PATCH`).

## [Unreleased]

### Added
- **One-Click Windows Launcher (`Start Volundr.bat`):** Auto-bootstraps `.venv`, installs core and GUI dependencies on first run, and launches Völundr windowless via `pythonw.exe`.
- **3DS Base Game `.cia` → Decrypted `.cci` Conversion & `seeddb.bin` Support:** Automatically reads `.cia` TMD Title IDs to convert Base Games (`00040000...`) and Demos (`00040002...`) into standalone decrypted `.cci` files in `roms/3ds/` for Azahar & Cocoon FE (including 9.6+ SeedCrypto eShop games via optional `tools/3ds/seeddb.bin`), while keeping Updates (`0004000e...`) and DLC (`0004008c...`) in `3ds/_cia_install/`. Added `cia_games_to_cci` toggle in `config.toml` / Advanced Settings and `3DS seeddb` check in Doctor & Tools.

### Changed
- **GUI-First Documentation:** Streamlined `README.md`, `docs/getting-started.md`, and `docs/desktop-gui.md` to guide newcomers directly to the Desktop GUI and `Start Volundr.bat`, moving CLI commands and manual tool tables into collapsible reference sections.

### Fixed
- **Windowless `pythonw.exe` Subprocess Execution:** Fixed `'NoneType' object has no attribute 'isatty'` when running conversions under windowless `pythonw.exe` (via `Start Volundr.bat` / `volundr_gui.pyw`), redirected `None` `stdout`/`stderr` safely to `os.devnull`, and added `CREATE_NO_WINDOW` on Windows so child CLI tools no longer flash console windows.
- **Dolphin & PS Vita `pkg2zip` Auto-Installers:** Switched `setup_volundr.py` Dolphin discovery to Dolphin's official JSON update API (`dolphin-emu.org/update/latest/beta`), fixed HTTP 403 error attribution for non-GitHub hosts, and updated `lusid1/pkg2zip` asset matching for `pkg2zip.zip`.
- **Readable Monospace Code Pills in GUI Dialogs:** Restyled inline code snippets in the Setup Wizard and Check for Updates dialogs to use smooth antialiased Consolas pills instead of Qt's fallback bitmap Courier font.

## [0.3.0] - 2026-11-26

### Added
- **Optional PySide6 (Qt 6) Desktop GUI & System Tray (`main.py gui` / `volundr_gui.pyw`):** Full dark-themed native desktop UI with a live **Watch & Activity** dashboard, **Doctor & Tools** diagnostics tab, **Settings** & **Advanced Settings** dialogs, system tray icon with live watching/paused status dot, desktop notifications, and windowless Windows launcher (`volundr_gui.pyw`). Kept strictly optional via `requirements-gui.txt` so CLI & Docker builds stay lean.
- **Graphical First-Time Setup Wizard (`SetupWizardDialog`):** 4-step guided setup dialog powered by `setup_volundr.py` that auto-detects missing/partial CLI tools and Python dependencies, streams download/install progress live, configures `config.toml` without losing comments, and can be reopened anytime from **Settings → Run Setup Wizard...**.
- **One-Click GitHub Updater (`UpdateDialog`):** Startup update check pill + formatted release notes dialog with safe `git pull --ff-only` update & restart, `ETag` conditional caching, and automatic `releases.atom` fallback when GitHub's unauthenticated API rate limit is hit.
- **Dedicated Windows Taskbar Icon & Völundr Brand Shield (`docs/assets/logo.svg`):** Sets a dedicated Windows `AppUserModelID` and renders multi-resolution (`16`–`256` px) icons so the Windows taskbar displays Völundr's icon instead of Python's default icon.
- **Default Paused GUI Startup:** Desktop GUI now opens in **Paused** state by default, with a persistent Settings toggle (*Start watching automatically when Völundr launches*) and `--autowatch` / `--no-autowatch` CLI flags.

### Fixed
- **Live Doctor Refresh & Runtime Module Cache:** Added `clear_module_cache()` (`importlib.invalidate_caches()` + `has_module.cache_clear()`) and live config re-resolution inside `refresh_doctor()` so newly installed Python dependencies (`pycryptodome`, `pyctr`) or newly placed console keys/tools turn green immediately without restarting the GUI.
- **Clean Terminal Exit on GUI Close:** Explicitly exits the Qt event loop when closing without tray-minimize and handles `Ctrl+C` (`SIGINT`) cleanly via a Qt timer so launching terminals never hang.

## [0.2.0] - 2026-11-25

### Added
- **Guided Setup Script (`setup_volundr.py`):** Stdlib-only interactive installer that creates `.venv`, generates `config.toml` with your `library_root`, downloads official upstream CLI tools into `tools/` (with `--dry-run`, `--yes`, and per-tool selection), and runs `main.py doctor`. Made the default Quick Start method in `README.md`.
- **Automatic Switch `prod.keys` → `keys.txt` Sync:** Auto-discovers standard Switch `prod.keys` (in `tools/nscb/ztools/`, `tools/nscb/`, `tools/switch/`, or `~/.switch/prod.keys`) and automatically copies/refreshes it to `keys.txt` for `NSC_Builder` (`squirrel`) without modifying the original `prod.keys`.
- **Duplicate/Older Switch Update Guard:** Update-only Switch drops with a numeric version `<=` the version already merged in the library donor now skip copying and rebuilding, saving multi-GB network/disk I/O while safely cleaning up the dropped file.
- **Zensical Documentation Site:** Added `zensical.toml`, `docs/`, and `.github/workflows/docs.yml` for automated GitHub Pages deployment.

### Fixed
- **Untagged No-Intro Switch `.xci` Pairing in `watch` Mode:** Pairs untagged Switch Base archives with sibling TitleID-bearing Updates/DLCs in `drop/` by normalized game title and passes `game_title_hint` when checking the library for an untagged Base donor.
- **Mis-Tagged Switch `[BASE]` Filenames:** Recognizes explicit `[BASE]` / `(BASE)` tags even when an archive filename accidentally carries an Update TitleID (`...800`).
- **Colons in Zip Entry Names:** Tightened Zip-Slip validation (`validate_safe_entries`) to block real Windows drive prefixes (`C:...`) while allowing colons inside game titles (e.g. `The Legend of Zelda: Breath of the Wild`), and sanitized Windows-illegal characters in stdlib zip preview/fallback paths.
- **Terminal Version Tag Display:** Escaped Rich markup in `watch` logs and routing summaries so bracketed version tags like `[v262144]` render verbatim.

## [0.1.0] - 2026-11-23

### Added
- Initial release of Völundr: cross-platform automated ROM ingestion pipeline for Windows, Linux, and Docker (`doctor`, `inspect`, `process`, `scan`, `watch`).
- Zero-extraction 5-way arcade classification (`cps1`, `cps2`, `cps3`, `neogeo`, `fbneo`) and binary header / ISO9660 disc sniffing.
- Format conversion & routing for PS1 (`.chd` / multi-disc `.pbp` + `.sbi`), PS2/PSP/Sega/PCE CD (`.chd`), GameCube/Wii (`.rvz`), Nintendo Switch (merged `.xci` + `.nsz` decompression), 3DS (streaming AES-CTR decryption + `.cci` + `_cia_install`), Wii U (`.wua` / `cdecrypt` / Loadiine fallback), and PS Vita (`param.sfo`, Vita3K `.zip`, `.pkg` + `pkg2zip`, Cocoon FE `.dpt` tags).
- Syncthing- and Samba-safe atomic transfers (`~syncthing~*.tmp` → byte verification → `os.replace()`), ROMarr two-library platform bucket support, example TOML templates, friendly missing-config guidance, and MIT License.
