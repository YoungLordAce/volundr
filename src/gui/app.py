"""PySide6 (Qt 6) desktop application & system tray controller for Völundr."""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

from PySide6.QtCore import QObject, QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QCloseEvent, QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src import __version__
from src.config_loader import (
    ConfigError,
    MissingConfigError,
    ensure_local_directories,
    load_config,
    save_basic_settings,
)
from src.gui.dialogs import AdvancedSettingsDialog, UpdateDialog
from src.gui.setup_wizard import SetupWizardDialog
from src.gui.theme import DARK_STYLESHEET, StatusIndicator, build_app_icon
from src.gui.updater import UpdateCheckResult, check_for_updates
from src.models import PipelineConfig, PostProcessAction, RoutingOutcome
from src.runner import terminate_active_subprocesses
from src.watcher import PipelineWatcher, scan_watch_dir_once

MAX_LOG_BLOCKS = 2000
MAX_RESULT_ROWS = 200


def _now_hms() -> str:
    return datetime.now().strftime("%H:%M:%S")


class _GuiSignals(QObject):
    """Thread-safe signal bridge from background watcher/worker threads into the Qt UI thread."""

    event_logged = Signal(str, str)       # (timestamp, message)
    outcome_received = Signal(object)     # RoutingOutcome
    error_received = Signal(str, str)     # (item_name, error_text)
    doctor_finished = Signal(list)        # list[(category, check, status, detail)]
    scan_finished = Signal(int, int)      # (outcomes_count, error_count)
    watcher_stopped = Signal()
    update_checked = Signal(object)       # UpdateCheckResult


class VolundrMainWindow(QMainWindow):
    """Main desktop window + system tray host for Völundr."""

    def __init__(
        self,
        config: PipelineConfig,
        *,
        auto_watch: bool | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.config = config
        self._watcher: PipelineWatcher | None = None
        self._watching = False
        self._scanning = False
        self._quitting = False
        self._tray_hint_shown = False

        self._signals = _GuiSignals(self)
        self._signals.event_logged.connect(self._on_event_logged)
        self._signals.outcome_received.connect(self._on_outcome_received)
        self._signals.error_received.connect(self._on_error_received)
        self._signals.doctor_finished.connect(self._on_doctor_finished)
        self._signals.scan_finished.connect(self._on_scan_finished)
        self._signals.watcher_stopped.connect(self._on_watcher_stopped)
        self._signals.update_checked.connect(self._on_background_update_checked)

        self._ui_settings = QSettings("Volundr", "VolundrGUI")

        self._icon_app = build_app_icon(watching=None)
        self._icon_watching = build_app_icon(watching=True)
        self._icon_paused = build_app_icon(watching=False)

        self.setWindowTitle(f"Völundr v{__version__} — Automatic ROM Forge")
        self.setWindowIcon(self._icon_app)
        self.resize(980, 680)
        self.setMinimumSize(780, 520)

        self._build_ui()
        self._setup_tray()
        self._load_ui_preferences()
        self._populate_settings_from_config()
        self._refresh_info_strip()
        self._refresh_state_ui()

        # Kick off non-blocking Doctor diagnostics right away
        self.refresh_doctor()

        # Default watcher state is Paused unless user enabled auto-watch in Settings or passed --autowatch
        should_watch = self._chk_auto_watch.isChecked() if auto_watch is None else auto_watch
        if should_watch:
            self.start_watching()

        if self._chk_auto_update_check.isChecked():
            self._check_updates_silently()

    # --------------------------------------------------------------------------
    # UI Construction
    # --------------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget(self)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # Top header bar
        header = QFrame()
        header.setObjectName("HeaderBar")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 12, 16, 12)
        header_layout.setSpacing(12)

        title_col = QVBoxLayout()
        title_col.setSpacing(1)
        title_lbl = QLabel("Völundr")
        title_lbl.setObjectName("AppTitle")
        ver_lbl = QLabel(f"v{__version__}  •  Automatic ROM Forge")
        ver_lbl.setObjectName("AppVersion")
        title_col.addWidget(title_lbl)
        title_col.addWidget(ver_lbl)
        header_layout.addLayout(title_col)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setFixedHeight(26)
        divider.setStyleSheet("color: #2f384d; background-color: #2f384d; max-width: 1px;")
        header_layout.addWidget(divider, 0, Qt.AlignmentFlag.AlignVCenter)

        self._status_indicator = StatusIndicator(self)
        header_layout.addWidget(self._status_indicator, 0, Qt.AlignmentFlag.AlignVCenter)

        self._btn_update_pill = QPushButton("Update Available")
        self._btn_update_pill.setObjectName("CompactBtn")
        self._btn_update_pill.setStyleSheet(
            "background-color: #1e3a8a; color: #93c5fd; border: 1px solid #3b82f6; "
            "border-radius: 10px; padding: 4px 10px; font-weight: 600; font-size: 12px;"
        )
        self._btn_update_pill.setVisible(False)
        self._btn_update_pill.clicked.connect(self.open_update_dialog)
        header_layout.addWidget(self._btn_update_pill)
        header_layout.addStretch(1)

        self._btn_toggle_watch = QPushButton("Auto Forge")
        self._btn_toggle_watch.setObjectName("PrimaryBtn")
        self._btn_toggle_watch.setToolTip("Continuously monitor the drop folder and forge incoming ROMs automatically")
        self._btn_toggle_watch.clicked.connect(self.toggle_watching)
        header_layout.addWidget(self._btn_toggle_watch)

        self._btn_scan = QPushButton("Forge Now")
        self._btn_scan.setToolTip("Run a one-shot pass over everything currently in the drop folder")
        self._btn_scan.clicked.connect(self.trigger_scan_once)
        header_layout.addWidget(self._btn_scan)

        btn_drop = QPushButton("Open Drop")
        btn_drop.clicked.connect(self.open_drop_folder)
        header_layout.addWidget(btn_drop)

        btn_lib = QPushButton("Open Library")
        btn_lib.clicked.connect(self.open_library_folder)
        header_layout.addWidget(btn_lib)

        root_layout.addWidget(header)

        # Tabs container
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(14, 12, 14, 14)
        body_layout.setSpacing(10)

        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_watch_tab(), "Forge && Activity")
        self._tabs.addTab(self._build_doctor_tab(), "Doctor && Tools")
        self._tabs.addTab(self._build_settings_tab(), "Settings")
        body_layout.addWidget(self._tabs)

        root_layout.addWidget(body, stretch=1)
        self.setCentralWidget(central)

    def _build_watch_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        self._info_strip = QLabel("")
        self._info_strip.setObjectName("InfoStrip")
        self._info_strip.setWordWrap(True)
        layout.addWidget(self._info_strip)

        splitter = QSplitter(Qt.Orientation.Vertical)

        # Upper pane: Recent processed items table
        results_box = QWidget()
        res_layout = QVBoxLayout(results_box)
        res_layout.setContentsMargins(0, 0, 0, 0)
        res_layout.setSpacing(4)
        res_hdr = QLabel("Recent Activity")
        res_hdr.setStyleSheet("font-weight: 600; color: #cbd5eb;")
        res_layout.addWidget(res_hdr)

        self._results_table = QTableWidget(0, 4)
        self._results_table.setHorizontalHeaderLabels(["Time", "Source", "Status", "Destination / Details"])
        self._results_table.setAlternatingRowColors(True)
        self._results_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._results_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._results_table.verticalHeader().setVisible(False)
        hdr = self._results_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._results_table.setColumnWidth(1, 260)
        res_layout.addWidget(self._results_table)
        splitter.addWidget(results_box)

        # Lower pane: Live event log
        log_box = QWidget()
        log_layout = QVBoxLayout(log_box)
        log_layout.setContentsMargins(0, 6, 0, 0)
        log_layout.setSpacing(4)

        log_top = QHBoxLayout()
        log_hdr = QLabel("Live Event Log")
        log_hdr.setStyleSheet("font-weight: 600; color: #cbd5eb;")
        log_top.addWidget(log_hdr)
        log_top.addStretch(1)
        btn_clear_log = QPushButton("Clear Log")
        btn_clear_log.setObjectName("CompactBtn")
        btn_clear_log.clicked.connect(self._clear_log)
        log_top.addWidget(btn_clear_log)
        log_layout.addLayout(log_top)

        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(MAX_LOG_BLOCKS)
        self._log_view.setPlaceholderText(
            "The anvil is quiet — drop ROMs into ./drop and click Auto Forge or Forge Now to begin."
        )
        log_layout.addWidget(self._log_view)
        splitter.addWidget(log_box)

        splitter.setSizes([240, 220])
        layout.addWidget(splitter, stretch=1)
        return tab

    def _build_doctor_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        top_bar = QHBoxLayout()
        self._doctor_summary = QLabel("Running diagnostics...")
        self._doctor_summary.setStyleSheet("color: #b7c2d9;")
        top_bar.addWidget(self._doctor_summary)
        top_bar.addStretch(1)

        btn_open_tools = QPushButton("Open tools/ Folder")
        btn_open_tools.clicked.connect(self.open_tools_folder)
        top_bar.addWidget(btn_open_tools)

        self._btn_refresh_doctor = QPushButton("Refresh Diagnostics")
        self._btn_refresh_doctor.setObjectName("PrimaryBtn")
        self._btn_refresh_doctor.clicked.connect(self.refresh_doctor)
        top_bar.addWidget(self._btn_refresh_doctor)
        layout.addLayout(top_bar)

        self._doctor_table = QTableWidget(0, 4)
        self._doctor_table.setHorizontalHeaderLabels(["Category", "Check", "Status", "Details"])
        self._doctor_table.setAlternatingRowColors(True)
        self._doctor_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._doctor_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._doctor_table.verticalHeader().setVisible(False)
        dh = self._doctor_table.horizontalHeader()
        dh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        dh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        dh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        dh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._doctor_table, stretch=1)
        return tab

    def _build_settings_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        # Core pipeline settings (persisted to config.toml)
        cfg_group = QGroupBox(f"Pipeline Configuration ({self.config.config_path.name})")
        form = QFormLayout(cfg_group)
        form.setSpacing(10)

        watch_row = QHBoxLayout()
        self._edit_watch_dir = QLineEdit()
        btn_browse_watch = QPushButton("Browse...")
        btn_browse_watch.clicked.connect(self._browse_watch_dir)
        watch_row.addWidget(self._edit_watch_dir, stretch=1)
        watch_row.addWidget(btn_browse_watch)
        form.addRow("Watch Folder (watch_dir):", watch_row)

        lib_row = QHBoxLayout()
        self._edit_library_root = QLineEdit()
        btn_browse_lib = QPushButton("Browse...")
        btn_browse_lib.clicked.connect(self._browse_library_root)
        lib_row.addWidget(self._edit_library_root, stretch=1)
        lib_row.addWidget(btn_browse_lib)
        form.addRow(f"ROM Library ({self.config.platform_key} library_root):", lib_row)

        self._combo_post_action = QComboBox()
        self._combo_post_action.addItem("move — Move originals to processed/ (recommended)", "move")
        self._combo_post_action.addItem("delete — Delete originals after verified arrival", "delete")
        form.addRow("Post-Process Action:", self._combo_post_action)

        layout.addWidget(cfg_group)

        # Desktop / Tray behavior (persisted via QSettings)
        ui_group = QGroupBox("Desktop && System Tray")
        ui_layout = QVBoxLayout(ui_group)
        self._chk_auto_watch = QCheckBox("Start Auto Forge automatically when Völundr launches")
        self._chk_min_to_tray = QCheckBox("Hide to system tray when closing the window")
        self._chk_notifications = QCheckBox("Show desktop notifications when ROMs finish or are quarantined")
        self._chk_auto_update_check = QCheckBox("Check GitHub for Völundr updates on startup")
        self._chk_auto_watch.toggled.connect(self._save_ui_preferences)
        self._chk_min_to_tray.toggled.connect(self._save_ui_preferences)
        self._chk_notifications.toggled.connect(self._save_ui_preferences)
        self._chk_auto_update_check.toggled.connect(self._save_ui_preferences)
        ui_layout.addWidget(self._chk_auto_watch)
        ui_layout.addWidget(self._chk_min_to_tray)
        ui_layout.addWidget(self._chk_notifications)
        ui_layout.addWidget(self._chk_auto_update_check)
        layout.addWidget(ui_group)

        # Maintenance & Setup shortcuts
        maint_group = QGroupBox("Setup && Maintenance")
        maint_row = QHBoxLayout(maint_group)
        maint_row.setSpacing(8)

        btn_wizard = QPushButton("Run Setup Wizard...")
        btn_wizard.clicked.connect(lambda: self.open_setup_wizard(is_first_run=False))
        maint_row.addWidget(btn_wizard)

        btn_advanced = QPushButton("Advanced Settings...")
        btn_advanced.clicked.connect(self.open_advanced_settings)
        maint_row.addWidget(btn_advanced)

        btn_updates = QPushButton("Check for Updates")
        btn_updates.clicked.connect(self.open_update_dialog)
        maint_row.addWidget(btn_updates)

        btn_open_cfg = QPushButton("Open config.toml")
        btn_open_cfg.clicked.connect(self._open_config_file)
        maint_row.addWidget(btn_open_cfg)
        maint_row.addStretch(1)
        layout.addWidget(maint_group)

        # Save bar
        save_row = QHBoxLayout()
        self._lbl_settings_status = QLabel("")
        self._lbl_settings_status.setStyleSheet("color: #8b95ad;")
        save_row.addWidget(self._lbl_settings_status)
        save_row.addStretch(1)

        btn_save = QPushButton("Save && Apply")
        btn_save.setObjectName("PrimaryBtn")
        btn_save.clicked.connect(self._save_settings)
        save_row.addWidget(btn_save)
        layout.addLayout(save_row)

        layout.addStretch(1)
        return tab

    # --------------------------------------------------------------------------
    # System Tray
    # --------------------------------------------------------------------------
    def _setup_tray(self) -> None:
        self._tray: QSystemTrayIcon | None = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return

        tray = QSystemTrayIcon(self._icon_paused, self)
        tray.setToolTip(f"Völundr v{__version__} — Paused")

        menu = QMenu(self)
        self._act_show_hide = QAction("Show / Hide Völundr", self)
        self._act_show_hide.triggered.connect(self._toggle_window_visibility)
        menu.addAction(self._act_show_hide)

        self._act_tray_watch = QAction("Auto Forge", self)
        self._act_tray_watch.triggered.connect(self.toggle_watching)
        menu.addAction(self._act_tray_watch)

        self._act_tray_scan = QAction("Forge Drop Folder Now", self)
        self._act_tray_scan.triggered.connect(self.trigger_scan_once)
        menu.addAction(self._act_tray_scan)

        menu.addSeparator()

        act_drop = QAction("Open Drop Folder", self)
        act_drop.triggered.connect(self.open_drop_folder)
        menu.addAction(act_drop)

        act_lib = QAction("Open Library Folder", self)
        act_lib.triggered.connect(self.open_library_folder)
        menu.addAction(act_lib)

        act_updates = QAction("Check for Updates...", self)
        act_updates.triggered.connect(self.open_update_dialog)
        menu.addAction(act_updates)

        menu.addSeparator()

        act_quit = QAction("Quit Völundr", self)
        act_quit.triggered.connect(self.quit_application)
        menu.addAction(act_quit)

        tray.setContextMenu(menu)
        tray.activated.connect(self._on_tray_activated)
        tray.show()
        self._tray = tray

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_and_raise()

    def _toggle_window_visibility(self) -> None:
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self.show_and_raise()

    def show_and_raise(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _notify(self, title: str, body: str, *, warning: bool = False) -> None:
        if self._tray is None or not self._chk_notifications.isChecked():
            return
        icon = (
            QSystemTrayIcon.MessageIcon.Warning
            if warning
            else QSystemTrayIcon.MessageIcon.Information
        )
        self._tray.showMessage(title, body, icon, 5000)

    # --------------------------------------------------------------------------
    # Watcher & One-Shot Scan Control
    # --------------------------------------------------------------------------
    def toggle_watching(self) -> None:
        if self._watching:
            self.stop_watching()
        else:
            self.start_watching()

    def start_watching(self) -> None:
        if self._watching or self._scanning:
            return
        ensure_local_directories(self.config)
        watcher = PipelineWatcher(
            self.config,
            on_event_log=lambda msg: self._signals.event_logged.emit(_now_hms(), msg),
            on_outcome=lambda outcome: self._signals.outcome_received.emit(outcome),
            on_error=lambda path, exc: self._signals.error_received.emit(path.name, str(exc)),
        )
        watcher.start_background(scan_existing=True)
        self._watcher = watcher
        self._watching = True
        self._append_log(
            f"Watch mode started — monitoring `{self.config.paths.watch_dir}` -> `{self.config.paths.library_root}`"
        )
        self._refresh_state_ui()

    def stop_watching(self) -> None:
        watcher = self._watcher
        if watcher is None:
            self._watching = False
            self._refresh_state_ui()
            return
        self._watcher = None
        self._watching = False
        self._btn_toggle_watch.setEnabled(False)
        self._btn_toggle_watch.setText("Stopping...")
        self._append_log("Stopping watcher cleanly (waiting for any active job)...")

        def _stop_worker() -> None:
            try:
                watcher.stop(timeout=15.0)
            finally:
                self._signals.watcher_stopped.emit()

        threading.Thread(target=_stop_worker, name="volundr-gui-stop", daemon=True).start()

    def _on_watcher_stopped(self) -> None:
        self._append_log("Watcher paused.")
        self._refresh_state_ui()

    def trigger_scan_once(self) -> None:
        if self._scanning:
            return
        if self._watching:
            self._append_log("Watcher is already active — newly dropped files are processed automatically.")
            return
        self._scanning = True
        self._refresh_state_ui()
        self._append_log(f"Starting one-shot scan of `{self.config.paths.watch_dir}`...")

        def _scan_worker() -> None:
            try:
                outcomes, errs = scan_watch_dir_once(
                    self.config,
                    dry_run=False,
                    verify_stability=True,
                    on_outcome=lambda o: self._signals.outcome_received.emit(o),
                    on_error=lambda p, e: self._signals.error_received.emit(p.name, str(e)),
                )
                self._signals.scan_finished.emit(len(outcomes), errs)
            except Exception as exc:
                self._signals.error_received.emit("scan", str(exc))
                self._signals.scan_finished.emit(0, 1)

        threading.Thread(target=_scan_worker, name="volundr-gui-scan", daemon=True).start()

    def _on_scan_finished(self, count: int, errors: int) -> None:
        self._scanning = False
        if count == 0 and errors == 0:
            self._append_log("One-shot scan complete: no actionable ROMs found in drop folder.")
        else:
            self._append_log(f"One-shot scan complete: {count} item(s) processed, {errors} error(s).")
        self._refresh_state_ui()

    # --------------------------------------------------------------------------
    # Signal Slots & Live Feed Rendering
    # --------------------------------------------------------------------------
    def _append_log(self, message: str, ts: str | None = None) -> None:
        stamp = ts or _now_hms()
        self._log_view.appendPlainText(f"[{stamp}] {message}")

    def _clear_log(self) -> None:
        self._log_view.clear()

    def _on_event_logged(self, ts: str, msg: str) -> None:
        self._append_log(msg, ts=ts)

    def _add_result_row(self, source_label: str, status_label: str, detail: str, color_hex: str) -> None:
        self._results_table.insertRow(0)
        vals = (_now_hms(), source_label, status_label, detail)
        for col, text in enumerate(vals):
            item = QTableWidgetItem(text)
            if col == 2:
                item.setForeground(QColor(color_hex))
            self._results_table.setItem(0, col, item)
        while self._results_table.rowCount() > MAX_RESULT_ROWS:
            self._results_table.removeRow(self._results_table.rowCount() - 1)

    def _on_outcome_received(self, outcome: RoutingOutcome) -> None:
        src_label = outcome.source_path.name
        if outcome.additional_sources:
            src_label = f"{src_label} (+{len(outcome.additional_sources)})"

        if outcome.quarantined_files:
            detail = ", ".join(p.name for p in outcome.quarantined_files)
            self._add_result_row(src_label, "QUARANTINED", detail, "#fbbf24")
            self._append_log(f"⚠ Quarantined {src_label}: {detail}")
            self._notify("Völundr — Quarantined", f"{src_label}\n{detail}", warning=True)
        elif outcome.routed_files:
            dest_names = ", ".join(str(p) for p in outcome.routed_files)
            self._add_result_row(src_label, "ROUTED", dest_names, "#4ade80")
            self._append_log(f"✔ Routed {src_label} -> {dest_names}")
            self._notify("Völundr — ROM Ready", f"{outcome.routed_files[0].name}")
        else:
            status_tag = "SKIPPED" if outcome.post_action_taken.startswith("skipped") else "DEFERRED"
            self._add_result_row(src_label, status_tag, outcome.post_action_taken, "#38bdf8")
            self._append_log(f"• {src_label}: {outcome.post_action_taken}")

    def _on_error_received(self, item_name: str, error_text: str) -> None:
        self._add_result_row(item_name, "ERROR", error_text, "#f87171")
        self._append_log(f"✖ ERROR ({item_name}): {error_text}")
        self._notify("Völundr — Error", f"{item_name}: {error_text}", warning=True)

    # --------------------------------------------------------------------------
    # Doctor Diagnostics Tab
    # --------------------------------------------------------------------------
    def refresh_doctor(self) -> None:
        self._btn_refresh_doctor.setEnabled(False)
        self._doctor_summary.setText("Running read-only diagnostics...")
        cfg_path = self.config.config_path
        platform_key = self.config.platform_key
        fallback_cfg = self.config

        def _doctor_worker() -> None:
            from main import collect_doctor_checks

            try:
                try:
                    fresh_cfg = load_config(cfg_path, platform_override=platform_key)
                except ConfigError:
                    fresh_cfg = fallback_cfg
                checks = collect_doctor_checks(fresh_cfg)
            except Exception as exc:
                checks = [("Runtime", "Diagnostics", "FAIL", str(exc))]
            self._signals.doctor_finished.emit(checks)

        threading.Thread(target=_doctor_worker, name="volundr-gui-doctor", daemon=True).start()

    def _on_doctor_finished(self, checks: list[tuple[str, str, str, str]]) -> None:
        self._doctor_table.setRowCount(0)
        ok_n = warn_n = fail_n = 0
        color_map = {"OK": "#4ade80", "WARN": "#fbbf24", "FAIL": "#f87171"}

        for cat, chk, status, detail in checks:
            if status == "OK":
                ok_n += 1
            elif status == "WARN":
                warn_n += 1
            else:
                fail_n += 1

            row = self._doctor_table.rowCount()
            self._doctor_table.insertRow(row)
            for col, text in enumerate((cat, chk, status, detail)):
                item = QTableWidgetItem(text)
                if col == 2:
                    item.setForeground(QColor(color_map.get(status, "#e6ebf5")))
                self._doctor_table.setItem(row, col, item)

        self._doctor_summary.setText(
            f"Profile: {self.config.platform_key}  •  {ok_n} OK  •  {warn_n} WARN  •  {fail_n} FAIL"
        )
        self._btn_refresh_doctor.setEnabled(True)

    # --------------------------------------------------------------------------
    # Settings Tab, QSettings Persistence & Dialogs
    # --------------------------------------------------------------------------
    @staticmethod
    def _read_bool_setting(settings: QSettings, key: str, default: bool) -> bool:
        raw = settings.value(key, default)
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            return raw.strip().lower() in ("1", "true", "yes", "on")
        return bool(raw)

    def _load_ui_preferences(self) -> None:
        self._chk_auto_watch.blockSignals(True)
        self._chk_min_to_tray.blockSignals(True)
        self._chk_notifications.blockSignals(True)
        self._chk_auto_update_check.blockSignals(True)
        try:
            self._chk_auto_watch.setChecked(
                self._read_bool_setting(self._ui_settings, "ui/auto_watch_on_launch", False)
            )
            self._chk_min_to_tray.setChecked(
                self._read_bool_setting(self._ui_settings, "ui/minimize_to_tray", True)
            )
            self._chk_notifications.setChecked(
                self._read_bool_setting(self._ui_settings, "ui/notifications", True)
            )
            self._chk_auto_update_check.setChecked(
                self._read_bool_setting(self._ui_settings, "ui/check_updates_on_start", True)
            )
        finally:
            self._chk_auto_watch.blockSignals(False)
            self._chk_min_to_tray.blockSignals(False)
            self._chk_notifications.blockSignals(False)
            self._chk_auto_update_check.blockSignals(False)

    def _save_ui_preferences(self) -> None:
        self._ui_settings.setValue("ui/auto_watch_on_launch", self._chk_auto_watch.isChecked())
        self._ui_settings.setValue("ui/minimize_to_tray", self._chk_min_to_tray.isChecked())
        self._ui_settings.setValue("ui/notifications", self._chk_notifications.isChecked())
        self._ui_settings.setValue(
            "ui/check_updates_on_start", self._chk_auto_update_check.isChecked()
        )
        self._ui_settings.sync()

    def _populate_settings_from_config(self) -> None:
        self._edit_watch_dir.setText(str(self.config.paths.watch_dir))
        self._edit_library_root.setText(self.config.paths.raw_library_root)
        idx = self._combo_post_action.findData(self.config.general.post_process_action.value)
        if idx >= 0:
            self._combo_post_action.setCurrentIndex(idx)

    def _reload_config_and_apply(self, status_note: str) -> None:
        reloaded = load_config(self.config.config_path, platform_override=self.config.platform_key)
        was_watching = self._watching
        if was_watching and self._watcher is not None:
            self._watcher.stop(timeout=10.0)
            self._watcher = None
            self._watching = False

        self.config = reloaded
        ensure_local_directories(self.config)
        self._populate_settings_from_config()
        self._refresh_info_strip()
        self._lbl_settings_status.setText(status_note)
        self._lbl_settings_status.setStyleSheet("color: #4ade80;")
        self._append_log("Configuration reloaded from config.toml.")
        self.refresh_doctor()

        if was_watching:
            self.start_watching()
        else:
            self._refresh_state_ui()

    def open_setup_wizard(self, *, is_first_run: bool = False) -> None:
        dlg = SetupWizardDialog(self.config.config_path, is_first_run=is_first_run, parent=self)
        result = dlg.exec()
        self._ui_settings.setValue("ui/setup_wizard_seen", True)
        self._ui_settings.sync()
        if result == SetupWizardDialog.DialogCode.Accepted:
            try:
                self._reload_config_and_apply(f"Setup wizard completed at {_now_hms()} ✓")
            except ConfigError as exc:
                QMessageBox.critical(self, "Configuration Error", str(exc))

    def maybe_show_first_run_wizard(self) -> None:
        """Open the Setup Wizard once on the user's very first GUI launch."""
        if not self._read_bool_setting(self._ui_settings, "ui/setup_wizard_seen", False):
            self.open_setup_wizard(is_first_run=True)

    def open_advanced_settings(self) -> None:
        dlg = AdvancedSettingsDialog(self.config, parent=self)
        if dlg.exec() == AdvancedSettingsDialog.DialogCode.Accepted:
            try:
                self._reload_config_and_apply(f"Advanced settings saved at {_now_hms()} ✓")
            except ConfigError as exc:
                QMessageBox.critical(self, "Configuration Error", str(exc))

    def _check_updates_silently(self) -> None:
        def _worker() -> None:
            res = check_for_updates()
            self._signals.update_checked.emit(res)

        threading.Thread(target=_worker, name="volundr-bg-update-check", daemon=True).start()

    def _on_background_update_checked(self, res: UpdateCheckResult) -> None:
        if res.update_available:
            self._btn_update_pill.setText(f"Update v{res.latest_version} Available")
            self._btn_update_pill.setVisible(True)
            self._append_log(
                f"New Völundr release available: v{res.latest_version} (current: v{res.current_version})"
            )

    def open_update_dialog(self) -> None:
        self.show_and_raise()
        dlg = UpdateDialog(self.config.paths.project_root, parent=self)
        dlg.restart_requested.connect(self._restart_application)
        dlg.exec()

    def _restart_application(self) -> None:
        """Spawn a fresh GUI instance and cleanly exit this process."""
        main_py = self.config.paths.project_root / "main.py"
        cmd = [sys.executable, str(main_py), "--config", str(self.config.config_path), "gui"]
        self._quitting = True
        self._shutdown_cleanup()
        subprocess.Popen(cmd, cwd=str(self.config.paths.project_root))
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _browse_watch_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Select Watch Folder (drop)", self._edit_watch_dir.text() or str(self.config.paths.project_root)
        )
        if chosen:
            self._edit_watch_dir.setText(chosen)

    def _browse_library_root(self) -> None:
        start = str(self.config.paths.library_root) if self.config.paths.library_root.is_dir() else str(self.config.paths.project_root)
        chosen = QFileDialog.getExistingDirectory(self, "Select ROM Library Root Folder", start)
        if chosen:
            self._edit_library_root.setText(chosen)

    def _save_settings(self) -> None:
        watch_val = self._edit_watch_dir.text().strip()
        lib_val = self._edit_library_root.text().strip()
        action_val = str(self._combo_post_action.currentData() or PostProcessAction.MOVE.value)
        self._save_ui_preferences()

        try:
            save_basic_settings(
                self.config.config_path,
                self.config.platform_key,
                watch_dir=watch_val,
                library_root=lib_val,
                post_process_action=action_val,
            )
            self._reload_config_and_apply(f"Saved at {_now_hms()} ✓")
        except ConfigError as exc:
            QMessageBox.critical(self, "Invalid Settings", str(exc))

    def _open_local_path(self, path: Path) -> None:
        if not path.exists():
            QMessageBox.warning(self, "Path Not Found", f"Directory does not exist yet:\n{path}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def open_drop_folder(self) -> None:
        ensure_local_directories(self.config)
        self._open_local_path(self.config.paths.watch_dir)

    def open_library_folder(self) -> None:
        self._open_local_path(self.config.paths.library_root)

    def open_tools_folder(self) -> None:
        tools_dir = self.config.paths.project_root / "tools"
        tools_dir.mkdir(parents=True, exist_ok=True)
        self._open_local_path(tools_dir)

    def _open_config_file(self) -> None:
        self._open_local_path(self.config.config_path)

    def _refresh_info_strip(self) -> None:
        self._info_strip.setText(
            f"<b>Drop Folder:</b> {self.config.paths.watch_dir} &nbsp;&nbsp;|&nbsp;&nbsp; "
            f"<b>Library:</b> {self.config.paths.library_root} &nbsp;&nbsp;|&nbsp;&nbsp; "
            f"<b>Post-Action:</b> {self.config.general.post_process_action.value}"
        )

    def _refresh_state_ui(self) -> None:
        if self._scanning:
            self._status_indicator.set_state("scanning")
            self._btn_toggle_watch.setEnabled(False)
            self._btn_scan.setEnabled(False)
            icon = self._icon_watching
            tooltip = f"Völundr v{__version__} — Forging drop folder..."
        elif self._watching:
            self._status_indicator.set_state("watching")
            self._btn_toggle_watch.setEnabled(True)
            self._btn_toggle_watch.setText("Pause Forge")
            self._btn_toggle_watch.setToolTip("Pause automatic monitoring of the drop folder")
            self._btn_toggle_watch.setObjectName("DangerBtn")
            self._btn_scan.setEnabled(False)
            icon = self._icon_watching
            tooltip = f"Völundr v{__version__} — Auto-Forging {self.config.paths.watch_dir.name}/"
        else:
            self._status_indicator.set_state("paused")
            self._btn_toggle_watch.setEnabled(True)
            self._btn_toggle_watch.setText("Auto Forge")
            self._btn_toggle_watch.setToolTip("Continuously monitor the drop folder and forge incoming ROMs automatically")
            self._btn_toggle_watch.setObjectName("PrimaryBtn")
            self._btn_scan.setEnabled(True)
            icon = self._icon_paused
            tooltip = f"Völundr v{__version__} — Paused"

        # Force Qt to re-evaluate objectName stylesheet change
        self._btn_toggle_watch.style().unpolish(self._btn_toggle_watch)
        self._btn_toggle_watch.style().polish(self._btn_toggle_watch)

        # Keep clean Völundr shield on the window/taskbar; show live status dot in system tray
        self.setWindowIcon(self._icon_app)
        if self._tray is not None:
            self._tray.setIcon(icon)
            self._tray.setToolTip(tooltip)
            self._act_tray_watch.setText("Pause Forge" if self._watching else "Auto Forge")
            self._act_tray_scan.setEnabled(not self._watching and not self._scanning)

    # --------------------------------------------------------------------------
    # Close-to-Tray & Clean Shutdown
    # --------------------------------------------------------------------------
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt override)
        self._save_ui_preferences()
        if (
            not self._quitting
            and self._tray is not None
            and self._chk_min_to_tray.isChecked()
        ):
            event.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self._tray.showMessage(
                    "Völundr minimized to tray",
                    "Völundr is tending the forge in the system tray. Right-click the icon to quit.",
                    QSystemTrayIcon.MessageIcon.Information,
                    4000,
                )
            return

        self._quitting = True
        self._shutdown_cleanup()
        event.accept()
        # Because `setQuitOnLastWindowClosed(False)` is active for tray support, explicitly
        # exit the Qt event loop whenever the window actually closes so terminals never hang!
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def _shutdown_cleanup(self) -> None:
        self._save_ui_preferences()
        if self._watcher is not None:
            try:
                self._watcher.stop(timeout=5.0)
            except Exception:
                pass
            self._watcher = None
        terminate_active_subprocesses()
        if self._tray is not None:
            self._tray.hide()
            self._tray.deleteLater()
            self._tray = None

    def quit_application(self) -> None:
        self._quitting = True
        self._shutdown_cleanup()
        self.hide()
        app = QApplication.instance()
        if app is not None:
            app.quit()


def _set_windows_app_user_model_id() -> None:
    """
    Assign a dedicated Windows AppUserModelID BEFORE `QApplication` initializes so Windows
    displays the Völundr shield icon on the taskbar instead of grouping under Python's icon.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("YoungLordAce.Volundr.GUI.1")
    except Exception:
        pass


def _bootstrap_missing_config_via_wizard(config_path: Path) -> PipelineConfig | None:
    """Launch the full graphical Setup Wizard if a new user opens the GUI without `config.toml`."""
    example_path = config_path.with_name("config.example.toml")
    if not example_path.is_file():
        QMessageBox.critical(
            None,
            "Völundr — Missing Configuration",
            f"Configuration file not found:\n{config_path}\n\n"
            "Run `python setup_volundr.py` or copy `config.example.toml` to `config.toml`.",
        )
        return None

    dlg = SetupWizardDialog(config_path, is_first_run=True)
    if dlg.exec() != SetupWizardDialog.DialogCode.Accepted or not config_path.is_file():
        return None

    settings = QSettings("Volundr", "VolundrGUI")
    settings.setValue("ui/setup_wizard_seen", True)
    settings.sync()
    return load_config(config_path)


def run_gui_app(
    config_path: Path,
    *,
    start_minimized: bool = False,
    auto_watch: bool | None = None,
) -> int:
    """Create the `QApplication`, load config, and start the Qt event loop."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    _set_windows_app_user_model_id()

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv[:1])
    app.setApplicationName("Völundr")
    app.setApplicationVersion(__version__)
    app.setWindowIcon(build_app_icon(watching=None))
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(DARK_STYLESHEET)

    try:
        config = load_config(config_path)
    except MissingConfigError:
        config = _bootstrap_missing_config_via_wizard(config_path)
        if config is None:
            return 2
    except ConfigError as exc:
        QMessageBox.critical(None, "Völundr — Configuration Error", str(exc))
        return 2

    window = VolundrMainWindow(config, auto_watch=auto_watch)

    # Allow Ctrl+C in a launching terminal to exit the Qt event loop cleanly on Windows & Linux
    def _handle_sigint(_signum: int, _frame) -> None:
        QTimer.singleShot(0, window.quit_application)

    try:
        signal.signal(signal.SIGINT, _handle_sigint)
    except (ValueError, OSError):
        pass
    sig_timer = QTimer()
    sig_timer.setInterval(250)
    sig_timer.timeout.connect(lambda: None)
    sig_timer.start()

    if start_minimized and QSystemTrayIcon.isSystemTrayAvailable():
        window.hide()
    else:
        window.show()
        QTimer.singleShot(150, window.maybe_show_first_run_wizard)

    try:
        return int(app.exec())
    finally:
        sig_timer.stop()
