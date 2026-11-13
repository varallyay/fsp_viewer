# fsp-tools

Read Ansys Lumerical FDTD project files (`.fsp`) and plot their results **without a Lumerical license**, on any OS (macOS, Linux, Windows).

## Usage

```bash
pip install numpy scipy h5py matplotlib
```

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

**View a model and save its geometry** (`fsp_gui.py`, with `fsp_export.py` in the same folder):

```bash
python fsp_gui.py model.fsp
```

The **Save Geometry** button (also in the File menu) saves the structures shown in the views in one of two formats:

- **GDS file (.gds)** — every outline as a GDSII polygon.
- **Python script for gdsfactory (.py)** — a script that rebuilds the layout and writes the same GDS when run. Each outline is cut into `gf.components.rectangle` and `gf.components.taper` pieces wherever these reproduce it exactly (a waveguide with tapered ends becomes rectangle + taper); only an outline that cannot be resolved this way, or whose taper widths are not an even number of nanometres, is written with `add_polygon`.

Shapes of a layer builder keep their GDS layer numbers, and its unpatterned slabs are left out. Rectangle and polygon objects get one GDS layer per material and z range, numbered from 1 in the order of the model tree; the script lists them with material and z range. Disabled objects are not saved. The same export runs without the GUI:

```bash
python fsp_export.py model.fsp model.gds            # GDSII
python fsp_export.py model.fsp model_geometry.py    # gdsfactory script
```

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
- **`.h5`** holds the monitor fields. Each `MonitorN` group has `x`, `y`, `z` in µm and float32 arrays `Ex … Hz` shaped `(z, y, x, 2·n_freq)`, with the last axis ordered `[Re f1, Im f1, Re f2, Im f2, …]`. Monitor names and frequencies are not in the `.h5`; take them from the `.fsp`.

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
- Structure-group transforms (offset, rotation) are not decoded, so shapes inside a moved or rotated group are exported in their stored, group-local coordinates.
- Only rectangles and polygons are decoded; other primitives (circles, rings, waveguides, imported 3D objects) and analysis groups are listed but not exported.
- Check your Lumerical license terms before using this on files covered by them.
