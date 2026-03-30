"""
Data + Video Synchronized Playback Tool
=======================================
Displays time-series data (from .blf, .csv, or .xlsx) alongside video playback
with automatic time synchronization based on absolute timestamps.

Supports:
  - BLF files with optional DBC decoding
  - CSV / Excel files with timestamped signal columns
  - Automatic DBC discovery from a dbc/ folder next to the data file
  - GUI file picker (double-click to launch) or CLI arguments

Requirements:
    pip install python-can opencv-python PyQt5 matplotlib numpy
    Optional: pip install cantools  (DBC decoding)
    Optional: pip install openpyxl  (Excel .xlsx support)
    Optional: ffprobe on PATH       (video metadata timestamps)
"""

import sys
import os
import re
import json
import time
import argparse
import subprocess
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict

import numpy as np
import can
import cv2
from PyQt5 import QtCore, QtGui, QtWidgets
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qt5agg import NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure

try:
    import cantools
    HAS_CANTOOLS = True
except ImportError:
    HAS_CANTOOLS = False

try:
    import openpyxl  # noqa: F401
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False


# ──────────────────────────────────────────────────────────────────────
# Data Models
# ──────────────────────────────────────────────────────────────────────

class Signal:
    """A single decoded or raw signal with timestamps and values."""

    __slots__ = ("name", "unit", "timestamps", "values")

    def __init__(self, name: str, unit: str = ""):
        self.name = name
        self.unit = unit
        self.timestamps: list[float] = []
        self.values: list[float] = []

    def finalize(self):
        self.timestamps = np.array(self.timestamps, dtype=np.float64)
        self.values = np.array(self.values, dtype=np.float64)


# ──────────────────────────────────────────────────────────────────────
# BLF Data Loader
# ──────────────────────────────────────────────────────────────────────

class BLFData:
    """Load and decode all CAN messages from a BLF file."""

    def __init__(self, blf_path: str, dbc_paths: list[str] | None = None):
        self.blf_path = blf_path
        self.signals: dict[str, Signal] = {}
        self.abs_start: float = 0.0
        self.abs_end: float = 0.0
        self.duration: float = 0.0
        self.msg_count: int = 0
        self.arb_ids: set[int] = set()

        self.db = None
        if dbc_paths and HAS_CANTOOLS:
            self.db = cantools.database.Database()
            for p in dbc_paths:
                self.db.add_dbc_file(p)
                print(f"Loaded DBC: {p}")
            print(f"  Total: {len(self.db.messages)} message definitions")

        self._load(blf_path)

    def _load(self, blf_path: str):
        print(f"Loading BLF: {blf_path}")
        reader = can.BLFReader(blf_path)

        raw_by_id: dict[int, dict[str, list]] = defaultdict(
            lambda: {"t": [], "bytes": []}
        )

        for msg in reader:
            if self.msg_count == 0:
                self.abs_start = msg.timestamp
            self.abs_end = msg.timestamp
            self.msg_count += 1
            self.arb_ids.add(msg.arbitration_id)
            raw_by_id[msg.arbitration_id]["t"].append(msg.timestamp)
            raw_by_id[msg.arbitration_id]["bytes"].append(bytes(msg.data))

        self.duration = self.abs_end - self.abs_start
        start_dt = datetime.fromtimestamp(self.abs_start, tz=timezone.utc)
        print(
            f"  {self.msg_count} messages, {len(self.arb_ids)} unique IDs, "
            f"{self.duration:.1f}s, start={start_dt.isoformat()}"
        )

        if self.db:
            self._decode_with_dbc(raw_by_id)
        else:
            self._decode_raw(raw_by_id)

        for sig in self.signals.values():
            sig.finalize()

        print(f"  {len(self.signals)} signals extracted")

    def _decode_with_dbc(self, raw_by_id):
        for aid, data in raw_by_id.items():
            try:
                msg_def = self.db.get_message_by_frame_id(aid)
            except KeyError:
                self._add_raw_signal(aid, data)
                continue

            for t, raw_bytes in zip(data["t"], data["bytes"]):
                try:
                    decoded = msg_def.decode(raw_bytes, decode_choices=False)
                except Exception:
                    continue
                for sig_name, value in decoded.items():
                    key = f"{msg_def.name}.{sig_name}"
                    if key not in self.signals:
                        unit = ""
                        try:
                            sig_def = msg_def.get_signal_by_name(sig_name)
                            unit = sig_def.unit or ""
                        except Exception:
                            pass
                        self.signals[key] = Signal(key, unit)
                    self.signals[key].timestamps.append(t)
                    self.signals[key].values.append(float(value))

    def _decode_raw(self, raw_by_id):
        for aid, data in raw_by_id.items():
            self._add_raw_signal(aid, data)

    def _add_raw_signal(self, aid: int, data: dict):
        dlc = max((len(b) for b in data["bytes"]), default=0)
        word_pairs = [(0, 2), (2, 4), (4, 6), (6, 8)]

        for start, end in word_pairs:
            if dlc < end:
                continue
            key = f"0x{aid:03X}_B{start}-{end - 1}"
            sig = Signal(key, "raw")
            for t, raw_bytes in zip(data["t"], data["bytes"]):
                if len(raw_bytes) >= end:
                    val = int.from_bytes(raw_bytes[start:end], "big")
                    sig.timestamps.append(t)
                    sig.values.append(float(val))
            if len(sig.timestamps) > 0:
                self.signals[key] = sig


# ──────────────────────────────────────────────────────────────────────
# CSV / Excel Data Loader
# ──────────────────────────────────────────────────────────────────────

class TabularData:
    """Load signals from a CSV or Excel file.

    Expected format:
      - First column is time (seconds, epoch, or elapsed)
      - Remaining columns are signal values
      - First row is headers (signal names)
      - Optional second row: units (detected if non-numeric)
    """

    def __init__(self, file_path: str):
        self.file_path = file_path
        self.signals: dict[str, Signal] = {}
        self.abs_start: float = 0.0
        self.abs_end: float = 0.0
        self.duration: float = 0.0
        self.msg_count: int = 0

        ext = Path(file_path).suffix.lower()
        if ext in (".xlsx", ".xls"):
            self._load_excel(file_path)
        else:
            self._load_csv(file_path)

    def _load_csv(self, path: str):
        import csv
        print(f"Loading CSV: {path}")
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            headers = next(reader)
            if not headers:
                raise ValueError("CSV file has no header row")

            # Peek at second row to check for units
            rows = list(reader)
            if not rows:
                raise ValueError("CSV file has no data rows")

            units, data_start = self._detect_units(headers, rows)
            self._parse_rows(headers, units, rows[data_start:])

    def _load_excel(self, path: str):
        if not HAS_OPENPYXL:
            raise ImportError(
                "openpyxl is required for Excel files. Install with: pip install openpyxl"
            )
        print(f"Loading Excel: {path}")
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb.active
        rows = []
        for row in ws.iter_rows(values_only=True):
            rows.append([str(c) if c is not None else "" for c in row])
        wb.close()

        if len(rows) < 2:
            raise ValueError("Excel file must have a header row and at least one data row")

        headers = rows[0]
        units, data_start = self._detect_units(headers, rows[1:])
        self._parse_rows(headers, units, rows[1 + data_start:])

    def _detect_units(self, headers, rows):
        """Check if the first data row contains units (all non-numeric strings)."""
        first_row = rows[0]
        is_units = True
        for i, val in enumerate(first_row):
            if i == 0:
                continue  # skip time column
            try:
                float(val)
                is_units = False
                break
            except (ValueError, TypeError):
                continue

        if is_units and len(rows) > 1:
            units = {headers[i]: str(first_row[i]) for i in range(1, min(len(headers), len(first_row)))}
            return units, 1
        return {}, 0

    def _parse_rows(self, headers, units, rows):
        time_col = headers[0]
        sig_names = headers[1:]

        # Initialize signals
        for name in sig_names:
            unit = units.get(name, "")
            self.signals[name] = Signal(name, unit)

        for row in rows:
            if len(row) < 2:
                continue
            try:
                t = float(row[0])
            except (ValueError, TypeError):
                continue

            if self.msg_count == 0:
                self.abs_start = t
            self.abs_end = t
            self.msg_count += 1

            for i, name in enumerate(sig_names, start=1):
                if i >= len(row):
                    break
                try:
                    val = float(row[i])
                    self.signals[name].timestamps.append(t)
                    self.signals[name].values.append(val)
                except (ValueError, TypeError):
                    continue

        self.duration = self.abs_end - self.abs_start

        # Finalize
        for sig in self.signals.values():
            sig.finalize()

        # Remove empty signals
        self.signals = {k: v for k, v in self.signals.items() if len(v.timestamps) > 0}

        print(
            f"  {self.msg_count} rows, {len(self.signals)} signals, "
            f"{self.duration:.1f}s"
        )


# ──────────────────────────────────────────────────────────────────────
# Video Timestamp Extraction
# ──────────────────────────────────────────────────────────────────────

def parse_blf_filename_time(blf_path: str) -> float | None:
    name = Path(blf_path).stem
    match = re.search(r"(\d{4})[_-](\d{2})[_-](\d{2})[_-](\d{2})[_-](\d{2})[_-](\d{2})", name)
    if match:
        try:
            dt = datetime(
                int(match.group(1)), int(match.group(2)), int(match.group(3)),
                int(match.group(4)), int(match.group(5)), int(match.group(6)),
            )
            return dt.timestamp()
        except ValueError:
            pass
    return None


def get_video_creation_time(video_path: str) -> tuple[float | None, str]:
    # Method 1: ffprobe
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", video_path],
            capture_output=True, text=True, check=True,
        )
        metadata = json.loads(result.stdout)

        tags = metadata.get("format", {}).get("tags", {})
        ct = (
            tags.get("creation_time")
            or tags.get("com.apple.quicktime.creationdate")
            or tags.get("date")
        )

        if not ct:
            for stream in metadata.get("streams", []):
                stags = stream.get("tags", {})
                ct = stags.get("creation_time")
                if ct and re.search(r"\d{4}", ct):
                    break
                ct = None

        if ct:
            dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
            return dt.timestamp(), "ffprobe metadata"
    except (FileNotFoundError, subprocess.CalledProcessError, KeyError, ValueError):
        pass

    # Method 2: file mtime fallback
    try:
        mtime = os.path.getmtime(video_path)
        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        cap.release()
        vid_dur = frames / fps if fps > 0 else 0
        start_estimate = mtime - vid_dur
        return start_estimate, "file mtime - duration (fallback)"
    except Exception:
        pass

    return None, ""


def compute_auto_offset(data, video_path: str) -> tuple[float, str]:
    """Compute offset so that data_time = video_time + offset."""
    report_lines = ["Sync Analysis:", "=" * 50]

    blf_internal = data.abs_start
    blf_dt = datetime.fromtimestamp(blf_internal, tz=timezone.utc)
    blf_dt_local = datetime.fromtimestamp(blf_internal)
    report_lines.append(f"Data start (UTC)   : {blf_dt.isoformat()}")
    report_lines.append(f"Data start (local) : {blf_dt_local.isoformat()}")

    # BLF filename timestamp (only for BLF files)
    blf_filename_t = None
    if hasattr(data, "blf_path"):
        blf_filename_t = parse_blf_filename_time(data.blf_path)
        if blf_filename_t:
            blf_fn_dt = datetime.fromtimestamp(blf_filename_t)
            delta_blf = blf_internal - blf_filename_t
            report_lines.append(f"Filename time      : {blf_fn_dt.isoformat()} (assumed local)")
            report_lines.append(f"  Internal vs filename: {delta_blf:+.1f}s")
            if abs(delta_blf) > 60:
                report_lines.append(
                    f"  WARNING: >60s difference — likely timezone offset ({delta_blf / 3600:+.1f}h)"
                )

    video_abs_start, video_method = get_video_creation_time(video_path)
    if video_abs_start is not None:
        vid_dt_local = datetime.fromtimestamp(video_abs_start)
        report_lines.append(f"Video start        : {vid_dt_local.isoformat()} (local)")
        report_lines.append(f"  Method: {video_method}")
    else:
        report_lines.append("Video start        : UNKNOWN")

    report_lines.append("-" * 50)

    offsets = {}
    if video_abs_start is not None:
        off1 = video_abs_start - blf_internal
        offsets["internal vs video"] = off1
        report_lines.append(f"Offset (data vs video): {off1:+.3f}s")

    if blf_filename_t is not None and video_abs_start is not None:
        off2 = video_abs_start - blf_filename_t
        offsets["filename vs video"] = off2
        report_lines.append(f"Offset (filename vs video): {off2:+.3f}s")

    if not offsets:
        offset = 0.0
        report_lines.append("\nResult: No timestamps available, offset = 0.0s")
        report_lines.append("  Use the Sync Wizard to manually align.")
    else:
        offset = offsets.get("internal vs video", list(offsets.values())[0])
        report_lines.append(f"\nResult: offset = {offset:+.3f}s")
        if len(offsets) > 1:
            spread = max(offsets.values()) - min(offsets.values())
            if spread > 2.0:
                report_lines.append(
                    f"  WARNING: Methods disagree by {spread:.1f}s — use Sync Wizard."
                )

    report = "\n".join(report_lines)
    print(f"\n{report}\n")
    return offset, report


# ──────────────────────────────────────────────────────────────────────
# Launcher Dialog (GUI file picker)
# ──────────────────────────────────────────────────────────────────────

class LauncherDialog(QtWidgets.QDialog):
    """File picker dialog shown when no CLI arguments are provided."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Data Video Sync — Open Files")
        self.setMinimumWidth(550)
        self.video_path = ""
        self.data_path = ""
        self.dbc_paths: list[str] = []
        self.no_dbc = False
        self.manual_offset: float | None = None

        layout = QtWidgets.QVBoxLayout(self)

        # Title
        title = QtWidgets.QLabel("<h2>Data Video Sync</h2>")
        title.setAlignment(QtCore.Qt.AlignCenter)
        layout.addWidget(title)

        subtitle = QtWidgets.QLabel(
            "Select a video file and a data file (BLF, CSV, or Excel) to begin."
        )
        subtitle.setAlignment(QtCore.Qt.AlignCenter)
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)
        layout.addSpacing(10)

        form = QtWidgets.QFormLayout()
        layout.addLayout(form)

        # Video file
        video_row = QtWidgets.QHBoxLayout()
        self.txt_video = QtWidgets.QLineEdit()
        self.txt_video.setPlaceholderText("Select a video file...")
        self.txt_video.setReadOnly(True)
        video_row.addWidget(self.txt_video)
        btn_video = QtWidgets.QPushButton("Browse...")
        btn_video.clicked.connect(self._browse_video)
        video_row.addWidget(btn_video)
        form.addRow("Video file:", video_row)

        # Data file (BLF / CSV / Excel)
        data_row = QtWidgets.QHBoxLayout()
        self.txt_data = QtWidgets.QLineEdit()
        self.txt_data.setPlaceholderText("Select a data file (.blf, .csv, .xlsx)...")
        self.txt_data.setReadOnly(True)
        data_row.addWidget(self.txt_data)
        btn_data = QtWidgets.QPushButton("Browse...")
        btn_data.clicked.connect(self._browse_data)
        data_row.addWidget(btn_data)
        form.addRow("Data file:", data_row)

        # DBC files
        dbc_row = QtWidgets.QHBoxLayout()
        self.txt_dbc = QtWidgets.QLineEdit()
        self.txt_dbc.setPlaceholderText("Auto-detect from dbc/ folder (optional)")
        self.txt_dbc.setReadOnly(True)
        dbc_row.addWidget(self.txt_dbc)
        btn_dbc = QtWidgets.QPushButton("Browse...")
        btn_dbc.clicked.connect(self._browse_dbc)
        dbc_row.addWidget(btn_dbc)
        self.chk_no_dbc = QtWidgets.QCheckBox("Skip DBC")
        self.chk_no_dbc.setToolTip("Don't load any DBC files")
        dbc_row.addWidget(self.chk_no_dbc)
        form.addRow("DBC files:", dbc_row)

        # Offset
        offset_row = QtWidgets.QHBoxLayout()
        self.chk_manual_offset = QtWidgets.QCheckBox("Manual offset:")
        self.chk_manual_offset.setChecked(False)
        offset_row.addWidget(self.chk_manual_offset)
        self.spin_offset = QtWidgets.QDoubleSpinBox()
        self.spin_offset.setRange(-3600.0, 3600.0)
        self.spin_offset.setDecimals(3)
        self.spin_offset.setValue(0.0)
        self.spin_offset.setSuffix(" s")
        self.spin_offset.setEnabled(False)
        offset_row.addWidget(self.spin_offset)
        self.chk_manual_offset.stateChanged.connect(
            lambda s: self.spin_offset.setEnabled(s == QtCore.Qt.Checked)
        )
        offset_row.addStretch()
        form.addRow("", offset_row)

        layout.addSpacing(15)

        # Buttons
        btn_row = QtWidgets.QHBoxLayout()
        layout.addLayout(btn_row)
        btn_row.addStretch()

        btn_open = QtWidgets.QPushButton("  Open  ")
        btn_open.setDefault(True)
        btn_open.setMinimumHeight(35)
        btn_open.clicked.connect(self._accept)
        btn_row.addWidget(btn_open)

        btn_cancel = QtWidgets.QPushButton("Cancel")
        btn_cancel.setMinimumHeight(35)
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)
        btn_row.addStretch()

    def _browse_video(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Select Video File", "",
            "Video Files (*.mp4 *.avi *.mkv *.mov *.wmv);;All Files (*)"
        )
        if path:
            self.txt_video.setText(path)

    def _browse_data(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Select Data File", "",
            "Data Files (*.blf *.csv *.xlsx *.xls);;BLF Files (*.blf);;CSV Files (*.csv);;Excel Files (*.xlsx *.xls);;All Files (*)"
        )
        if path:
            self.txt_data.setText(path)
            # Auto-detect DBC folder
            if path.lower().endswith(".blf") and not self.txt_dbc.text():
                dbc_dir = Path(path).parent / "dbc"
                if dbc_dir.is_dir():
                    found = sorted(dbc_dir.glob("*.dbc"))
                    if found:
                        self.txt_dbc.setText("; ".join(str(f) for f in found))
                        self.dbc_paths = [str(f) for f in found]

    def _browse_dbc(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Select DBC Files", "",
            "DBC Files (*.dbc);;All Files (*)"
        )
        if paths:
            self.txt_dbc.setText("; ".join(paths))
            self.dbc_paths = paths

    def _accept(self):
        self.video_path = self.txt_video.text().strip()
        self.data_path = self.txt_data.text().strip()
        self.no_dbc = self.chk_no_dbc.isChecked()

        if not self.video_path:
            QtWidgets.QMessageBox.warning(self, "Missing File", "Please select a video file.")
            return
        if not self.data_path:
            QtWidgets.QMessageBox.warning(self, "Missing File", "Please select a data file.")
            return
        if not Path(self.video_path).is_file():
            QtWidgets.QMessageBox.warning(self, "File Not Found", f"Video file not found:\n{self.video_path}")
            return
        if not Path(self.data_path).is_file():
            QtWidgets.QMessageBox.warning(self, "File Not Found", f"Data file not found:\n{self.data_path}")
            return

        if self.chk_manual_offset.isChecked():
            self.manual_offset = self.spin_offset.value()

        self.accept()


# ──────────────────────────────────────────────────────────────────────
# Signal Selector Widget
# ──────────────────────────────────────────────────────────────────────

class SignalSelector(QtWidgets.QWidget):
    """Searchable, checkable list of available signals."""

    selection_changed = QtCore.pyqtSignal()

    def __init__(self, signals: dict[str, Signal], parent=None):
        super().__init__(parent)
        self.all_signals = signals

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Filter signals...")
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)

        self.list_widget = QtWidgets.QListWidget()
        self.list_widget.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        layout.addWidget(self.list_widget)

        btn_row = QtWidgets.QHBoxLayout()
        btn_all = QtWidgets.QPushButton("Select All Visible")
        btn_all.clicked.connect(lambda: self._set_all_visible(True))
        btn_none = QtWidgets.QPushButton("Deselect All")
        btn_none.clicked.connect(lambda: self._set_all_visible(False))
        btn_row.addWidget(btn_all)
        btn_row.addWidget(btn_none)
        layout.addLayout(btn_row)

        self._populate()

    def _populate(self):
        for name in sorted(self.all_signals.keys()):
            sig = self.all_signals[name]
            label = name
            if sig.unit:
                label += f" [{sig.unit}]"
            item = QtWidgets.QListWidgetItem(label)
            item.setFlags(item.flags() | QtCore.Qt.ItemIsUserCheckable)
            item.setCheckState(QtCore.Qt.Unchecked)
            item.setData(QtCore.Qt.UserRole, name)
            self.list_widget.addItem(item)
        self.list_widget.itemChanged.connect(lambda _: self.selection_changed.emit())

    def _filter(self, text: str):
        text_lower = text.lower()
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            item.setHidden(text_lower not in item.text().lower())

    def _set_all_visible(self, checked: bool):
        state = QtCore.Qt.Checked if checked else QtCore.Qt.Unchecked
        self.list_widget.blockSignals(True)
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if not item.isHidden():
                item.setCheckState(state)
        self.list_widget.blockSignals(False)
        self.selection_changed.emit()

    def selected_signal_names(self) -> list[str]:
        names = []
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.checkState() == QtCore.Qt.Checked:
                names.append(item.data(QtCore.Qt.UserRole))
        return names


# ──────────────────────────────────────────────────────────────────────
# Plot Settings Panel
# ──────────────────────────────────────────────────────────────────────

class PlotSettings(QtWidgets.QGroupBox):
    settings_changed = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__("Plot Settings", parent)
        layout = QtWidgets.QFormLayout(self)

        self.spin_window = QtWidgets.QDoubleSpinBox()
        self.spin_window.setRange(1.0, 7200.0)
        self.spin_window.setSingleStep(10.0)
        self.spin_window.setValue(3600.0)
        self.spin_window.setSuffix(" s")
        self.spin_window.valueChanged.connect(self.settings_changed.emit)
        layout.addRow("Time window:", self.spin_window)

        self.chk_separate = QtWidgets.QCheckBox("Separate Y-axis per signal")
        self.chk_separate.stateChanged.connect(self.settings_changed.emit)
        layout.addRow(self.chk_separate)

        self.chk_grid = QtWidgets.QCheckBox("Show grid")
        self.chk_grid.setChecked(True)
        self.chk_grid.stateChanged.connect(self.settings_changed.emit)
        layout.addRow(self.chk_grid)

        self.spin_linewidth = QtWidgets.QDoubleSpinBox()
        self.spin_linewidth.setRange(0.3, 5.0)
        self.spin_linewidth.setSingleStep(0.1)
        self.spin_linewidth.setValue(1.0)
        self.spin_linewidth.valueChanged.connect(self.settings_changed.emit)
        layout.addRow("Line width:", self.spin_linewidth)

        self.spin_downsample = QtWidgets.QSpinBox()
        self.spin_downsample.setRange(500, 100000)
        self.spin_downsample.setSingleStep(500)
        self.spin_downsample.setValue(5000)
        self.spin_downsample.setSuffix(" pts")
        self.spin_downsample.valueChanged.connect(self.settings_changed.emit)
        layout.addRow("Max points/signal:", self.spin_downsample)

        # Y-axis
        layout.addRow(QtWidgets.QLabel("<b>Y-Axis Range</b>"))

        self.chk_auto_y = QtWidgets.QCheckBox("Auto-scale Y-axis")
        self.chk_auto_y.setChecked(True)
        self.chk_auto_y.stateChanged.connect(self._on_auto_y_changed)
        layout.addRow(self.chk_auto_y)

        self.spin_ymin = QtWidgets.QDoubleSpinBox()
        self.spin_ymin.setRange(-1e9, 1e9)
        self.spin_ymin.setValue(0.0)
        self.spin_ymin.setDecimals(2)
        self.spin_ymin.setEnabled(False)
        self.spin_ymin.valueChanged.connect(self.settings_changed.emit)
        layout.addRow("Y min:", self.spin_ymin)

        self.spin_ymax = QtWidgets.QDoubleSpinBox()
        self.spin_ymax.setRange(-1e9, 1e9)
        self.spin_ymax.setValue(100.0)
        self.spin_ymax.setDecimals(2)
        self.spin_ymax.setEnabled(False)
        self.spin_ymax.valueChanged.connect(self.settings_changed.emit)
        layout.addRow("Y max:", self.spin_ymax)

        self.spin_ypad = QtWidgets.QDoubleSpinBox()
        self.spin_ypad.setRange(0.0, 50.0)
        self.spin_ypad.setValue(5.0)
        self.spin_ypad.setSuffix(" %")
        self.spin_ypad.valueChanged.connect(self.settings_changed.emit)
        layout.addRow("Y padding:", self.spin_ypad)

        layout.addRow(QtWidgets.QLabel("<b>Per-Signal Y Lock</b>"))
        self.chk_lock_per_signal = QtWidgets.QCheckBox("Lock Y-range per signal")
        self.chk_lock_per_signal.stateChanged.connect(self._on_lock_per_signal_changed)
        layout.addRow(self.chk_lock_per_signal)

        self.locked_y_ranges: dict[str, tuple[float, float]] = {}

    def _on_lock_per_signal_changed(self, state):
        if state != QtCore.Qt.Checked:
            self.locked_y_ranges.clear()
        self.settings_changed.emit()

    def _on_auto_y_changed(self, state):
        manual = state != QtCore.Qt.Checked
        self.spin_ymin.setEnabled(manual)
        self.spin_ymax.setEnabled(manual)
        self.settings_changed.emit()

    @property
    def time_window(self) -> float:
        return self.spin_window.value()

    @property
    def separate_axes(self) -> bool:
        return self.chk_separate.isChecked()

    @property
    def show_grid(self) -> bool:
        return self.chk_grid.isChecked()

    @property
    def line_width(self) -> float:
        return self.spin_linewidth.value()

    @property
    def max_points(self) -> int:
        return self.spin_downsample.value()

    @property
    def auto_y(self) -> bool:
        return self.chk_auto_y.isChecked()

    @property
    def y_min(self) -> float:
        return self.spin_ymin.value()

    @property
    def y_max(self) -> float:
        return self.spin_ymax.value()

    @property
    def y_padding_pct(self) -> float:
        return self.spin_ypad.value() / 100.0

    @property
    def lock_per_signal(self) -> bool:
        return self.chk_lock_per_signal.isChecked()


# ──────────────────────────────────────────────────────────────────────
# Main Player Window
# ──────────────────────────────────────────────────────────────────────

class SyncPlayer(QtWidgets.QMainWindow):
    def __init__(self, video_path: str, data, auto_offset: float, sync_report: str = ""):
        super().__init__()
        self.setWindowTitle(f"Data Video Sync — {Path(video_path).name}")

        self.data = data
        self.sync_report = sync_report
        self.cap = cv2.VideoCapture(video_path)
        if not self.cap.isOpened():
            raise RuntimeError(f"Cannot open video: {video_path}")

        self.video_fps = self.cap.get(cv2.CAP_PROP_FPS) or 30.0
        self.video_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.video_duration = self.video_frames / self.video_fps

        self.playing = False
        self.playback_start_wall = 0.0
        self.playback_start_video = 0.0
        self.offset_sec = auto_offset
        self.playback_speed = 1.0
        self._slider_dragging = False
        self._last_frame_no = -1
        self._last_plot_time = 0.0
        self._plot_interval = 0.1

        self._build_ui(auto_offset)
        self._init_plot_cache()
        self._setup_timer()
        self._seek_video(0.0)
        self._update_plot(0.0)

    def _build_ui(self, auto_offset: float):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QHBoxLayout(central)

        # Left panel
        left_panel = QtWidgets.QVBoxLayout()
        root.addLayout(left_panel, stretch=0)

        self.signal_selector = SignalSelector(self.data.signals)
        self.signal_selector.setMinimumWidth(280)
        self.signal_selector.setMaximumWidth(350)
        self.signal_selector.selection_changed.connect(
            lambda: self._update_plot(self._current_video_time())
        )
        left_panel.addWidget(QtWidgets.QLabel(
            f"<b>Signals</b> ({len(self.data.signals)} available)"
        ))
        left_panel.addWidget(self.signal_selector, stretch=1)

        self.plot_settings = PlotSettings()
        self.plot_settings.settings_changed.connect(
            lambda: self._update_plot(self._current_video_time())
        )
        left_panel.addWidget(self.plot_settings)

        # Right panel
        right_panel = QtWidgets.QVBoxLayout()
        root.addLayout(right_panel, stretch=1)

        media_row = QtWidgets.QHBoxLayout()
        right_panel.addLayout(media_row, stretch=1)

        # Video
        video_container = QtWidgets.QVBoxLayout()
        media_row.addLayout(video_container, stretch=1)
        self.video_label = QtWidgets.QLabel()
        self.video_label.setMinimumSize(640, 480)
        self.video_label.setAlignment(QtCore.Qt.AlignCenter)
        self.video_label.setStyleSheet("background-color: black;")
        video_container.addWidget(self.video_label)

        # Plot
        plot_container = QtWidgets.QVBoxLayout()
        media_row.addLayout(plot_container, stretch=1)
        self.fig = Figure(figsize=(7, 5), tight_layout=True)
        self.canvas = FigureCanvas(self.fig)
        self.toolbar = NavigationToolbar(self.canvas, self)
        plot_container.addWidget(self.toolbar)
        plot_container.addWidget(self.canvas)

        # Transport controls
        transport = QtWidgets.QHBoxLayout()
        right_panel.addLayout(transport)

        self.btn_play = QtWidgets.QPushButton("▶ Play")
        self.btn_play.setFixedWidth(90)
        self.btn_play.clicked.connect(self._toggle_play)
        transport.addWidget(self.btn_play)

        btn_prev_frame = QtWidgets.QPushButton("◁ Frame")
        btn_prev_frame.setFixedWidth(65)
        btn_prev_frame.setToolTip("Step back one frame (shortcut: ,)")
        btn_prev_frame.clicked.connect(lambda: self._step_frames(-1))
        transport.addWidget(btn_prev_frame)

        for delta, label, width in [
            (-5.0, "◀◀ -5s", 70), (-1.0, "◀ -1s", 60),
            (1.0, "+1s ▶", 60), (5.0, "+5s ▶▶", 70),
        ]:
            btn = QtWidgets.QPushButton(label)
            btn.setFixedWidth(width)
            btn.clicked.connect(lambda _, d=delta: self._step(d))
            transport.addWidget(btn)

        btn_next_frame = QtWidgets.QPushButton("Frame ▷")
        btn_next_frame.setFixedWidth(65)
        btn_next_frame.setToolTip("Step forward one frame (shortcut: .)")
        btn_next_frame.clicked.connect(lambda: self._step_frames(1))
        transport.addWidget(btn_next_frame)

        transport.addWidget(QtWidgets.QLabel("Speed:"))
        self.combo_speed = QtWidgets.QComboBox()
        self.combo_speed.addItems(["0.25x", "0.5x", "1x", "2x", "4x"])
        self.combo_speed.setCurrentText("1x")
        self.combo_speed.currentTextChanged.connect(self._on_speed_changed)
        transport.addWidget(self.combo_speed)

        transport.addStretch()

        transport.addWidget(QtWidgets.QLabel("Go to:"))
        self.spin_goto = QtWidgets.QDoubleSpinBox()
        self.spin_goto.setRange(0.0, self.video_duration)
        self.spin_goto.setDecimals(2)
        self.spin_goto.setSuffix(" s")
        self.spin_goto.setSingleStep(1.0)
        self.spin_goto.setFixedWidth(110)
        transport.addWidget(self.spin_goto)
        btn_goto = QtWidgets.QPushButton("Go")
        btn_goto.setFixedWidth(35)
        btn_goto.clicked.connect(lambda: self._seek_to(self.spin_goto.value()))
        transport.addWidget(btn_goto)

        transport.addWidget(QtWidgets.QLabel(" "))
        self.lbl_time = QtWidgets.QLabel("0:00.0 / 0:00.0")
        self.lbl_time.setStyleSheet("font-family: monospace; font-size: 13px;")
        transport.addWidget(self.lbl_time)

        # Timeline slider
        slider_row = QtWidgets.QHBoxLayout()
        right_panel.addLayout(slider_row)
        self.slider_time = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.slider_time.setRange(0, int(self.video_duration * 1000))
        self.slider_time.sliderPressed.connect(self._on_slider_press)
        self.slider_time.sliderMoved.connect(self._on_slider_moved)
        self.slider_time.sliderReleased.connect(self._on_slider_release)
        slider_row.addWidget(self.slider_time)

        # Offset controls
        offset_row = QtWidgets.QHBoxLayout()
        right_panel.addLayout(offset_row)
        offset_row.addWidget(QtWidgets.QLabel("Sync offset (sec):"))
        self.spin_offset = QtWidgets.QDoubleSpinBox()
        self.spin_offset.setRange(-3600.0, 3600.0)
        self.spin_offset.setSingleStep(0.1)
        self.spin_offset.setDecimals(3)
        self.spin_offset.setValue(auto_offset)
        self.spin_offset.valueChanged.connect(self._on_offset_changed)
        offset_row.addWidget(self.spin_offset)

        for delta, label in [
            (-1.0, "-1s"), (-0.1, "-0.1s"), (-0.01, "-10ms"),
            (0.01, "+10ms"), (0.1, "+0.1s"), (1.0, "+1s"),
        ]:
            btn = QtWidgets.QPushButton(label)
            btn.setFixedWidth(55)
            btn.clicked.connect(lambda _, d=delta: self.spin_offset.setValue(
                self.spin_offset.value() + d
            ))
            offset_row.addWidget(btn)

        offset_row.addStretch()

        # Sync wizard
        sync_row = QtWidgets.QHBoxLayout()
        right_panel.addLayout(sync_row)
        btn_wizard = QtWidgets.QPushButton("Sync Wizard")
        btn_wizard.clicked.connect(self._open_sync_wizard)
        sync_row.addWidget(btn_wizard)
        btn_report = QtWidgets.QPushButton("Sync Report")
        btn_report.clicked.connect(self._show_sync_report)
        sync_row.addWidget(btn_report)
        sync_row.addStretch()

        # Status bar
        self.statusBar().showMessage(
            f"Video: {self.video_duration:.1f}s @ {self.video_fps:.1f}fps  |  "
            f"Data: {self.data.duration:.1f}s, {len(self.data.signals)} signals  |  "
            f"Offset: {auto_offset:+.3f}s"
        )

        self.resize(1600, 850)

    def _setup_timer(self):
        self.timer = QtCore.QTimer()
        interval = max(16, int(1000 / self.video_fps))
        self.timer.setInterval(interval)
        self.timer.timeout.connect(self._tick)

    # ── Keyboard shortcuts ────────────────────────────────
    def keyPressEvent(self, event):
        key = event.key()
        if key == QtCore.Qt.Key_Space:
            self._toggle_play()
        elif key == QtCore.Qt.Key_Left:
            self._step(-1.0)
        elif key == QtCore.Qt.Key_Right:
            self._step(1.0)
        elif key == QtCore.Qt.Key_Home:
            self._seek_to(0.0)
        elif key == QtCore.Qt.Key_End:
            self._seek_to(self.video_duration - 0.1)
        elif key == QtCore.Qt.Key_Comma:
            self._step_frames(-1)
        elif key == QtCore.Qt.Key_Period:
            self._step_frames(1)
        else:
            super().keyPressEvent(event)

    # ── Playback control ──────────────────────────────────
    def _toggle_play(self):
        if self.playing:
            self.playing = False
            self.timer.stop()
            self.btn_play.setText("▶ Play")
        else:
            self.playing = True
            self.playback_start_wall = time.monotonic()
            self.playback_start_video = self._current_video_time()
            self.timer.start()
            self.btn_play.setText("⏸ Pause")

    def _step_frames(self, n_frames: int):
        was_playing = self.playing
        if was_playing:
            self.playing = False
            self.timer.stop()
            self.btn_play.setText("▶ Play")
        frame_dur = 1.0 / self.video_fps
        t = self._current_video_time() + n_frames * frame_dur
        t = max(0.0, min(self.video_duration - frame_dur, t))
        self._seek_to(t)

    def _step(self, delta: float):
        was_playing = self.playing
        if was_playing:
            self.playing = False
            self.timer.stop()
        t = max(0.0, min(self.video_duration, self._current_video_time() + delta))
        self._seek_to(t)
        if was_playing:
            self.playing = True
            self.playback_start_wall = time.monotonic()
            self.playback_start_video = t
            self.timer.start()

    def _seek_to(self, t: float):
        t = max(0.0, min(self.video_duration - 0.01, t))
        self._seek_video(t)
        self._update_plot(t)
        self._update_slider(t)
        self._update_time_label(t)

    def _tick(self):
        elapsed = (time.monotonic() - self.playback_start_wall) * self.playback_speed
        video_t = self.playback_start_video + elapsed

        if video_t >= self.video_duration:
            self._toggle_play()
            return

        self._seek_video(video_t)
        now = time.monotonic()
        if now - self._last_plot_time >= self._plot_interval:
            self._update_plot(video_t)
            self._last_plot_time = now
        self._update_slider(video_t)
        self._update_time_label(video_t)

    def _current_video_time(self) -> float:
        return self.slider_time.value() / 1000.0

    # ── Video display ─────────────────────────────────────
    def _seek_video(self, t_sec: float):
        frame_no = int(t_sec * self.video_fps)
        if frame_no == self._last_frame_no:
            return
        if frame_no == self._last_frame_no + 1:
            ret, frame = self.cap.read()
        else:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, frame_no)
            ret, frame = self.cap.read()
        self._last_frame_no = frame_no
        if ret:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w, ch = frame.shape
            img = QtGui.QImage(frame.data, w, h, ch * w, QtGui.QImage.Format_RGB888)
            scaled = QtGui.QPixmap.fromImage(img).scaled(
                self.video_label.size(),
                QtCore.Qt.KeepAspectRatio,
                QtCore.Qt.FastTransformation,
            )
            self.video_label.setPixmap(scaled)

    # ── Plot update ───────────────────────────────────────
    def _init_plot_cache(self):
        self._cached_selected = None
        self._cached_separate = None
        self._cached_lines = {}
        self._cached_vlines = []
        self._cached_axes = []

    def _needs_rebuild(self, selected: list[str]) -> bool:
        return (
            selected != self._cached_selected
            or self.plot_settings.separate_axes != self._cached_separate
        )

    def _update_plot(self, video_t: float):
        selected = self.signal_selector.selected_signal_names()
        if not selected:
            if self._cached_selected != []:
                self.fig.clear()
                ax = self.fig.add_subplot(111)
                ax.text(0.5, 0.5, "Select signals to plot",
                        ha="center", va="center", transform=ax.transAxes,
                        fontsize=14, color="gray")
                self.canvas.draw_idle()
                self._init_plot_cache()
                self._cached_selected = []
            return

        data_t = video_t + self.offset_sec
        half_win = self.plot_settings.time_window / 2
        t_min = data_t - half_win
        t_max = data_t + half_win
        lw = self.plot_settings.line_width
        max_pts = self.plot_settings.max_points
        rebuild = self._needs_rebuild(selected)

        if rebuild:
            self.fig.clear()
            self._cached_lines = {}
            self._cached_vlines = []
            self._cached_axes = []

            if self.plot_settings.separate_axes and len(selected) > 1:
                axes = self.fig.subplots(len(selected), 1, sharex=True)
                if not isinstance(axes, np.ndarray):
                    axes = [axes]
                else:
                    axes = list(axes)
                for ax, name in zip(axes, selected):
                    line, = ax.plot([], [], linewidth=lw)
                    self._cached_lines[name] = line
                    vl = ax.axvline(data_t, color="red", linestyle="--", linewidth=0.8, alpha=0.7)
                    self._cached_vlines.append(vl)
                    sig = self.data.signals[name]
                    label = f"{name} [{sig.unit}]" if sig.unit else name
                    ax.set_ylabel(label, fontsize=8)
                    if self.plot_settings.show_grid:
                        ax.grid(True, alpha=0.3)
                axes[-1].set_xlabel("Data Time (s)")
                self._cached_axes = axes
            else:
                ax = self.fig.add_subplot(111)
                for name in selected:
                    sig = self.data.signals[name]
                    label = f"{name} [{sig.unit}]" if sig.unit else name
                    line, = ax.plot([], [], label=label, linewidth=lw)
                    self._cached_lines[name] = line
                vl = ax.axvline(data_t, color="red", linestyle="--", linewidth=0.8, alpha=0.7)
                self._cached_vlines.append(vl)
                ax.set_xlabel("Data Time (s)")
                ax.set_ylabel("Value")
                ax.legend(loc="upper right", fontsize=7, ncol=2)
                if self.plot_settings.show_grid:
                    ax.grid(True, alpha=0.3)
                self._cached_axes = [ax]

            self._cached_selected = list(selected)
            self._cached_separate = self.plot_settings.separate_axes

        # Fast data update
        all_vals = []
        per_signal_vals = {}

        for name in selected:
            sig = self.data.signals[name]
            rel_t = sig.timestamps - self.data.abs_start
            mask = (rel_t >= t_min) & (rel_t <= t_max)
            t_slice = rel_t[mask]
            v_slice = sig.values[mask]
            t_slice, v_slice = self._downsample(t_slice, v_slice, max_pts)

            if name in self._cached_lines:
                self._cached_lines[name].set_data(t_slice, v_slice)
                self._cached_lines[name].set_linewidth(lw)

            if len(v_slice) > 0:
                all_vals.append(v_slice)
                per_signal_vals[name] = v_slice

        for vl in self._cached_vlines:
            vl.set_xdata([data_t, data_t])

        for ax in self._cached_axes:
            ax.set_xlim(t_min, t_max)

        pad_pct = self.plot_settings.y_padding_pct

        if self.plot_settings.separate_axes and self.plot_settings.lock_per_signal:
            for i, name in enumerate(selected):
                if i >= len(self._cached_axes):
                    break
                ax = self._cached_axes[i]
                if name in self.plot_settings.locked_y_ranges:
                    ylo, yhi = self.plot_settings.locked_y_ranges[name]
                    ax.set_ylim(ylo, yhi)
                elif name in per_signal_vals:
                    vals = per_signal_vals[name]
                    vmin, vmax = float(np.nanmin(vals)), float(np.nanmax(vals))
                    span = vmax - vmin if (vmax - vmin) > 1e-10 else max(abs(vmin), 1.0)
                    pad = span * pad_pct
                    ylo, yhi = vmin - pad, vmax + pad
                    self.plot_settings.locked_y_ranges[name] = (ylo, yhi)
                    ax.set_ylim(ylo, yhi)
        elif self.plot_settings.auto_y:
            if all_vals:
                combined = np.concatenate(all_vals)
                if len(combined) > 0:
                    vmin, vmax = float(np.nanmin(combined)), float(np.nanmax(combined))
                    span = vmax - vmin if (vmax - vmin) > 1e-10 else max(abs(vmin), 1.0)
                    pad = span * pad_pct
                    for ax in self._cached_axes:
                        ax.set_ylim(vmin - pad, vmax + pad)
        else:
            for ax in self._cached_axes:
                ax.set_ylim(self.plot_settings.y_min, self.plot_settings.y_max)

        for ax in self._cached_axes:
            ax.grid(self.plot_settings.show_grid, alpha=0.3)

        self.canvas.draw_idle()

    @staticmethod
    def _downsample(t: np.ndarray, v: np.ndarray, max_pts: int):
        if len(t) <= max_pts:
            return t, v
        stride = len(t) // max_pts
        return t[::stride], v[::stride]

    # ── UI event handlers ─────────────────────────────────
    def _update_slider(self, t: float):
        self.slider_time.blockSignals(True)
        self.slider_time.setValue(int(t * 1000))
        self.slider_time.blockSignals(False)

    def _update_time_label(self, t: float):
        cur = f"{int(t) // 60}:{t % 60:05.2f}"
        tot = f"{int(self.video_duration) // 60}:{self.video_duration % 60:05.2f}"
        data_t = t + self.offset_sec
        data_str = f"{int(data_t) // 60}:{abs(data_t) % 60:05.2f}"
        self.lbl_time.setText(f"Video: {cur} / {tot}  |  Data: {data_str}")

    def _on_slider_press(self):
        self._slider_dragging = True
        self.timer.stop()

    def _on_slider_moved(self, value):
        t = value / 1000.0
        self._seek_video(t)
        self._update_plot(t)
        self._update_time_label(t)

    def _on_slider_release(self):
        self._slider_dragging = False
        t = self._current_video_time()
        self._seek_video(t)
        self._update_plot(t)
        self._update_time_label(t)
        if self.playing:
            self.playback_start_wall = time.monotonic()
            self.playback_start_video = t
            self.timer.start()

    def _on_offset_changed(self, val: float):
        self.offset_sec = val
        self._update_plot(self._current_video_time())

    def _on_speed_changed(self, text: str):
        self.playback_speed = float(text.replace("x", ""))
        if self.playing:
            self.playback_start_wall = time.monotonic()
            self.playback_start_video = self._current_video_time()

    def closeEvent(self, event):
        self.timer.stop()
        self.cap.release()
        super().closeEvent(event)

    # ── Sync ──────────────────────────────────────────────
    def _show_sync_report(self):
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Sync Analysis Report")
        dlg.resize(550, 400)
        layout = QtWidgets.QVBoxLayout(dlg)
        text = QtWidgets.QTextEdit()
        text.setReadOnly(True)
        text.setFontFamily("Consolas")
        text.setPlainText(self.sync_report or "No sync report available.")
        layout.addWidget(text)
        btn = QtWidgets.QPushButton("Close")
        btn.clicked.connect(dlg.close)
        layout.addWidget(btn)
        dlg.exec_()

    def _open_sync_wizard(self):
        was_playing = self.playing
        if was_playing:
            self._toggle_play()
        dlg = SyncWizardDialog(self, self.data, self._current_video_time())
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            new_offset = dlg.computed_offset
            if new_offset is not None:
                self.spin_offset.setValue(new_offset)
                self._init_plot_cache()
                self._update_plot(self._current_video_time())


class SyncWizardDialog(QtWidgets.QDialog):
    def __init__(self, parent: SyncPlayer, data, current_video_t: float):
        super().__init__(parent)
        self.setWindowTitle("Sync Wizard — Mark Matching Events")
        self.resize(500, 350)
        self.computed_offset = None

        layout = QtWidgets.QVBoxLayout(self)
        instructions = QtWidgets.QLabel(
            "<b>How to use:</b><br><br>"
            "1. Navigate the <b>video</b> to a recognizable event.<br>"
            "2. Click <b>'Mark Video Time'</b> to capture the position.<br>"
            "3. Enter the corresponding <b>data time</b> for the same event.<br>"
            "4. Click <b>'Compute Offset'</b>.<br><br>"
            "<i>offset = data_time - video_time</i>"
        )
        instructions.setWordWrap(True)
        layout.addWidget(instructions)
        layout.addSpacing(10)

        step1 = QtWidgets.QHBoxLayout()
        layout.addLayout(step1)
        step1.addWidget(QtWidgets.QLabel("Video event time (s):"))
        self.spin_video_t = QtWidgets.QDoubleSpinBox()
        self.spin_video_t.setRange(0.0, parent.video_duration)
        self.spin_video_t.setDecimals(2)
        self.spin_video_t.setValue(current_video_t)
        self.spin_video_t.setSuffix(" s")
        step1.addWidget(self.spin_video_t)
        btn_mark = QtWidgets.QPushButton("Mark Video Time (current)")
        btn_mark.clicked.connect(
            lambda: self.spin_video_t.setValue(parent._current_video_time())
        )
        step1.addWidget(btn_mark)

        step2 = QtWidgets.QHBoxLayout()
        layout.addLayout(step2)
        step2.addWidget(QtWidgets.QLabel("Data event time (s):"))
        self.spin_data_t = QtWidgets.QDoubleSpinBox()
        self.spin_data_t.setRange(0.0, data.duration)
        self.spin_data_t.setDecimals(2)
        self.spin_data_t.setValue(0.0)
        self.spin_data_t.setSuffix(" s")
        step2.addWidget(self.spin_data_t)

        layout.addSpacing(10)
        self.lbl_preview = QtWidgets.QLabel("")
        self.lbl_preview.setStyleSheet("font-weight: bold; font-size: 13px;")
        layout.addWidget(self.lbl_preview)
        self.spin_video_t.valueChanged.connect(self._update_preview)
        self.spin_data_t.valueChanged.connect(self._update_preview)

        layout.addSpacing(10)
        btn_row = QtWidgets.QHBoxLayout()
        layout.addLayout(btn_row)
        btn_compute = QtWidgets.QPushButton("Compute Offset && Apply")
        btn_compute.setDefault(True)
        btn_compute.clicked.connect(self._compute_and_accept)
        btn_row.addWidget(btn_compute)
        btn_cancel = QtWidgets.QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)

        self._update_preview()

    def _update_preview(self):
        offset = self.spin_data_t.value() - self.spin_video_t.value()
        self.lbl_preview.setText(f"Preview: offset = {offset:+.3f}s")

    def _compute_and_accept(self):
        self.computed_offset = self.spin_data_t.value() - self.spin_video_t.value()
        self.accept()


# ──────────────────────────────────────────────────────────────────────
# Data Loading Helper
# ──────────────────────────────────────────────────────────────────────

def load_data(data_path: str, dbc_paths: list[str] | None = None):
    """Load data from BLF, CSV, or Excel based on file extension."""
    ext = Path(data_path).suffix.lower()
    if ext == ".blf":
        return BLFData(data_path, dbc_paths)
    elif ext in (".csv", ".xlsx", ".xls"):
        return TabularData(data_path)
    else:
        raise ValueError(f"Unsupported data file format: {ext}")


def auto_discover_dbc(data_path: str) -> list[str] | None:
    """Look for a dbc/ folder next to the data file."""
    dbc_dir = Path(data_path).parent / "dbc"
    if dbc_dir.is_dir():
        found = sorted(dbc_dir.glob("*.dbc"))
        if found:
            paths = [str(f) for f in found]
            print(f"Auto-discovered {len(paths)} DBC file(s) in {dbc_dir}")
            return paths
    return None


# ──────────────────────────────────────────────────────────────────────
# CLI Entry Point
# ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Synchronized video + data playback tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  data_video_sync                                          (opens GUI file picker)
  data_video_sync video.mp4 data.blf
  data_video_sync video.mp4 data.csv
  data_video_sync video.mp4 data.blf --dbc battery.dbc
  data_video_sync video.mp4 data.blf --offset 2.5
  data_video_sync video.mp4 data.blf --no-dbc

Keyboard shortcuts:
  Space       Play / Pause
  Left/Right  Step ±1s
  Home/End    Jump to start / end
  , / .       Step ±1 frame
""",
    )
    parser.add_argument("video", nargs="?", help="Video file (.mp4, .avi, etc.)")
    parser.add_argument("data", nargs="?", help="Data file (.blf, .csv, .xlsx)")
    parser.add_argument("--dbc", nargs="+", help="DBC file(s) for CAN decoding")
    parser.add_argument("--no-dbc", action="store_true", help="Skip DBC loading")
    parser.add_argument("--offset", type=float, default=None, help="Manual time offset (seconds)")

    args = parser.parse_args()

    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")

    # If no positional args provided, show the GUI launcher
    if not args.video or not args.data:
        dlg = LauncherDialog()
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            sys.exit(0)
        video_path = dlg.video_path
        data_path = dlg.data_path
        dbc_paths = None if dlg.no_dbc else (dlg.dbc_paths or None)
        manual_offset = dlg.manual_offset
    else:
        video_path = args.video
        data_path = args.data
        manual_offset = args.offset

        if not Path(video_path).is_file():
            print(f"Error: Video file not found: {video_path}")
            sys.exit(1)
        if not Path(data_path).is_file():
            print(f"Error: Data file not found: {data_path}")
            sys.exit(1)

        if args.no_dbc:
            dbc_paths = None
        elif args.dbc:
            dbc_paths = args.dbc
        else:
            dbc_paths = None  # will auto-discover below

    # Auto-discover DBC if not set
    if dbc_paths is None and not (hasattr(args, "no_dbc") and args.no_dbc):
        dbc_paths = auto_discover_dbc(data_path)

    # Load data
    data = load_data(data_path, dbc_paths)

    # Compute sync offset
    sync_report = ""
    if manual_offset is not None:
        offset = manual_offset
        sync_report = f"Manual offset: {offset:+.3f}s"
    else:
        offset, sync_report = compute_auto_offset(data, video_path)

    player = SyncPlayer(video_path, data, offset, sync_report)
    player.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
