#!/usr/bin/env python3
"""
fsp_gui.py — Interactive 4-View 3D Model Visualizer, Parameter Inspector & Results Plotter
for Ansys Lumerical FDTD (.fsp / .h5) files.

Uses `fsp_reader.py` and the analysis routines from `fsp_plots.py` without modifying either file.

Layout:
  - 4-View Geometry Visualization Field:
      * Top-Left     : XY View (Top view)
      * Bottom-Left  : XZ View (Side view)
      * Top-Right    : 3D View (Interactive pointer rotation & zoom, as in 3D Geometry Generation)
      * Bottom-Right : YZ View (Cross-section view)
    Ports are highlighted in yellow across all four views.
  - Model Parameters Editor:
      * Editor-style inspector with line numbers, syntax highlighting, search filter, and view tabs
        (Summary, All Objects & Properties, Materials, Exported Data Preview).
  - Control Bar & Fifth Larger Plot Window:
      * Port Selection dropdown + Results Selection dropdown + Plot button + Export Data button
        (copies the currently selected and visualized dataset to the system clipboard).
      * Save Geometry button: writes the displayed structures as a GDSII file or as a Python
        script that rebuilds them with gdsfactory rectangles and tapers (see fsp_export.py).
      * Fifth larger plot window (docked large Matplotlib plot window + optional pop-out window).

Usage:
  python fsp_gui.py [my_simulation.fsp] [my_simulation.h5]
"""

import math
import os
import re
import struct
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np

try:
    import h5py
except ImportError:
    h5py = None

try:
    from scipy.interpolate import RegularGridInterpolator
except ImportError:
    RegularGridInterpolator = None

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.figure import Figure
from matplotlib.patches import Polygon as MplPolygon, Rectangle as MplRectangle

import fsp_export
from fsp_reader import C0, FSPFile, FormatError, _fmt, _um, summary as fsp_summary

os.environ.setdefault("TK_SILENCE_DEPRECATION", "1")

APP_NAME = "Lumerical FSP 3D Viewer & Results Analyzer"
__version__ = "1.0.0"

# Trapezoidal integration helper (NumPy 1.x / 2.x compatible, as in fsp_plots.py)
TRAPZ = np.trapezoid if hasattr(np, "trapezoid") else np.trapz

# ======================================================================
# Dark UI Theme (matching the Ansys Lumerical 3D Geom Generation project)
# ======================================================================
if sys.platform == "darwin":
    FONT_UI, FONT_MONO = "Helvetica Neue", "Menlo"
elif sys.platform.startswith("win"):
    FONT_UI, FONT_MONO = "Segoe UI", "Consolas"
else:
    FONT_UI, FONT_MONO = "DejaVu Sans", "DejaVu Sans Mono"

THEME = {
    "app_bg": "#0f1115",
    "bar_bg": "#161a20",
    "panel_bg": "#1a1e25",
    "border": "#2a303a",
    "plot_bg": "#12151a",
    "grid": "#1f242c",
    "grid_minor": "#181c22",
    "grid_axis": "#3a414d",
    "guide": "#2f3742",
    "text": "#e6e9ef",
    "text_muted": "#8b93a1",
    "text_dim": "#5c6370",
    "accent": "#4c8dff",
    "accent_hover": "#6aa1ff",
    "success": "#2fb37a",
    "success_hover": "#3fc78c",
    "warning": "#f0b429",
    "danger": "#ff6b6b",
    "btn_bg": "#242a33",
    "btn_hover": "#2f3642",
    "entry_bg": "#0f1216",
    "port_yellow": "#ffd700",       # Primary yellow for ports
    "port_yellow_bright": "#ffee55", # Highlighted / selected port yellow
    "port_yellow_fill": "#594b00",   # Dark translucent-like yellow fill for 2D/3D port planes
    "fdtd_orange": "#ff922b",
}

LAYER_PALETTE = [
    ("#c0392b", "#ff7675"),  # Core red / coral (SiN / waveguide core)
    ("#2b6cb0", "#74b9ff"),  # Trench / clad blue
    ("#2f9e6a", "#7ee2b0"),  # Green
    ("#8a5cd6", "#c7a8ff"),  # Purple
    ("#d9a21b", "#ffd166"),  # Gold / InP
    ("#1f9fb0", "#7fe0ec"),  # Teal
    ("#c24f9b", "#f5a3d6"),  # Pink
    ("#5c6b73", "#9db4c0"),  # Substrate / BOX slate
]


def mix_color(color, other, amount):
    """Blends two #rrggbb colors: amount 0 -> color, 1 -> other."""
    a = [int(color[i : i + 2], 16) for i in (1, 3, 5)]
    b = [int(other[i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{max(0, min(255, round(x + (y - x) * amount))):02x}" for x, y in zip(a, b))


def face_colors(base, kind):
    """(fill, outline) of a 3D face of the given kind, shaded from the base color."""
    if kind == "port":
        return "#e6c200", "#ffff66"
    if kind == "port_selected":
        return "#ffea00", "#ffffff"
    fill = {
        "top": mix_color(base, "#ffffff", 0.28),
        "side": base,
        "end": mix_color(base, "#000000", 0.14),
        "bottom": mix_color(base, "#000000", 0.28),
    }.get(kind, base)
    return fill, mix_color(base, "#ffffff", 0.22)


# ======================================================================
# 2D & 3D Geometry / BSP Helpers (from 3D Geometry Generation project)
# ======================================================================
VIEW3D_HALF_MAX = 1.35
VIEW3D_X_PER_SECTION = 2.25
VIEW3D_MAX_RADIUS = 1.5
VIEW3D_MIN_HALF = 0.04


def view_box_half_extents(dx, dy, dz):
    """Half extents (hx, hy, hz) of the 3D view box with schematic x-compression."""
    dx, dy, dz = (max(float(d), 1e-6) for d in (dx, dy, dz))
    section = max(dy, dz)
    lx = min(dx, VIEW3D_X_PER_SECTION * section)
    scale = VIEW3D_HALF_MAX / max(lx, section)
    half = [lx * scale, dy * scale, dz * scale]
    radius = math.sqrt(sum(h * h for h in half))
    if radius > VIEW3D_MAX_RADIUS:
        half = [h * VIEW3D_MAX_RADIUS / radius for h in half]
    hx, hy, hz = (max(h, VIEW3D_MIN_HALF) for h in half)
    return hx, hy, hz, dx / lx


def nice_tick_step(span, pixels, min_px):
    raw = abs(span) * min_px / max(float(pixels), 1.0)
    if not raw > 0 or math.isinf(raw):
        return 1.0
    mag = 10.0 ** math.floor(math.log10(raw))
    for m in (1, 2, 5, 10):
        if m * mag >= raw * (1 - 1e-12):
            return m * mag
    return 10 * mag


def minor_tick_step(step):
    lead = round(step / 10.0 ** math.floor(math.log10(step) + 1e-12))
    return step / (4 if lead == 2 else 5)


def format_tick(value, step):
    decimals = max(0, -math.floor(math.log10(step) + 1e-9))
    text = f"{round(value / step) * step:.{decimals}f}"
    return "0" if float(text) == 0 else text


def box_faces_3d(x0, x1, y0, y1, z0, z1, color, tag="solid"):
    """Creates 6 oriented quad faces for a 3D box [x0, x1] x [y0, y1] x [z0, z1]."""
    v000 = (x0, y0, z0)
    v100 = (x1, y0, z0)
    v110 = (x1, y1, z0)
    v010 = (x0, y1, z0)
    v001 = (x0, y0, z1)
    v101 = (x1, y0, z1)
    v111 = (x1, y1, z1)
    v011 = (x0, y1, z1)
    return [
        ([v001, v101, v111, v011], "top", color, tag),
        ([v000, v010, v110, v100], "bottom", color, tag),
        ([v000, v100, v101, v001], "side", color, tag),
        ([v110, v010, v011, v111], "side", color, tag),
        ([v010, v000, v001, v011], "end", color, tag),
        ([v100, v110, v111, v101], "end", color, tag),
    ]


def extruded_poly_faces_3d(xy_pts, z0, z1, color, tag="solid"):
    """Creates 3D faces for a 2D polygon extruded between z0 and z1."""
    pts = [(float(p[0]), float(p[1])) for p in xy_pts]
    while len(pts) > 1 and math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 1e-9:
        pts.pop()
    if len(pts) < 3:
        return []
    # Ensure counter-clockwise orientation
    area = 0.5 * sum(
        pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1]
        for i in range(len(pts))
    )
    if area < 0:
        pts = list(reversed(pts))

    bot = [(x, y, float(z0)) for x, y in pts]
    top = [(x, y, float(z1)) for x, y in pts]
    faces = [
        (top, "top", color, tag),
        (list(reversed(bot)), "bottom", color, tag),
    ]
    n = len(pts)
    for i in range(n):
        j = (i + 1) % n
        dx = pts[j][0] - pts[i][0]
        dy = pts[j][1] - pts[i][1]
        kind = "end" if abs(dy) > abs(dx) * 1.5 else "side"
        faces.append(([bot[i], bot[j], top[j], top[i]], kind, color, tag))
    return faces


# ======================================================================
# Scene & Data Extraction from FSPFile (and optional H5File)
# ======================================================================
_MATERIAL_UUID_RE = re.compile(rb"materialuuid\x00\x00\x00\x00\x00\x27\x00\x00\x00(\{[0-9a-f-]{36}\})")


def _material_uuid_after(chunk, pos):
    """The materialuuid property of the shape whose name ends at `pos` in a tag-1000 node."""
    m = _MATERIAL_UUID_RE.search(chunk, pos)
    return m.group(1).decode() if m else ""


def _decode_tag1000_shapes(fsp):
    """
    Recovers primitive Rectangles and 2D-extruded Polygons stored in tag-1000 nodes
    (used in models built from primitives/structure groups such as model.fsp).
    Does not modify fsp_reader.py.
    """
    shapes = []
    d = fsp.data

    matches = list(FSPFile.NODE_RE.finditer(d))
    for idx, m in enumerate(matches):
        o = m.start()
        tag = struct.unpack_from("<i", d, o)[0]
        if tag != 1000:
            continue
        next_o = matches[idx + 1].start() if idx + 1 < len(matches) else len(d)
        chunk = d[o:next_o]

        # Check if this node is a Structure Group (\x0d\x00\x00\x00\x12\x00\x00\x00 at 0x2e)
        sg_enabled = 1
        if chunk[0x2E : 0x36] == b"\x0d\x00\x00\x00\x12\x00\x00\x00":
            first_acf = re.search(rb"\x0b\x00\x00\x00acf_custom\x00", chunk)
            if first_acf and first_acf.start() >= 14:
                q = first_acf.start() - 4
                _, sg_enabled, _, _ = struct.unpack_from("<BiBi", chunk, q - 10)
            if not sg_enabled:
                continue

        # 1. Rectangles (\x06\x00\x00\x00\x20\x00\x00\x00)
        for rm in re.finditer(rb"\x06\x00\x00\x00\x20\x00\x00\x00", chunk):
            try:
                p0 = rm.start()
                vals = struct.unpack_from("<7d", chunk, p0 + 12)
                p = p0 + 68
                slen = struct.unpack_from("<i", chunk, p)[0]
                if not (0 < slen < 100):
                    continue
                p += 4
                idx_str = chunk[p : p + slen].rstrip(b"\0").decode("latin1")
                p += slen + 8
                nlen = struct.unpack_from("<i", chunk, p)[0]
                if not (0 < nlen < 200):
                    continue
                p += 4
                name = chunk[p : p + nlen].rstrip(b"\0").decode("latin1")
                p += nlen
                if name == "rectangle" and idx_str == "1.0":
                    continue
                dx, dy, dz = struct.unpack_from("<3d", chunk, p + 44)
                acf = re.search(rb"\x0d\x00\x00\x00CachedDecomp\x00|\x0b\x00\x00\x00acf_custom\x00", chunk[p:])
                enabled = 1
                if acf and acf.start() >= 14:
                    q = p + acf.start() - 4
                    _, enabled, _, _ = struct.unpack_from("<BiBi", chunk, q - 10)
                ymin, xmin, ymax, xmax, zmin, zmax = [v * 1e6 for v in vals[1:]]
                xmin += dx * 1e6
                xmax += dx * 1e6
                ymin += dy * 1e6
                ymax += dy * 1e6
                zmin += dz * 1e6
                zmax += dz * 1e6
                shapes.append({
                    "kind": "rect",
                    "name": name,
                    "enabled": bool(enabled),
                    "index_str": idx_str,
                    "material_uuid": _material_uuid_after(chunk, p),
                    "alpha": 0.5,
                    "xmin": min(xmin, xmax),
                    "xmax": max(xmin, xmax),
                    "ymin": min(ymin, ymax),
                    "ymax": max(ymin, ymax),
                    "zmin": min(zmin, zmax),
                    "zmax": max(zmin, zmax),
                    "xy_poly": [(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax)],
                })
            except Exception:
                continue

        # 2. Extruded Polygons (\x0b\x00\x00\x00\x16\x00\x00\x00\x00)
        for pm in re.finditer(rb"\x0b\x00\x00\x00\x16\x00\x00\x00\x00", chunk):
            try:
                p0 = pm.start()
                if chunk[p0 + 22] != 3:  # 1-byte matrix tag
                    continue
                p = p0 + 23
                lbl_len = struct.unpack_from("<i", chunk, p)[0]
                if not (0 <= lbl_len < 100):
                    continue
                p += 4 + lbl_len
                total, ndim = struct.unpack_from("<2i", chunk, p)
                if not (0 < total < 10000 and ndim == 2):
                    continue
                dims = struct.unpack_from("<2i", chunk, p + 8)
                p += 24
                pts = struct.unpack_from(f"<{total}d", chunk, p)
                p += 8 * total
                _, z1, _, z2 = struct.unpack_from("<BdBd", chunk, p)
                p += 18
                _, slen = struct.unpack_from("<Bi", chunk, p)
                p += 5
                idx_str = chunk[p : p + slen].rstrip(b"\0").decode("latin1")
                p += slen + 11
                nlen = struct.unpack_from("<i", chunk, p)[0]
                p += 4
                name = chunk[p : p + nlen].rstrip(b"\0").decode("latin1")
                p += nlen
                dx, dy, dz = struct.unpack_from("<3d", chunk, p + 44)
                # For polygons, check enabled flag right before the prop block
                acf = re.search(rb"\x0d\x00\x00\x00CachedDecomp\x00|\x0b\x00\x00\x00acf_custom\x00", chunk[p:])
                enabled = 1
                if acf and acf.start() >= 14:
                    q = p + acf.start() - 4
                    _, enabled, _, _ = struct.unpack_from("<BiBi", chunk, q - 10)
                n_verts = dims[1] if dims[0] == 2 else dims[0]
                poly = [
                    ((pts[2 * k] + dx) * 1e6, (pts[2 * k + 1] + dy) * 1e6)
                    for k in range(n_verts)
                ]
                xs = [pt[0] for pt in poly]
                ys = [pt[1] for pt in poly]
                zmin = (min(z1, z2) + dz) * 1e6
                zmax = (max(z1, z2) + dz) * 1e6
                shapes.append({
                    "kind": "poly",
                    "name": name,
                    "enabled": bool(enabled),
                    "index_str": idx_str,
                    "material_uuid": _material_uuid_after(chunk, p),
                    "alpha": 0.5,
                    "xmin": min(xs),
                    "xmax": max(xs),
                    "ymin": min(ys),
                    "ymax": max(ys),
                    "zmin": zmin,
                    "zmax": zmax,
                    "xy_poly": poly,
                })
            except Exception:
                continue

    return shapes


class FSPModelData:
    """Extracted geometry, ports, monitors, and plotting data for an FSPFile (+ optional H5)."""

    def __init__(self, fsp_path, h5_path=None):
        self.fsp_path = os.path.abspath(fsp_path)
        self.fsp = FSPFile(self.fsp_path)
        self.h5_path = None
        self.h5 = None

        # Auto-discover matching .h5 file if not explicitly provided
        if h5_path and os.path.isfile(h5_path):
            self.load_h5(h5_path)
        else:
            stem = os.path.splitext(self.fsp_path)[0]
            candidate = stem + ".h5"
            if os.path.isfile(candidate):
                self.load_h5(candidate)
            else:
                # Also check HDF5DataFile property in DFT monitors
                for ob in self.fsp.objects:
                    h5_rel = ob.props.get("HDF5DataFile")
                    if isinstance(h5_rel, str) and h5_rel:
                        cand2 = os.path.join(os.path.dirname(self.fsp_path), os.path.basename(h5_rel))
                        if os.path.isfile(cand2):
                            self.load_h5(cand2)
                            break

        self.fdtd_box = self._extract_fdtd_box()
        self.ports = self._extract_ports()
        self.mode_monitors = self._extract_mode_monitors()
        self.dft_monitors = self._extract_dft_monitors()
        self.structures = self._extract_structures()
        self._power_cache = None

    def load_h5(self, h5_path):
        if h5py is None:
            return
        try:
            if self.h5 is not None:
                self.h5.close()
        except Exception:
            pass
        self.h5_path = os.path.abspath(h5_path)
        self.h5 = h5py.File(self.h5_path, "r")
        self._power_cache = None

    def _extract_fdtd_box(self):
        try:
            p = self.fsp.get("::model::FDTD").props
            x0 = (p["GUIx"] - p["GUIwidth"] / 2.0) * 1e6
            x1 = (p["GUIx"] + p["GUIwidth"] / 2.0) * 1e6
            y0 = (p["GUIy"] - p["GUIheight"] / 2.0) * 1e6
            y1 = (p["GUIy"] + p["GUIheight"] / 2.0) * 1e6
            z0 = p["GUIz1"] * 1e6
            z1 = p["GUIz2"] * 1e6
            return (x0, x1, y0, y1, z0, z1)
        except Exception:
            return None

    def _extract_ports(self):
        ports = []
        for idx, ob in enumerate(self.fsp.objects):
            if "portXcoord" not in ob.props:
                continue
            p = ob.props
            short_name = ob.name.split("::")[-1]
            x = float(p.get("portXcoord", 0.0)) * 1e6
            y = float(p.get("portYcoord", 0.0)) * 1e6
            z = float(p.get("portZcoord", 0.0)) * 1e6
            dx = float(p.get("portXspan", 0.0)) * 1e6
            dy = float(p.get("portYspan", 0.0)) * 1e6
            dz = float(p.get("portZspan", 0.0)) * 1e6

            # Find associated DFT monitor and Mode Expansion Monitor
            dft_obj = None
            mode_exp_obj = None
            mode_src_obj = None
            for other in self.fsp.objects:
                if other.name == f"::{short_name} DFT":
                    dft_obj = other
                elif other.name == f"::{short_name} Mode Expansion Monitor":
                    mode_exp_obj = other
                elif other.name == f"::{short_name} Mode Source":
                    mode_src_obj = other

            h5_group = None
            if dft_obj is not None:
                h5_group = dft_obj.props.get("HDF5DataGroup")
            if not h5_group:
                h5_group = f"Monitor{len(ports)}"

            ports.append({
                "name": short_name,
                "full_name": ob.name,
                "index": len(ports),
                "x": x,
                "y": y,
                "z": z,
                "xspan": dx,
                "yspan": dy,
                "zspan": dz,
                "xmin": x - dx / 2.0,
                "xmax": x + dx / 2.0,
                "ymin": y - dy / 2.0,
                "ymax": y + dy / 2.0,
                "zmin": z - dz / 2.0,
                "zmax": z + dz / 2.0,
                "direction": "+" if p.get("portInjectionDirection", 1) else "-",
                "is_source": bool(p.get("isSourcePort", 0)),
                "enabled": bool(p.get("enabled", 1)),
                "h5_group": h5_group,
                "dft_obj": dft_obj,
                "mode_exp_obj": mode_exp_obj,
                "mode_src_obj": mode_src_obj,
            })
        return ports

    def _extract_mode_monitors(self):
        """Standalone mode expansion monitors (e.g. ::model::monitor, ::model::monitor_TM)."""
        mons = []
        port_exp_names = {f"::{pt['name']} Mode Expansion Monitor" for pt in self.ports}
        for ob in self.fsp.objects:
            if ob.name in port_exp_names:
                continue
            if "freqEHnx0" in ob.props and "yMatrix" in ob.props and "zMatrix" in ob.props:
                p = ob.props
                short = ob.name.split("::")[-1]
                x = float(p.get("left", 0.0)) * 1e6
                ym = p["yMatrix"].ravel() * 1e6
                zm = p["zMatrix"].ravel() * 1e6
                mons.append({
                    "name": short,
                    "full_name": ob.name,
                    "x": x,
                    "y": 0.5 * (ym[0] + ym[-1]),
                    "z": 0.5 * (zm[0] + zm[-1]),
                    "ymin": float(ym[0]),
                    "ymax": float(ym[-1]),
                    "zmin": float(zm[0]),
                    "zmax": float(zm[-1]),
                    "obj": ob,
                })
        return mons

    def _extract_dft_monitors(self):
        mons = {}
        for ob in self.fsp.objects:
            if "monitorShape" in ob.props and not ob.name.startswith("::input") and not ob.name.startswith("::output") and not ob.name.startswith("::port"):
                short = ob.name.split("::")[-1]
                h5_grp = ob.props.get("HDF5DataGroup")
                if not h5_grp and short == "dft_xz_propagation":
                    h5_grp = "Monitor7"
                mons[short] = {"obj": ob, "h5_group": h5_grp}
        return mons

    def _extract_structures(self):
        """
        Extracts 2D/3D structure items from both Layer Builder objects (e.g. Ridge_SSC)
        and primitive/structure-group objects (tag 1000 rectangles/polygons).
        """
        items = []

        # 1. Layer Builder objects
        for ob in self.fsp.objects:
            if "numProcessLayers" not in ob.props:
                continue
            p = ob.props
            cx = float(p.get("x", 0.0)) * 1e6
            cy = float(p.get("y", 0.0)) * 1e6
            xspan = float(p.get("xSpan", 100e-6)) * 1e6
            yspan = float(p.get("ySpan", 50e-6)) * 1e6
            bx0, bx1 = cx - xspan / 2.0, cx + xspan / 2.0
            by0, by1 = cy - yspan / 2.0, cy + yspan / 2.0

            # Collect GDS polygons by layer string (e.g. "30:0", "510:0", "191:901")
            gds_polys = {}
            for i in range(p.get("GDSLayerCount", 0)):
                lay_num = p.get(f"GDSLayerNumber_{i}")
                cnt = p.get(f"GDSLayerObjectCount_{i}", 0)
                polys = []
                for j in range(cnt):
                    arr = p.get(f"GDSLayerObject_{i}_{j}")
                    if isinstance(arr, np.ndarray) and arr.ndim == 2 and arr.shape[1] == 2:
                        pts = [(float(x) * 1e6, float(y) * 1e6) for x, y in arr]
                        # Ignore degenerate 0-area polygons like (0,0)...(0,0)
                        xs = [pt[0] for pt in pts]
                        ys = [pt[1] for pt in pts]
                        if max(xs) - min(xs) > 1e-6 and max(ys) - min(ys) > 1e-6:
                            polys.append(pts)
                if lay_num and polys:
                    gds_polys[lay_num] = polys

            # Process layers
            for i in range(p["numProcessLayers"]):
                k = f"{i}_process_layer"
                lname = p.get(k + "name", f"layer_{i}")
                z0 = float(p.get(k + "startPosition", 0.0)) * 1e6
                t = float(p.get(k + "thickness", 0.0)) * 1e6
                z1 = z0 + t
                bg_mat = p.get(k + "material") or "-"
                pat_mat = p.get(k + "patternMaterial") or ""
                pat_lay = p.get(k + "patternLayerName") or ""

                n_bg = None
                if bg_mat != "-":
                    try:
                        idx_arr = self.fsp.material_index(bg_mat)
                        if idx_arr is not None:
                            n_bg = float(idx_arr[0].real)
                    except Exception:
                        pass
                n_pat = None
                if pat_mat:
                    try:
                        idx_arr = self.fsp.material_index(pat_mat)
                        if idx_arr is not None:
                            n_pat = float(idx_arr[0].real)
                    except Exception:
                        pass

                if pat_lay and pat_lay in gds_polys:
                    # Patterned waveguide core or trench layer
                    is_core = "core" in lname.lower() or "sin" in lname.lower() or (n_pat and n_pat > 1.6)
                    fill_c, edge_c = ("#c0392b", "#ff7675") if is_core else ("#3d7bd9", "#8fbfff")
                    for p_idx, poly in enumerate(gds_polys[pat_lay]):
                        xs = [pt[0] for pt in poly]
                        ys = [pt[1] for pt in poly]
                        items.append({
                            "name": f"{lname} ({pat_lay})" if len(gds_polys[pat_lay]) == 1 else f"{lname} ({pat_lay}) #{p_idx+1}",
                            "layer_name": lname,
                            "gds_layer": pat_lay,
                            "material": pat_mat,
                            "n": n_pat,
                            "is_background": False,
                            "is_core": is_core,
                            "xy_poly": poly,
                            "xmin": min(xs),
                            "xmax": max(xs),
                            "ymin": min(ys),
                            "ymax": max(ys),
                            "zmin": min(z0, z1),
                            "zmax": max(z0, z1),
                            "fill": fill_c,
                            "edge": edge_c,
                        })
                elif bg_mat != "-":
                    # Background slab layer (Si_Substrate, BOX, SiO2_under, CLAD)
                    slab_colors = {
                        "Si_Substrate": ("#2b303a", "#5c6470"),
                        "BOX": ("#253342", "#4a6582"),
                        "SiO2_under": ("#2c3e50", "#5d7b93"),
                        "CLAD": ("#1f3347", "#486b8a"),
                    }
                    fill_c, edge_c = slab_colors.get(lname, ("#28303d", "#526075"))
                    items.append({
                        "name": lname,
                        "layer_name": lname,
                        "gds_layer": "",
                        "material": bg_mat,
                        "n": n_bg,
                        "is_background": True,
                        "is_core": False,
                        "xy_poly": [(bx0, by0), (bx1, by0), (bx1, by1), (bx0, by1)],
                        "xmin": bx0,
                        "xmax": bx1,
                        "ymin": by0,
                        "ymax": by1,
                        "zmin": min(z0, z1),
                        "zmax": max(z0, z1),
                        "fill": fill_c,
                        "edge": edge_c,
                    })

            # Also add any unreferenced GDS layers (like 191:901 oxide ridge outline) as a guide/ridge
            referenced = {
                p.get(f"{i}_process_layerpatternLayerName")
                for i in range(p["numProcessLayers"])
            }
            for lay_num, polys in gds_polys.items():
                if lay_num not in referenced:
                    for poly in polys:
                        xs = [pt[0] for pt in poly]
                        ys = [pt[1] for pt in poly]
                        items.append({
                            "name": f"GDS {lay_num}",
                            "layer_name": f"GDS {lay_num}",
                            "gds_layer": lay_num,
                            "material": "GDS Region",
                            "n": None,
                            "is_background": True,
                            "is_core": False,
                            "xy_poly": poly,
                            "xmin": min(xs),
                            "xmax": max(xs),
                            "ymin": min(ys),
                            "ymax": max(ys),
                            "zmin": 0.0,
                            "zmax": 0.37,
                            "fill": "#2d3b36",
                            "edge": "#588173",
                        })

        # 2. If no Layer Builder structures were found, decode tag-1000 primitive shapes
        if not items:
            raw_shapes = _decode_tag1000_shapes(self.fsp)
            by_uuid = {m.get("materialuuid"): m.get("name") for m in self.fsp.materials}
            core_idx = 0
            for sh in raw_shapes:
                if not sh["enabled"]:
                    continue
                name = sh["name"]
                span_y = sh["ymax"] - sh["ymin"]
                span_z = sh["zmax"] - sh["zmin"]
                is_bg = name.lower() in ("box", "bcb", "sio2_layer", "si substrate", "substrate") or (
                    span_y >= 20.0 and span_z >= 2.0
                )
                if is_bg:
                    fill_c, edge_c = ("#253342", "#4a6582")
                else:
                    fill_c, edge_c = LAYER_PALETTE[core_idx % len(LAYER_PALETTE)]
                    core_idx += 1
                # A shape uses a material of the database, or else its own index ("object defined
                # dielectric"); the index field is still stored, but unused, in the first case.
                material = by_uuid.get(sh.get("material_uuid"))
                n_val = None
                if material:
                    try:
                        idx_arr = self.fsp.material_index(material)
                        if idx_arr is not None:
                            n_val = float(idx_arr[0].real)
                    except Exception:
                        pass
                else:
                    material = f"n={sh['index_str']}"
                    try:
                        n_val = float(sh["index_str"])
                    except Exception:
                        pass
                items.append({
                    "name": name,
                    "layer_name": name,
                    "gds_layer": "",
                    "material": material,
                    "n": n_val,
                    "is_background": is_bg,
                    "is_core": not is_bg,
                    "xy_poly": sh["xy_poly"],
                    "xmin": sh["xmin"],
                    "xmax": sh["xmax"],
                    "ymin": sh["ymin"],
                    "ymax": sh["ymax"],
                    "zmin": sh["zmin"],
                    "zmax": sh["zmax"],
                    "fill": fill_c,
                    "edge": edge_c,
                })

        return items

    # ------------------------------------------------------------------
    # Results & Plotting Data Methods (from fsp_plots.py & fsp_reader.py)
    # ------------------------------------------------------------------
    def get_frequencies_and_wavelengths(self):
        """Returns (freqs_hz, wavelengths_nm, center_idx) from DFT monitor or FDTD settings."""
        for pt in self.ports:
            dft = pt.get("dft_obj")
            if dft is not None and "f" in dft.results:
                freqs = dft.results["f"].ravel()
                lam = C0 / freqs * 1e9
                kc = int(np.argmin(np.abs(lam - 1310.0)))
                return freqs, lam, kc
        for mon_info in self.dft_monitors.values():
            ob = mon_info["obj"]
            if "f" in ob.results:
                freqs = ob.results["f"].ravel()
                lam = C0 / freqs * 1e9
                kc = int(np.argmin(np.abs(lam - 1310.0)))
                return freqs, lam, kc
            fr = ob.props.get("frequency")
            if isinstance(fr, np.ndarray) and fr.size > 0:
                freqs = fr.ravel()
                lam = C0 / freqs * 1e9
                kc = int(np.argmin(np.abs(lam - 1310.0)))
                return freqs, lam, kc
        try:
            fd = self.fsp.get("::model::FDTD").props
            f1, f2 = fd["fStart"], fd["fEnd"]
            freqs = np.linspace(f1, f2, 5)
            lam = C0 / freqs * 1e9
            kc = int(np.argmin(np.abs(lam - 1310.0)))
            return freqs, lam, kc
        except Exception:
            freqs = np.array([C0 / 1310e-9])
            return freqs, np.array([1310.0]), 0

    def stored_mode(self, port_or_mon_name):
        """
        Extracts stored cross-section refractive index n(z, y) and mode fields Ex..Hz(z, y)
        from `::<port> Mode Expansion Monitor` (or standalone mode monitor), just like
        `stored_mode(port)` in `fsp_plots.py`.
        """
        target_ob = None
        for pt in self.ports:
            if pt["name"] == port_or_mon_name and pt["mode_exp_obj"] is not None:
                target_ob = pt["mode_exp_obj"]
                break
        if target_ob is None:
            for mm in self.mode_monitors:
                if mm["name"] == port_or_mon_name:
                    target_ob = mm["obj"]
                    break
        if target_ob is None:
            target_ob = self.fsp.get(f"::{port_or_mon_name} Mode Expansion Monitor")

        p = target_ob.props
        y = p["yMatrix"].ravel() * 1e6
        z = p["zMatrix"].ravel() * 1e6

        # Determine indices for E, H, and refractive index from dataEHnName*
        data_count = int(p.get("dataCount", 3))
        idx_e, idx_h, idx_n = 0, 1, 2
        for i in range(data_count):
            nm = str(p.get(f"dataEHnName{i}", ""))
            if nm.startswith("E") and idx_e == 0:
                idx_e = i
            elif nm.startswith("H") and idx_h == 1:
                idx_h = i
            elif nm == "index":
                idx_n = i

        g = lambda c, i: p[f"freqEHn{c}{i}"][0, :, :, 0].T  # shape (Nz, Ny)
        M = {f"E{c}": g(c, idx_e) for c in "xyz"} | {f"H{c}": g(c, idx_h) for c in "xyz"}
        M["n"] = np.abs(g("x", idx_n).real)
        neff_arr = p.get("neff")
        neff = float(neff_arr.ravel()[0].real) if isinstance(neff_arr, np.ndarray) and neff_arr.size else None
        mode_freq = p.get("modeFrequency")
        mode_lam_nm = (C0 / mode_freq * 1e9) if mode_freq else None
        return y, z, M, neff, mode_lam_nm

    def h5_field(self, mon_group, comp, k):
        """Reads complex field component `comp` at frequency index `k` from H5 group `mon_group`."""
        if self.h5 is None or mon_group not in self.h5:
            raise KeyError(f"HDF5 group '{mon_group}' not available (open the matching .h5 file).")
        a = self.h5[mon_group][comp]
        return a[..., 2 * k] + 1j * a[..., 2 * k + 1]

    def compute_power_and_mode_coupling(self):
        """
        Computes `P`, `Prel`, `eta`, `Tmode`, `xs`, `freqs`, `lam` across all ports and frequencies
        using the exact overlap-integral equations from `fsp_plots.py` (section 5).
        """
        if self._power_cache is not None:
            return self._power_cache
        if self.h5 is None:
            raise RuntimeError("No .h5 field file is loaded.")
        if RegularGridInterpolator is None:
            raise RuntimeError("scipy is required for mode overlap interpolation.")

        freqs, lam, _ = self.get_frequencies_and_wavelengths()
        n_ports = len(self.ports)
        n_freqs = len(freqs)
        P = np.zeros((n_ports, n_freqs))
        eta = np.zeros_like(P)
        xs = np.zeros(n_ports)

        def integ(F2, y_arr, z_arr):
            return TRAPZ(TRAPZ(F2, y_arr, axis=1), z_arr, axis=0)

        for i, pt in enumerate(self.ports):
            m = pt["h5_group"]
            if m not in self.h5:
                raise RuntimeError(f"HDF5 group '{m}' for port '{pt['name']}' not found in .h5 file.")
            ys, zs, M, _, _ = self.stored_mode(pt["name"])
            keep = ys >= -1e-9
            ys = ys[keep]
            M = {k: v[:, keep] for k, v in M.items()}

            y = self.h5[m]["y"][()]
            z = self.h5[m]["z"][()]
            xs[i] = float(self.h5[m]["x"][0])

            for k in range(n_freqs):
                E = {c: self.h5_field(m, c, k)[:, :, 0] for c in ("Ey", "Ez", "Hy", "Hz")}
                P[i, k] = integ(np.real(E["Ey"] * np.conj(E["Hz"]) - E["Ez"] * np.conj(E["Hy"])), y, z)
                Z, Y = np.meshgrid(np.clip(zs, z[0], z[-1]), np.clip(ys, y[0], y[-1]), indexing="ij")
                pts = np.stack([Z, Y], -1)
                Ei = {
                    c: RegularGridInterpolator((z, y), v.real)(pts)
                    + 1j * RegularGridInterpolator((z, y), v.imag)(pts)
                    for c, v in E.items()
                }
                a = integ(Ei["Ey"] * np.conj(M["Hz"]) - Ei["Ez"] * np.conj(M["Hy"]), ys, zs)
                b = integ(M["Ey"] * np.conj(Ei["Hz"]) - M["Ez"] * np.conj(Ei["Hy"]), ys, zs)
                c = integ(np.real(M["Ey"] * np.conj(M["Hz"]) - M["Ez"] * np.conj(M["Hy"])), ys, zs)
                d = integ(np.real(Ei["Ey"] * np.conj(Ei["Hz"]) - Ei["Ez"] * np.conj(Ei["Hy"])), ys, zs)
                eta[i, k] = np.real(a * b / c) / d

        Prel = P / P[0]
        Tmode = Prel * eta
        self._power_cache = (xs, freqs, lam, P, Prel, eta, Tmode)
        return self._power_cache


# ======================================================================
# Reusable UI Widgets (matching 3D Geometry Generation project)
# ======================================================================
class FlatButton(tk.Label):
    """Flat, theme-able button (tk.Button ignores colors on macOS)."""

    VARIANTS = {
        "primary": ("accent", "accent_hover", "#ffffff"),
        "success": ("success", "success_hover", "#ffffff"),
        "secondary": ("btn_bg", "btn_hover", "text"),
        "ghost": ("panel_bg", "btn_hover", "text_muted"),
        "warning": ("warning", "#ffd166", "#0f1115"),
    }

    def __init__(self, parent, text, command=None, variant="secondary", size=10, bold=False, padx=11, pady=5, tooltip=None):
        bg, hover, fg = (THEME.get(c, c) for c in self.VARIANTS[variant])
        if variant == "ghost":
            bg = parent.cget("bg")
        super().__init__(
            parent,
            text=text,
            bg=bg,
            fg=fg,
            font=(FONT_UI, size, "bold" if bold else "normal"),
            padx=padx,
            pady=pady,
            cursor="hand2",
        )
        self._bg, self._hover, self._fg = bg, hover, fg
        self._variant = variant
        self.command = command
        self.bind("<Enter>", lambda e: self.config(bg=self._hover, fg="#ffffff" if variant == "ghost" else self._fg))
        self.bind("<Leave>", lambda e: self.config(bg=self._bg, fg=self._fg))
        self.bind("<ButtonRelease-1>", self._on_click)
        if tooltip:
            Tooltip(self, tooltip)

    def _on_click(self, event):
        if 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height() and self.command:
            self.command()


class Tooltip:
    """Minimal hover tooltip."""

    def __init__(self, widget, text, delay_ms=450):
        self.widget, self.text, self.delay_ms = widget, text, delay_ms
        self._job = None
        self._tip = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None):
        self._cancel()
        self._job = self.widget.after(self.delay_ms, self._show)

    def _cancel(self):
        if self._job is not None:
            self.widget.after_cancel(self._job)
            self._job = None

    def _show(self):
        if self._tip is not None:
            return
        x = self.widget.winfo_rootx()
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self._tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(
            tw,
            text=self.text,
            justify=tk.LEFT,
            bg="#262c36",
            fg=THEME["text"],
            font=(FONT_UI, 10),
            padx=8,
            pady=5,
            highlightthickness=1,
            highlightbackground=THEME["border"],
        ).pack()

    def _hide(self, _event=None):
        self._cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


def apply_ttk_dark_style(root):
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure(
        "Dark.TCombobox",
        fieldbackground=THEME["entry_bg"],
        background=THEME["btn_bg"],
        foreground=THEME["text"],
        arrowcolor=THEME["text_muted"],
        bordercolor=THEME["border"],
        lightcolor=THEME["entry_bg"],
        darkcolor=THEME["entry_bg"],
        selectbackground=THEME["entry_bg"],
        selectforeground=THEME["text"],
        padding=4,
    )
    style.map(
        "Dark.TCombobox",
        fieldbackground=[("readonly", THEME["entry_bg"])],
        foreground=[("readonly", THEME["text"])],
        bordercolor=[("focus", THEME["accent"])],
        background=[("active", THEME["btn_hover"])],
    )
    root.option_add("*TCombobox*Listbox.background", THEME["bar_bg"])
    root.option_add("*TCombobox*Listbox.foreground", THEME["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", THEME["accent"])
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
    root.option_add("*TCombobox*Listbox.font", (FONT_UI, 11))


def _wheel_event(event, direction):
    event.delta = direction
    return event


# ======================================================================
# 2D Orthographic Model View Canvas (XY, XZ, YZ)
# ======================================================================
class ModelOrthoCanvas2D(tk.Frame):
    """
    Interactive 2D orthographic view (XY, XZ, or YZ) showing:
      - Model structures (layers, GDS polygons, waveguide tapers, trenches)
      - FDTD simulation region boundary
      - Ports highlighted in YELLOW (with labels and propagation arrows)
    Supports pointer pan (left-drag), scroll-wheel zoom, Y-exaggeration toggle, and Fit view.
    """

    def __init__(self, parent, view_kind, title, u_label, v_label, on_port_clicked=None):
        super().__init__(parent, bg=THEME["panel_bg"], highlightthickness=1, highlightbackground=THEME["border"])
        self.view_kind = view_kind  # "xy", "xz", or "yz"
        self.title = title
        self.u_label = u_label
        self.v_label = v_label
        self.on_port_clicked = on_port_clicked

        self.default_u_range = [-35.0, 230.0] if view_kind in ("xy", "xz") else [-12.0, 12.0]
        self.default_v_range = [-26.0, 26.0] if view_kind == "xy" else [-6.5, 6.5]
        self.u_range = list(self.default_u_range)
        self.v_range = list(self.default_v_range)

        self.model = None
        self.selected_port = None
        self.exaggerate_v = False
        self._pan_prev = None
        self._port_hit_boxes = []

        # Header bar
        header = tk.Frame(self, bg=THEME["panel_bg"])
        header.pack(fill=tk.X, side=tk.TOP)

        tk.Label(
            header,
            text=self.title,
            font=(FONT_UI, 11, "bold"),
            bg=THEME["panel_bg"],
            fg=THEME["text"],
            anchor="w",
        ).pack(side=tk.LEFT, padx=(10, 6), pady=4)

        self.info_lbl = tk.Label(
            header,
            text="no model",
            font=(FONT_UI, 9),
            bg=THEME["btn_bg"],
            fg=THEME["text_dim"],
            padx=6,
            pady=1,
        )
        self.info_lbl.pack(side=tk.LEFT, padx=(0, 6))

        self.coord_lbl = tk.Label(
            header,
            text="",
            anchor="w",
            font=(FONT_MONO, 9),
            bg=THEME["panel_bg"],
            fg=THEME["text_muted"],
        )

        FlatButton(
            header,
            "Fit",
            self.reset_view,
            variant="ghost",
            size=9,
            padx=6,
            pady=2,
            tooltip="Reset zoom and pan to fit the model",
        ).pack(side=tk.RIGHT, padx=(0, 6), pady=3)

        if view_kind == "xy":
            self.btn_zoom_core = FlatButton(
                header,
                "Zoom Core",
                self.toggle_core_zoom,
                variant="ghost",
                size=9,
                padx=6,
                pady=2,
                tooltip="Toggle y-exaggerated zoom around the waveguide core (|y| ≤ 1.2 μm)",
            )
            self.btn_zoom_core.pack(side=tk.RIGHT, padx=(0, 3), pady=3)

        self.coord_lbl.pack(side=tk.LEFT, padx=4)

        self.canvas = tk.Canvas(self, bg=THEME["plot_bg"], highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self.margin_left = 52
        self.margin_right = 14
        self.margin_top = 16
        self.margin_bottom = 30

        self.canvas.bind("<Configure>", lambda e: self.redraw())
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<ButtonPress-1>", self._on_left_down)
        self.canvas.bind("<B1-Motion>", self._on_left_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_left_up)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Button-4>", lambda e: self._on_mousewheel(_wheel_event(e, +1)))
        self.canvas.bind("<Button-5>", lambda e: self._on_mousewheel(_wheel_event(e, -1)))

    def set_model(self, model: FSPModelData, selected_port=None):
        self.model = model
        self.selected_port = selected_port
        self.exaggerate_v = False
        self._compute_default_ranges()
        self.reset_view()

    def set_selected_port(self, port_name):
        self.selected_port = port_name
        self.redraw()

    def _compute_default_ranges(self):
        if self.model is None:
            return
        xs, ys, zs = [], [], []
        for st in self.model.structures:
            xs.extend([st["xmin"], st["xmax"]])
            ys.extend([st["ymin"], st["ymax"]])
            zs.extend([st["zmin"], st["zmax"]])
        if self.model.fdtd_box:
            fx0, fx1, fy0, fy1, fz0, fz1 = self.model.fdtd_box
            xs.extend([fx0, fx1])
            ys.extend([fy0, fy1])
            zs.extend([fz0, fz1])
        for pt in self.model.ports:
            xs.extend([pt["xmin"], pt["xmax"]])
            ys.extend([pt["ymin"], pt["ymax"]])
            zs.extend([pt["zmin"], pt["zmax"]])

        def pad_range(vals, default_lo, default_hi, pad=0.08):
            if not vals:
                return [default_lo, default_hi]
            lo, hi = min(vals), max(vals)
            span = max(hi - lo, 1.0)
            return [lo - span * pad, hi + span * pad]

        if self.view_kind == "xy":
            self.default_u_range = pad_range(xs, -35.0, 230.0)
            self.default_v_range = pad_range(ys, -26.0, 26.0)
        elif self.view_kind == "xz":
            self.default_u_range = pad_range(xs, -35.0, 230.0)
            self.default_v_range = pad_range(zs, -6.5, 6.5, pad=0.12)
        else:  # yz
            # Focus on FDTD / port y-span if available so waveguide & ports are prominent
            port_ys = []
            for pt in self.model.ports:
                port_ys.extend([pt["ymin"], pt["ymax"]])
            if self.model.fdtd_box:
                port_ys.extend([self.model.fdtd_box[2], self.model.fdtd_box[3]])
            self.default_u_range = pad_range(port_ys or ys, -8.0, 8.0, pad=0.18)
            self.default_v_range = pad_range(zs, -6.5, 6.5, pad=0.12)

    def toggle_core_zoom(self):
        if self.model is None:
            return
        self.exaggerate_v = not self.exaggerate_v
        if self.exaggerate_v:
            core_ys = [
                v
                for st in self.model.structures
                if st.get("is_core")
                for v in (st["ymin"], st["ymax"])
            ]
            max_y = max((abs(v) for v in core_ys), default=0.5) * 2.2
            self.v_range = [-max(max_y, 0.6), max(max_y, 0.6)]
        else:
            self.v_range = list(self.default_v_range)
        self.redraw()

    def reset_view(self):
        self.exaggerate_v = False
        self.u_range = list(self.default_u_range)
        self.v_range = list(self.default_v_range)
        self.redraw()

    def _plot_rect(self):
        w = max(self.canvas.winfo_width(), 80)
        h = max(self.canvas.winfo_height(), 80)
        x0 = self.margin_left
        y0 = self.margin_top
        x1 = max(x0 + 20, w - self.margin_right)
        y1 = max(y0 + 20, h - self.margin_bottom)
        return x0, y0, x1, y1

    def uv_to_px(self, u, v):
        x0, y0, x1, y1 = self._plot_rect()
        u_min, u_max = self.u_range
        v_min, v_max = self.v_range
        px = x0 + (u - u_min) / max(1e-12, u_max - u_min) * (x1 - x0)
        py = y1 - (v - v_min) / max(1e-12, v_max - v_min) * (y1 - y0)
        return px, py

    def px_to_uv(self, px, py):
        x0, y0, x1, y1 = self._plot_rect()
        u_min, u_max = self.u_range
        v_min, v_max = self.v_range
        u = u_min + (px - x0) / max(1.0, x1 - x0) * (u_max - u_min)
        v = v_min + (y1 - py) / max(1.0, y1 - y0) * (v_max - v_min)
        return u, v

    def tick_steps(self):
        x0, y0, x1, y1 = self._plot_rect()
        su = nice_tick_step(self.u_range[1] - self.u_range[0], x1 - x0, 68)
        sv = nice_tick_step(self.v_range[1] - self.v_range[0], y1 - y0, 36)
        return (su, minor_tick_step(su)), (sv, minor_tick_step(sv))

    def _on_motion(self, event):
        u, v = self.px_to_uv(event.x, event.y)
        self.coord_lbl.config(text=f"{self.u_label}={u:.3g}, {self.v_label}={v:.3g} μm")

    def _on_left_down(self, event):
        # Check if clicking near a port marker
        for pname, bx0, by0, bx1, by1 in self._port_hit_boxes:
            if bx0 - 5 <= event.x <= bx1 + 5 and by0 - 5 <= event.y <= by1 + 5:
                if self.on_port_clicked:
                    self.on_port_clicked(pname)
                return
        self._pan_prev = (event.x, event.y)
        self.canvas.config(cursor="fleur")

    def _on_left_drag(self, event):
        if self._pan_prev is None:
            return
        dx = event.x - self._pan_prev[0]
        dy = event.y - self._pan_prev[1]
        self._pan_prev = (event.x, event.y)
        x0, y0, x1, y1 = self._plot_rect()
        du = -dx / max(1.0, x1 - x0) * (self.u_range[1] - self.u_range[0])
        dv = dy / max(1.0, y1 - y0) * (self.v_range[1] - self.v_range[0])
        self.u_range = [self.u_range[0] + du, self.u_range[1] + du]
        self.v_range = [self.v_range[0] + dv, self.v_range[1] + dv]
        self.redraw()

    def _on_left_up(self, _event):
        self._pan_prev = None
        self.canvas.config(cursor="crosshair")

    def _on_mousewheel(self, event):
        u_c, v_c = self.px_to_uv(event.x, event.y)
        factor = 0.84 if event.delta > 0 else 1.19
        u_min, u_max = self.u_range
        v_min, v_max = self.v_range
        if (u_max - u_min) * factor < 1e-3 or (v_max - v_min) * factor < 1e-3:
            return
        self.u_range = [u_c - (u_c - u_min) * factor, u_c + (u_max - u_c) * factor]
        self.v_range = [v_c - (v_c - v_min) * factor, v_c + (v_max - v_c) * factor]
        self.redraw()

    def redraw(self):
        T = THEME
        c = self.canvas
        c.delete("all")
        self._port_hit_boxes = []
        x0, y0, x1, y1 = self._plot_rect()

        c.create_rectangle(x0, y0, x1, y1, fill=T["plot_bg"], outline=T["border"])

        u_min, u_max = self.u_range
        v_min, v_max = self.v_range
        tick_font = (FONT_UI, 8)

        # 1. Grid and ticks
        (step_u, minor_u), (step_v, minor_v) = self.tick_steps()
        for minor, step, lo, hi, vertical in (
            (minor_u, step_u, u_min, u_max, True),
            (minor_v, step_v, v_min, v_max, False),
        ):
            if minor <= 0:
                continue
            per_major = max(1, round(step / minor))
            i_start = math.ceil(lo / minor - 1e-9)
            i_end = math.floor(hi / minor + 1e-9)
            if i_end - i_start > 400:
                continue
            for i in range(i_start, i_end + 1):
                val = i * minor
                is_major = i % per_major == 0
                if vertical:
                    px, _ = self.uv_to_px(val, 0)
                    line = (px, y0, px, y1)
                else:
                    _, py = self.uv_to_px(0, val)
                    line = (x0, py, x1, py)
                color = T["grid_axis"] if i == 0 else T["grid"] if is_major else T["grid_minor"]
                c.create_line(*line, fill=color, width=1)
                if is_major:
                    label = format_tick(val, step)
                    if vertical:
                        c.create_text(px, y1 + 10, text=label, font=tick_font, fill=T["text_muted"])
                    else:
                        c.create_text(x0 - 5, py, text=label, font=tick_font, fill=T["text_muted"], anchor="e")

        # Axis titles
        c.create_text(
            (x0 + x1) / 2, y1 + 22, text=f"{self.u_label} (μm)", font=(FONT_UI, 9, "bold"), fill=T["text_muted"]
        )
        c.create_text(
            15, (y0 + y1) / 2, text=f"{self.v_label}\n(μm)", font=(FONT_UI, 9, "bold"), fill=T["text_muted"],
            justify=tk.CENTER,
        )

        if self.model is None:
            self.info_lbl.config(text="no file", fg=T["text_dim"])
            c.create_text(
                (x0 + x1) / 2,
                (y0 + y1) / 2,
                text="Open an .fsp file to visualize the model",
                font=(FONT_UI, 11),
                fill=T["text_dim"],
            )
            return

        n_ports = len(self.model.ports)
        self.info_lbl.config(
            text=f"{len(self.model.structures)} layers · {n_ports} ports",
            fg=T["port_yellow"],
        )

        # 2. Draw structures (background slabs first, then patterned/core structures)
        # Among them the larger ones first: a shape inside another one (an active layer in its
        # cladding) is then drawn on top instead of being hidden.
        def _view_area(s):
            du, dv = {"xy": ("x", "y"), "xz": ("x", "z"), "yz": ("y", "z")}[self.view_kind]
            return (s[du + "max"] - s[du + "min"]) * (s[dv + "max"] - s[dv + "min"])

        ordered_structs = sorted(self.model.structures,
                                 key=lambda s: (not s["is_background"], s["is_core"], -_view_area(s)))
        for st in ordered_structs:
            if self.view_kind == "xy":
                if st["is_background"] and st.get("gds_layer") == "":
                    # Subtle outline for full wafer background slab in XY
                    px0, py1 = self.uv_to_px(st["xmin"], st["ymin"])
                    px1, py0 = self.uv_to_px(st["xmax"], st["ymax"])
                    c.create_rectangle(px0, py0, px1, py1, fill="", outline=st["edge"], dash=(2, 4))
                else:
                    coords = [coord for u, v in st["xy_poly"] for coord in self.uv_to_px(u, v)]
                    if len(coords) >= 6:
                        c.create_polygon(*coords, fill=st["fill"], outline=st["edge"], width=1.5)
            elif self.view_kind == "xz":
                px0, py1 = self.uv_to_px(st["xmin"], st["zmin"])
                px1, py0 = self.uv_to_px(st["xmax"], st["zmax"])
                c.create_rectangle(
                    px0, py0, px1, py1,
                    fill=st["fill"],
                    outline=st["edge"],
                    width=1.5 if st["is_core"] else 1.0,
                )
                if abs(py1 - py0) >= 12 and (st["is_background"] or st["is_core"]):
                    c.create_text(
                        max(x0 + 6, min(x1 - 40, px0 + 6)),
                        0.5 * (py0 + py1),
                        text=st["layer_name"],
                        font=(FONT_UI, 8),
                        fill=T["text_muted"] if st["is_background"] else "#ffffff",
                        anchor="w",
                    )
            elif self.view_kind == "yz":
                px0, py1 = self.uv_to_px(st["ymin"], st["zmin"])
                px1, py0 = self.uv_to_px(st["ymax"], st["zmax"])
                c.create_rectangle(
                    px0, py0, px1, py1,
                    fill=st["fill"],
                    outline=st["edge"],
                    width=1.6 if st["is_core"] else 1.0,
                )
                if st["is_background"] and abs(py1 - py0) >= 12:
                    c.create_text(
                        x0 + 6,
                        0.5 * (py0 + py1),
                        text=f"{st['layer_name']}" + (f" (n={st['n']:.3f})" if st["n"] else ""),
                        font=(FONT_UI, 8),
                        fill=T["text_muted"],
                        anchor="w",
                    )

        # 3. Draw FDTD region in orange
        if self.model.fdtd_box:
            fx0, fx1, fy0, fy1, fz0, fz1 = self.model.fdtd_box
            if self.view_kind == "xy":
                rx0, ry1 = self.uv_to_px(fx0, fy0)
                rx1, ry0 = self.uv_to_px(fx1, fy1)
            elif self.view_kind == "xz":
                rx0, ry1 = self.uv_to_px(fx0, fz0)
                rx1, ry0 = self.uv_to_px(fx1, fz1)
            else:
                rx0, ry1 = self.uv_to_px(fy0, fz0)
                rx1, ry0 = self.uv_to_px(fy1, fz1)
            c.create_rectangle(rx0, ry0, rx1, ry1, fill="", outline=T["fdtd_orange"], width=1.5, dash=(5, 3))
            c.create_text(
                min(x1 - 4, max(x0 + 4, rx1 - 4)),
                max(y0 + 10, ry0 + 10),
                text="FDTD",
                font=(FONT_UI, 8, "bold"),
                fill=T["fdtd_orange"],
                anchor="ne",
            )

        # 4. Draw PORTS in YELLOW (#ffd700 / #ffee55)
        for pt in self.model.ports:
            is_sel = (pt["name"] == self.selected_port) or (self.selected_port == "All Ports")
            p_color = T["port_yellow_bright"] if pt["name"] == self.selected_port else T["port_yellow"]
            lw = 3.0 if pt["name"] == self.selected_port else 2.0
            if not pt["enabled"]:  # a disabled port takes no part in the simulation: dimmed
                p_color, lw = "#8a7a1a", 1.0
            short_lbl = pt["name"].replace("output_", "out").replace("input_port", "in")

            if self.view_kind == "xy":
                px, py0 = self.uv_to_px(pt["x"], pt["ymax"])
                _, py1 = self.uv_to_px(pt["x"], pt["ymin"])
                # Full vertical guide dash + thick yellow port span
                c.create_line(px, y0, px, y1, fill="#665800", width=1, dash=(2, 4))
                c.create_line(px, py0, px, py1, fill=p_color, width=lw)
                c.create_oval(px - 3, py0 - 3, px + 3, py0 + 3, fill=p_color, outline="#000000")
                c.create_oval(px - 3, py1 - 3, px + 3, py1 + 3, fill=p_color, outline="#000000")
                # Direction arrow at port center
                _, pyc = self.uv_to_px(pt["x"], pt["y"])
                arr_dx = 10 if pt["direction"] == "+" else -10
                c.create_line(px, pyc, px + arr_dx, pyc, fill=p_color, width=2, arrow=tk.LAST)
                self._port_hit_boxes.append((pt["name"], px - 6, min(py0, py1), px + 6, max(py0, py1)))

            elif self.view_kind == "xz":
                px, pz0 = self.uv_to_px(pt["x"], pt["zmax"])
                _, pz1 = self.uv_to_px(pt["x"], pt["zmin"])
                c.create_line(px, y0, px, y1, fill="#665800", width=1, dash=(2, 4))
                c.create_line(px, pz0, px, pz1, fill=p_color, width=lw)
                c.create_oval(px - 3, pz0 - 3, px + 3, pz0 + 3, fill=p_color, outline="#000000")
                c.create_oval(px - 3, pz1 - 3, px + 3, pz1 + 3, fill=p_color, outline="#000000")
                self._port_hit_boxes.append((pt["name"], px - 6, min(pz0, pz1), px + 6, max(pz0, pz1)))

            else:  # yz cross-section
                py0, pz1 = self.uv_to_px(pt["ymin"], pt["zmin"])
                py1, pz0 = self.uv_to_px(pt["ymax"], pt["zmax"])
                c.create_rectangle(
                    py0,
                    pz0,
                    py1,
                    pz1,
                    fill="",
                    outline=p_color,
                    width=lw,
                    dash=() if is_sel else (4, 2),
                )
                c.create_text(
                    py1 - 4,
                    pz0 + 10 + 12 * pt["index"],  # one line per port: they share this frame
                    text=f"{pt['name']} (x={pt['x']:g} μm)" + ("" if pt["enabled"] else ", disabled"),
                    font=(FONT_UI, 8, "bold"),
                    fill=p_color,
                    anchor="ne",
                )

        # Port labels in XY / XZ views (staggered so dense output ports don't overlap)
        if self.view_kind in ("xy", "xz"):
            last_px = -999
            row_idx = 0
            for pt in sorted(self.model.ports, key=lambda item: item["x"]):
                px, _ = self.uv_to_px(pt["x"], 0)
                if not (x0 <= px <= x1):
                    continue
                if px - last_px < 34:
                    row_idx = (row_idx + 1) % 3
                else:
                    row_idx = 0
                    last_px = px
                lbl_y = y0 + 10 + row_idx * 13
                is_sel = pt["name"] == self.selected_port
                short_lbl = pt["name"].replace("output_port", "P").replace("input_port", "In")
                if not pt["enabled"]:
                    short_lbl += " (off)"
                tid = c.create_text(
                    px,
                    lbl_y,
                    text=short_lbl,
                    font=(FONT_MONO, 8, "bold"),
                    fill="#0f1115" if is_sel else T["port_yellow"] if pt["enabled"] else "#8a7a1a",
                )
                bx0, by0, bx1, by1 = c.bbox(tid)
                bg = c.create_rectangle(
                    bx0 - 3,
                    by0 - 1,
                    bx1 + 3,
                    by1 + 1,
                    fill=T["port_yellow_bright"] if is_sel else "#2b2500",
                    outline=T["port_yellow"],
                )
                c.tag_lower(bg, tid)
                self._port_hit_boxes.append((pt["name"], bx0 - 3, by0 - 1, bx1 + 3, by1 + 1))

        # Clip border on top
        c.create_rectangle(x0, y0, x1, y1, fill="", outline=T["border"])


# ======================================================================
# 3D Interactive Viewport (matching 3D Geometry Generation project)
# ======================================================================
class ModelView3DCanvas(tk.Frame):
    """
    Interactive 3D view (top-right quadrant) movable by the pointer:
      - Left-drag to rotate (azimuth & elevation)
      - Scroll wheel to zoom
      - Right/middle-drag to pan
      - Renders 3D waveguide cores, trenches, optional background slabs, FDTD box,
        and YELLOW 3D port planes (`#ffd700`).
    """

    EYE_DISTANCE = 3.2 / 0.35

    def __init__(self, parent, on_port_clicked=None):
        super().__init__(parent, bg=THEME["panel_bg"], highlightthickness=1, highlightbackground=THEME["border"])
        self.on_port_clicked = on_port_clicked

        header = tk.Frame(self, bg=THEME["panel_bg"])
        header.pack(fill=tk.X, side=tk.TOP)

        tk.Label(
            header,
            text="3D View (x-y-z)",
            font=(FONT_UI, 11, "bold"),
            bg=THEME["panel_bg"],
            fg=THEME["text"],
            anchor="w",
        ).pack(side=tk.LEFT, padx=(10, 6), pady=4)

        tk.Label(
            header,
            text="drag to rotate · scroll to zoom",
            font=(FONT_UI, 9),
            bg=THEME["panel_bg"],
            fg=THEME["text_dim"],
        ).pack(side=tk.LEFT, padx=4)

        FlatButton(
            header,
            "Reset view",
            self.reset_camera,
            variant="ghost",
            size=9,
            padx=6,
            pady=2,
            tooltip="Reset 3D rotation, pan, and zoom",
        ).pack(side=tk.RIGHT, padx=(0, 6), pady=3)

        self.show_slabs_var = tk.BooleanVar(value=False)
        self.btn_slabs = FlatButton(
            header,
            "Slabs: Off",
            self._toggle_slabs,
            variant="ghost",
            size=9,
            padx=6,
            pady=2,
            tooltip="Toggle visibility of full-wafer background slabs (substrate/clad) in 3D",
        )
        self.btn_slabs.pack(side=tk.RIGHT, padx=(0, 3), pady=3)

        self.canvas = tk.Canvas(self, bg=THEME["plot_bg"], highlightthickness=0, cursor="fleur")
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self.azim = -55.0
        self.elev = 26.0
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self._drag_prev = None
        self._pan_prev = None

        self.model = None
        self.selected_port = None
        self.faces_3d = []
        self.port_faces_3d = []
        self.bbox_3d = None
        self._half = (1.0, 1.0, 1.0, 1.0)

        self.canvas.bind("<Configure>", lambda e: self.redraw())
        self.canvas.bind("<ButtonPress-1>", self._on_mouse_down)
        self.canvas.bind("<B1-Motion>", self._on_mouse_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_mouse_up)
        self.canvas.bind("<ButtonPress-2>", self._on_pan_down)
        self.canvas.bind("<B2-Motion>", self._on_pan_drag)
        self.canvas.bind("<ButtonPress-3>", self._on_pan_down)
        self.canvas.bind("<B3-Motion>", self._on_pan_drag)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Button-4>", lambda e: self._on_mousewheel(_wheel_event(e, +1)))
        self.canvas.bind("<Button-5>", lambda e: self._on_mousewheel(_wheel_event(e, -1)))

    def _toggle_slabs(self):
        val = not self.show_slabs_var.get()
        self.show_slabs_var.set(val)
        self.btn_slabs.config(text=f"Slabs: {'On' if val else 'Off'}")
        self._rebuild_3d_scene()
        self.redraw()

    def reset_camera(self):
        self.azim = -55.0
        self.elev = 26.0
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self.redraw()

    def set_model(self, model: FSPModelData, selected_port=None):
        self.model = model
        self.selected_port = selected_port
        self._rebuild_3d_scene()
        self.redraw()

    def set_selected_port(self, port_name):
        self.selected_port = port_name
        self._rebuild_3d_scene()
        self.redraw()

    def _rebuild_3d_scene(self):
        self.faces_3d = []
        self.port_faces_3d = []
        if self.model is None:
            self.bbox_3d = None
            return

        show_slabs = self.show_slabs_var.get()
        for st in self.model.structures:
            if st["is_background"] and not show_slabs:
                continue
            faces = extruded_poly_faces_3d(st["xy_poly"], st["zmin"], st["zmax"], st["fill"], tag=st["name"])
            self.faces_3d.extend(faces)

        # Port 3D yellow planes (slightly thickened or quad plates at port x)
        for pt in self.model.ports:
            x = pt["x"]
            y0, y1 = pt["ymin"], pt["ymax"]
            z0, z1 = pt["zmin"], pt["zmax"]
            quad = [(x, y0, z0), (x, y1, z0), (x, y1, z1), (x, y0, z1)]
            is_sel = pt["name"] == self.selected_port
            self.port_faces_3d.append({
                "name": pt["name"],
                "x": x,
                "y": pt["y"],
                "z": z1,
                "direction": pt["direction"],
                "is_selected": is_sel,
                "enabled": pt["enabled"],
                "verts": quad,
            })

        # Compute bounding box from FDTD box + core structures + ports
        xs, ys, zs = [], [], []
        if self.model.fdtd_box:
            fx0, fx1, fy0, fy1, fz0, fz1 = self.model.fdtd_box
            xs.extend([fx0, fx1])
            ys.extend([fy0, fy1])
            zs.extend([fz0, fz1])
        for f in self.faces_3d:
            for vx, vy, vz in f[0]:
                xs.append(vx)
                ys.append(vy)
                zs.append(vz)
        for pf in self.port_faces_3d:
            for vx, vy, vz in pf["verts"]:
                xs.append(vx)
                ys.append(vy)
                zs.append(vz)

        if xs and ys and zs:
            self.bbox_3d = (min(xs), max(xs), min(ys), max(ys), min(zs), max(zs))
            xmin, xmax, ymin, ymax, zmin, zmax = self.bbox_3d
            self._half = view_box_half_extents(xmax - xmin, ymax - ymin, zmax - zmin)
        else:
            self.bbox_3d = None

    def _on_mouse_down(self, event):
        self._drag_prev = (event.x, event.y)

    def _on_mouse_drag(self, event):
        if self._drag_prev is None:
            return
        dx = event.x - self._drag_prev[0]
        dy = event.y - self._drag_prev[1]
        self._drag_prev = (event.x, event.y)
        self.azim += dx * 0.6
        self.elev = max(-85.0, min(85.0, self.elev + dy * 0.6))
        self.redraw()

    def _on_mouse_up(self, _event):
        self._drag_prev = None

    def _on_pan_down(self, event):
        self._pan_prev = (event.x, event.y)

    def _on_pan_drag(self, event):
        if self._pan_prev is None:
            return
        self.pan_x += event.x - self._pan_prev[0]
        self.pan_y += event.y - self._pan_prev[1]
        self._pan_prev = (event.x, event.y)
        self.redraw()

    def _on_mousewheel(self, event):
        factor = 1.12 if event.delta > 0 else 0.89
        self.zoom = max(0.3, min(4.5, self.zoom * factor))
        self.redraw()

    def _normalize(self, x, y, z):
        if self.bbox_3d is None:
            return x, y, z
        xmin, xmax, ymin, ymax, zmin, zmax = self.bbox_3d
        xmid = 0.5 * (xmin + xmax)
        ymid = 0.5 * (ymin + ymax)
        zmid = 0.5 * (zmin + zmax)
        dx = max(xmax - xmin, 1e-6)
        dy = max(ymax - ymin, 1e-6)
        dz = max(zmax - zmin, 1e-6)
        hx, hy, hz, _ = self._half
        nx = (x - xmid) / (dx * 0.5) * hx
        ny = (y - ymid) / (dy * 0.5) * hy
        nz = (z - zmid) / (dz * 0.5) * hz
        return nx, ny, nz

    def _project_point(self, x, y, z, cx, cy, scale):
        nx, ny, nz = self._normalize(x, y, z)
        az = math.radians(self.azim)
        el = math.radians(self.elev)
        cos_a, sin_a = math.cos(az), math.sin(az)
        cos_e, sin_e = math.cos(el), math.sin(el)

        rx = nx * cos_a - ny * sin_a
        ry = nx * sin_a + ny * cos_a
        rz = nz

        px_3d = rx
        py_3d = -ry * sin_e + rz * cos_e
        depth = ry * cos_e + rz * sin_e

        persp = 1.0 / max(0.4, 3.2 - depth * 0.35) * 2.8
        sx = cx + self.pan_x + px_3d * scale * self.zoom * persp
        sy = cy + self.pan_y - py_3d * scale * self.zoom * persp
        return sx, sy, depth

    def redraw(self):
        c = self.canvas
        c.delete("all")
        w = max(c.winfo_width(), 80)
        h = max(c.winfo_height(), 80)
        cx = w * 0.5
        cy = h * 0.52
        scale = min(w, h) * 0.31

        if self.model is None or self.bbox_3d is None:
            c.create_text(
                cx, cy - 8, text="3D Viewport", font=(FONT_UI, 13, "bold"), fill=THEME["text_muted"]
            )
            c.create_text(
                cx, cy + 14, text="Open an .fsp file to inspect 3D geometry & ports",
                font=(FONT_UI, 10), fill=THEME["text_dim"],
            )
            self._draw_axes_triad(w, h)
            return

        # 1. Draw 3D bounding / FDTD wireframe box
        xmin, xmax, ymin, ymax, zmin, zmax = self.model.fdtd_box or self.bbox_3d
        box_corners = [
            (xmin, ymin, zmin), (xmax, ymin, zmin), (xmax, ymax, zmin), (xmin, ymax, zmin),
            (xmin, ymin, zmax), (xmax, ymin, zmax), (xmax, ymax, zmax), (xmin, ymax, zmax),
        ]
        proj_corners = [self._project_point(*pt, cx, cy, scale) for pt in box_corners]
        box_edges = [
            (0, 1), (1, 2), (2, 3), (3, 0),
            (4, 5), (5, 6), (6, 7), (7, 4),
            (0, 4), (1, 5), (2, 6), (3, 7),
        ]
        for i, j in box_edges:
            c.create_line(
                proj_corners[i][0], proj_corners[i][1],
                proj_corners[j][0], proj_corners[j][1],
                fill=THEME["fdtd_orange"] if self.model.fdtd_box else THEME["grid_axis"],
                dash=(3, 3),
            )

        # 2. Painter's algorithm depth sort of all solid faces + yellow port planes
        render_queue = []
        for verts, kind, base_color, _tag in self.faces_3d:
            pts = [self._project_point(*v, cx, cy, scale) for v in verts]
            avg_depth = sum(p[2] for p in pts) / len(pts)
            fill_c, edge_c = face_colors(base_color, kind)
            render_queue.append((avg_depth, "solid", pts, fill_c, edge_c, None))

        for pf in self.port_faces_3d:
            pts = [self._project_point(*v, cx, cy, scale) for v in pf["verts"]]
            avg_depth = sum(p[2] for p in pts) / len(pts) + 1e-4
            kind = "port_selected" if pf["is_selected"] else "port"
            fill_c, edge_c = face_colors(THEME["port_yellow"], kind)
            render_queue.append((avg_depth, "port", pts, fill_c, edge_c, pf))

        render_queue.sort(key=lambda item: item[0])

        for _depth, rkind, pts, fill_c, edge_c, pf in render_queue:
            coords = [coord for pt in pts for coord in (pt[0], pt[1])]
            if rkind == "port":
                # Draw yellow port plane with stipple/translucent look and bright yellow border
                c.create_polygon(
                    *coords,
                    fill=THEME["port_yellow_bright"] if pf["is_selected"] else THEME["port_yellow"],
                    stipple="gray50" if pf["enabled"] else "gray12",  # a disabled port: faint
                    outline=edge_c,
                    width=2.4 if pf["is_selected"] else 1.6,
                )
                # Port label at top center of port frame
                top_x = 0.5 * (pts[2][0] + pts[3][0])
                top_y = 0.5 * (pts[2][1] + pts[3][1]) - 7
                short = pf["name"].replace("output_port", "P").replace("input_port", "In")
                if not pf["enabled"]:
                    short += " (off)"
                c.create_text(
                    top_x,
                    top_y,
                    text=short,
                    font=(FONT_MONO, 8, "bold"),
                    fill=THEME["port_yellow_bright"] if pf["is_selected"] else THEME["port_yellow"],
                )
            else:
                c.create_polygon(*coords, fill=fill_c, outline=edge_c, width=1.0)

        # Dimension badges
        p_x0, p_x1, p_y1, p_z1 = proj_corners[0], proj_corners[1], proj_corners[2], proj_corners[5]
        self._badge(0.5 * (p_x0[0] + p_x1[0]), 0.5 * (p_x0[1] + p_x1[1]) + 14, f"Δx={xmax - xmin:g} μm", "#74c0fc")
        self._badge(0.5 * (p_x1[0] + p_y1[0]) + 24, 0.5 * (p_x1[1] + p_y1[1]) + 8, f"Δy={ymax - ymin:g} μm", "#63e6be")
        self._badge(0.5 * (p_x1[0] + p_z1[0]) + 30, 0.5 * (p_x1[1] + p_z1[1]), f"Δz={zmax - zmin:g} μm", "#ffa8a8")

        # Legend pill in top-left corner
        c.create_text(
            14, 12,
            text=f"■ Ports ({len(self.model.ports)}) in Yellow   ■ FDTD Box",
            font=(FONT_UI, 9, "bold"),
            fill=THEME["port_yellow"],
            anchor="nw",
        )

        self._draw_axes_triad(w, h)

    def _badge(self, x, y, text, color):
        tid = self.canvas.create_text(x, y, text=text, font=(FONT_UI, 8, "bold"), fill=color)
        bx0, by0, bx1, by1 = self.canvas.bbox(tid)
        bg = self.canvas.create_rectangle(bx0 - 4, by0 - 1, bx1 + 4, by1 + 1, fill=THEME["app_bg"], outline="")
        self.canvas.tag_lower(bg, tid)

    def _draw_axes_triad(self, w, h):
        ox, oy = 42, h - 34
        axis_len = 24
        az = math.radians(self.azim)
        el = math.radians(self.elev)
        cos_a, sin_a = math.cos(az), math.sin(az)
        cos_e, sin_e = math.cos(el), math.sin(el)

        for label, (ux, uy, uz), color in (
            ("x", (1, 0, 0), "#74c0fc"),
            ("y", (0, 1, 0), "#63e6be"),
            ("z", (0, 0, 1), "#ffa8a8"),
        ):
            rx = ux * cos_a - uy * sin_a
            ry = ux * sin_a + uy * cos_a
            rz = uz
            sx = ox + rx * axis_len
            sy = oy - (-ry * sin_e + rz * cos_e) * axis_len
            self.canvas.create_line(ox, oy, sx, sy, fill=color, width=2, arrow=tk.LAST)
            self.canvas.create_text(
                sx + (sx - ox) * 0.3,
                sy + (sy - oy) * 0.3,
                text=label,
                font=(FONT_UI, 9, "bold"),
                fill=color,
            )


# ======================================================================
# Editor-Like Model Parameters Panel
# ======================================================================
class ParameterEditorPanel(tk.Frame):
    """
    Code-editor style inspector for all FSP model parameters:
      - Line numbers gutter
      - Syntax coloring for sections, object headers, keys, values
      - Modes: Summary (human-readable settings), All Objects & Properties, Materials
      - Live Search / Filter entry + Copy button
    """

    def __init__(self, parent):
        super().__init__(parent, bg=THEME["panel_bg"], highlightthickness=1, highlightbackground=THEME["border"])
        self.model = None
        self.mode = "summary"  # "summary", "props", "materials"
        self._full_text = ""

        # Header bar
        header = tk.Frame(self, bg=THEME["panel_bg"])
        header.pack(fill=tk.X, side=tk.TOP)

        tk.Label(
            header,
            text="Model Parameters Editor",
            font=(FONT_UI, 11, "bold"),
            bg=THEME["panel_bg"],
            fg=THEME["text"],
        ).pack(side=tk.LEFT, padx=(10, 8), pady=5)

        # Mode switcher buttons
        self.btn_summary = FlatButton(
            header, "Summary", lambda: self.set_mode("summary"), variant="primary", size=9, padx=8, pady=3,
            tooltip="Human-readable model summary (layers, GDS polygons, FDTD region, ports, monitors)",
        )
        self.btn_summary.pack(side=tk.LEFT, padx=2, pady=3)

        self.btn_props = FlatButton(
            header, "All Objects & Props", lambda: self.set_mode("props"), variant="secondary", size=9, padx=8, pady=3,
            tooltip="Every object in the .fsp tree and all its properties & result arrays",
        )
        self.btn_props.pack(side=tk.LEFT, padx=2, pady=3)

        self.btn_mats = FlatButton(
            header, "Materials", lambda: self.set_mode("materials"), variant="secondary", size=9, padx=8, pady=3,
            tooltip="Embedded Lumerical material database",
        )
        self.btn_mats.pack(side=tk.LEFT, padx=2, pady=3)

        # Copy button on right
        FlatButton(
            header, "Copy Text", self.copy_editor_text, variant="ghost", size=9, padx=8, pady=3,
            tooltip="Copy the contents of this parameter editor to the clipboard",
        ).pack(side=tk.RIGHT, padx=(4, 8), pady=3)

        # Search filter box
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self._apply_filter())
        ent_filter = tk.Entry(
            header,
            textvariable=self.filter_var,
            width=18,
            font=(FONT_MONO, 10),
            bg=THEME["entry_bg"],
            fg=THEME["text"],
            insertbackground=THEME["text"],
            relief=tk.FLAT,
            highlightthickness=1,
            highlightbackground=THEME["border"],
            highlightcolor=THEME["accent"],
        )
        ent_filter.pack(side=tk.RIGHT, padx=4, pady=4, ipady=2)
        tk.Label(
            header, text="Filter:", font=(FONT_UI, 9), bg=THEME["panel_bg"], fg=THEME["text_muted"]
        ).pack(side=tk.RIGHT, padx=(6, 2))

        # Editor body: line-number gutter + main text area + scrollbars
        body = tk.Frame(self, bg=THEME["entry_bg"])
        body.pack(fill=tk.BOTH, expand=True)

        self.vscroll = tk.Scrollbar(body, orient=tk.VERTICAL)
        self.hscroll = tk.Scrollbar(body, orient=tk.HORIZONTAL)

        self.line_nums = tk.Text(
            body,
            width=5,
            padx=6,
            pady=6,
            bg="#141820",
            fg=THEME["text_dim"],
            font=(FONT_MONO, 10),
            relief=tk.FLAT,
            highlightthickness=0,
            state=tk.DISABLED,
            takefocus=0,
            wrap=tk.NONE,
        )
        self.text = tk.Text(
            body,
            padx=10,
            pady=6,
            bg=THEME["entry_bg"],
            fg=THEME["text"],
            insertbackground=THEME["text"],
            selectbackground=THEME["accent"],
            selectforeground="#ffffff",
            font=(FONT_MONO, 10),
            relief=tk.FLAT,
            highlightthickness=0,
            wrap=tk.NONE,
            undo=True,
            yscrollcommand=self._on_text_yscroll,
            xscrollcommand=self.hscroll.set,
        )
        self.vscroll.config(command=self._on_scrollbar_y)
        self.hscroll.config(command=self.text.xview)

        self.vscroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.hscroll.pack(side=tk.BOTTOM, fill=tk.X)
        self.line_nums.pack(side=tk.LEFT, fill=tk.Y)
        self.text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Syntax highlight tags
        self.text.tag_configure("section", foreground="#6aa1ff", font=(FONT_MONO, 10, "bold"))
        self.text.tag_configure("port_line", foreground=THEME["port_yellow"], font=(FONT_MONO, 10, "bold"))
        self.text.tag_configure("key", foreground="#7ee2b0")
        self.text.tag_configure("comment", foreground=THEME["text_muted"])

        self.set_text("# Open an .fsp file to inspect all model parameters, ports, layers, and materials.\n")

    def _on_text_yscroll(self, *args):
        self.vscroll.set(*args)
        self.line_nums.yview_moveto(args[0])

    def _on_scrollbar_y(self, *args):
        self.text.yview(*args)
        self.line_nums.yview(*args)

    def set_model(self, model: FSPModelData):
        self.model = model
        self._rebuild_content()

    def set_mode(self, mode):
        self.mode = mode
        for btn, m in ((self.btn_summary, "summary"), (self.btn_props, "props"), (self.btn_mats, "materials")):
            bg = THEME["accent"] if m == mode else THEME["btn_bg"]
            fg = "#ffffff" if m == mode else THEME["text"]
            btn._bg = bg
            btn._fg = fg
            btn.config(bg=bg, fg=fg)
        self._rebuild_content()

    def _rebuild_content(self):
        if self.model is None:
            return
        f = self.model.fsp
        lines = []
        lines.append(f"# FSP File : {self.model.fsp_path}")
        lines.append(f"# H5 File  : {self.model.h5_path or '(none loaded)'}")
        lines.append("")

        if self.mode == "summary":
            lines.append(fsp_summary(f))
            # Also append extracted primitive structures if not a Layer Builder file
            if self.model.structures and not any("numProcessLayers" in o.props for o in f.objects):
                lines.append("EXTRACTED STRUCTURES")
                for st in self.model.structures:
                    lines.append(
                        f"  {st['name']:<18} x=[{st['xmin']:.3f}, {st['xmax']:.3f}] "
                        f"y=[{st['ymin']:.3f}, {st['ymax']:.3f}] "
                        f"z=[{st['zmin']:.3f}, {st['zmax']:.3f}] um  ({st['material']})"
                    )
                lines.append("")
            if self.model.mode_monitors:
                lines.append("STANDALONE MODE MONITORS")
                for mm in self.model.mode_monitors:
                    lines.append(
                        f"  {mm['name']:<18} x={mm['x']:.3f} um, "
                        f"y=[{mm['ymin']:.3f}, {mm['ymax']:.3f}] um, "
                        f"z=[{mm['zmin']:.3f}, {mm['zmax']:.3f}] um"
                    )
                lines.append("")
        elif self.mode == "props":
            for ob in f.objects:
                lines.append(f"== {ob.name}  (uuid {ob.uuid}, offset 0x{ob.offset:x}, tag {ob.tag})")
                for k, v in ob.props.items():
                    lines.append(f"   {k:45s} = {_fmt(v)}")
                for k, v in ob.results.items():
                    lines.append(f"   [result] {k:36s} = {_fmt(v)}")
                lines.append("")
        elif self.mode == "materials":
            lines.append(f"MATERIAL DATABASE ({len(f.materials)} materials)")
            lines.append("")
            for m in f.materials:
                mname = m.get("name", "<unnamed>")
                mtype = m.get("type", "?")
                idx_str = ""
                try:
                    n = f.material_index(mname)
                    if n is not None:
                        idx_str = f"  n = {n[0].real:.4f}"
                except Exception:
                    pass
                lines.append(f"== {mname}  (type {mtype}){idx_str}")
                for k, v in m.items():
                    if k != "name":
                        lines.append(f"   {k:35s} = {_fmt(v)}")
                lines.append("")

        self._full_text = "\n".join(lines)
        self._apply_filter()

    def _apply_filter(self):
        query = self.filter_var.get().strip().lower()
        if not query:
            self.set_text(self._full_text)
            return
        matched = [
            line
            for line in self._full_text.splitlines()
            if query in line.lower() or line.startswith("==") or line.startswith("#")
        ]
        self.set_text("\n".join(matched) if matched else f"# No lines matching '{query}'")

    def set_text(self, content):
        self.text.delete("1.0", tk.END)
        self.text.insert("1.0", content)

        # Syntax highlighting
        for idx, line in enumerate(content.splitlines(), start=1):
            if line.startswith("#"):
                self.text.tag_add("comment", f"{idx}.0", f"{idx}.end")
            elif line.startswith("==") or (line.isupper() and len(line) > 3):
                self.text.tag_add("section", f"{idx}.0", f"{idx}.end")
            elif "port" in line.lower() and ("x=" in line or "SOURCE" in line):
                self.text.tag_add("port_line", f"{idx}.0", f"{idx}.end")
            elif "=" in line and line.startswith("   "):
                eq_pos = line.index("=")
                self.text.tag_add("key", f"{idx}.0", f"{idx}.{eq_pos}")

        n_lines = max(1, len(content.splitlines()))
        nums = "\n".join(f"{i:4d}" for i in range(1, n_lines + 1))
        self.line_nums.config(state=tk.NORMAL)
        self.line_nums.delete("1.0", tk.END)
        self.line_nums.insert("1.0", nums)
        self.line_nums.config(state=tk.DISABLED)

    def copy_editor_text(self):
        content = self.text.get("1.0", "end-1c")
        self.clipboard_clear()
        self.clipboard_append(content)
        self.update()


# ======================================================================
# Main Application Window (fsp_gui.py)
# ======================================================================
class FSPGuiApp:
    """
    Main GUI combining:
      1. 4-View Geometry Visualization Field (Up-Left: XY, Bottom-Left: XZ, Top-Right: 3D, Bottom-Right: YZ)
         with yellow ports
      2. Model Parameters Editor
      3. Plot button + Port dropdown + Results dropdown + Export Data to Clipboard button
      4. Fifth Larger Plot Window (embedded large right pane + optional standalone window)
    """

    RESULT_OPTIONS = [
        "Cross-Sections & Port Modes (n & |E|²)",
        "Stored Port Mode |E|² Profile (YZ)",
        "Stored Refractive Index n(y, z) (YZ)",
        "Simulated Port Field |E|² (YZ, .h5)",
        "Power & Mode Coupling vs. Port x (.h5)",
        "Port Transmission vs. Wavelength λ (.h5)",
        "XZ Propagation |E|² at y=0 (dB, .h5)",
        "Top-View Layout & Taper Zoom (XY)",
        "Port Mode Field Components (|Ex|..|Hz|)",
        "Source Time Signal & Spectrum",
    ]

    def __init__(self, root, initial_fsp=None, initial_h5=None):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1640x960")
        self.root.minsize(1220, 740)
        self.root.configure(bg=THEME["app_bg"])
        try:
            self.root.tk.call("::tk::unsupported::MacWindowStyle", "appearance", self.root, "darkaqua")
        except tk.TclError:
            pass
        apply_ttk_dark_style(self.root)

        self.model: FSPModelData | None = None
        self.last_exported_tsv: str = ""
        self.last_exported_desc: str = ""
        self.popout_win: tk.Toplevel | None = None
        self.popout_fig: Figure | None = None
        self.popout_canvas: FigureCanvasTkAgg | None = None

        self._build_ui()
        self._build_menu()

        if initial_fsp and os.path.isfile(initial_fsp):
            self.root.after(100, lambda: self.open_fsp_file(initial_fsp, initial_h5))

    def _build_menu(self):
        menubar = tk.Menu(self.root)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Open .fsp File…", command=self.browse_fsp, accelerator="Cmd+O")
        file_menu.add_command(label="Open .h5 Field File…", command=self.browse_h5)
        file_menu.add_separator()
        file_menu.add_command(label="Save Geometry as GDS File…", command=lambda: self.save_geometry("gds"))
        file_menu.add_command(label="Save Geometry as Python (gdsfactory) Script…",
                              command=lambda: self.save_geometry("py"))
        file_menu.add_separator()
        file_menu.add_command(label="Export Plot Data to Clipboard", command=self.export_data_to_clipboard, accelerator="Cmd+C")
        file_menu.add_separator()
        file_menu.add_command(label="Quit", command=self.root.quit, accelerator="Cmd+Q")
        menubar.add_cascade(label="File", menu=file_menu)
        self.root.config(menu=menubar)
        self.root.bind_all("<Command-o>" if sys.platform == "darwin" else "<Control-o>", lambda e: self.browse_fsp())

    def _build_ui(self):
        T = THEME

        # ==================================================================
        # Top Control & Toolbar Strip
        # ==================================================================
        toolbar = tk.Frame(self.root, bg=T["bar_bg"], padx=12, pady=8)
        toolbar.pack(fill=tk.X, side=tk.TOP)

        # App branding + Open buttons
        tk.Label(
            toolbar, text="◆", font=(FONT_UI, 16), bg=T["bar_bg"], fg=T["port_yellow"]
        ).pack(side=tk.LEFT, padx=(0, 6))
        tk.Label(
            toolbar, text="FSP Viewer & Plotter", font=(FONT_UI, 13, "bold"), bg=T["bar_bg"], fg=T["text"]
        ).pack(side=tk.LEFT, padx=(0, 12))

        FlatButton(
            toolbar,
            "📂 Open .fsp…",
            self.browse_fsp,
            variant="primary",
            size=10,
            bold=True,
            padx=12,
            pady=5,
            tooltip="Open an Ansys Lumerical FDTD project file (.fsp)",
        ).pack(side=tk.LEFT, padx=(0, 6))

        FlatButton(
            toolbar,
            "📂 Open .h5…",
            self.browse_h5,
            variant="secondary",
            size=10,
            padx=10,
            pady=5,
            tooltip="Optionally load or switch the companion HDF5 monitor field file (.h5)",
        ).pack(side=tk.LEFT, padx=(0, 10))

        self.btn_save_geometry = FlatButton(
            toolbar,
            "💾 Save Geometry ▾",
            self._show_save_geometry_menu,
            variant="secondary",
            size=10,
            padx=10,
            pady=5,
            tooltip="Save the displayed geometry as a GDS file, or as a Python script that builds it "
                    "with gdsfactory rectangles and tapers",
        )
        self.btn_save_geometry.pack(side=tk.LEFT, padx=(0, 10))
        self.save_geometry_menu = tk.Menu(self.root, tearoff=0)
        self.save_geometry_menu.add_command(label="GDS file (.gds)…", command=lambda: self.save_geometry("gds"))
        self.save_geometry_menu.add_command(label="Python script for gdsfactory (.py)…",
                                            command=lambda: self.save_geometry("py"))

        self.file_lbl = tk.Label(
            toolbar,
            text="No file loaded",
            font=(FONT_MONO, 9),
            bg=T["bar_bg"],
            fg=T["text_muted"],
        )
        self.file_lbl.pack(side=tk.LEFT, padx=(0, 12))

        # Right side controls: Port selection dropdown + Results selection dropdown + Plot + Export Data
        FlatButton(
            toolbar,
            "📋 Export Data",
            self.export_data_to_clipboard,
            variant="success",
            size=10,
            bold=True,
            padx=12,
            pady=5,
            tooltip="Export the currently selected and visualized plot data as a table to the system clipboard",
        ).pack(side=tk.RIGHT, padx=(6, 0))

        FlatButton(
            toolbar,
            "📈 Plot",
            self.plot_selected,
            variant="primary",
            size=10,
            bold=True,
            padx=14,
            pady=5,
            tooltip="Plot the selected Port and Result in the fifth larger plot window",
        ).pack(side=tk.RIGHT, padx=(6, 0))

        # Wavelength selector (for multi-frequency H5 plots)
        self.lam_var = tk.StringVar(value="Center λ")
        self.cmb_lam = ttk.Combobox(
            toolbar,
            textvariable=self.lam_var,
            state="readonly",
            width=11,
            style="Dark.TCombobox",
            font=(FONT_UI, 10),
            values=["Center λ"],
        )
        self.cmb_lam.pack(side=tk.RIGHT, padx=(4, 4))
        self.cmb_lam.bind("<<ComboboxSelected>>", lambda e: self.plot_selected())
        Tooltip(self.cmb_lam, "Select wavelength λ for single-wavelength field plots")

        # Results dropdown menu
        self.result_var = tk.StringVar(value=self.RESULT_OPTIONS[0])
        self.cmb_result = ttk.Combobox(
            toolbar,
            textvariable=self.result_var,
            state="readonly",
            width=32,
            style="Dark.TCombobox",
            font=(FONT_UI, 10),
            values=self.RESULT_OPTIONS,
        )
        self.cmb_result.pack(side=tk.RIGHT, padx=(4, 4))
        self.cmb_result.bind("<<ComboboxSelected>>", lambda e: self.plot_selected())
        tk.Label(
            toolbar, text="Result:", font=(FONT_UI, 10, "bold"), bg=T["bar_bg"], fg=T["text_muted"]
        ).pack(side=tk.RIGHT, padx=(8, 2))

        # Port dropdown menu
        self.port_var = tk.StringVar(value="All Ports")
        self.cmb_port = ttk.Combobox(
            toolbar,
            textvariable=self.port_var,
            state="readonly",
            width=16,
            style="Dark.TCombobox",
            font=(FONT_UI, 10),
            values=["All Ports"],
        )
        self.cmb_port.pack(side=tk.RIGHT, padx=(4, 4))
        self.cmb_port.bind("<<ComboboxSelected>>", self._on_port_dropdown_changed)
        tk.Label(
            toolbar, text="Port:", font=(FONT_UI, 10, "bold"), bg=T["bar_bg"], fg=T["port_yellow"]
        ).pack(side=tk.RIGHT, padx=(8, 2))

        # ==================================================================
        # Main Resizable Split Area:
        #   Left Side  : 4-View Visualization Field (top) + Model Parameters Editor (bottom)
        #   Right Side : Fifth Larger Plot Window (docked + pop-out option)
        # ==================================================================
        self.main_paned = tk.PanedWindow(
            self.root, orient=tk.HORIZONTAL, bg=T["app_bg"], sashwidth=6, bd=0, opaqueresize=True
        )
        self.main_paned.pack(fill=tk.BOTH, expand=True, padx=6, pady=(4, 6))

        # Left vertical split (4-View Visualization Field + Parameter Editor)
        self.left_paned = tk.PanedWindow(
            self.main_paned, orient=tk.VERTICAL, bg=T["app_bg"], sashwidth=6, bd=0, opaqueresize=True
        )

        # ------------------------------------------------------------------
        # 1. Four-Picture Visualization Field (2x2 Grid)
        #    Up-Left     : XY View
        #    Bottom-Left : XZ View
        #    Top-Right   : 3D View (movable by pointer)
        #    Bottom-Right: YZ View
        # ------------------------------------------------------------------
        quad_frame = tk.Frame(self.left_paned, bg=T["app_bg"])
        quad_frame.grid_rowconfigure(0, weight=1, uniform="qrow")
        quad_frame.grid_rowconfigure(1, weight=1, uniform="qrow")
        quad_frame.grid_columnconfigure(0, weight=1, uniform="qcol")
        quad_frame.grid_columnconfigure(1, weight=1, uniform="qcol")

        self.view_xy = ModelOrthoCanvas2D(
            quad_frame, "xy", "XY View (Top)", "x", "y", on_port_clicked=self.select_port
        )
        self.view_xz = ModelOrthoCanvas2D(
            quad_frame, "xz", "XZ View (Side)", "x", "z", on_port_clicked=self.select_port
        )
        self.view_3d = ModelView3DCanvas(
            quad_frame, on_port_clicked=self.select_port
        )
        self.view_yz = ModelOrthoCanvas2D(
            quad_frame, "yz", "YZ View (Cross-Section)", "y", "z", on_port_clicked=self.select_port
        )

        self.view_xy.grid(row=0, column=0, sticky="nsew", padx=(0, 3), pady=(0, 3))
        self.view_3d.grid(row=0, column=1, sticky="nsew", padx=(3, 0), pady=(0, 3))
        self.view_xz.grid(row=1, column=0, sticky="nsew", padx=(0, 3), pady=(3, 0))
        self.view_yz.grid(row=1, column=1, sticky="nsew", padx=(3, 0), pady=(3, 0))

        # ------------------------------------------------------------------
        # 2. Editor-Like Model Parameters Panel
        # ------------------------------------------------------------------
        self.param_editor = ParameterEditorPanel(self.left_paned)

        self.left_paned.add(quad_frame, minsize=360, height=560)
        self.left_paned.add(self.param_editor, minsize=180, height=320)

        # ------------------------------------------------------------------
        # 3. Fifth Larger Plot Window (Right Pane + Pop-Out Window Button)
        # ------------------------------------------------------------------
        self.plot_panel = tk.Frame(
            self.main_paned, bg=T["panel_bg"], highlightthickness=1, highlightbackground=T["border"]
        )
        plot_header = tk.Frame(self.plot_panel, bg=T["panel_bg"])
        plot_header.pack(fill=tk.X, side=tk.TOP)

        tk.Label(
            plot_header,
            text="Results Plot Window (5th Window)",
            font=(FONT_UI, 11, "bold"),
            bg=T["panel_bg"],
            fg=T["text"],
        ).pack(side=tk.LEFT, padx=(10, 8), pady=5)

        self.status_lbl = tk.Label(
            plot_header,
            text="",
            font=(FONT_UI, 9),
            bg=T["panel_bg"],
            fg=T["success"],
        )
        self.status_lbl.pack(side=tk.LEFT, padx=6)

        FlatButton(
            plot_header,
            "↗ Separate Window",
            self.open_popout_plot_window,
            variant="ghost",
            size=9,
            padx=8,
            pady=3,
            tooltip="Open or raise a standalone large 5th plot window",
        ).pack(side=tk.RIGHT, padx=(2, 8), pady=3)

        FlatButton(
            plot_header,
            "📋 Export Data",
            self.export_data_to_clipboard,
            variant="ghost",
            size=9,
            padx=8,
            pady=3,
            tooltip="Copy the currently plotted data table to the clipboard",
        ).pack(side=tk.RIGHT, padx=2, pady=3)

        # Matplotlib Figure & Canvas
        self.fig = Figure(figsize=(7.6, 6.4), dpi=100, facecolor="#12151a")
        self.mpl_canvas = FigureCanvasTkAgg(self.fig, master=self.plot_panel)
        self.mpl_canvas.get_tk_widget().configure(bg="#12151a", highlightthickness=0)
        self.mpl_canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        toolbar_frame = tk.Frame(self.plot_panel, bg=T["bar_bg"])
        toolbar_frame.pack(fill=tk.X, side=tk.BOTTOM)
        self.mpl_toolbar = NavigationToolbar2Tk(self.mpl_canvas, toolbar_frame, pack_toolbar=False)
        self.mpl_toolbar.update()
        self.mpl_toolbar.pack(side=tk.LEFT, fill=tk.X)

        self.main_paned.add(self.left_paned, minsize=580, width=860)
        self.main_paned.add(self.plot_panel, minsize=480, width=760)

        self._draw_empty_plot()

    # ==================================================================
    # File Loading & Port Selection
    # ==================================================================
    def browse_fsp(self):
        path = filedialog.askopenfilename(
            parent=self.root,
            title="Open Ansys Lumerical FDTD File (.fsp)",
            filetypes=[("Lumerical FDTD files", "*.fsp"), ("All files", "*.*")],
        )
        if path:
            self.open_fsp_file(path)

    def browse_h5(self):
        if self.model is None:
            messagebox.showinfo("Open .fsp First", "Please open an .fsp project file first.")
            return
        path = filedialog.askopenfilename(
            parent=self.root,
            title="Open Companion HDF5 Field File (.h5)",
            filetypes=[("HDF5 files", "*.h5 *.hdf5"), ("All files", "*.*")],
        )
        if path:
            self.model.load_h5(path)
            self._refresh_after_load()

    def open_fsp_file(self, fsp_path, h5_path=None):
        try:
            self.model = FSPModelData(fsp_path, h5_path)
        except Exception as exc:
            messagebox.showerror("Error Opening .fsp File", f"Could not read {fsp_path}:\n\n{exc}")
            return
        self._refresh_after_load()

    def _refresh_after_load(self):
        if self.model is None:
            return
        fsp_name = os.path.basename(self.model.fsp_path)
        h5_name = os.path.basename(self.model.h5_path) if self.model.h5_path else "no .h5"
        self.file_lbl.config(
            text=f"{fsp_name}  ({h5_name})",
            fg=THEME["text"],
        )
        self.root.title(f"{APP_NAME} — {fsp_name}")

        # Populate Port dropdown
        port_choices = ["All Ports"] + [pt["name"] for pt in self.model.ports]
        for mm in self.model.mode_monitors:
            port_choices.append(mm["name"])
        self.cmb_port["values"] = port_choices
        default_port = self.model.ports[0]["name"] if self.model.ports else "All Ports"
        self.port_var.set("All Ports" if len(self.model.ports) > 1 else default_port)

        # Populate Wavelength dropdown
        freqs, lam, kc = self.model.get_frequencies_and_wavelengths()
        lam_choices = [f"Center ({lam[kc]:.1f} nm)"] + [f"{l:.1f} nm (#{idx})" for idx, l in enumerate(lam)]
        self.cmb_lam["values"] = lam_choices
        self.lam_var.set(lam_choices[0])

        # Update 4 views and Parameter Editor
        sel_port = self.port_var.get()
        self.view_xy.set_model(self.model, sel_port)
        self.view_xz.set_model(self.model, sel_port)
        self.view_yz.set_model(self.model, sel_port)
        self.view_3d.set_model(self.model, sel_port)
        self.param_editor.set_model(self.model)

        # Automatically plot initial result in the 5th window
        self.plot_selected()

    def select_port(self, port_name):
        if port_name in self.cmb_port["values"]:
            self.port_var.set(port_name)
        self._on_port_dropdown_changed()

    def _on_port_dropdown_changed(self, _event=None):
        sel_port = self.port_var.get()
        self.view_xy.set_selected_port(sel_port)
        self.view_xz.set_selected_port(sel_port)
        self.view_yz.set_selected_port(sel_port)
        self.view_3d.set_selected_port(sel_port)
        self.plot_selected()

    def _selected_freq_index(self):
        if self.model is None:
            return 0
        _, lam, kc = self.model.get_frequencies_and_wavelengths()
        txt = self.lam_var.get()
        m = re.search(r"#(\d+)", txt)
        if m:
            idx = int(m.group(1))
            if 0 <= idx < len(lam):
                return idx
        return kc

    # ==================================================================
    # Plotting in the 5th Larger Window & Data Export Preparation
    # ==================================================================
    def _draw_empty_plot(self, message="Open an .fsp file and click 'Plot'"):
        self.fig.clear()
        ax = self.fig.add_subplot(111)
        ax.set_facecolor("#12151a")
        ax.text(
            0.5, 0.5, message, color=THEME["text_muted"], fontsize=12, ha="center", va="center", transform=ax.transAxes
        )
        ax.set_xticks([])
        ax.set_yticks([])
        for sp in ax.spines.values():
            sp.set_color(THEME["border"])
        self.mpl_canvas.draw_idle()

    def _style_mpl_axes(self, axs):
        if not isinstance(axs, (list, tuple, np.ndarray)):
            axs = [axs]
        else:
            axs = np.asarray(axs).ravel().tolist()
        for ax in axs:
            ax.set_facecolor("#161a22")
            ax.tick_params(colors=THEME["text_muted"], labelsize=8.5)
            ax.xaxis.label.set_color(THEME["text"])
            ax.yaxis.label.set_color(THEME["text"])
            ax.title.set_color(THEME["text"])
            for sp in ax.spines.values():
                sp.set_color(THEME["border"])

    def open_popout_plot_window(self):
        """Opens or raises a standalone fifth larger window for the selected plot."""
        if self.popout_win is not None and self.popout_win.winfo_exists():
            self.popout_win.lift()
            self.popout_win.focus_force()
            self._render_onto_figure(self.popout_fig)
            self.popout_canvas.draw_idle()
            return

        win = tk.Toplevel(self.root)
        win.title(f"{APP_NAME} — Fifth Plot Window")
        win.geometry("1080x760")
        win.configure(bg=THEME["panel_bg"])
        self.popout_win = win

        top = tk.Frame(win, bg=THEME["bar_bg"], padx=10, pady=6)
        top.pack(fill=tk.X, side=tk.TOP)
        tk.Label(
            top,
            textvariable=self.result_var,
            font=(FONT_UI, 12, "bold"),
            bg=THEME["bar_bg"],
            fg=THEME["text"],
        ).pack(side=tk.LEFT, padx=6)
        FlatButton(
            top,
            "📋 Export Data to Clipboard",
            self.export_data_to_clipboard,
            variant="success",
            size=10,
            bold=True,
            padx=12,
            pady=4,
        ).pack(side=tk.RIGHT, padx=6)

        self.popout_fig = Figure(figsize=(10.2, 6.8), dpi=100, facecolor="#12151a")
        self.popout_canvas = FigureCanvasTkAgg(self.popout_fig, master=win)
        self.popout_canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        tb_frame = tk.Frame(win, bg=THEME["bar_bg"])
        tb_frame.pack(fill=tk.X, side=tk.BOTTOM)
        tb = NavigationToolbar2Tk(self.popout_canvas, tb_frame, pack_toolbar=False)
        tb.update()
        tb.pack(side=tk.LEFT, fill=tk.X)

        self._render_onto_figure(self.popout_fig)
        self.popout_canvas.draw_idle()

    def plot_selected(self):
        if self.model is None:
            self._draw_empty_plot("Please open an .fsp file first.")
            return
        self.status_lbl.config(text="")
        self._render_onto_figure(self.fig)
        self.mpl_canvas.draw_idle()
        if self.popout_win is not None and self.popout_win.winfo_exists():
            self._render_onto_figure(self.popout_fig)
            self.popout_canvas.draw_idle()

    def _get_target_ports(self, max_all=4):
        """Returns list of port/monitor names to plot based on the Port dropdown."""
        sel = self.port_var.get()
        all_names = [pt["name"] for pt in self.model.ports if pt.get("mode_exp_obj") is not None]
        if len(all_names) < 2 and self.model.mode_monitors:
            for mm in self.model.mode_monitors:
                if mm["name"] not in all_names:
                    all_names.append(mm["name"])
        if not all_names:
            all_names = [pt["name"] for pt in self.model.ports]
        if sel == "All Ports" or not sel:
            if len(all_names) > max_all:
                # Pick representative subset like fsp_plots.py (first, second, middle, last)
                idxs = np.linspace(0, len(all_names) - 1, max_all, dtype=int)
                return [all_names[i] for i in idxs]
            return all_names
        return [sel]

    def _render_onto_figure(self, fig: Figure):
        fig.clear()
        res_kind = self.result_var.get()
        port_sel = self.port_var.get()
        k_freq = self._selected_freq_index()
        freqs, lam, _ = self.model.get_frequencies_and_wavelengths()

        try:
            if res_kind == "Cross-Sections & Port Modes (n & |E|²)":
                self._plot_cross_sections_and_modes(fig, port_sel)
            elif res_kind == "Stored Port Mode |E|² Profile (YZ)":
                self._plot_stored_single_quantity(fig, port_sel, quantity="E2")
            elif res_kind == "Stored Refractive Index n(y, z) (YZ)":
                self._plot_stored_single_quantity(fig, port_sel, quantity="n")
            elif res_kind == "Simulated Port Field |E|² (YZ, .h5)":
                self._plot_simulated_port_fields(fig, port_sel, k_freq, lam)
            elif res_kind == "Power & Mode Coupling vs. Port x (.h5)":
                self._plot_power_and_mode_coupling(fig, port_sel)
            elif res_kind == "Port Transmission vs. Wavelength λ (.h5)":
                self._plot_transmission_vs_wavelength(fig, port_sel)
            elif res_kind == "XZ Propagation |E|² at y=0 (dB, .h5)":
                self._plot_xz_propagation(fig, k_freq, lam)
            elif res_kind == "Top-View Layout & Taper Zoom (XY)":
                self._plot_layout_top_view(fig, port_sel)
            elif res_kind == "Port Mode Field Components (|Ex|..|Hz|)":
                self._plot_port_field_components(fig, port_sel, k_freq, lam)
            elif res_kind == "Source Time Signal & Spectrum":
                self._plot_source_pulse(fig)
            else:
                self._plot_cross_sections_and_modes(fig, port_sel)
        except Exception as exc:
            fig.clear()
            ax = fig.add_subplot(111)
            self._style_mpl_axes(ax)
            ax.text(
                0.5,
                0.5,
                f"Cannot plot '{res_kind}':\n\n{exc}",
                color=THEME["warning"],
                fontsize=10.5,
                ha="center",
                va="center",
                transform=ax.transAxes,
                wrap=True,
            )
            ax.set_xticks([])
            ax.set_yticks([])

    # ------------------------------------------------------------------
    # Individual Plot Implementations (matching fsp_plots.py + export data)
    # ------------------------------------------------------------------
    def _plot_cross_sections_and_modes(self, fig: Figure, port_sel: str):
        show = self._get_target_ports(max_all=4)
        if not show:
            raise RuntimeError("No ports or mode expansion monitors found in this .fsp file.")

        ncols = len(show)
        axs = fig.subplots(2, ncols, squeeze=False)
        self._style_mpl_axes(axs)

        port_x_map = {pt["name"]: pt["x"] for pt in self.model.ports}
        for mm in self.model.mode_monitors:
            port_x_map[mm["name"]] = mm["x"]

        im_n = None
        im_e = None
        export_blocks = []

        for j, pname in enumerate(show):
            y, z, M, neff, mode_lam = self.model.stored_mode(pname)
            E2 = sum(np.abs(M[f"E{c}"]) ** 2 for c in "xyz")
            E2_norm = E2 / max(float(E2.max()), 1e-30)

            im_n = axs[0, j].pcolormesh(y, z, M["n"], shading="auto", cmap="viridis", vmin=1.0, vmax=max(2.0, float(M["n"].max())))
            x_val = port_x_map.get(pname, 0.0)
            neff_str = f", n_eff={neff:.4f}" if neff else ""
            axs[0, j].set_title(f"{pname} (x={x_val:.1f} μm)\nRefractive index{neff_str}", fontsize=9)

            im_e = axs[1, j].pcolormesh(y, z, E2_norm, shading="auto", cmap="inferno", vmin=0.0, vmax=1.0)
            try:
                axs[1, j].contour(y, z, M["n"], levels=[1.2, 1.445, 1.7], colors="w", linewidths=0.5)
            except Exception:
                pass
            axs[1, j].set_title("Port mode |E|² (normalised)", fontsize=9)

            for ax in axs[:, j]:
                ax.set_aspect("equal")
                ax.set_xlabel("y (μm)", fontsize=8.5)
            axs[0, j].set_ylabel("z (μm)", fontsize=8.5)
            axs[1, j].set_ylabel("z (μm)", fontsize=8.5)

            export_blocks.append((pname, y, z, M["n"], E2_norm))

        if im_n is not None:
            cb1 = fig.colorbar(im_n, ax=axs[0, :].tolist(), shrink=0.82, pad=0.02)
            cb1.set_label("n", color=THEME["text"])
            cb1.ax.yaxis.set_tick_params(color=THEME["text_muted"], labelcolor=THEME["text_muted"])
        if im_e is not None:
            cb2 = fig.colorbar(im_e, ax=axs[1, :].tolist(), shrink=0.82, pad=0.02)
            cb2.set_label("|E|² norm.", color=THEME["text"])
            cb2.ax.yaxis.set_tick_params(color=THEME["text_muted"], labelcolor=THEME["text_muted"])

        fig.suptitle(
            "Cross-sections and port modes stored in the .fsp",
            color=THEME["text"],
            fontsize=11,
            fontweight="bold",
        )
        fig.set_layout_engine("constrained")

        # Format export TSV for the first/selected port
        pname, y, z, n_mat, e2_mat = export_blocks[0]
        self._store_grid_export(
            f"Stored Mode |E|^2 & Refractive Index for {pname}",
            y,
            z,
            [("n", n_mat), ("E2_norm", e2_mat)],
        )

    def _plot_stored_single_quantity(self, fig: Figure, port_sel: str, quantity="E2"):
        show = self._get_target_ports(max_all=4)
        ncols = len(show)
        axs = fig.subplots(1, ncols, squeeze=False)
        self._style_mpl_axes(axs)

        im = None
        first_export = None
        for j, pname in enumerate(show):
            y, z, M, neff, _ = self.model.stored_mode(pname)
            ax = axs[0, j]
            if quantity == "n":
                mat = M["n"]
                im = ax.pcolormesh(y, z, mat, shading="auto", cmap="viridis")
                ax.set_title(f"{pname}\nRefractive index n(y, z)", fontsize=9.5)
            else:
                E2 = sum(np.abs(M[f"E{c}"]) ** 2 for c in "xyz")
                mat = E2 / max(float(E2.max()), 1e-30)
                im = ax.pcolormesh(y, z, mat, shading="auto", cmap="inferno", vmin=0.0, vmax=1.0)
                try:
                    ax.contour(y, z, M["n"], levels=[1.2, 1.445, 1.7], colors="w", linewidths=0.6)
                except Exception:
                    pass
                neff_txt = f" (n_eff={neff:.4f})" if neff else ""
                ax.set_title(f"{pname}{neff_txt}\nStored Mode |E|²", fontsize=9.5)
            ax.set_aspect("equal")
            ax.set_xlabel("y (μm)")
            if j == 0:
                ax.set_ylabel("z (μm)")
            if first_export is None:
                first_export = (pname, y, z, mat)

        if im is not None:
            cb = fig.colorbar(im, ax=axs[0, :].tolist(), shrink=0.82)
            cb.set_label("n" if quantity == "n" else "|E|² (norm.)", color=THEME["text"])
            cb.ax.yaxis.set_tick_params(color=THEME["text_muted"], labelcolor=THEME["text_muted"])

        fig.set_layout_engine("constrained")
        if first_export:
            pname, y, z, mat = first_export
            self._store_grid_export(f"Stored {quantity} for {pname}", y, z, [(quantity, mat)])

    def _plot_simulated_port_fields(self, fig: Figure, port_sel: str, k_freq: int, lam: np.ndarray):
        if self.model.h5 is None:
            raise RuntimeError("Companion .h5 file is required to plot simulated DFT port fields.")

        ports = self.model.ports if port_sel == "All Ports" else [
            pt for pt in self.model.ports if pt["name"] == port_sel
        ]
        if not ports:
            ports = self.model.ports

        ncols = len(ports)
        axs = fig.subplots(1, ncols, squeeze=False)
        self._style_mpl_axes(axs)

        im = None
        first_export = None
        for j, pt in enumerate(ports):
            ax = axs[0, j]
            m = pt["h5_group"]
            y = self.model.h5[m]["y"][()]
            z = self.model.h5[m]["z"][()]
            E2 = sum(np.abs(self.model.h5_field(m, c, k_freq)[:, :, 0]) ** 2 for c in ("Ex", "Ey", "Ez"))
            if y[0] >= -1e-6 and len(y) > 1:
                yy = np.concatenate([-y[:0:-1], y])
                E2f = np.concatenate([E2[:, :0:-1], E2], axis=1)
            else:
                yy, E2f = y, E2
            E2_norm = E2f / max(float(E2f.max()), 1e-30)
            im = ax.pcolormesh(yy, z, E2_norm, shading="auto", cmap="inferno", vmin=0.0, vmax=1.0)
            try:
                ys, zs, M, _, _ = self.model.stored_mode(pt["name"])
                ax.contour(ys, zs, M["n"], levels=[1.2, 1.445, 1.7], colors="w", linewidths=0.5)
            except Exception:
                pass
            ax.set_xlim(yy[0], yy[-1])
            ax.set_ylim(z[0], z[-1])
            ax.set_aspect("equal")
            x_h5 = float(self.model.h5[m]["x"][0])
            ax.set_title(f"{pt['name'].replace('output_', '')}\nx = {x_h5:.2f} μm", fontsize=9)
            ax.set_xlabel("y (μm)")
            if j == 0:
                ax.set_ylabel("z (μm)")
            if first_export is None:
                first_export = (pt["name"], yy, z, E2_norm)

        if im is not None:
            cb = fig.colorbar(im, ax=axs[0, :].tolist(), shrink=0.8)
            cb.set_label("|E|² (norm.)", color=THEME["text"])
            cb.ax.yaxis.set_tick_params(color=THEME["text_muted"], labelcolor=THEME["text_muted"])

        fig.suptitle(
            f"Simulated |E|² in port plane (normalised), λ = {lam[k_freq]:.1f} nm (white = index contours)",
            color=THEME["text"],
            fontsize=10.5,
            fontweight="bold",
        )
        fig.set_layout_engine("constrained")
        if first_export:
            pname, yy, z, mat = first_export
            self._store_grid_export(
                f"Simulated Port |E|^2 ({pname}, lambda={lam[k_freq]:.1f} nm)",
                yy,
                z,
                [("E2_norm", mat)],
            )

    def _plot_power_and_mode_coupling(self, fig: Figure, port_sel: str):
        xs, freqs, lam, P, Prel, eta, Tmode = self.model.compute_power_and_mode_coupling()
        a1, a2 = fig.subplots(1, 2)
        self._style_mpl_axes([a1, a2])

        start_idx = 1 if len(xs) > 1 else 0
        for k in range(len(freqs)):
            a1.plot(xs[start_idx:], Prel[start_idx:, k], "o-", label=f"{lam[k]:.0f} nm", lw=1.8, markersize=5)
            a2.plot(xs[start_idx:], Tmode[start_idx:, k], "s-", label=f"{lam[k]:.0f} nm", lw=1.8, markersize=5)

        # Highlight selected port with vertical yellow line if a specific port is selected
        for pt in self.model.ports:
            if pt["name"] == port_sel:
                for ax in (a1, a2):
                    ax.axvline(pt["x"], color=THEME["port_yellow"], ls="--", lw=1.5, label=f"Selected: {pt['name']}")

        a1.set_title("Total forward power / input power (P / Pin)", fontsize=10)
        a1.set_xlabel("x (μm)")
        a1.set_ylabel("P / Pin")
        a1.legend(fontsize=8, facecolor="#1a1e25", edgecolor=THEME["border"], labelcolor=THEME["text"])
        a1.grid(alpha=0.25, color=THEME["grid_axis"])

        a2.set_title("Power in stored port mode / input power (Tmode)", fontsize=10)
        a2.set_xlabel("x (μm)")
        a2.set_ylabel("Tmode")
        a2.legend(fontsize=8, facecolor="#1a1e25", edgecolor=THEME["border"], labelcolor=THEME["text"])
        a2.grid(alpha=0.25, color=THEME["grid_axis"])

        fig.set_layout_engine("constrained")

        # Build export table matching port_power_table.txt from fsp_plots.py
        header_cols = (
            ["x_um", "port"]
            + [f"P/Pin@{l:.1f}nm" for l in lam]
            + [f"mode/Pin@{l:.1f}nm" for l in lam]
        )
        rows = ["\t".join(header_cols)]
        for i, pt in enumerate(self.model.ports):
            vals = [f"{xs[i]:.4f}", pt["name"]]
            vals += [f"{v:.6f}" for v in Prel[i]]
            vals += [f"{v:.6f}" for v in Tmode[i]]
            rows.append("\t".join(vals))
        self.last_exported_tsv = "\n".join(rows)
        self.last_exported_desc = f"Port Power & Mode Coupling Table ({len(self.model.ports)} ports × {len(lam)} λ)"

    def _plot_transmission_vs_wavelength(self, fig: Figure, port_sel: str):
        xs, freqs, lam, P, Prel, eta, Tmode = self.model.compute_power_and_mode_coupling()
        a1, a2 = fig.subplots(1, 2)
        self._style_mpl_axes([a1, a2])

        order = np.argsort(lam)
        lam_s = lam[order]

        if port_sel != "All Ports":
            indices = [i for i, pt in enumerate(self.model.ports) if pt["name"] == port_sel]
        else:
            indices = list(range(1, len(self.model.ports))) if len(self.model.ports) > 1 else [0]

        rows = ["wavelength_nm\tport\tx_um\tP_over_Pin\tTmode_over_Pin\tInsertionLoss_dB"]
        for i in indices:
            pname = self.model.ports[i]["name"]
            p_lin = Prel[i, order]
            t_lin = Tmode[i, order]
            t_db = 10.0 * np.log10(np.clip(t_lin, 1e-12, None))
            a1.plot(lam_s, p_lin, "o--", alpha=0.7, label=f"{pname} P/Pin")
            a1.plot(lam_s, t_lin, "s-", lw=2.0, label=f"{pname} Mode T")
            a2.plot(lam_s, t_db, "s-", lw=2.0, label=f"{pname} (x={xs[i]:g} μm)")
            for l_val, pv, tv, dbv in zip(lam_s, p_lin, t_lin, t_db):
                rows.append(f"{l_val:.2f}\t{pname}\t{xs[i]:.3f}\t{pv:.6f}\t{tv:.6f}\t{dbv:.4f}")

        a1.set_title("Linear Transmission vs. Wavelength λ", fontsize=10)
        a1.set_xlabel("Wavelength λ (nm)")
        a1.set_ylabel("Transmission (linear)")
        a1.grid(alpha=0.25, color=THEME["grid_axis"])
        a1.legend(fontsize=8, facecolor="#1a1e25", edgecolor=THEME["border"], labelcolor=THEME["text"])

        a2.set_title("Mode Coupling Transmission (dB) vs. Wavelength λ", fontsize=10)
        a2.set_xlabel("Wavelength λ (nm)")
        a2.set_ylabel("Mode Coupling (dB)")
        a2.grid(alpha=0.25, color=THEME["grid_axis"])
        a2.legend(fontsize=8, facecolor="#1a1e25", edgecolor=THEME["border"], labelcolor=THEME["text"])

        fig.set_layout_engine("constrained")
        self.last_exported_tsv = "\n".join(rows)
        self.last_exported_desc = f"Port Transmission Spectrum ({len(indices)} ports × {len(lam_s)} wavelengths)"

    def _plot_xz_propagation(self, fig: Figure, k_freq: int, lam: np.ndarray):
        if self.model.h5 is None:
            raise RuntimeError("Companion .h5 file is required to plot XZ propagation fields.")
        mon_info = self.model.dft_monitors.get("dft_xz_propagation")
        h5_grp = mon_info["h5_group"] if mon_info and mon_info["h5_group"] else "Monitor7"
        if h5_grp not in self.model.h5:
            raise RuntimeError(f"XZ propagation group '{h5_grp}' not found in .h5 file.")

        g = self.model.h5[h5_grp]
        x, z = g["x"][()], g["z"][()]
        axs = fig.subplots(2, 1)
        self._style_mpl_axes(axs)

        E2 = sum(np.abs(self.model.h5_field(h5_grp, c, k_freq)[:, 0, :]) ** 2 for c in ("Ex", "Ey", "Ez"))
        dB = 10.0 * np.log10(E2 / max(float(E2.max()), 1e-30) + 1e-12)

        for ax, xr, title_suffix in (
            (axs[0], None, "Full device"),
            (axs[1], (185.0, float(x[-1])) if x[-1] > 190 else None, "Tip & output ports zoom"),
        ):
            sel = slice(None) if xr is None else (x >= xr[0]) & (x <= xr[1])
            im = ax.pcolormesh(x[sel], z, dB[:, sel], shading="auto", cmap="inferno", vmin=-35, vmax=0, rasterized=True)
            # Draw yellow port lines on top of XZ field
            for pt in self.model.ports:
                if xr is None or (xr[0] <= pt["x"] <= xr[1]):
                    ax.axvline(pt["x"], color=THEME["port_yellow"], ls="--", lw=1.2)
            ax.set_ylabel("z (μm)")
            ax.set_title(f"|E|² at y = 0 (dB), λ = {lam[k_freq]:.1f} nm — {title_suffix}", fontsize=9.5)
            ax.set_xlim(*(xr or (x[0], x[-1])))

        axs[-1].set_xlabel("x (μm)")
        cb = fig.colorbar(im, ax=axs.tolist(), shrink=0.75, label="dB")
        cb.set_label("dB", color=THEME["text"])
        cb.ax.yaxis.set_tick_params(color=THEME["text_muted"], labelcolor=THEME["text_muted"])
        fig.set_layout_engine("constrained")

        # Subsample x if very large (>500 cols) for responsive clipboard export
        step = max(1, len(x) // 400)
        self._store_grid_export(
            f"XZ Propagation |E|^2 (dB) at y=0, lambda={lam[k_freq]:.1f} nm",
            x[::step],
            z,
            [("E2_dB", dB[:, ::step])],
            u_name="x_um",
            v_name="z_um",
        )

    def _plot_layout_top_view(self, fig: Figure, port_sel: str):
        a1, a2 = fig.subplots(2, 1, gridspec_kw={"height_ratios": [1.35, 1.0]})
        self._style_mpl_axes([a1, a2])

        xs_all = [v for st in self.model.structures for v in (st["xmin"], st["xmax"])] or [-35, 225]
        ys_all = [v for st in self.model.structures for v in (st["ymin"], st["ymax"])] or [-25, 25]
        core_ys = [v for st in self.model.structures if st["is_core"] for v in (st["ymin"], st["ymax"])] or [-0.6, 0.6]
        core_lim = max(0.6, max(abs(v) for v in core_ys) * 1.6)

        for ax, ylim in ((a1, (min(ys_all) * 1.05, max(ys_all) * 1.05)), (a2, (-core_lim, core_lim))):
            for st in self.model.structures:
                if st["is_background"] and not st["gds_layer"]:
                    continue
                ax.add_patch(
                    MplPolygon(
                        st["xy_poly"],
                        closed=True,
                        fc=st["fill"],
                        ec=st["edge"],
                        lw=0.9,
                        alpha=0.85,
                        label=st["name"],
                    )
                )
            if self.model.fdtd_box:
                fx0, fx1, fy0, fy1, _, _ = self.model.fdtd_box
                ax.add_patch(
                    MplRectangle((fx0, fy0), fx1 - fx0, fy1 - fy0, fill=False, ec=THEME["fdtd_orange"], lw=1.5, label="FDTD region")
                )
            for pt in self.model.ports:
                is_sel = pt["name"] == port_sel
                ax.axvline(
                    pt["x"],
                    color=THEME["port_yellow_bright"] if is_sel else THEME["port_yellow"],
                    lw=2.2 if is_sel else 1.3,
                    ls="-" if is_sel else "--",
                    label="Port (yellow)" if pt["index"] == 0 else None,
                )
            ax.set_xlim(min(xs_all) - 5, max(xs_all) + 8)
            ax.set_ylim(*ylim)
            ax.set_xlabel("x (μm)")
            ax.set_ylabel("y (μm)")

        handles, labels = a1.get_legend_handles_labels()
        uniq = dict(zip(labels, handles))
        if uniq:
            a1.legend(uniq.values(), uniq.keys(), loc="lower left", fontsize=8, facecolor="#1a1e25", edgecolor=THEME["border"], labelcolor=THEME["text"])
        a1.set_title("Top view: structures, FDTD region, and yellow ports", fontsize=10)
        a2.set_title("Waveguide core zoom (y exaggerated)", fontsize=10)
        for pt in self.model.ports:
            a2.text(
                pt["x"],
                core_lim * 0.88,
                pt["name"].replace("output_", "").replace("_port", ""),
                rotation=90,
                fontsize=7.5,
                color=THEME["port_yellow"],
                ha="right",
                va="top",
            )
        fig.set_layout_engine("constrained")

        # Export polygon vertices + port coordinates
        rows = ["# Structure Polygons and Port Coordinates (um)", "type\tname\tx_um\ty_um\tzmin_um\tzmax_um"]
        for st in self.model.structures:
            for vx, vy in st["xy_poly"]:
                rows.append(f"structure\t{st['name']}\t{vx:.4f}\t{vy:.4f}\t{st['zmin']:.4f}\t{st['zmax']:.4f}")
        for pt in self.model.ports:
            rows.append(f"port\t{pt['name']}\t{pt['x']:.4f}\t{pt['y']:.4f}\t{pt['zmin']:.4f}\t{pt['zmax']:.4f}")
        self.last_exported_tsv = "\n".join(rows)
        self.last_exported_desc = "Top-View Geometry Polygons & Port Coordinates"

    def _plot_port_field_components(self, fig: Figure, port_sel: str, k_freq: int, lam: np.ndarray):
        target = self._get_target_ports(max_all=1)[0]
        y, z, M, neff, _ = self.model.stored_mode(target)
        axs = fig.subplots(2, 3)
        self._style_mpl_axes(axs)

        comps = [("Ex", axs[0, 0]), ("Ey", axs[0, 1]), ("Ez", axs[0, 2]),
                 ("Hx", axs[1, 0]), ("Hy", axs[1, 1]), ("Hz", axs[1, 2])]
        export_list = []
        for cname, ax in comps:
            arr = np.abs(M[cname])
            im = ax.pcolormesh(y, z, arr, shading="auto", cmap="magma")
            ax.set_title(f"|{cname}| ({target})", fontsize=9)
            ax.set_aspect("equal")
            ax.set_xlabel("y (μm)")
            ax.set_ylabel("z (μm)")
            cb = fig.colorbar(im, ax=ax, shrink=0.78)
            cb.ax.yaxis.set_tick_params(color=THEME["text_muted"], labelcolor=THEME["text_muted"])
            export_list.append((f"abs_{cname}", arr))

        fig.suptitle(f"Stored Mode Field Components for {target}", color=THEME["text"], fontsize=10.5, fontweight="bold")
        fig.set_layout_engine("constrained")
        self._store_grid_export(f"Mode Field Components for {target}", y, z, export_list)

    def _plot_source_pulse(self, fig: Figure):
        src_ob = None
        for ob in self.model.fsp.objects:
            if "time" in ob.results and "time_signal" in ob.results:
                src_ob = ob
                break
        if src_ob is None:
            raise RuntimeError("No Mode Source time-domain signal stored in this .fsp file.")

        t_fs = src_ob.results["time"].ravel() * 1e15
        sig = src_ob.results["time_signal"].ravel()
        f_vec = src_ob.results.get("f_vec", np.array([])).ravel()
        p0 = src_ob.results.get("P0", np.array([])).ravel()

        a1, a2 = fig.subplots(1, 2)
        self._style_mpl_axes([a1, a2])

        a1.plot(t_fs, np.real(sig), color="#4c8dff", lw=1.6, label="Re(signal)")
        if np.iscomplexobj(sig):
            a1.plot(t_fs, np.imag(sig), color="#ff6b6b", lw=1.2, alpha=0.8, label="Im(signal)")
        a1.set_title(f"{src_ob.name.split('::')[-1]} — Time Pulse", fontsize=10)
        a1.set_xlabel("Time (fs)")
        a1.set_ylabel("Amplitude")
        a1.grid(alpha=0.25, color=THEME["grid_axis"])
        a1.legend(fontsize=8, facecolor="#1a1e25", edgecolor=THEME["border"], labelcolor=THEME["text"])

        if f_vec.size and p0.size == f_vec.size:
            lam_nm = C0 / f_vec * 1e9
            a2.plot(lam_nm, np.abs(p0) / max(float(np.abs(p0).max()), 1e-30), "o-", color="#ffd700", lw=1.8)
            a2.set_title("Source Spectrum P0(λ) (normalised)", fontsize=10)
            a2.set_xlabel("Wavelength λ (nm)")
            a2.set_ylabel("Normalised Power")
            a2.grid(alpha=0.25, color=THEME["grid_axis"])

        fig.set_layout_engine("constrained")
        rows = ["time_fs\treal_signal\timag_signal"]
        for tv, sv in zip(t_fs, sig):
            rows.append(f"{tv:.4f}\t{ float(np.real(sv)):.6e}\t{float(np.imag(sv)):.6e}")
        self.last_exported_tsv = "\n".join(rows)
        self.last_exported_desc = f"Source Time Pulse ({len(t_fs)} samples)"

    def _store_grid_export(self, title, u_arr, v_arr, named_matrices, u_name="y_um", v_name="z_um"):
        """Formats 2D grid data (v_arr × u_arr) into a clean tab-separated table for clipboard export."""
        lines = [f"# {title}", f"# Grid: {len(v_arr)} {v_name} × {len(u_arr)} {u_name}"]
        header = [v_name, u_name] + [name for name, _ in named_matrices]
        lines.append("\t".join(header))
        # Subsample slightly if huge so clipboard stays snappy
        su = max(1, len(u_arr) // 200)
        sv = max(1, len(v_arr) // 200)
        for iv in range(0, len(v_arr), sv):
            for iu in range(0, len(u_arr), su):
                row = [f"{v_arr[iv]:.4f}", f"{u_arr[iu]:.4f}"]
                for _, mat in named_matrices:
                    row.append(f"{float(np.real(mat[iv, iu])):.6g}")
                lines.append("\t".join(row))
        self.last_exported_tsv = "\n".join(lines)
        self.last_exported_desc = f"{title} ({len(lines) - 3} data rows)"

    # ==================================================================
    # Save Geometry (GDS file or gdsfactory script)
    # ==================================================================
    def _show_save_geometry_menu(self):
        b = self.btn_save_geometry
        try:
            self.save_geometry_menu.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height())
        finally:
            self.save_geometry_menu.grab_release()

    def save_geometry(self, kind, path=None):
        """Save the displayed structures: kind 'gds' writes a GDSII file, 'py' a Python script
        that builds the same layout with gdsfactory. Returns the path, or None if nothing was saved."""
        if self.model is None:
            messagebox.showinfo("Open .fsp First", "Please open an .fsp project file first.")
            return None
        shapes = fsp_export.export_shapes(self.model.structures)
        if not shapes:
            messagebox.showinfo("No Geometry", "This model has no rectangles, polygons or GDS shapes to save.")
            return None
        stem = os.path.splitext(os.path.basename(self.model.fsp_path))[0]
        if path is None:
            if kind == "py":
                options = dict(title="Save Geometry as Python (gdsfactory) Script", defaultextension=".py",
                               initialfile=stem + "_geometry.py", filetypes=[("Python scripts", "*.py")])
            else:
                options = dict(title="Save Geometry as GDS File", defaultextension=".gds",
                               initialfile=stem + ".gds", filetypes=[("GDSII files", "*.gds")])
            path = filedialog.asksaveasfilename(parent=self.root, initialdir=os.path.dirname(self.model.fsp_path),
                                                **options)
            if not path:
                return None
        if not path.lower().endswith(".py" if kind == "py" else ".gds"):
            path += ".py" if kind == "py" else ".gds"
        try:
            fsp_export.save(self.model.structures, path, self.model.fsp_path)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Error Saving Geometry", f"Could not save {path}:\n\n{exc}")
            return None
        n_layers = len(fsp_export.layer_table(shapes))
        text = f"✓ Saved geometry: {os.path.basename(path)} ({len(shapes)} shapes on {n_layers} GDS layers"
        if kind == "py":
            n_poly = sum(fsp_export.decompose(sh["points"]) is None for sh in shapes)
            text += ", all from rectangles and tapers" if not n_poly else f", {n_poly} as polygons"
        self.status_lbl.config(text=text + ")", fg=THEME["success"])
        return path

    # ==================================================================
    # Export Data to Clipboard
    # ==================================================================
    def export_data_to_clipboard(self):
        if not self.last_exported_tsv:
            messagebox.showinfo("No Data to Export", "Please open an .fsp file and plot a result first.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.last_exported_tsv)
        self.root.update()
        n_lines = len(self.last_exported_tsv.splitlines())
        self.status_lbl.config(
            text=f"✓ Copied to clipboard: {self.last_exported_desc} ({n_lines} lines)",
            fg=THEME["success"],
        )


def main():
    fsp_arg = sys.argv[1] if len(sys.argv) > 1 else None
    h5_arg = sys.argv[2] if len(sys.argv) > 2 else None
    # If launched without arguments and my_simulation.fsp exists in current folder, pre-load it for convenience
    if fsp_arg is None and os.path.isfile("my_simulation.fsp"):
        fsp_arg = "my_simulation.fsp"
    root = tk.Tk()
    FSPGuiApp(root, initial_fsp=fsp_arg, initial_h5=h5_arg)
    root.mainloop()


if __name__ == "__main__":
    main()
