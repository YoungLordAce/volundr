"""Secondary modal dialogs for Völundr GUI (Advanced Settings & One-Click Updater)."""

from __future__ import annotations

from pathlib import Path
import threading

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QFont, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from src.config_loader import ConfigError, save_advanced_settings
from src.gui.updater import (
    UpdateApplyResult,
    UpdateCheckResult,
    check_for_updates,
    perform_git_update,
)
from src.models import PipelineConfig


class AdvancedSettingsDialog(QDialog):
    """Modal dialog exposing `[general]` watcher/storage & `[conversion]` format options."""

    def __init__(self, config: PipelineConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self.setWindowTitle("Völundr — Advanced Settings")
        self.resize(580, 620)
        self.setMinimumSize(500, 480)
        self._build_ui()
        self._load_from_config()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(10)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(4, 4, 8, 4)
        layout.setSpacing(12)

        # 1. Watcher & Storage ([general])
        gen_group = QGroupBox("Watcher & Staging Storage ([general])")
        gen_form = QFormLayout(gen_group)
        gen_form.setSpacing(8)

        self._spin_poll_sec = QDoubleSpinBox()
        self._spin_poll_sec.setRange(0.2, 60.0)
        self._spin_poll_sec.setSingleStep(0.5)
        self._spin_poll_sec.setSuffix(" s")
        gen_form.addRow("Stability poll interval:", self._spin_poll_sec)

        self._spin_checks = QSpinBox()
        self._spin_checks.setRange(1, 20)
        gen_form.addRow("Stability checks required:", self._spin_checks)

        self._spin_min_gb = QDoubleSpinBox()
        self._spin_min_gb.setRange(0.0, 500.0)
        self._spin_min_gb.setSingleStep(1.0)
        self._spin_min_gb.setSuffix(" GiB")
        gen_form.addRow("Min free staging space:", self._spin_min_gb)
        layout.addWidget(gen_group)

        # 2. Optical & Multi-Disc ([conversion])
        disc_group = QGroupBox("PlayStation, Optical & Multi-Disc ([conversion])")
        disc_form = QFormLayout(disc_group)
        disc_form.setSpacing(8)

        self._chk_psx_pbp = QCheckBox("Merge multi-disc PS1 sets into single .pbp (via psxpackager)")
        disc_form.addRow(self._chk_psx_pbp)

        self._spin_pbp_lvl = QSpinBox()
        self._spin_pbp_lvl.setRange(0, 9)
        disc_form.addRow("PS1 .pbp compression level (0–9):", self._spin_pbp_lvl)

        self._chk_psp_chd = QCheckBox("Convert PSP .iso images to .chd (2 KiB hunk size)")
        disc_form.addRow(self._chk_psp_chd)

        self._chk_gen_m3u = QCheckBox("Generate .m3u playlists for non-PS1 multi-disc games")
        disc_form.addRow(self._chk_gen_m3u)

        self._edit_multidisc_dir = QLineEdit()
        disc_form.addRow("Multi-disc subfolder:", self._edit_multidisc_dir)
        layout.addWidget(disc_group)

        # 3. Nintendo Systems ([conversion])
        nin_group = QGroupBox("Nintendo Systems (GameCube / Wii / Switch / 3DS / Wii U)")
        nin_form = QFormLayout(nin_group)
        nin_form.setSpacing(8)

        self._combo_rvz_comp = QComboBox()
        for codec in ("zstd", "lzma2", "lzma", "bzip2", "none"):
            self._combo_rvz_comp.addItem(codec, codec)
        nin_form.addRow("GC / Wii .rvz compression:", self._combo_rvz_comp)

        self._spin_rvz_lvl = QSpinBox()
        self._spin_rvz_lvl.setRange(1, 22)
        nin_form.addRow("GC / Wii .rvz compression level:", self._spin_rvz_lvl)

        self._combo_switch_fmt = QComboBox()
        self._combo_switch_fmt.addItem("xci (recommended for merged Base+Update+DLC)", "xci")
        self._combo_switch_fmt.addItem("nsp", "nsp")
        nin_form.addRow("Switch merge format:", self._combo_switch_fmt)

        self._chk_decomp_nsz = QCheckBox("Decompress .nsz / .xcz to uncompressed .nsp / .xci")
        nin_form.addRow(self._chk_decomp_nsz)

        self._chk_3ds_cci = QCheckBox("Rename decrypted .3ds cartridge dumps to .cci (for Azahar)")
        nin_form.addRow(self._chk_3ds_cci)

        self._chk_cia_to_cci = QCheckBox("Convert Base Game 3DS .cia files to decrypted .cci (for Azahar / Cocoon)")
        nin_form.addRow(self._chk_cia_to_cci)

        self._edit_cia_dir = QLineEdit()
        nin_form.addRow("3DS .cia updates/DLC subfolder:", self._edit_cia_dir)

        self._chk_wiiu_wua = QCheckBox("Pack Wii U Loadiine folders to .wua (when zarchive is present)")
        nin_form.addRow(self._chk_wiiu_wua)
        layout.addWidget(nin_group)

        # 4. PlayStation Vita ([conversion])
        vita_group = QGroupBox("PlayStation Vita (Vita3K)")
        vita_form = QFormLayout(vita_group)
        vita_form.setSpacing(8)

        self._chk_vita_zip = QCheckBox("Repack Vita dumps & .pkg packages into Vita3K .zip + .dpt")
        vita_form.addRow(self._chk_vita_zip)

        self._edit_vita_addons = QLineEdit()
        vita_form.addRow("PS Vita updates/DLC subfolder:", self._edit_vita_addons)
        layout.addWidget(vita_group)

        layout.addStretch(1)
        scroll.setWidget(inner)
        outer.addWidget(scroll, stretch=1)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        btn_save = QPushButton("Save && Apply")
        btn_save.setObjectName("PrimaryBtn")
        btn_save.clicked.connect(self._on_save)
        btn_row.addWidget(btn_cancel)
        btn_row.addWidget(btn_save)
        outer.addLayout(btn_row)

    def _load_from_config(self) -> None:
        g = self.config.general
        c = self.config.conversion
        self._spin_poll_sec.setValue(g.stability_poll_seconds)
        self._spin_checks.setValue(g.stability_checks_required)
        self._spin_min_gb.setValue(g.min_free_staging_gb)

        self._chk_psx_pbp.setChecked(c.psx_multidisc_pbp)
        self._spin_pbp_lvl.setValue(c.psx_pbp_compression_level)
        self._chk_psp_chd.setChecked(c.psp_to_chd)
        self._chk_gen_m3u.setChecked(c.generate_m3u)
        self._edit_multidisc_dir.setText(c.multidisc_subdir)

        rvz_idx = self._combo_rvz_comp.findData(c.rvz_compression)
        if rvz_idx >= 0:
            self._combo_rvz_comp.setCurrentIndex(rvz_idx)
        self._spin_rvz_lvl.setValue(c.rvz_compression_level)

        sw_idx = self._combo_switch_fmt.findData(c.switch_merge_format)
        if sw_idx >= 0:
            self._combo_switch_fmt.setCurrentIndex(sw_idx)
        self._chk_decomp_nsz.setChecked(c.decompress_nsz)
        self._chk_3ds_cci.setChecked(c.rename_3ds_to_cci)
        self._chk_cia_to_cci.setChecked(c.cia_games_to_cci)
        self._edit_cia_dir.setText(c.cia_subdir)
        self._chk_wiiu_wua.setChecked(c.wiiu_to_wua)

        self._chk_vita_zip.setChecked(c.vita_repack_zip)
        self._edit_vita_addons.setText(c.vita_addons_subdir)

    def _on_save(self) -> None:
        general_updates = {
            "stability_poll_seconds": float(self._spin_poll_sec.value()),
            "stability_checks_required": int(self._spin_checks.value()),
            "min_free_staging_gb": float(self._spin_min_gb.value()),
        }
        conversion_updates = {
            "psx_multidisc_pbp": self._chk_psx_pbp.isChecked(),
            "psx_pbp_compression_level": int(self._spin_pbp_lvl.value()),
            "psp_to_chd": self._chk_psp_chd.isChecked(),
            "generate_m3u": self._chk_gen_m3u.isChecked(),
            "multidisc_subdir": self._edit_multidisc_dir.text().strip() or "_discs",
            "rvz_compression": str(self._combo_rvz_comp.currentData() or "zstd"),
            "rvz_compression_level": int(self._spin_rvz_lvl.value()),
            "switch_merge_format": str(self._combo_switch_fmt.currentData() or "xci"),
            "decompress_nsz": self._chk_decomp_nsz.isChecked(),
            "rename_3ds_to_cci": self._chk_3ds_cci.isChecked(),
            "cia_games_to_cci": self._chk_cia_to_cci.isChecked(),
            "cia_subdir": self._edit_cia_dir.text().strip() or "_cia_install",
            "wiiu_to_wua": self._chk_wiiu_wua.isChecked(),
            "vita_repack_zip": self._chk_vita_zip.isChecked(),
            "vita_addons_subdir": self._edit_vita_addons.text().strip() or "_updates_dlc",
        }
        try:
            save_advanced_settings(
                self.config.config_path,
                general_updates=general_updates,
                conversion_updates=conversion_updates,
            )
        except ConfigError as exc:
            QMessageBox.critical(self, "Invalid Advanced Settings", str(exc))
            return
        self.accept()


class _UpdateSignals(QObject):
    check_done = Signal(object)   # UpdateCheckResult
    apply_done = Signal(object)   # UpdateApplyResult


class UpdateDialog(QDialog):
    """Modal dialog for checking GitHub Releases and running a one-click `git pull` update."""

    restart_requested = Signal()

    def __init__(self, project_root: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.project_root = project_root
        self._release_url = "https://github.com/YoungLordAce/volundr/releases"
        self._signals = _UpdateSignals(self)
        self._signals.check_done.connect(self._on_check_done)
        self._signals.apply_done.connect(self._on_apply_done)

        self.setWindowTitle("Völundr — Check for Updates")
        self.resize(560, 400)
        self._build_ui()
        self.start_check()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        self._lbl_status = QLabel("Checking GitHub for the latest Völundr release...")
        self._lbl_status.setWordWrap(True)
        self._lbl_status.setStyleSheet("font-size: 14px; font-weight: 600;")
        layout.addWidget(self._lbl_status)

        self._notes_view = QTextBrowser()
        self._notes_view.setOpenExternalLinks(True)
        self._notes_view.setPlaceholderText("Release notes will appear here...")
        self._notes_view.document().setDefaultStyleSheet(
            "body { color: #dbe3f2; font-family: 'Segoe UI', 'Inter', sans-serif; font-size: 13px; }"
            "h1, h2, h3 { color: #f4f7ff; margin-top: 8px; margin-bottom: 4px; }"
            "ul, ol { margin-top: 2px; margin-bottom: 8px; }"
            "li { margin-bottom: 6px; line-height: 140%; }"
            "code, tt, pre { background-color: #1c2333; color: #7dd3fc; font-family: Consolas, 'Cascadia Mono', 'DejaVu Sans Mono', monospace; font-size: 13px; }"
            "a { color: #60a5fa; }"
            "strong, b { color: #f1f5f9; }"
        )
        layout.addWidget(self._notes_view, stretch=1)

        btn_row = QHBoxLayout()
        self._btn_open_web = QPushButton("Open on GitHub")
        self._btn_open_web.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(self._release_url))
        )
        btn_row.addWidget(self._btn_open_web)
        btn_row.addStretch(1)

        self._btn_update = QPushButton("Update")
        self._btn_update.setObjectName("PrimaryBtn")
        self._btn_update.setEnabled(False)
        self._btn_update.clicked.connect(self._start_apply_update)
        btn_row.addWidget(self._btn_update)

        self._btn_restart = QPushButton("Restart Völundr")
        self._btn_restart.setObjectName("PrimaryBtn")
        self._btn_restart.setVisible(False)
        self._btn_restart.clicked.connect(self._on_restart_clicked)
        btn_row.addWidget(self._btn_restart)

        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)

    def start_check(self) -> None:
        self._btn_update.setEnabled(False)
        self._lbl_status.setText("Checking GitHub for the latest Völundr release...")

        def _worker() -> None:
            res = check_for_updates()
            self._signals.check_done.emit(res)

        threading.Thread(target=_worker, name="volundr-update-check", daemon=True).start()

    def _on_check_done(self, res: UpdateCheckResult) -> None:
        self._release_url = res.release_url
        if res.error:
            self._lbl_status.setText(f"Update check failed: {res.error}")
            self._notes_view.setPlainText(
                f"Current version: v{res.current_version}\n"
                f"Releases page:   {res.release_url}"
            )
            return

        if res.update_available:
            self._lbl_status.setText(
                f"Update available: v{res.latest_version}  (you are on v{res.current_version})"
            )
            if res.release_notes_is_html:
                self._notes_view.setHtml(
                    f"<h2>{res.release_title}</h2>{res.release_notes}"
                    if res.release_notes
                    else f"<h2>{res.release_title}</h2>"
                )
            else:
                notes_md = (
                    f"## {res.release_title}\n\n{res.release_notes}"
                    if res.release_notes
                    else f"## {res.release_title}"
                )
                self._notes_view.setMarkdown(notes_md)
            self._style_inline_code_fragments()
            self._btn_update.setEnabled(True)
        else:
            self._lbl_status.setText(
                f"You're up to date! (v{res.current_version} — latest release is v{res.latest_version})"
            )
            if res.release_notes_is_html:
                self._notes_view.setHtml(
                    res.release_notes or f"<p>Latest published release: <b>{res.release_title}</b></p>"
                )
            else:
                notes_md = res.release_notes or f"Latest published release: **{res.release_title}**"
                self._notes_view.setMarkdown(notes_md)
            self._style_inline_code_fragments()
            self._btn_update.setText("Update")
            self._btn_update.setEnabled(False)

    def _style_inline_code_fragments(self) -> None:
        """Restyle Qt Markdown/HTML fixed-pitch fragments from bitmap Courier to crisp Consolas pills."""
        doc = self._notes_view.document()
        code_font = QFont("Consolas", 9)
        code_font.setStyleHint(QFont.StyleHint.Monospace)
        code_fmt = QTextCharFormat()
        code_fmt.setFont(code_font)
        code_fmt.setForeground(QColor("#7dd3fc"))
        code_fmt.setBackground(QColor("#222b3d"))

        cursor = QTextCursor(doc)
        cursor.beginEditBlock()
        block = doc.begin()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                if frag.isValid() and frag.charFormat().fontFixedPitch():
                    cursor.setPosition(frag.position())
                    cursor.setPosition(frag.position() + frag.length(), QTextCursor.MoveMode.KeepAnchor)
                    cursor.mergeCharFormat(code_fmt)
                it += 1
            block = block.next()
        cursor.endEditBlock()

    def _start_apply_update(self) -> None:
        self._btn_update.setEnabled(False)
        self._lbl_status.setText("Updating via `git pull --ff-only` and refreshing dependencies...")

        def _worker() -> None:
            res = perform_git_update(self.project_root)
            self._signals.apply_done.emit(res)

        threading.Thread(target=_worker, name="volundr-update-apply", daemon=True).start()

    def _on_apply_done(self, res: UpdateApplyResult) -> None:
        if res.success:
            self._lbl_status.setText("Update complete ✓")
            self._notes_view.setPlainText(res.message)
            if res.needs_restart:
                self._btn_update.setVisible(False)
                self._btn_restart.setVisible(True)
        else:
            self._lbl_status.setText("Could not apply automatic update")
            self._notes_view.setPlainText(res.message)
            self._btn_update.setEnabled(True)

    def _on_restart_clicked(self) -> None:
        self.restart_requested.emit()
        self.accept()
