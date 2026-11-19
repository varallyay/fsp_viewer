# FSP Viewer

<img src="icon.png" width="96" alt="FSP Viewer icon: a black square on a yellow background">

**Version 0.2.3** — see [Version History](#version-history).

View Ansys Lumerical FDTD project files (`.fsp`), plot their results and save their geometry as GDS **without a Lumerical license**, on macOS and Linux, and on Windows as a ready-to-run program that needs no Python (see [Windows application](#windows-application)).

- `fsp_gui.py` — **FSP Viewer**, the application: four geometry views, a parameter inspector, result plots, geometry export, installation and updates.
- `fsp_reader.py` — reads `.fsp` files; a command-line inspector and a library.
- `fsp_export.py` — saves the geometry as a GDSII file or as a gdsfactory script.
- `fsp_plots.py` — writes a fixed set of result figures from the command line.
- `build_windows.py` — builds the Windows application (`requirements-windows.txt`, `.github/workflows/windows.yml`).

---

## Requirements

```bash
pip install numpy scipy h5py matplotlib
```

- **Python 3.10+** with Tk (included in the installers of python.org and in Anaconda; on Linux: `sudo apt install python3-tk`).
- **numpy** and **matplotlib** (with Pillow, which matplotlib brings along) are needed by the viewer; **h5py** to read monitor fields from `.h5` files; **scipy** for the overlap of fields with port modes.
- **gdsfactory** only to *run* a geometry script the viewer saved (`pip install gdsfactory`); saving it needs nothing.
- macOS, optional: **PyObjC** (`pip install pyobjc-framework-Cocoa`) shows *FSP Viewer* instead of *Python* in the menu bar.

---

## Starting the Viewer

```bash
python3 fsp_gui.py                       # an empty window; open a file with Open .fsp…
python3 fsp_gui.py model.fsp             # opens model.fsp at once
python3 fsp_gui.py model.fsp fields.h5   # ... with the monitor data of fields.h5
```

A file name given after `fsp_gui.py` is opened immediately. The monitor fields are looked up by themselves: `model.h5` next to `model.fsp`, the `.h5` file named inside the project, or `model_output.h5` (the name Lumerical gives it) next to the project or in the folder `model/`. A project without results names no field file, so a `model_output.h5` left beside it by an earlier run is not opened by itself; **Open .h5…** still opens it. Without an `.h5` file everything stored in the `.fsp` itself still works (geometry, settings, port modes, source pulse).

| Option | What it does |
| :--- | :--- |
| `--install` | Install the application for you, then exit |
| `--install-systemwide` | Install it for all users (Linux: with `sudo`), then exit |
| `--uninstall` | Remove the installed application, then exit |
| `--update` | Install a newer version from GitHub over the installed one, then exit |
| `--version` | Show the version number |

---

## Installing

Installed, the program starts like any other application — from Launchpad or the application menu, as **FSP Viewer** with its own icon (a black square on a yellow background) instead of *python*.

At the first start the viewer asks whether to install it: **systemwide**, **only for you**, or not at all. The same is available later in **Help > Install FSP Viewer…** and **Help > Uninstall…**, and from the terminal with the options above.

| | Systemwide | Only for you |
| :--- | :--- | :--- |
| **macOS** | `/Applications/FSP Viewer.app` | `~/Applications/FSP Viewer.app` |
| **Linux** | `/opt/fsp_viewer` + `/usr/local/share/applications/fsp_viewer.desktop` | `~/.local/share/fsp_viewer` + `~/.local/share/applications/fsp_viewer.desktop` |

- Installing copies `fsp_gui.py`, `fsp_reader.py` and `fsp_export.py` and writes a launcher that runs them with the Python interpreter that did the installation (the one that has numpy and matplotlib). Nothing is compiled.
- **macOS**: the bundle gets the icon with a transparent margin and rounded corners (`APP_ICON_MARGIN = 0.1`, `APP_ICON_ROUNDING = 0.20`), so it has the size of native icons in the Dock and in Launchpad. `.fsp` files can be opened with it from Finder (*Open With*).
- **Linux**: systemwide installation needs administrator rights: `sudo python3 fsp_gui.py --install-systemwide`. The menu entry opens a `.fsp` file passed to it. The installation also adds the terminal command `fsp_gui` (in `/usr/local/bin`, or `~/.local/bin` for a personal installation), which is the way to start it where there is no application menu, such as WSL: `fsp_gui model.fsp`.
- The folder you installed from can be moved or deleted afterwards. The file `~/.fsp_viewer.json` remembers that the question was answered, what is installed and when updates were last looked for.

### Updates from GitHub

An installed FSP Viewer looks for a new version on <https://github.com/varallyay/fsp_viewer> **once a week**, at the start, without holding up the window. If there is one, it shows the installed and the new version and offers **Update now**, **Not now** or **Skip this version**; after the update it offers to **restart**, and reopens the file that was open.

- **Help > Check for Updates…** looks at once and also says when the program is up to date.
- The checkbox *Look for new versions on GitHub every week* in these dialogs turns the weekly look off or on; `UPDATE_CHECK_DAYS` at the top of `fsp_gui.py` sets the interval.
- The newest GitHub release is used if the project has releases, otherwise the main branch. All program files are downloaded and checked first; unless every one arrives, nothing is changed.
- A copy that is not installed does not replace itself: it tells you that a newer version exists and opens the GitHub page. Started from a folder with a newer version than the installed one, it offers to update the installed copy from itself.
- `python3 fsp_gui.py --update` does the same from the terminal.

---

## The Viewer Window

### Toolbar
| Control | What it does |
| :--- | :--- |
| **Open .fsp…** (Ctrl/Cmd+O) | Open a Lumerical FDTD project |
| **Open .h5…** | Load or switch the monitor field file of the project |
| **Save Geometry ▾** | Save the displayed structures as a GDS file or as a gdsfactory script (see below) |
| **Port** | The port (or stand-alone mode monitor) the plots refer to; *All Ports* shows several side by side. It is also highlighted in the four views |
| **Result** | What to plot (see *Result plots*) |
| **λ** | The wavelength of single-wavelength field plots, from the frequencies of the monitors |
| **Plot** | Draw the selected result |
| **Export Data** | Copy the numbers of the current plot to the clipboard as a tab-separated table (for a spreadsheet) |

The window title names the open project, and the `.h5` field file in brackets when one is open: `FSP Viewer — model.fsp (model_output.h5)`.

### Status bar
The thin bar at the bottom says what the program is doing — opening a file, drawing the views, computing a plot (⏳, with a busy cursor) — and afterwards what came of it: the project loaded (structures, ports, monitors, time taken), the plot drawn, a file saved, data copied, or why a plot could not be made. On the right it shows the `.h5` field file that is open, or why there is none (not found, found but not opened because the project holds no results, or `h5py` missing).

### Geometry views
Four linked views of the model, with lengths in µm:

- **XY (top)**, **XZ (side)** and **YZ (cross-section)**: drag to pan, scroll to zoom, **Fit** resets the view; the position of the pointer is shown in the header. The XY view has **Zoom Core**, which stretches y around the waveguide so that narrow tapers become visible.
- **3D**: drag to rotate, right-drag to pan, scroll to zoom, **Reset view**; **Slabs** shows or hides the full-wafer layers (substrate, cladding). Badges give the size of the scene.
- **Structures**: layer-builder layers with their GDS polygons, and rectangle and polygon objects, also inside structure groups. Each material has a colour; a shape inside another one (an active layer in its cladding) is drawn on top. Disabled objects are not shown.
- **FDTD region**: the dashed orange box.
- **Ports**: yellow, with their name and an arrow for the direction; a click on a port selects it. A port that is disabled in the project is dimmed and marked *(off)*.

### Model Parameters Editor
A read-only inspector with line numbers and syntax colours:

- **Summary** — layers and their materials, GDS polygons, structures, the FDTD region (size, time, band, mesh, PML, symmetry), ports, mode source and monitors.
- **All Objects & Props** — every object of the model tree with all its properties and result arrays.
- **Materials** — the material database stored in the file.
- **Filter** shows only the lines that contain a text; **Copy Text** copies the page to the clipboard.

### Result plots
The plot is drawn in the large panel on the right, with the matplotlib toolbar (zoom, pan, save as image); **↗ Separate Window** opens it in a window of its own.

| Result | Needs `.h5` | What it shows |
| :--- | :---: | :--- |
| Cross-Sections & Port Modes (n & \|E\|²) | | Refractive index and mode profile stored for each port, with the effective index |
| Stored Port Mode \|E\|² Profile (YZ) | | The mode of the selected port |
| Stored Refractive Index n(y, z) (YZ) | | The index cross-section at the port |
| Simulated Field \|E\|² in YZ Planes | ✓ or in `.fsp` | The simulated field in the port and field-monitor planes at the chosen wavelength |
| Power Through YZ Planes vs x & λ | ✓ or in `.fsp` | Power through the port and monitor planes along the device, relative to the first one, and its spectrum |
| Power Spectrum at Selected Plane | ✓ or in `.fsp` | P / P_source of the selected port or monitor (all of them with *All Ports*) against λ, linear and dB (fields from an `.h5` file: P / P(first plane at the source), see below); dashed: the part in the stored mode of that plane (a port's own mode, or a mode expansion monitor at the same x) |
| S-Parameters from Power | ✓ or in `.fsp` | \|S_i1\|² = P_i / P_source of every plane, the mode-resolved \|S_i1\|², and \|S11\|² ≈ 1 − P(first plane after the source) / P_source (not for fields from an `.h5` file); magnitudes only (phases are not in the powers) |
| Power & Mode Coupling vs. Port x | ✓ | Power through each port and its overlap with the port mode |
| Port Transmission vs. Wavelength λ | ✓ | Transmission spectrum of the ports |
| XZ Propagation \|E\|² at y=0 (dB) | ✓ | The field along the device |
| Top-View Layout & Taper Zoom (XY) | | The layout, and the same with y stretched |
| Port Mode Field Components | | \|Ex\| … \|Hz\| of the stored mode |
| Source Time Signal & Spectrum | | The pulse of the source |

Besides the ports, the frequency-domain field monitors in y-z planes (e.g. `yz_input`, `yz_half_out`, … along a taper) are shown in cyan in the views and can be chosen in the **Port** list. Their fields are read from the `.h5` file or, when the project stores them itself, from the `.fsp`.

Large projects (many GB of field results) are not read into memory: the file is memory-mapped, and a result is read when a plot needs it — a field at one wavelength is read alone, not the whole spectrum.

Structure groups are drawn where they place their objects: the translation and rotation of a group (and the rotation of a rectangle or polygon itself) are applied, so a shifted or rotated coupon is shown as such.

### Saving the geometry
**Save Geometry ▾** (also in the File menu) saves the structures shown in the views in one of two formats:

- **GDS file (.gds)** — every outline as a GDSII polygon.
- **Python script for gdsfactory (.py)** — a script that rebuilds the layout and writes the same GDS when run. Each outline is cut into `gf.components.rectangle` and `gf.components.taper` pieces wherever these reproduce it exactly (a waveguide with tapered ends becomes rectangle + taper); only an outline that cannot be resolved this way, or whose taper widths are not an even number of nanometres, is written with `add_polygon`.

Shapes of a layer builder keep their GDS layer numbers, and its unpatterned slabs are left out. Rectangle and polygon objects get one GDS layer per material and z range, numbered from 1 in the order of the model tree; the script lists them with material and z range. Disabled objects are not saved. The same export runs without the viewer:

```bash
python fsp_export.py model.fsp model.gds            # GDSII
python fsp_export.py model.fsp model_geometry.py    # gdsfactory script
```

### Menus
- **File**: Open .fsp File…, Open .h5 Field File…, Save Geometry as GDS File…, Save Geometry as Python (gdsfactory) Script…, Export Plot Data to Clipboard, Quit.
- **Help**: About FSP Viewer, Check for Updates…, Install FSP Viewer…, Uninstall…, GitHub Page.

### Configuration
The parameters at the top of `fsp_gui.py`:

| Parameter | Default | Meaning |
| :--- | :--- | :--- |
| `APP_NAME` | `"FSP Viewer"` | Window title and name of the installed application |
| `APP_VERSION` | `"0.2.3"` | Version number; decides whether a copy on GitHub is newer |
| `APP_ICON_YELLOW`, `APP_ICON_BLACK` | `"#ffd700"`, `"#000000"` | Colours of the icon |
| `APP_ICON_SQUARE` | `0.5` | Side of the black square, as a fraction of the yellow field |
| `APP_ICON_MARGIN` | `0.1` | Transparent margin on each side, as a fraction of the icon size |
| `APP_ICON_ROUNDING` | `0.20` | Corner radius, as a fraction of the yellow field |
| `APP_FILES` | the three `.py` files | The files an installation copies and an update downloads |
| `CONFIG_FILE` | `"~/.fsp_viewer.json"` | Where the answers and the installation are remembered |
| `UPDATE_REPOSITORY` | `"varallyay/fsp_viewer"` | The GitHub project with the new versions |
| `UPDATE_CHECK_DAYS` | `7` | Days between the automatic looks for a new version; 0 turns them off |

The icon is drawn by the program; `icon.png` in this folder is a copy for this page.

---

## Windows application

On Windows FSP Viewer also runs **without Python and without installing anything**: a folder with `FSP Viewer.exe` and its libraries, for example on a simulation server.

1. Download `FSP-Viewer-<version>-windows.zip` from the [newest release](https://github.com/varallyay/fsp_viewer/releases/latest).
2. Unpack it anywhere (a local disk is faster than a network share). It holds the folder `FSP Viewer`.
3. Start `FSP Viewer.exe` in it. An `.fsp` file dropped on the `.exe` (or given in a shortcut / on the command line: `"FSP Viewer.exe" model.fsp`) is opened at once.

- Keep the folder together: `FSP Viewer.exe` needs the `_internal` folder next to it. A shortcut to the `.exe` can go on the desktop.
- Windows may warn at the first start (*Windows protected your PC*), because the program is not signed: **More info > Run anyway**.
- The program looks for a new version once a week; it cannot replace itself, so it offers the download page of the new release. Unpack the new `.zip` and delete the old folder.
- Messages and error reports go to `%USERPROFILE%\.fsp_viewer.log` (the program has no console window); settings to `%USERPROFILE%\.fsp_viewer.json`.

**How it is built.** The GitHub Actions workflow `.github/workflows/windows.yml` runs `build_windows.py` on a Windows machine of GitHub whenever a version tag is pushed (`git tag v0.2.3 && git push origin v0.2.3`; the tag must match `APP_VERSION`), and attaches the `.zip` to that release; started by hand (*Actions > Windows build > Run workflow*) it only keeps the `.zip` as a download of the run. `build_windows.py` packs the program with PyInstaller, gives the `.exe` its icon and version information, starts the packed program in a self-test (every library present, the window opens) and makes the `.zip`. To build on a Windows PC yourself:

```bat
python -m pip install -r requirements-windows.txt
python build_windows.py                 &REM  -> dist\FSP Viewer\ and dist\FSP-Viewer-<version>-windows.zip
python build_windows.py model.fsp       &REM  the self-test also opens model.fsp and draws every result
```

---

## Command-Line Tools

**Inspect a project file** (`fsp_reader.py`, needs only numpy):

```bash
python fsp_reader.py model.fsp                   # list all objects in the model tree
python fsp_reader.py model.fsp --summary         # readable settings: layers, GDS polygons, FDTD region, ports, monitors
python fsp_reader.py model.fsp --props FDTD      # every property of objects whose name contains "FDTD"
python fsp_reader.py model.fsp --materials       # list the embedded material database
python fsp_reader.py model.fsp --json out.json   # export everything (arrays over --max-elems are summarised)
python fsp_reader.py model.fsp --npz out.npz     # export every array at full size
python fsp_reader.py model.fsp --gds out.gds     # export layer-builder GDS polygons to GDSII
```

The GDS export writes the layer-builder polygons with their original GDS layer numbers. If the model has no layer builder, it exports the individual rectangle and polygon objects instead, one GDS layer per object, and prints a map of layer → object, material and z range. The script of any script-driven structure group is saved next to the GDS as `<name>_<group>.lsf`, because Lumerical regenerates that geometry from the script and does not store it. Units are a 1 µm user unit and a 1 nm database grid; the export needs nothing beyond numpy.

**Plot results** (`fsp_plots.py`, keep it in the same folder as `fsp_reader.py`):

```bash
python fsp_plots.py model.fsp model.h5 outdir
```

This writes five figures and `port_power_table.txt` to `outdir`: the top-view layout, cross-sections with port modes, the xz propagation in dB, the field profile in each port plane, and the power and port-mode coupling at each port. It is written for the edge-coupler model it was developed on (object names such as `Ridge_SSC`, `input_port`, `output_port2..7`, `dft_xz_propagation`), so other models will need small edits.

**Use it as a library:**

```python
from fsp_reader import FSPFile
f = FSPFile("model.fsp")
fdtd = f.get("::model::FDTD")
print(fdtd["MaxSimTime"], fdtd["xGrid"].shape)
print(f.get("::input_port DFT").results["f"])   # monitor frequencies
print(f.material_index("SSC PH18 TS-PE-SiN"))   # n of a constant-index material
```

## Where the data is

- **`.fsp`** holds the full model: object properties, the material database, GDS polygons, mesh, and port mode profiles (E, H and index). For DFT monitors it stores only coordinates and frequencies, **not** the field data.
- **`.h5`** holds the monitor fields. Each `MonitorN` group has `x`, `y`, `z` in µm and float32 arrays `Ex … Hz` shaped `(z, y, x, 2·n_freq)`, with the last axis ordered `[Re f1, Im f1, Re f2, Im f2, …]`. Monitor names and frequencies are not in the `.h5`; take them from the `.fsp`. The fields have no absolute power scale that matches the source power `P0` of the `.fsp`, so powers from them are given relative to the first plane at the source. Every frequency of a component is compressed together: the viewer reads a component whole, once, and keeps it (up to 512 MB in all).

## File format (reverse-engineered)

All integers are little-endian int32, and floats are float64.

| Part | Layout |
|---|---|
| Header | `LUMERICAL file version 1.1\0`, a few int32s, empty table of contents |
| Materials | `Lumerical material data file version 2.0\0`, count, then one property block per material |
| Property block | count, then records: name length (incl. `\0`), name, type, value |
| Record types | 0 string (len + bytes) · 1 double (count + f64) · 2 int (count + i32) · 3 double matrix · 5 int32 matrix |
| Matrix body | label length, label, n_elements, n_dims, dims…, is_real, unknown int, column-major data; **complex = all real parts, then all imaginary parts** |
| Object node | tag `1001`, length 38, `{uuid}`, property block, result count, named matrices, child count, children |

Nodes tagged `1000` (structures and layer-builder internals) start with a partly decoded serialised blob. Rectangles store their index and bounds as `ymin xmin ymax xmax zmin zmax`; polygons store a 2 × N vertex matrix followed by z min and z max; structure groups store their script as text. Record type 11 is a raw byte array (`int32 length + bytes`).

## Limitations

- There is no official specification. The format was decoded from one file saved by a recent Lumerical release, so other versions may differ.
- The reader is read-only; it cannot write `.fsp` files.
- Of the rotations of a structure group or shape, a single one (e.g. about z) is placed exactly; for several the order of the axes is inferred, not confirmed.
- Only rectangles and polygons are decoded; other primitives (circles, rings, waveguides, imported 3D objects) and analysis groups are listed but not exported.
- Check your Lumerical license terms before using this on files covered by them.

---

## Version History

### 0.2.3 — 2026-11-19
- **Status bar** at the bottom of the window: what is being done (opening, drawing, computing), its outcome and time, and the state of the `.h5` field file. The file names moved from the toolbar to the window title (long names pushed the Port and Result selectors out of the window).

### 0.2.2 — 2026-11-18
- **Freeze with `.h5` field files fixed** (e.g. a burst-mode run, `model/model_output.h5`): every frequency of a field component is compressed together, so reading one frequency unpacked the whole component. A spectrum took minutes, and closing the frozen window ended in *Fatal Python error: PyEval_RestoreThread*. Now a component is read once and kept, and the power spectrum and S-parameters take about 0.5 s per plane.
- **Power from `.h5` fields** is given relative to the first plane at the source (the `.h5` fields have another scale than the source power; the result was ~10⁴³). For the IMEC taper it agrees with the absolute values of the `.fsp` results within 0.3 % across the band.
- **Source power** now follows the spectrum of the source pulse: the plane at the source shows P / P_source ≈ 1 over the whole band instead of 0.93 at the band edges.
- An `.h5` group is only used for a port or monitor at the same x (a disabled port took the field of `yz_input`); *XZ Propagation* uses only an x-z plane; the older `.h5` plots skip ports that recorded nothing; *Power & Mode Coupling* shows at most 7 wavelengths; a port without a stored mode says so instead of `'yMatrix'`.

### 0.2.1 — 2026-11-18
- **Windows application** without Python: `build_windows.py` (PyInstaller) and the GitHub Actions workflow `.github/workflows/windows.yml`, which attaches `FSP-Viewer-<version>-windows.zip` to each release. The packed program is self-tested during the build (`--self-test`).
- A copy that cannot update itself (the Windows application, or a copy that is not installed) looks for new versions too and offers the download page of the new release.
- Windows: menus show Ctrl instead of Cmd, Ctrl+Q quits; Install / Uninstall are not offered where they are not supported; a packaged program writes its messages to `~/.fsp_viewer.log`.

### 0.2.0 — 2026-11-17
- **Large files**: a project of many GB is memory-mapped instead of read; results are read when a plot needs them, a field at one wavelength alone (a 10.7 GB project opens in under a second with about 0.4 GB of memory, instead of 23 GB).
- **Structure groups**: their children are decoded, and the translation and rotation of the groups (and the rotation of a shape) are applied, so a shifted or rotated InP coupon is shown, exported and summarised where it is.
- **Field monitors** in y-z planes (e.g. `yz_half_out`) are shown in cyan, can be chosen like ports, and their fields are read from the `.h5` or from the `.fsp`; mode expansion monitors with any name (e.g. `monitor_TE`) are shown as ports.
- **New results**: Power Through YZ Planes vs x & λ, Power Spectrum at Selected Plane (P / P_source, with the part in the stored mode), and S-Parameters from Power (\|S_i1\|², mode-resolved \|S_i1\|², \|S11\|² estimate).
- **3D view**: exact drawing order (BSP tree), see-through ports (stippled fills do not work on macOS), and background slabs shown as outlines.
- **2D views**: drawing is clipped to the axes; Zoom Core zooms on the waveguides; Shift / Ctrl+wheel zooms one axis; the header shows how much an axis is stretched; each waveguide layer has its own colour.
- **Startup**: the window comes to the front, also over a terminal in full screen.

### 0.1.0 — 2026-11-13
- **Version numbers** start here: `APP_VERSION` in `fsp_gui.py`, shown by `--version` and in Help > About.
- **Icon**: a black square on a yellow background, for the window, the Dock and the installed application.
- **Installing** on macOS and Linux, for all users or only for you: the program starts from Launchpad or the application menu as *FSP Viewer*, not as *python*. Asked at the first start; also Help > Install / Uninstall and `--install`, `--install-systemwide`, `--uninstall`.
- **Updates** from <https://github.com/varallyay/fsp_viewer>: looked for once a week, offered with Update now / Not now / Skip this version, followed by a restart that reopens the file; Help > Check for Updates and `--update`.
- **Opening a file from the command line**: `python3 fsp_gui.py model.fsp [fields.h5]`; a missing file is reported instead of ignored. The viewer no longer opens `my_simulation.fsp` by itself.
- **Save Geometry**: GDS file, or a gdsfactory script built from rectangles and tapers (`fsp_export.py`).
- **Views**: a shape inside another one is drawn on top; shapes show the material of the database instead of an unused index; disabled ports are dimmed and marked.

### 0.0.9
- The first published version: reader, plots and the viewer with four geometry views, the parameter editor and the result plots.
