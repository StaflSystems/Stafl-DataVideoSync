# Data Video Sync

Synchronized playback of time-series data and video for test review and analysis.

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

## What It Does

Plays a video file side-by-side with time-series data from a BLF (CAN log), CSV, or Excel file. A red cursor on the plot tracks the current video position so you can correlate physical events with sensor data.

**Supported data formats:**
| Format | Notes |
|--------|-------|
| `.blf` | Vector BLF CAN log. Optionally decoded with DBC files. |
| `.csv` | First column = time, remaining columns = signals. |
| `.xlsx` | Same layout as CSV. Requires `openpyxl`. |

## Quick Start

### Option A — Run from source

```
pip install python-can opencv-python PyQt5 matplotlib numpy
pip install cantools    # optional, for DBC decoding
pip install openpyxl    # optional, for Excel support
```

Double-click `data_video_sync.py` or run:

```
python data_video_sync.py
```

A file picker dialog will open. Select your video and data file, then click **Open**.

You can also pass files directly:

```
python data_video_sync.py video.mp4 data.blf
python data_video_sync.py video.mp4 data.csv
python data_video_sync.py video.mp4 data.blf --dbc signals.dbc
```

### Option B — Use the standalone .exe

Download `Data_Video_Sync.exe` from the latest release. No Python install needed — just run it.

```
Data_Video_Sync.exe
Data_Video_Sync.exe video.mp4 data.blf
```

## DBC Auto-Discovery

Place your `.dbc` files in a `dbc/` folder next to your BLF file:

```
my_test/
  recording.blf
  camera.mp4
  dbc/
    battery.dbc
    motor.dbc
```

They will be loaded automatically. Use `--no-dbc` to skip, or `--dbc file.dbc` to override.

## CSV / Excel Format

The first column is treated as time (seconds). All other columns are signal values. An optional second row can contain units (detected automatically if all values are non-numeric).

```csv
Time,Temperature,Voltage,Current
s,°C,V,A
0.0,25.1,3.65,0.0
0.1,25.2,3.64,1.2
0.2,25.5,3.63,1.3
```

## Controls

| Action | Control |
|--------|---------|
| Play / Pause | `Space` or ▶ button |
| Step ±1 second | `←` / `→` arrow keys |
| Step ±1 frame | `,` / `.` keys |
| Jump to start / end | `Home` / `End` |
| Adjust sync offset | Offset spinner ± buttons |
| Precise alignment | **Sync Wizard** button |

### Sync Wizard

If the auto-detected offset is wrong, use the Sync Wizard:

1. Navigate the video to a recognizable event (e.g., relay click, heater turn-on).
2. Click **Mark Video Time**.
3. Find the same event in the data plot and enter its time.
4. Click **Compute Offset & Apply**.

## Building the .exe

```
pip install pyinstaller
pyinstaller data_video_sync.spec
```

Output: `dist/Data_Video_Sync.exe`

## CLI Reference

```
data_video_sync.py [video] [data] [options]

positional arguments:
  video           Video file (.mp4, .avi, .mkv, etc.)
  data            Data file (.blf, .csv, .xlsx)

options:
  --dbc FILE...   DBC file(s) for CAN signal decoding
  --no-dbc        Skip DBC loading entirely
  --offset SEC    Manual time offset in seconds
```

If `video` and `data` are omitted, the GUI file picker is shown.

## License

MIT
