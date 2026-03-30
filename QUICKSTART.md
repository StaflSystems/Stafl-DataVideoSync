# Quick Start Guide

## 1. Get Your Files Ready

Put your test files in one folder:

```
my_test/
  camera.mp4          ← video file
  recording.blf       ← CAN log (or .csv / .xlsx)
  dbc/                ← optional folder
    signals.dbc       ← auto-detected if present
```

## 2. Run the Tool

Double-click **Data_Video_Sync.exe**. A file picker opens:

1. Click **Browse** next to "Video file" → select your `.mp4`
2. Click **Browse** next to "Data file" → select your `.blf`, `.csv`, or `.xlsx`
3. DBC files are auto-detected. Leave as-is unless you need to change them.
4. Click **Open**

## 3. Select Signals

In the left panel, check the signals you want to plot. Use the search box to filter by name.

## 4. Play & Review

| What you want to do | How |
|---|---|
| Play / Pause | Press **Space** |
| Step 1 second | **←** or **→** arrow keys |
| Step 1 frame | **,** or **.** keys |
| Jump to start or end | **Home** / **End** |
| Go to a specific time | Type in the "Go to" box and click **Go** |
| Change playback speed | Use the **Speed** dropdown |

## 5. Fix the Sync (if needed)

If the data and video don't line up:

- **Quick fix:** Use the **± buttons** next to "Sync offset" to nudge the alignment
- **Precise fix:** Click **Sync Wizard** →
  1. Pause the video at a recognizable event (e.g., a relay click or heater turning on)
  2. Click **Mark Video Time**
  3. Find the same event on the data plot and type its time
  4. Click **Compute Offset & Apply**

## Tips

- **DBC files** decode raw CAN data into named signals with units. Place them in a `dbc/` folder next to your BLF file and they load automatically.
- **CSV/Excel** files should have time in the first column and signal values in the remaining columns.
- Use **Separate Y-axis per signal** in Plot Settings when signals have very different scales.
- Use **Lock Y-range per signal** to keep the Y-axis steady while scrolling through time.
