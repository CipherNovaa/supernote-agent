#!/usr/bin/env python3
"""
Supernote Screensaver Tool
PyQt6 app with live pywal theming, e-ink preview, dithering toggle,
USB auto-detect, and WiFi transfer.
"""

import sys
import os
import json
import subprocess
import tempfile
import glob
import re
import threading
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QFileDialog, QTabWidget, QLineEdit,
    QScrollArea, QFrame, QProgressBar, QCheckBox, QSplitter,
    QListWidget, QListWidgetItem, QMessageBox, QSlider, QGroupBox,
    QStatusBar, QSizePolicy
)
from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QTimer, QFileSystemWatcher,
    QSize, QMimeData
)
from PyQt6.QtGui import (
    QPixmap, QImage, QDragEnterEvent, QDropEvent, QColor,
    QPalette, QFont, QIcon
)

import requests

# ─── Constants ────────────────────────────────────────────────────────────────
SUPERNOTE_W = 1404
SUPERNOTE_H = 1872
WAL_COLORS  = Path.home() / ".cache/wal/colors.json"
WIFI_CONFIG = Path.home() / ".config/supernote-tool/wifi.json"
PREVIEW_W   = 280
PREVIEW_H   = int(PREVIEW_W * SUPERNOTE_H / SUPERNOTE_W)


# ─── pywal theme loader ────────────────────────────────────────────────────────
def load_wal_colors():
    """Read pywal colors.json and return a dict of color strings."""
    defaults = {
        "background": "#1a1a2e",
        "foreground": "#e0e0e0",
        "color0":     "#1a1a2e",
        "color1":     "#7f849c",
        "color2":     "#89b4fa",
        "color3":     "#a6e3a1",
        "color4":     "#89dceb",
        "color5":     "#cba6f7",
        "color6":     "#94e2d5",
        "color7":     "#cdd6f4",
        "color8":     "#313244",
        "color15":    "#cdd6f4",
    }
    try:
        with open(WAL_COLORS) as f:
            data = json.load(f)
        colors = data.get("colors", {})
        special = data.get("special", {})
        return {
            "background": special.get("background", defaults["background"]),
            "foreground": special.get("foreground", defaults["foreground"]),
            **{f"color{i}": colors.get(f"color{i}", defaults.get(f"color{i}", "#888888"))
               for i in range(16)},
        }
    except Exception:
        return defaults


def build_stylesheet(c):
    """Build a Qt stylesheet from a wal color dict."""
    bg      = c["background"]
    fg      = c["foreground"]
    c1      = c["color1"]
    c2      = c["color2"]
    c4      = c["color4"]
    c5      = c["color5"]
    c8      = c["color8"]
    c15     = c["color15"]

    return f"""
    QMainWindow, QWidget {{
        background-color: {bg};
        color: {fg};
        font-family: "IosevkaNF", "Iosevka Nerd Font", "Noto Sans", sans-serif;
        font-size: 13px;
    }}
    QTabWidget::pane {{
        border: 1px solid {c8};
        background: {bg};
        border-radius: 6px;
    }}
    QTabBar::tab {{
        background: {c8};
        color: {c1};
        padding: 8px 18px;
        border-radius: 4px 4px 0 0;
        margin-right: 2px;
    }}
    QTabBar::tab:selected {{
        background: {c2};
        color: {bg};
        font-weight: bold;
    }}
    QPushButton {{
        background-color: {c8};
        color: {fg};
        border: 1px solid {c1};
        border-radius: 6px;
        padding: 7px 16px;
        font-size: 13px;
    }}
    QPushButton:hover {{
        background-color: {c2};
        color: {bg};
        border-color: {c2};
    }}
    QPushButton:pressed {{
        background-color: {c5};
        color: {bg};
    }}
    QPushButton#primary {{
        background-color: {c2};
        color: {bg};
        border-color: {c2};
        font-weight: bold;
    }}
    QPushButton#primary:hover {{
        background-color: {c4};
    }}
    QPushButton#danger {{
        background-color: {c8};
        color: {c["color1"]};
        border-color: {c["color1"]};
    }}
    QLineEdit {{
        background-color: {c8};
        color: {fg};
        border: 1px solid {c1};
        border-radius: 5px;
        padding: 6px 10px;
    }}
    QLineEdit:focus {{
        border-color: {c2};
    }}
    QLabel {{
        color: {fg};
    }}
    QLabel#heading {{
        font-size: 15px;
        font-weight: bold;
        color: {c2};
    }}
    QLabel#subheading {{
        font-size: 12px;
        color: {c1};
    }}
    QLabel#dim {{
        color: {c1};
        font-size: 11px;
    }}
    QListWidget {{
        background-color: {c8};
        color: {fg};
        border: 1px solid {c1};
        border-radius: 6px;
        padding: 4px;
    }}
    QListWidget::item {{
        padding: 6px;
        border-radius: 4px;
    }}
    QListWidget::item:selected {{
        background-color: {c2};
        color: {bg};
    }}
    QListWidget::item:hover {{
        background-color: {c8};
        color: {fg};
    }}
    QScrollBar:vertical {{
        background: {bg};
        width: 8px;
        border-radius: 4px;
    }}
    QScrollBar::handle:vertical {{
        background: {c1};
        border-radius: 4px;
        min-height: 20px;
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0;
    }}
    QProgressBar {{
        background-color: {c8};
        border: 1px solid {c1};
        border-radius: 5px;
        text-align: center;
        color: {fg};
        height: 16px;
    }}
    QProgressBar::chunk {{
        background-color: {c2};
        border-radius: 4px;
    }}
    QCheckBox {{
        color: {fg};
        spacing: 8px;
    }}
    QCheckBox::indicator {{
        width: 16px;
        height: 16px;
        border: 1px solid {c1};
        border-radius: 3px;
        background: {c8};
    }}
    QCheckBox::indicator:checked {{
        background: {c2};
        border-color: {c2};
    }}
    QGroupBox {{
        border: 1px solid {c8};
        border-radius: 8px;
        margin-top: 12px;
        padding-top: 8px;
        color: {c1};
        font-size: 11px;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 10px;
        padding: 0 4px;
        color: {c5};
    }}
    QSlider::groove:horizontal {{
        height: 4px;
        background: {c8};
        border-radius: 2px;
    }}
    QSlider::handle:horizontal {{
        background: {c2};
        width: 14px;
        height: 14px;
        margin: -5px 0;
        border-radius: 7px;
    }}
    QSlider::sub-page:horizontal {{
        background: {c2};
        border-radius: 2px;
    }}
    QFrame#preview_frame {{
        background-color: {c8};
        border: 2px solid {c1};
        border-radius: 8px;
    }}
    QFrame#drop_zone {{
        background-color: {c8};
        border: 2px dashed {c1};
        border-radius: 10px;
    }}
    QStatusBar {{
        background-color: {c8};
        color: {c1};
        font-size: 11px;
    }}
    """


# ─── Image conversion worker ──────────────────────────────────────────────────
class ConvertWorker(QThread):
    progress   = pyqtSignal(int, int)   # current, total
    done       = pyqtSignal(list)       # list of output paths
    error      = pyqtSignal(str)

    def __init__(self, files, out_dir, dither):
        super().__init__()
        self.files   = files
        self.out_dir = out_dir
        self.dither  = dither

    def run(self):
        results = []
        for i, src in enumerate(self.files):
            self.progress.emit(i, len(self.files))
            name   = Path(src).stem + ".png"
            dest   = str(Path(self.out_dir) / name)
            cmd    = ["convert", src,
                      "-resize", f"{SUPERNOTE_W}x{SUPERNOTE_H}^",
                      "-gravity", "center",
                      "-extent", f"{SUPERNOTE_W}x{SUPERNOTE_H}",
                      "-colorspace", "Gray",
                      "-depth", "8"]
            if self.dither:
                cmd += ["-dither", "FloydSteinberg",
                        "-remap", "pattern:gray50"]
            cmd.append(dest)
            try:
                subprocess.run(cmd, check=True, capture_output=True)
                results.append(dest)
            except subprocess.CalledProcessError as e:
                self.error.emit(f"Failed: {Path(src).name}\n{e.stderr.decode()}")
        self.progress.emit(len(self.files), len(self.files))
        self.done.emit(results)


# ─── WiFi upload worker ───────────────────────────────────────────────────────
class UploadWorker(QThread):
    progress = pyqtSignal(int, int, str)
    done     = pyqtSignal()
    error    = pyqtSignal(str)

    def __init__(self, files, ip):
        super().__init__()
        self.files = files
        self.ip    = ip

    def run(self):
        url = f"http://{self.ip}/upload"
        for i, f in enumerate(self.files):
            self.progress.emit(i, len(self.files), Path(f).name)
            try:
                with open(f, "rb") as fh:
                    r = requests.post(url, files={"file": (Path(f).name, fh)},
                                      timeout=15)
                if r.status_code not in (200, 201):
                    self.error.emit(f"HTTP {r.status_code} for {Path(f).name}")
                    return
            except Exception as e:
                self.error.emit(f"Upload failed: {e}")
                return
        self.done.emit()


# ─── Drop Zone ────────────────────────────────────────────────────────────────
class DropZone(QLabel):
    files_dropped = pyqtSignal(list)

    def __init__(self):
        super().__init__()
        self.setObjectName("drop_zone")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setText("Drop images here\nor click to browse")
        self.setMinimumHeight(100)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Select images", str(Path.home()),
            "Images (*.png *.jpg *.jpeg *.webp *.bmp *.tiff)"
        )
        if files:
            self.files_dropped.emit(files)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        files = [u.toLocalFile() for u in event.mimeData().urls()
                 if u.toLocalFile().lower().endswith(
                     (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff"))]
        if files:
            self.files_dropped.emit(files)


# ─── Preview panel ────────────────────────────────────────────────────────────
class PreviewPanel(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("preview_frame")
        self.setFixedSize(PREVIEW_W + 24, PREVIEW_H + 60)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(6)

        self.title_lbl = QLabel("Preview")
        self.title_lbl.setObjectName("subheading")
        self.title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.title_lbl)

        self.img_lbl = QLabel()
        self.img_lbl.setFixedSize(PREVIEW_W, PREVIEW_H)
        self.img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img_lbl.setStyleSheet("background: #111; border-radius: 4px;")
        layout.addWidget(self.img_lbl)

    def show_image(self, path, dither=False, label="Preview"):
        """Load image, apply grayscale + optional dithering, display."""
        self.title_lbl.setText(label)
        try:
            # Use ImageMagick to produce a quick preview PNG in /tmp
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            tmp.close()
            cmd = ["convert", path,
                   "-resize", f"{PREVIEW_W}x{PREVIEW_H}^",
                   "-gravity", "center",
                   "-extent", f"{PREVIEW_W}x{PREVIEW_H}",
                   "-colorspace", "Gray",
                   "-depth", "8"]
            if dither:
                cmd += ["-dither", "FloydSteinberg", "-remap", "pattern:gray50"]
            cmd.append(tmp.name)
            subprocess.run(cmd, check=True, capture_output=True)
            pix = QPixmap(tmp.name)
            self.img_lbl.setPixmap(pix)
            os.unlink(tmp.name)
        except Exception as e:
            self.img_lbl.setText(f"Preview failed\n{e}")

    def clear(self):
        self.img_lbl.clear()
        self.img_lbl.setText("No image")
        self.title_lbl.setText("Preview")


# ─── Convert Tab ─────────────────────────────────────────────────────────────
class ConvertTab(QWidget):
    def __init__(self, status_bar):
        super().__init__()
        self.status_bar  = status_bar
        self.input_files = []
        self.output_dir  = str(Path.home() / "Pictures" / "supernote-screensavers")
        self.worker      = None
        self._build_ui()

    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setSpacing(16)
        root.setContentsMargins(16, 16, 16, 16)

        # ── Left column ──
        left = QVBoxLayout()
        left.setSpacing(10)

        heading = QLabel("Screensaver Converter")
        heading.setObjectName("heading")
        left.addWidget(heading)

        self.drop = DropZone()
        self.drop.files_dropped.connect(self.add_files)
        left.addWidget(self.drop)

        # File list
        self.file_list = QListWidget()
        self.file_list.setMaximumHeight(180)
        self.file_list.currentRowChanged.connect(self._on_select)
        left.addWidget(self.file_list)

        # Buttons row
        btn_row = QHBoxLayout()
        self.clear_btn = QPushButton("Clear list")
        self.clear_btn.setObjectName("danger")
        self.clear_btn.clicked.connect(self._clear_files)
        btn_row.addWidget(self.clear_btn)
        btn_row.addStretch()
        left.addLayout(btn_row)

        # Options
        opts = QGroupBox("Options")
        opts_layout = QVBoxLayout(opts)

        self.dither_cb = QCheckBox("Floyd-Steinberg dithering")
        self.dither_cb.setChecked(False)
        self.dither_cb.stateChanged.connect(self._refresh_preview)
        opts_layout.addWidget(self.dither_cb)

        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("Output dir:"))
        self.out_edit = QLineEdit(self.output_dir)
        self.out_edit.textChanged.connect(lambda t: setattr(self, 'output_dir', t))
        out_row.addWidget(self.out_edit)
        browse_btn = QPushButton("…")
        browse_btn.setFixedWidth(32)
        browse_btn.clicked.connect(self._pick_out_dir)
        out_row.addWidget(browse_btn)
        opts_layout.addLayout(out_row)

        left.addWidget(opts)

        # Progress
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        left.addWidget(self.progress)

        # Convert button
        self.convert_btn = QPushButton("Convert to Supernote format")
        self.convert_btn.setObjectName("primary")
        self.convert_btn.clicked.connect(self._convert)
        left.addWidget(self.convert_btn)

        left.addStretch()
        root.addLayout(left, 2)

        # ── Right column: previews ──
        right = QVBoxLayout()
        right.setSpacing(12)
        right.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)

        prev_row = QHBoxLayout()
        prev_row.setSpacing(16)

        self.orig_preview = PreviewPanel()
        self.proc_preview = PreviewPanel()
        self.proc_preview.title_lbl.setText("E-ink simulation")

        prev_row.addWidget(self.orig_preview)
        prev_row.addWidget(self.proc_preview)
        right.addLayout(prev_row)

        hint = QLabel("Left: original   ·   Right: e-ink simulation")
        hint.setObjectName("dim")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right.addWidget(hint)

        right.addStretch()
        root.addLayout(right, 3)

    def add_files(self, files):
        for f in files:
            if f not in self.input_files:
                self.input_files.append(f)
                self.file_list.addItem(Path(f).name)
        self.status_bar.showMessage(f"{len(self.input_files)} image(s) queued")
        if self.file_list.count() == 1:
            self.file_list.setCurrentRow(0)

    def _clear_files(self):
        self.input_files.clear()
        self.file_list.clear()
        self.orig_preview.clear()
        self.proc_preview.clear()
        self.status_bar.showMessage("List cleared")

    def _on_select(self, row):
        if 0 <= row < len(self.input_files):
            f = self.input_files[row]
            self.orig_preview.show_image(f, dither=False, label="Original")
            self.proc_preview.show_image(
                f, dither=self.dither_cb.isChecked(), label="E-ink simulation"
            )

    def _refresh_preview(self):
        row = self.file_list.currentRow()
        if 0 <= row < len(self.input_files):
            self.proc_preview.show_image(
                self.input_files[row],
                dither=self.dither_cb.isChecked(),
                label="E-ink simulation (dithered)" if self.dither_cb.isChecked()
                      else "E-ink simulation"
            )

    def _pick_out_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Output directory", self.output_dir)
        if d:
            self.output_dir = d
            self.out_edit.setText(d)

    def _convert(self):
        if not self.input_files:
            self.status_bar.showMessage("No files selected!")
            return
        Path(self.output_dir).mkdir(parents=True, exist_ok=True)
        self.progress.setVisible(True)
        self.progress.setRange(0, len(self.input_files))
        self.convert_btn.setEnabled(False)
        self.worker = ConvertWorker(
            self.input_files, self.output_dir, self.dither_cb.isChecked()
        )
        self.worker.progress.connect(lambda c, t: self.progress.setValue(c))
        self.worker.done.connect(self._on_done)
        self.worker.error.connect(lambda e: self.status_bar.showMessage(f"Error: {e}"))
        self.worker.start()

    def _on_done(self, results):
        self.progress.setVisible(False)
        self.convert_btn.setEnabled(True)
        self.status_bar.showMessage(
            f"✓ Converted {len(results)} image(s) → {self.output_dir}"
        )


# ─── Transfer Tab ─────────────────────────────────────────────────────────────
class TransferTab(QWidget):
    def __init__(self, status_bar):
        super().__init__()
        self.status_bar = status_bar
        self.worker     = None
        self._load_wifi_config()
        self._build_ui()
        self._detect_usb()

    def _load_wifi_config(self):
        self.wifi_ip = ""
        try:
            with open(WIFI_CONFIG) as f:
                self.wifi_ip = json.load(f).get("ip", "")
        except Exception:
            pass

    def _save_wifi_config(self):
        WIFI_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        with open(WIFI_CONFIG, "w") as f:
            json.dump({"ip": self.wifi_ip_edit.text()}, f)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(16, 16, 16, 16)

        heading = QLabel("Transfer to Supernote")
        heading.setObjectName("heading")
        layout.addWidget(heading)

        # File picker
        files_group = QGroupBox("Files to transfer")
        fg_layout   = QVBoxLayout(files_group)

        self.transfer_list = QListWidget()
        self.transfer_list.setMaximumHeight(140)
        fg_layout.addWidget(self.transfer_list)

        fl_row = QHBoxLayout()
        add_btn = QPushButton("Add files…")
        add_btn.clicked.connect(self._add_files)
        add_dir_btn = QPushButton("Add folder…")
        add_dir_btn.clicked.connect(self._add_dir)
        clr_btn = QPushButton("Clear")
        clr_btn.setObjectName("danger")
        clr_btn.clicked.connect(self.transfer_list.clear)
        fl_row.addWidget(add_btn)
        fl_row.addWidget(add_dir_btn)
        fl_row.addStretch()
        fl_row.addWidget(clr_btn)
        fg_layout.addLayout(fl_row)
        layout.addWidget(files_group)

        # USB group
        usb_group = QGroupBox("USB Transfer")
        usb_layout = QVBoxLayout(usb_group)

        self.usb_status = QLabel("Scanning for device…")
        self.usb_status.setObjectName("subheading")
        usb_layout.addWidget(self.usb_status)

        usb_btn_row = QHBoxLayout()
        self.usb_refresh_btn = QPushButton("Refresh")
        self.usb_refresh_btn.clicked.connect(self._detect_usb)
        self.usb_send_btn = QPushButton("Send via USB")
        self.usb_send_btn.setObjectName("primary")
        self.usb_send_btn.setEnabled(False)
        self.usb_send_btn.clicked.connect(self._send_usb)
        usb_btn_row.addWidget(self.usb_refresh_btn)
        usb_btn_row.addStretch()
        usb_btn_row.addWidget(self.usb_send_btn)
        usb_layout.addLayout(usb_btn_row)
        layout.addWidget(usb_group)

        # WiFi group
        wifi_group = QGroupBox("WiFi Transfer (Browse & Access)")
        wf_layout  = QVBoxLayout(wifi_group)

        ip_row = QHBoxLayout()
        ip_row.addWidget(QLabel("Device IP:"))
        self.wifi_ip_edit = QLineEdit(self.wifi_ip)
        self.wifi_ip_edit.setPlaceholderText("the address shown in the device's Browse & Access screen")
        self.wifi_ip_edit.textChanged.connect(self._save_wifi_config)
        ip_row.addWidget(self.wifi_ip_edit)
        wf_layout.addLayout(ip_row)

        hint = QLabel("Enable Browse & Access on device: Settings → Browse & Access")
        hint.setObjectName("dim")
        wf_layout.addWidget(hint)

        wf_btn_row = QHBoxLayout()
        ping_btn = QPushButton("Test connection")
        ping_btn.clicked.connect(self._ping_wifi)
        self.wifi_send_btn = QPushButton("Send via WiFi")
        self.wifi_send_btn.setObjectName("primary")
        self.wifi_send_btn.clicked.connect(self._send_wifi)
        wf_btn_row.addWidget(ping_btn)
        wf_btn_row.addStretch()
        wf_btn_row.addWidget(self.wifi_send_btn)
        wf_layout.addLayout(wf_btn_row)
        layout.addWidget(wifi_group)

        # Progress
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.transfer_label = QLabel("")
        self.transfer_label.setObjectName("dim")
        layout.addWidget(self.transfer_label)

        layout.addStretch()

    def _add_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Select files", str(Path.home()),
            "Images (*.png *.jpg *.jpeg)"
        )
        for f in files:
            self.transfer_list.addItem(f)

    def _add_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Select folder")
        if d:
            for f in sorted(Path(d).glob("*.png")):
                self.transfer_list.addItem(str(f))
            for f in sorted(Path(d).glob("*.jpg")):
                self.transfer_list.addItem(str(f))

    def _detect_usb(self):
        """Look for a Supernote mount under /media/$USER or /run/media/$USER."""
        candidates = []
        user = os.environ.get("USER", "")
        for base in [f"/media/{user}", f"/run/media/{user}"]:
            if Path(base).exists():
                for mount in Path(base).iterdir():
                    screensaver_dir = mount / "screensaver"
                    if screensaver_dir.exists():
                        candidates.append(str(screensaver_dir))

        if candidates:
            self.usb_mount = candidates[0]
            self.usb_status.setText(f"✓ Device found: {self.usb_mount}")
            self.usb_send_btn.setEnabled(True)
        else:
            self.usb_mount = None
            self.usb_status.setText("✗ No Supernote detected (plug in via USB and select File Transfer)")
            self.usb_send_btn.setEnabled(False)

    def _get_transfer_files(self):
        return [self.transfer_list.item(i).text()
                for i in range(self.transfer_list.count())]

    def _send_usb(self):
        files = self._get_transfer_files()
        if not files:
            self.status_bar.showMessage("No files to transfer")
            return
        if not self.usb_mount:
            self.status_bar.showMessage("No USB device found")
            return
        ok, fail = 0, 0
        for f in files:
            try:
                dest = Path(self.usb_mount) / Path(f).name
                import shutil
                shutil.copy2(f, dest)
                ok += 1
            except Exception as e:
                fail += 1
                self.status_bar.showMessage(f"Copy failed: {e}")
        self.status_bar.showMessage(
            f"✓ Copied {ok} file(s) to {self.usb_mount}" +
            (f" ({fail} failed)" if fail else "")
        )

    def _ping_wifi(self):
        ip = self.wifi_ip_edit.text().strip()
        if not ip:
            self.status_bar.showMessage("Enter an IP address first")
            return
        try:
            r = requests.get(f"http://{ip}", timeout=4)
            self.status_bar.showMessage(f"✓ Connected to {ip} (HTTP {r.status_code})")
        except Exception as e:
            self.status_bar.showMessage(f"✗ Cannot reach {ip}: {e}")

    def _send_wifi(self):
        files = self._get_transfer_files()
        ip    = self.wifi_ip_edit.text().strip()
        if not files:
            self.status_bar.showMessage("No files to transfer")
            return
        if not ip:
            self.status_bar.showMessage("Enter WiFi IP address")
            return
        self.progress.setVisible(True)
        self.progress.setRange(0, len(files))
        self.wifi_send_btn.setEnabled(False)
        self.worker = UploadWorker(files, ip)
        self.worker.progress.connect(self._on_upload_progress)
        self.worker.done.connect(self._on_upload_done)
        self.worker.error.connect(lambda e: self.status_bar.showMessage(f"Error: {e}"))
        self.worker.start()

    def _on_upload_progress(self, current, total, name):
        self.progress.setValue(current)
        self.transfer_label.setText(f"Uploading: {name} ({current+1}/{total})")

    def _on_upload_done(self):
        self.progress.setVisible(False)
        self.wifi_send_btn.setEnabled(True)
        self.transfer_label.setText("")
        self.status_bar.showMessage("✓ All files uploaded via WiFi")


# ─── Main Window ──────────────────────────────────────────────────────────────
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Supernote Screensaver Tool")
        self.setMinimumSize(1000, 680)
        self.resize(1100, 720)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready")

        tabs = QTabWidget()
        self.convert_tab  = ConvertTab(self.status_bar)
        self.transfer_tab = TransferTab(self.status_bar)
        tabs.addTab(self.convert_tab,  "Convert")
        tabs.addTab(self.transfer_tab, "Transfer")
        self.setCentralWidget(tabs)

        # pywal watcher
        self._apply_theme()
        if WAL_COLORS.exists():
            self.watcher = QFileSystemWatcher([str(WAL_COLORS)])
            self.watcher.fileChanged.connect(lambda _: self._apply_theme())

    def _apply_theme(self):
        colors = load_wal_colors()
        self.setStyleSheet(build_stylesheet(colors))
        self.status_bar.showMessage("Theme updated from pywal", 2000)


# ─── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setApplicationName("Supernote Screensaver Tool")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
