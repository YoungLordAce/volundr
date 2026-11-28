"""Interactive PySide6 First-Time Setup Wizard powered by `setup_volundr.py`."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

from PySide6.QtCore import QObject, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

import setup_volundr
from src.config_loader import (
    ConfigError,
    MissingConfigError,
    detect_platform_key,
    load_config,
    save_basic_settings,
)
from src.models import PostProcessAction
from src.runner import clear_module_cache, has_module


def _code(text: str) -> str:
    """Format an inline code/path pill using crisp ClearType Consolas instead of Qt's bitmap Courier."""
    return (
        "<span style=\"font-family: Consolas, 'Cascadia Mono', 'DejaVu Sans Mono', monospace; "
        "font-size: 12px; color: #7dd3fc; background-color: #222b3d;\">"
        f"&nbsp;{text}&nbsp;</span>"
    )


def detect_tool_install_status(project_root: Path, platform_key: str) -> dict[str, tuple[str, str]]:
    """
    Probe each tool group in `setup_volundr.TOOL_GROUPS` and return
    `{tool_id: (state, detail)}` where `state` is `"installed"`, `"partial"`, or `"missing"`.
    """
    status: dict[str, tuple[str, str]] = {}

    # 7z
    found_7z = setup_volundr._find_installed_7z(project_root, platform_key)
    if found_7z is not None:
        status["7z"] = ("installed", f"Installed ({found_7z.name})")
    else:
        status["7z"] = ("missing", "Missing")

    # chdman
    chd_exe = project_root / "tools" / "chdman" / ("chdman.exe" if platform_key == "windows" else "chdman")
    if chd_exe.is_file() or shutil.which("chdman"):
        status["chdman"] = ("installed", "Installed")
    else:
        status["chdman"] = ("missing", "Missing")

    # psxpackager
    psx_exe = project_root / "tools" / "psxpackager" / (
        "psxpackager.exe" if platform_key == "windows" else "psxpackager"
    )
    if psx_exe.is_file() or shutil.which("psxpackager"):
        status["psxpackager"] = ("installed", "Installed")
    else:
        status["psxpackager"] = ("missing", "Missing")

    # dolphin
    if platform_key == "windows":
        dol_found = any(
            p.is_file()
            for p in (
                project_root / "tools" / "dolphin" / "DolphinTool.exe",
                Path(r"C:\Program Files\Dolphin-x64\DolphinTool.exe"),
            )
        ) or bool(shutil.which("DolphinTool"))
    else:
        dol_found = bool(shutil.which("dolphin-tool") or shutil.which("DolphinTool"))
    status["dolphin"] = ("installed", "Installed") if dol_found else ("missing", "Missing")

    # switch
    if platform_key == "windows":
        sq_found = (project_root / "tools" / "nscb" / "ztools" / "squirrel.exe").is_file()
        status["switch"] = ("installed", "Installed") if sq_found else ("missing", "Missing")
    else:
        venv_nsz = setup_volundr._venv_python_path(project_root, platform_key).parent / "nsz"
        nsz_found = venv_nsz.is_file() or bool(shutil.which("nsz"))
        status["switch"] = ("installed", "Installed") if nsz_found else ("missing", "Missing")

    # wiiu (check both zarchive and cdecrypt)
    if platform_key == "windows":
        has_za = (project_root / "tools" / "wiiu" / "zarchive.exe").is_file() or bool(shutil.which("zarchive"))
        has_cd = (project_root / "tools" / "wiiu" / "cdecrypt.exe").is_file() or bool(shutil.which("cdecrypt"))
    else:
        has_za = bool(shutil.which("zarchive"))
        has_cd = bool(shutil.which("cdecrypt"))

    if has_za and has_cd:
        status["wiiu"] = ("installed", "Installed")
    elif has_za or has_cd:
        missing_name = "cdecrypt" if has_za else "zarchive"
        status["wiiu"] = ("partial", f"Missing {missing_name}")
    else:
        status["wiiu"] = ("missing", "Missing")

    # psvita
    if platform_key == "windows":
        has_p2z = (project_root / "tools" / "psvita" / "pkg2zip.exe").is_file() or bool(shutil.which("pkg2zip"))
    else:
        has_p2z = bool(shutil.which("pkg2zip"))
    status["psvita"] = ("installed", "Installed") if has_p2z else ("missing", "Missing")

    return status


def detect_missing_python_deps() -> list[str]:
    """Return missing Python packages from `requirements.txt`."""
    clear_module_cache()
    checks = (
        ("watchdog", "watchdog"),
        ("rich", "rich"),
        ("pycryptodome", "Crypto"),
        ("pyctr", "pyctr"),
    )
    return [pkg for pkg, mod in checks if not has_module(mod)]


class _WizardSignals(QObject):
    log_line = Signal(str)
    progress_text = Signal(str)
    setup_finished = Signal(bool, str)


class _SignalStream(io.TextIOBase):
    """Redirect `print()` output from `setup_volundr` into a Qt signal line-by-line."""

    def __init__(self, emit_fn) -> None:
        super().__init__()
        self._emit = emit_fn
        self._buf = ""

    def write(self, s: str) -> int:
        if not s:
            return 0
        # Normalize carriage-return download progress updates into clean lines
        cleaned = s.replace("\r\n", "\n").replace("\r", "\n")
        self._buf += cleaned
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            stripped = line.rstrip()
            if stripped:
                self._emit(stripped)
        return len(s)

    def flush(self) -> None:
        if self._buf.strip():
            self._emit(self._buf.rstrip())
        self._buf = ""


class SetupWizardDialog(QDialog):
    """4-step graphical guided setup wizard backed by `setup_volundr.py`."""

    _STEPS = (
        "1. Folders && Config",
        "2. Tools && Dependencies",
        "3. Install && Configure",
        "4. Console Keys && Finish",
    )

    def __init__(
        self,
        config_path: Path,
        *,
        is_first_run: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.config_path = config_path.resolve()
        self.project_root = self.config_path.parent
        self.platform_key = detect_platform_key()
        self.arch = setup_volundr.detect_arch()
        self.is_first_run = is_first_run
        self._running = False
        self._setup_ran = False
        self._setup_succeeded = False

        self._signals = _WizardSignals(self)
        self._signals.log_line.connect(self._append_log)
        self._signals.progress_text.connect(self._on_progress_text)
        self._signals.setup_finished.connect(self._on_setup_finished)

        self.setWindowTitle("Völundr — Guided Setup Wizard")
        self.resize(680, 540)
        self.setMinimumSize(600, 480)
        self.setModal(True)

        self._build_ui()
        self._load_initial_values()
        self._refresh_step_ui()

    # --------------------------------------------------------------------------
    # UI Construction
    # --------------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header banner with step pills
        header = QFrame()
        header.setObjectName("HeaderBar")
        hdr_layout = QVBoxLayout(header)
        hdr_layout.setContentsMargins(18, 14, 18, 12)
        hdr_layout.setSpacing(8)

        title = QLabel("Völundr Setup Wizard")
        title.setObjectName("AppTitle")
        hdr_layout.addWidget(title)

        step_row = QHBoxLayout()
        step_row.setSpacing(8)
        self._step_labels: list[QLabel] = []
        for idx, label in enumerate(self._STEPS):
            lbl = QLabel(label.replace("&&", "&"))
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            step_row.addWidget(lbl)
            self._step_labels.append(lbl)
            if idx < len(self._STEPS) - 1:
                sep = QLabel("›")
                sep.setStyleSheet("color: #4b5670; font-weight: 700;")
                step_row.addWidget(sep)
        step_row.addStretch(1)
        hdr_layout.addLayout(step_row)
        root.addWidget(header)

        # Stacked wizard pages
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(18, 16, 18, 14)
        body_layout.setSpacing(12)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_page_folders())
        self._stack.addWidget(self._build_page_tools())
        self._stack.addWidget(self._build_page_install())
        self._stack.addWidget(self._build_page_finish())
        body_layout.addWidget(self._stack, stretch=1)

        # Bottom navigation bar
        nav_row = QHBoxLayout()
        self._btn_cancel = QPushButton("Skip for Now" if self.is_first_run else "Cancel")
        self._btn_cancel.clicked.connect(self.reject)
        nav_row.addWidget(self._btn_cancel)
        nav_row.addStretch(1)

        self._btn_back = QPushButton("Back")
        self._btn_back.clicked.connect(self._go_back)
        nav_row.addWidget(self._btn_back)

        self._btn_next = QPushButton("Next")
        self._btn_next.setObjectName("PrimaryBtn")
        self._btn_next.clicked.connect(self._go_next)
        nav_row.addWidget(self._btn_next)
        body_layout.addLayout(nav_row)

        root.addWidget(body, stretch=1)

    def _build_page_folders(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        intro = QLabel(
            "Prepare your forge — choose where Völundr watches for incoming ROMs and where it writes "
            f"your converted, emulator-ready library. Existing comments in {_code('config.toml')} "
            "are preserved safely."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #cbd5eb; line-height: 140%;")
        layout.addWidget(intro)

        self._lbl_config_note = QLabel("")
        self._lbl_config_note.setObjectName("InfoStrip")
        self._lbl_config_note.setWordWrap(True)
        layout.addWidget(self._lbl_config_note)

        form = QFormLayout()
        form.setSpacing(10)

        watch_row = QHBoxLayout()
        self._edit_watch_dir = QLineEdit()
        btn_watch = QPushButton("Browse...")
        btn_watch.clicked.connect(self._browse_watch_dir)
        watch_row.addWidget(self._edit_watch_dir, stretch=1)
        watch_row.addWidget(btn_watch)
        form.addRow("Watch Folder (drop):", watch_row)

        lib_row = QHBoxLayout()
        self._edit_library_root = QLineEdit()
        self._edit_library_root.setPlaceholderText(
            r"e.g. D:\Emulation\roms or Z:\games\library\roms"
            if self.platform_key == "windows"
            else "e.g. /mnt/nas/games/library/roms"
        )
        btn_lib = QPushButton("Browse...")
        btn_lib.clicked.connect(self._browse_library_root)
        lib_row.addWidget(self._edit_library_root, stretch=1)
        lib_row.addWidget(btn_lib)
        form.addRow(f"ROM Library ({self.platform_key}):", lib_row)

        self._combo_post_action = QComboBox()
        self._combo_post_action.addItem("move — Move originals to processed/ (recommended)", "move")
        self._combo_post_action.addItem("delete — Delete originals after verified arrival", "delete")
        form.addRow("Post-Process Action:", self._combo_post_action)

        layout.addLayout(form)
        layout.addStretch(1)
        return page

    def _build_page_tools(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        desc = QLabel(
            f"Choose which official upstream CLI tools to fetch into {_code('./tools/')}. "
            "Missing or incomplete tools are pre-selected automatically; tools already installed "
            "are skipped unless you enable re-download."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #cbd5eb;")
        layout.addWidget(desc)

        # Quick select controls
        bar = QHBoxLayout()
        btn_missing = QPushButton("Select Missing")
        btn_missing.setObjectName("CompactBtn")
        btn_missing.clicked.connect(self._select_missing_tools)
        bar.addWidget(btn_missing)

        btn_all = QPushButton("Select All")
        btn_all.setObjectName("CompactBtn")
        btn_all.clicked.connect(lambda: self._set_all_tool_checks(True))
        bar.addWidget(btn_all)

        btn_none = QPushButton("Select None")
        btn_none.setObjectName("CompactBtn")
        btn_none.clicked.connect(lambda: self._set_all_tool_checks(False))
        bar.addWidget(btn_none)
        bar.addStretch(1)

        self._chk_force_redownload = QCheckBox("Re-download if installed")
        bar.addWidget(self._chk_force_redownload)
        layout.addLayout(bar)

        # Scrollable checklist
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        container = QWidget()
        list_layout = QVBoxLayout(container)
        list_layout.setContentsMargins(10, 10, 10, 10)
        list_layout.setSpacing(8)

        # Optional Python dependencies row (`requirements.txt`)
        missing_py = detect_missing_python_deps()
        py_row = QHBoxLayout()
        self._chk_python_deps = QCheckBox("Python Dependencies (requirements.txt)")
        self._chk_python_deps.setStyleSheet("font-weight: 600;")
        self._chk_python_deps.setChecked(bool(missing_py))
        py_badge = QLabel(
            f"Missing: {', '.join(missing_py)}" if missing_py else "All installed"
        )
        py_badge.setStyleSheet(
            "color: #fbbf24; font-size: 12px; font-weight: 600;"
            if missing_py
            else "color: #4ade80; font-size: 12px; font-weight: 600;"
        )
        py_row.addWidget(self._chk_python_deps)
        py_row.addStretch(1)
        py_row.addWidget(py_badge)
        list_layout.addLayout(py_row)

        py_sub = QLabel("Installs watchdog, rich, pycryptodome & pyctr (3DS auto-decryption).")
        py_sub.setStyleSheet("color: #8b95ad; font-size: 12px; padding-left: 22px;")
        list_layout.addWidget(py_sub)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #262e3f; background-color: #262e3f; max-height: 1px;")
        list_layout.addWidget(sep)

        # External CLI tool rows
        self._tool_status = detect_tool_install_status(self.project_root, self.platform_key)
        self._tool_checkboxes: dict[str, QCheckBox] = {}

        badge_colors = {
            "installed": "#4ade80",
            "partial": "#fbbf24",
            "missing": "#fbbf24",
        }
        for tid, label, summary in setup_volundr.TOOL_GROUPS:
            state, detail = self._tool_status.get(tid, ("missing", "Missing"))
            row = QHBoxLayout()
            chk = QCheckBox(label)
            chk.setStyleSheet("font-weight: 600;")
            chk.setChecked(state != "installed")
            self._tool_checkboxes[tid] = chk

            badge = QLabel(detail)
            badge.setStyleSheet(
                f"color: {badge_colors.get(state, '#9aa5be')}; font-size: 12px; font-weight: 600;"
            )
            row.addWidget(chk)
            row.addStretch(1)
            row.addWidget(badge)
            list_layout.addLayout(row)

            sub = QLabel(summary)
            sub.setStyleSheet("color: #8b95ad; font-size: 12px; padding-left: 22px;")
            list_layout.addWidget(sub)

        list_layout.addStretch(1)
        scroll.setWidget(container)
        layout.addWidget(scroll, stretch=1)
        return page

    def _build_page_install(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self._lbl_progress = QLabel("Ready to apply configuration and install selected tools.")
        self._lbl_progress.setStyleSheet("font-weight: 600; color: #f4f7ff;")
        layout.addWidget(self._lbl_progress)

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        layout.addWidget(self._progress_bar)

        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setPlaceholderText("Setup output will appear here...")
        layout.addWidget(self._log_view, stretch=1)
        return page

    def _build_page_finish(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        self._lbl_finish_title = QLabel("Setup Complete ✓")
        self._lbl_finish_title.setStyleSheet("font-size: 16px; font-weight: 700; color: #4ade80;")
        layout.addWidget(self._lbl_finish_title)

        self._lbl_finish_summary = QLabel("")
        self._lbl_finish_summary.setWordWrap(True)
        self._lbl_finish_summary.setStyleSheet("color: #cbd5eb;")
        layout.addWidget(self._lbl_finish_summary)

        keys_card = QLabel(
            "<div style='line-height: 155%;'>"
            "<b style='color: #f4f7ff;'>User-Supplied Console Keys Reminder</b><br><br>"
            "Völundr <b>never</b> downloads copyrighted console keys or license databases. "
            "If you process encrypted titles for these systems, copy your own legally dumped files to:<br>"
            f"• <b>Nintendo Switch:</b> {_code('tools/nscb/ztools/prod.keys')} "
            f"(or {_code('~/.switch/prod.keys')} — auto-synced to {_code('keys.txt')})<br>"
            f"• <b>Nintendo 3DS:</b> {_code('tools/3ds/aes_keys.txt')} or {_code('tools/3ds/boot9.bin')}<br>"
            f"• <b>PlayStation Vita:</b> {_code('tools/psvita/PSV_GAMES.tsv')} (optional)"
            "</div>"
        )
        keys_card.setObjectName("InfoStrip")
        keys_card.setWordWrap(True)
        layout.addWidget(keys_card)

        btn_row = QHBoxLayout()
        btn_tools = QPushButton("Open tools/ Folder")
        btn_tools.clicked.connect(self._open_tools_folder)
        btn_row.addWidget(btn_tools)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        note = QLabel(
            "Click <b>Finish</b> to reload your configuration and refresh the Doctor & Tools tab. "
            "You can reopen this wizard anytime from <b>Settings → Run Setup Wizard...</b>."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #9aa5be;")
        layout.addWidget(note)
        layout.addStretch(1)
        return page

    # --------------------------------------------------------------------------
    # Initial State & Helpers
    # --------------------------------------------------------------------------
    def _load_initial_values(self) -> None:
        if self.config_path.is_file():
            self._lbl_config_note.setText(
                f"<b>Existing configuration detected:</b> {_code(self.config_path.name)} "
                "(only the paths below will be updated; all other settings & comments stay intact)."
            )
            try:
                cfg = load_config(self.config_path, platform_override=self.platform_key)
                self._edit_watch_dir.setText(str(cfg.paths.watch_dir))
                self._edit_library_root.setText(cfg.paths.raw_library_root)
                idx = self._combo_post_action.findData(cfg.general.post_process_action.value)
                if idx >= 0:
                    self._combo_post_action.setCurrentIndex(idx)
                return
            except ConfigError:
                pass
        else:
            self._lbl_config_note.setText(
                f"<b>First-time setup:</b> {_code(self.config_path.name)} will be created "
                f"from {_code('config.example.toml')} with the folders you choose below."
            )

        self._edit_watch_dir.setText(str(self.project_root / "drop"))
        self._edit_library_root.setText("")

    def _browse_watch_dir(self) -> None:
        start = self._edit_watch_dir.text().strip() or str(self.project_root)
        chosen = QFileDialog.getExistingDirectory(self, "Select Watch Folder (drop)", start)
        if chosen:
            self._edit_watch_dir.setText(chosen)

    def _browse_library_root(self) -> None:
        current = self._edit_library_root.text().strip()
        start = current if current and Path(current).is_dir() else str(self.project_root)
        chosen = QFileDialog.getExistingDirectory(self, "Select Destination ROM Library Folder", start)
        if chosen:
            self._edit_library_root.setText(chosen)

    def _open_tools_folder(self) -> None:
        tools_dir = self.project_root / "tools"
        tools_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(tools_dir)))

    def _select_missing_tools(self) -> None:
        self._chk_python_deps.setChecked(bool(detect_missing_python_deps()))
        for tid, chk in self._tool_checkboxes.items():
            state, _ = self._tool_status.get(tid, ("missing", "Missing"))
            chk.setChecked(state != "installed")

    def _set_all_tool_checks(self, checked: bool) -> None:
        self._chk_python_deps.setChecked(checked)
        for chk in self._tool_checkboxes.values():
            chk.setChecked(checked)

    def selected_tool_ids(self) -> list[str]:
        return [tid for tid in setup_volundr.ALL_TOOL_IDS if self._tool_checkboxes[tid].isChecked()]

    # --------------------------------------------------------------------------
    # Navigation & Execution
    # --------------------------------------------------------------------------
    def _refresh_step_ui(self) -> None:
        idx = self._stack.currentIndex()
        for i, lbl in enumerate(self._step_labels):
            if i == idx:
                lbl.setStyleSheet("color: #60a5fa; font-weight: 700; font-size: 12px;")
            elif i < idx:
                lbl.setStyleSheet("color: #4ade80; font-weight: 600; font-size: 12px;")
            else:
                lbl.setStyleSheet("color: #677188; font-weight: 600; font-size: 12px;")

        self._btn_back.setVisible(idx > 0 and idx < 3)
        self._btn_back.setEnabled(not self._running and idx > 0)
        self._btn_cancel.setEnabled(not self._running)
        self._btn_cancel.setVisible(idx < 3)

        if idx == 0:
            self._btn_next.setText("Next")
            self._btn_next.setEnabled(True)
        elif idx == 1:
            self._btn_next.setText("Run Setup")
            self._btn_next.setEnabled(True)
        elif idx == 2:
            self._btn_next.setText("Next")
            self._btn_next.setEnabled(not self._running and self._setup_ran)
        else:
            self._btn_next.setText("Finish")
            self._btn_next.setEnabled(True)

    def _go_back(self) -> None:
        if self._running:
            return
        idx = self._stack.currentIndex()
        if idx > 0:
            self._stack.setCurrentIndex(idx - 1)
            self._refresh_step_ui()

    def _go_next(self) -> None:
        if self._running:
            return
        idx = self._stack.currentIndex()
        if idx == 0:
            if not self._edit_library_root.text().strip():
                reply = QMessageBox.question(
                    self,
                    "ROM Library Folder Empty",
                    "You haven't picked a destination ROM Library folder yet.\n\n"
                    "Continue anyway and configure it later in Settings?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return
            self._stack.setCurrentIndex(1)
            self._refresh_step_ui()
        elif idx == 1:
            self._stack.setCurrentIndex(2)
            self._start_setup_worker()
        elif idx == 2:
            self._stack.setCurrentIndex(3)
            self._refresh_step_ui()
        else:
            self.accept()

    def _append_log(self, line: str) -> None:
        self._log_view.appendPlainText(line)
        cursor = self._log_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self._log_view.setTextCursor(cursor)

    def _on_progress_text(self, text: str) -> None:
        self._lbl_progress.setText(text)

    def _start_setup_worker(self) -> None:
        self._running = True
        self._setup_ran = False
        self._setup_succeeded = False
        self._log_view.clear()
        self._progress_bar.setRange(0, 0)  # Indeterminate smooth progress while working
        self._refresh_step_ui()

        watch_val = self._edit_watch_dir.text().strip()
        lib_val = self._edit_library_root.text().strip()
        post_val = str(self._combo_post_action.currentData() or PostProcessAction.MOVE.value)
        install_py_deps = self._chk_python_deps.isChecked()
        chosen_tools = self.selected_tool_ids()
        force_tools = self._chk_force_redownload.isChecked()

        def _worker() -> None:
            stream = _SignalStream(lambda line: self._signals.log_line.emit(line))
            try:
                # 1. Config file creation / update
                self._signals.progress_text.emit("Step 1/3 — Configuring config.toml...")
                self._signals.log_line.emit("=== Step 1: Configuration (config.toml) ===")
                if not self.config_path.is_file():
                    example_path = self.config_path.with_name("config.example.toml")
                    if not example_path.is_file():
                        raise MissingConfigError(f"Missing template file: {example_path}")
                    self.config_path.write_text(
                        example_path.read_text(encoding="utf-8"),
                        encoding="utf-8",
                    )
                    self._signals.log_line.emit(f"  [OK] Created {self.config_path.name} from config.example.toml")

                save_basic_settings(
                    self.config_path,
                    self.platform_key,
                    watch_dir=watch_val or None,
                    library_root=lib_val or None,
                    post_process_action=post_val,
                )
                self._signals.log_line.emit(
                    f"  [OK] Saved watch_dir, library_root, and post_process_action={post_val}."
                )

                setup_volundr._ensure_key_placeholder_dirs(self.project_root, dry_run=False)

                # 2. Optional Python dependencies
                req_file = self.project_root / "requirements.txt"
                if install_py_deps and req_file.is_file():
                    self._signals.progress_text.emit("Step 2/3 — Installing Python dependencies (requirements.txt)...")
                    self._signals.log_line.emit("\n=== Step 2: Python Dependencies (requirements.txt) ===")
                    kwargs: dict = {
                        "stdout": subprocess.PIPE,
                        "stderr": subprocess.STDOUT,
                        "text": True,
                        "encoding": "utf-8",
                        "errors": "replace",
                    }
                    if os.name == "nt":
                        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    proc = subprocess.Popen(
                        [sys.executable, "-m", "pip", "install", "-r", str(req_file)],
                        **kwargs,
                    )
                    assert proc.stdout is not None
                    for raw_line in proc.stdout:
                        line = raw_line.rstrip()
                        if line:
                            self._signals.log_line.emit(f"  {line}")
                    rc = proc.wait()
                    clear_module_cache()
                    if rc == 0:
                        self._signals.log_line.emit("  [OK] Python dependencies installed.")
                    else:
                        self._signals.log_line.emit(
                            f"  [WARN] pip exited with code {rc} — check output above."
                        )
                else:
                    self._signals.log_line.emit("\n=== Step 2: Python Dependencies ===\n  [SKIP] Skipped.")

                # 3. External CLI tools via `setup_volundr.install_selected_tools`
                if chosen_tools:
                    self._signals.progress_text.emit(
                        f"Step 3/3 — Downloading {len(chosen_tools)} external tool group(s)..."
                    )
                    self._signals.log_line.emit("\n=== Step 3: External CLI Tools (./tools/) ===")
                    with redirect_stdout(stream), redirect_stderr(stream):
                        results = setup_volundr.install_selected_tools(
                            chosen_tools,
                            project_root=self.project_root,
                            platform_key=self.platform_key,
                            arch=self.arch,
                            force=force_tools,
                            dry_run=False,
                        )
                    stream.flush()
                    installed_n = sum(1 for r in results if r.status == "installed")
                    skipped_n = sum(1 for r in results if r.status == "skipped")
                    manual_n = sum(1 for r in results if r.status == "manual")
                    failed_n = sum(1 for r in results if r.status == "failed")
                    summary = (
                        f"Configuration saved. Tools: {installed_n} installed, "
                        f"{skipped_n} already present"
                        + (f", {manual_n} manual" if manual_n else "")
                        + (f", {failed_n} failed" if failed_n else "")
                        + "."
                    )
                else:
                    self._signals.log_line.emit("\n=== Step 3: External CLI Tools ===\n  [SKIP] No tools selected.")
                    summary = "Configuration saved cleanly. External tool downloads were skipped."

                self._signals.setup_finished.emit(True, summary)
            except Exception as exc:
                stream.flush()
                self._signals.log_line.emit(f"\n[ERROR] {exc}")
                self._signals.setup_finished.emit(False, f"Setup encountered an error: {exc}")

        threading.Thread(target=_worker, name="volundr-setup-wizard", daemon=True).start()

    def _on_setup_finished(self, ok: bool, summary: str) -> None:
        self._running = False
        self._setup_ran = True
        self._setup_succeeded = ok
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(100 if ok else 0)
        self._lbl_progress.setText(
            "Setup finished! Click Next to review the console keys reminder."
            if ok
            else "Setup finished with warnings/errors — review the log above."
        )
        self._lbl_finish_title.setText("Setup Complete ✓" if ok else "Setup Finished With Issues")
        self._lbl_finish_title.setStyleSheet(
            "font-size: 16px; font-weight: 700; color: #4ade80;"
            if ok
            else "font-size: 16px; font-weight: 700; color: #fbbf24;"
        )
        self._lbl_finish_summary.setText(summary)
        self._refresh_step_ui()
