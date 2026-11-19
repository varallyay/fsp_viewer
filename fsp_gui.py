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
  python3 fsp_gui.py                       start with an empty window
  python3 fsp_gui.py model.fsp             open model.fsp at once (and model.h5 or model_output.h5 next to it)
  python3 fsp_gui.py model.fsp fields.h5   open model.fsp with the monitor data of fields.h5
  python3 fsp_gui.py --install             install the application for you (--install-systemwide: for all users)
  python3 fsp_gui.py --uninstall           remove the installed application
  python3 fsp_gui.py --update              install a newer version from GitHub over the installed one
  python3 fsp_gui.py --version             show the version number
"""

import ast
import collections
import contextlib
import gc
import json
import math
import os
import plistlib
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
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
from fsp_reader import C0, FSPFile, FormatError, LazyMatrix, _fmt, _um, summary as fsp_summary
from fsp_reader import structures as fsp_structures

os.environ.setdefault("TK_SILENCE_DEPRECATION", "1")

# ======================================================================
# Application parameters - edit these to customise the app
# ======================================================================
APP_NAME = "FSP Viewer"         # Window title, name of the installed application and (macOS) menu-bar name
APP_VERSION = "0.2.4"           # See the version history in README.md
APP_DESCRIPTION = "Lumerical FSP 3D Viewer & Results Analyzer"
APP_SLUG = "fsp_viewer"         # Folder and file names of the installation
APP_CLASS = "Fsp_viewer"        # Window class (Linux: connects the windows to the menu entry)
APP_ID = "hu.feti.fsp-viewer"   # What macOS calls the program among its own

APP_ICON_YELLOW = "#ffd700"     # The icon: a black square on a yellow background
APP_ICON_BLACK = "#000000"
APP_ICON_SQUARE = 0.5           # Side of the black square, as a fraction of the yellow field
APP_ICON_MARGIN = 0.1           # Transparent margin on each side, as a fraction of the icon size
APP_ICON_ROUNDING = 0.20        # Corner radius, as a fraction of the yellow field

# The files of the program, the main one first: installing copies them, an update downloads them
APP_FILES = ("fsp_gui.py", "fsp_reader.py", "fsp_export.py")

CONFIG_FILE = "~/.fsp_viewer.json"            # Settings file in the home folder: first start, installation
UPDATE_REPOSITORY = "varallyay/fsp_viewer"    # GitHub project (owner/name) that publishes new versions
APP_WEBSITE = "https://github.com/" + UPDATE_REPOSITORY
UPDATE_CHECK_DAYS = 7           # Look for a new version this often (days); 0 = only with Help > Check for Updates

__version__ = APP_VERSION

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
    "monitor_cyan": "#3fc9ec",       # Field monitors in y-z planes (fields recorded along the device)
    "monitor_cyan_bright": "#a8ecff",
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


def core_y_window(structures, pad=1.8, min_half=0.6):
    """(lo, hi) y range around the narrow waveguide cores. Wide core blocks (an InP slab of an EAM)
    are left out, so that the window is a real zoom on the waveguides."""
    cores = [s for s in structures if s.get("is_core")]
    if not cores:
        return -min_half, min_half
    spans = sorted(s["ymax"] - s["ymin"] for s in cores)
    limit = 4.0 * spans[len(spans) // 2]
    narrow = [s for s in cores if s["ymax"] - s["ymin"] <= limit] or cores
    lo = min(s["ymin"] for s in narrow)
    hi = max(s["ymax"] for s in narrow)
    mid, half = 0.5 * (lo + hi), max(min_half, 0.5 * (hi - lo) * pad)
    return mid - half, mid + half


# ----------------------------------------------------------------------
# 2D clipping (the canvas does not clip items to the plot area by itself)
# ----------------------------------------------------------------------
def clip_polygon_to_rect(pts, x0, y0, x1, y1):
    """Sutherland-Hodgman clip of a polygon [(x, y), ...] to the rectangle [x0, x1] x [y0, y1]."""
    for axis, bound, keep_above in ((0, x0, True), (0, x1, False), (1, y0, True), (1, y1, False)):
        if not pts:
            break
        out = []
        for i, a in enumerate(pts):
            b = pts[(i + 1) % len(pts)]
            a_in = a[axis] >= bound if keep_above else a[axis] <= bound
            b_in = b[axis] >= bound if keep_above else b[axis] <= bound
            if a_in:
                out.append(a)
            if a_in != b_in:
                t = (bound - a[axis]) / (b[axis] - a[axis])
                out.append((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))
        pts = out
    return pts


def clip_segment_to_rect(xa, ya, xb, yb, x0, y0, x1, y1):
    """Liang-Barsky clip of a segment; (xa, ya, xb, yb) of the visible part, or None."""
    t0, t1 = 0.0, 1.0
    dx, dy = xb - xa, yb - ya
    for p, q in ((-dx, xa - x0), (dx, x1 - xa), (-dy, ya - y0), (dy, y1 - ya)):
        if p == 0:
            if q < 0:
                return None
            continue
        t = q / p
        if p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return None
    return xa + t0 * dx, ya + t0 * dy, xa + t1 * dx, ya + t1 * dy


# ----------------------------------------------------------------------
# BSP tree: exact back-to-front order of the 3D faces for the painter's algorithm.
# Sorting faces by their mean depth fails for faces of very different size (a long slab
# next to a small port plane) and for faces that cut through each other (a port plane through
# the BOX); the BSP tree splits such faces, so that every fragment is drawn in the right order.
# ----------------------------------------------------------------------
BSP_EPS = 1e-7


def polygon_plane(verts):
    """(unit normal, offset) of a planar polygon (Newell's method, right-hand rule), or None if degenerate."""
    nx = ny = nz = 0.0
    n = len(verts)
    for i in range(n):
        x0, y0, z0 = verts[i]
        x1, y1, z1 = verts[(i + 1) % n]
        nx += (y0 - y1) * (z0 + z1)
        ny += (z0 - z1) * (x0 + x1)
        nz += (x0 - x1) * (y0 + y1)
    length = math.sqrt(nx * nx + ny * ny + nz * nz)
    if length < 1e-12:
        return None
    nx, ny, nz = nx / length, ny / length, nz / length
    cx = sum(v[0] for v in verts) / n
    cy = sum(v[1] for v in verts) / n
    cz = sum(v[2] for v in verts) / n
    return (nx, ny, nz), nx * cx + ny * cy + nz * cz


def _plane_dist(plane, p):
    (nx, ny, nz), d = plane
    return nx * p[0] + ny * p[1] + nz * p[2] - d


def convex_pieces(verts, edges, normal):
    """Splits a planar polygon into convex pieces (ear clipping when it is concave).
    `edges[i]` tells whether the edge verts[i] -> verts[i+1] is a real outline edge;
    the diagonals added here are not."""
    n = len(verts)

    def turn(a, b, c):
        ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        vx, vy, vz = c[0] - b[0], c[1] - b[1], c[2] - b[2]
        return (normal[0] * (uy * vz - uz * vy) + normal[1] * (uz * vx - ux * vz)
                + normal[2] * (ux * vy - uy * vx))

    if all(turn(verts[i - 1], verts[i], verts[(i + 1) % n]) >= -1e-12 for i in range(n)):
        return [(list(verts), list(edges))]

    def inside(p, a, b, c):
        return turn(a, b, p) >= 0 and turn(b, c, p) >= 0 and turn(c, a, p) >= 0

    idx = list(range(n))
    # real outline edge between original vertices i -> j (j follows i in the original polygon)
    real = {(i, (i + 1) % n): edges[i] for i in range(n)}
    pieces = []
    guard = 0
    while len(idx) > 3 and guard < 10 * n:
        guard += 1
        m = len(idx)
        for k in range(m):
            ia, ib, ic = idx[k - 1], idx[k], idx[(k + 1) % m]
            a, b, c = verts[ia], verts[ib], verts[ic]
            if turn(a, b, c) <= 1e-12:
                continue
            if any(inside(verts[j], a, b, c) for j in idx if j not in (ia, ib, ic)):
                continue
            pieces.append(([a, b, c], [real.get((ia, ib), False), real.get((ib, ic), False),
                                       real.get((ic, ia), False)]))
            real[(ia, ic)] = False
            idx.pop(k)
            break
        else:
            break
    if len(idx) >= 3:
        m = len(idx)
        pieces.append(([verts[i] for i in idx], [real.get((idx[k], idx[(k + 1) % m]), False) for k in range(m)]))
    return pieces


class BSPNode:
    __slots__ = ("plane", "polys", "segs", "front", "back")

    def __init__(self, plane=None):
        self.plane = plane
        self.polys = []
        self.segs = []
        self.front = None
        self.back = None


def _split_polygon(poly, plane):
    """Splits poly = {"v", "e", ...} by the plane into (front, back); either may be None."""
    verts, edges = poly["v"], poly["e"]
    dist = [_plane_dist(plane, v) for v in verts]
    side = [0 if abs(d) < BSP_EPS else (1 if d > 0 else -1) for d in dist]
    front, fe, back, be = [], [], [], []
    n = len(verts)
    for i in range(n):
        j = (i + 1) % n
        a, sa, sb = verts[i], side[i], side[j]
        if sa >= 0:
            front.append(a)
            fe.append(edges[i])
        if sa <= 0:
            back.append(a)
            be.append(edges[i])
        if sa * sb < 0:
            b = verts[j]
            t = dist[i] / (dist[i] - dist[j])
            p = (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]), a[2] + t * (b[2] - a[2]))
            front.append(p)
            fe.append(edges[i])
            back.append(p)
            be.append(edges[i])

    def finish(vs, es):
        if len(vs) < 3:
            return None
        # the cut edge (both ends on the splitting plane) is not an outline edge of the face
        m = len(vs)
        es = [es[k] and not (abs(_plane_dist(plane, vs[k])) < BSP_EPS
                             and abs(_plane_dist(plane, vs[(k + 1) % m])) < BSP_EPS) for k in range(m)]
        if polygon_plane(vs) is None:
            return None
        return dict(poly, v=vs, e=es)

    return finish(front, fe), finish(back, be)


def build_bsp(polys, segs):
    """BSP tree of convex polygons {"v": verts, "e": edge flags, "plane": (n, d), ...}
    and line segments {"a": p, "b": q, ...}."""
    if not polys:
        if not segs:
            return None
        node = BSPNode()
        node.segs = segs
        return node

    # Splitter: the face that cuts the fewest others (balanced between both sides)
    if len(polys) <= 48:
        candidates = polys
    else:
        step = len(polys) / 48.0
        candidates = [polys[int(k * step)] for k in range(48)]
    best, best_score = candidates[0], None
    for cand in candidates:
        plane = cand["plane"]
        n_front = n_back = n_split = 0
        for p in polys:
            ds = [_plane_dist(plane, v) for v in p["v"]]
            has_f = any(d > BSP_EPS for d in ds)
            has_b = any(d < -BSP_EPS for d in ds)
            if has_f and has_b:
                n_split += 1
            elif has_f:
                n_front += 1
            elif has_b:
                n_back += 1
        score = 8 * n_split + abs(n_front - n_back)
        if best_score is None or score < best_score:
            best, best_score = cand, score
            if score == 0:
                break

    node = BSPNode(best["plane"])
    front, back, front_segs, back_segs = [], [], [], []
    for p in polys:
        ds = [_plane_dist(node.plane, v) for v in p["v"]]
        has_f = any(d > BSP_EPS for d in ds)
        has_b = any(d < -BSP_EPS for d in ds)
        if not has_f and not has_b:
            node.polys.append(p)
        elif not has_b:
            front.append(p)
        elif not has_f:
            back.append(p)
        else:
            f, b = _split_polygon(p, node.plane)
            if f:
                front.append(f)
            if b:
                back.append(b)
    for s in segs:
        da, db = _plane_dist(node.plane, s["a"]), _plane_dist(node.plane, s["b"])
        if abs(da) < BSP_EPS and abs(db) < BSP_EPS:
            node.segs.append(s)
        elif da >= -BSP_EPS and db >= -BSP_EPS:
            front_segs.append(s)
        elif da <= BSP_EPS and db <= BSP_EPS:
            back_segs.append(s)
        else:
            t = da / (da - db)
            a, b = s["a"], s["b"]
            p = (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]), a[2] + t * (b[2] - a[2]))
            first, second = dict(s, b=p), dict(s, a=p)
            (front_segs if da > 0 else back_segs).append(first)
            (front_segs if db > 0 else back_segs).append(second)
    node.front = build_bsp(front, front_segs)
    node.back = build_bsp(back, back_segs)
    return node


def bsp_back_to_front(node, eye):
    """Polygons and segments of the tree in drawing order (farthest first) as seen from `eye`.
    Faces of closed solids that look away from the eye (back faces) are left out."""
    out = []
    stack = [(node, False)]
    while stack:
        nd, expanded = stack.pop()
        if nd is None:
            continue
        if nd.plane is None:
            out.extend(("seg", s) for s in nd.segs)
            continue
        if expanded:
            for p in nd.polys:
                if p.get("cull") and _plane_dist(p["plane"], eye) <= 0:
                    continue
                out.append(("poly", p))
            out.extend(("seg", s) for s in nd.segs)
            continue
        near, far = (nd.front, nd.back) if _plane_dist(nd.plane, eye) > 0 else (nd.back, nd.front)
        # popped in reverse: far subtree, then this node, then near subtree
        stack.append((near, False))
        stack.append((nd, True))
        stack.append((far, False))
    return out


# ======================================================================
# Scene & Data Extraction from FSPFile (and optional H5File)
# ======================================================================
def _decode_tag1000_shapes(fsp):
    """
    Rectangles and polygons of models built from primitives / structure groups (tag-1000
    objects), placed where the model has them: the translation and rotation of the structure
    groups and the rotation of the shape itself are applied (fsp_reader.structures).
    """
    shapes = []
    for st in fsp_structures(fsp):
        if st["xy"] is None or st["z"] is None:
            continue
        if st["name"] == "rectangle" and st["index"] == "1.0":
            continue  # helper object of the solver
        poly = [(float(x) * 1e6, float(y) * 1e6) for x, y in st["xy"]]
        xs = [pt[0] for pt in poly]
        ys = [pt[1] for pt in poly]
        shapes.append({
            "kind": "rect" if st["kind"] == "rectangle" else "poly",
            "name": st["name"],
            "path": st["path"],
            "enabled": st["enabled"],
            "rotated": st["rotated"],
            "index_str": st["index"],
            "material": None if st["material"].startswith(("<object defined", "?")) else st["material"],
            "alpha": 0.5,
            "xmin": min(xs),
            "xmax": max(xs),
            "ymin": min(ys),
            "ymax": max(ys),
            "zmin": st["z"][0] * 1e6,
            "zmax": st["z"][1] * 1e6,
            "xy_poly": poly,
        })
    return shapes


class FSPModelData:
    """Extracted geometry, ports, monitors, and plotting data for an FSPFile (+ optional H5)."""

    def __init__(self, fsp_path, h5_path=None):
        self.fsp_path = os.path.abspath(fsp_path)
        self.fsp = FSPFile(self.fsp_path)
        self.h5_path = None
        self.h5 = None

        # Without an explicit .h5 file, look for the one that belongs to the project
        self.h5_skipped = None   # a field file that was found but not opened, for the status line
        self.h5_missing = h5_path if h5_path and not os.path.isfile(h5_path) else None  # given, not found
        if h5_path and os.path.isfile(h5_path):
            self.load_h5(h5_path)
        else:
            found = self._find_h5()
            if found:
                self.load_h5(found)

        self.fdtd_box = self._extract_fdtd_box()
        self.ports = self._extract_ports()
        self.mode_monitors = self._extract_mode_monitors()
        self.dft_monitors = self._extract_dft_monitors()
        self.field_monitors = self._extract_field_monitors()
        self.structures = self._extract_structures()
        self._power_cache = None
        self._freqs = None

    def _find_h5(self):
        """The field file of the project, or None. Tried in this order:
        model.h5 next to model.fsp; the file named inside the project (a project with results
        names it in its monitors); model_output.h5, the name Lumerical gives it, next to the
        project or in the folder model/ (where a cloud run puts it).
        A project without results (layout mode) names no file: a model_output.h5 found beside
        it is left over from an earlier run and is not opened by itself."""
        folder, stem = os.path.dirname(self.fsp_path), os.path.splitext(os.path.basename(self.fsp_path))[0]
        candidate = os.path.join(folder, stem + ".h5")
        if os.path.isfile(candidate):
            return candidate
        named = [v for v in (ob.props.get("HDF5DataFile") for ob in self.fsp.objects) if isinstance(v, str) and v]
        for rel in dict.fromkeys(named):
            rel = rel.replace("\\", "/")
            for candidate in (os.path.join(folder, rel), os.path.join(folder, os.path.basename(rel))):
                if os.path.isfile(candidate):
                    return candidate
        for candidate in (os.path.join(folder, stem + "_output.h5"),
                          os.path.join(folder, stem, stem + "_output.h5")):
            if os.path.isfile(candidate):
                if named:
                    return candidate
                self.h5_skipped = candidate
                return None
        return None

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
        for cache in ("_h5_cache", "_h5_group_x", "_power_spectra", "_mode_spectra", "_power_ref"):  # results of the previous file
            self.__dict__.pop(cache, None)

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
                xb = p.get("xGridBounds")
                placed = isinstance(xb, np.ndarray) and xb.size > 0  # empty: never put on the mesh
                x = float(xb.ravel()[0]) * 1e6 if placed else float(p.get("left", 0.0)) * 1e6
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
                    "enabled": bool(p.get("enabled", 1)),
                    "placed": placed,
                    "obj": ob,
                })
        return mons

    def port_planes(self):
        """The ports, and the standalone mode expansion monitors (whatever their name, e.g.
        monitor_TE): planes where the field is projected on a stored mode, drawn as ports."""
        planes = [dict(pt, kind="port") for pt in self.ports]
        for mm in self.mode_monitors:
            if mm["placed"]:
                planes.append(dict(mm, kind="mode_monitor", direction=None))
        for i, pl in enumerate(planes):
            pl["index"] = i
        return planes

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

    def _extract_field_monitors(self):
        """
        Frequency-domain field monitors in y-z planes, other than the DFT monitors of the ports
        (e.g. yz_input, yz_taper_in, ... along a taper). They record the field at more places
        along the device, and are shown and plotted like ports.
        """
        port_dfts = {pt["dft_obj"].name for pt in self.ports if pt["dft_obj"] is not None}
        mons = []
        for ob in self.fsp.objects:
            p = ob.props
            if ob.name in port_dfts or p.get("monitorShape") != 4 or p.get("type") != 1 or "outputEx" not in p:
                continue

            def bounds(axis):
                v = p.get(axis + "GridBounds")
                return np.asarray(v, float).ravel() * 1e6 if isinstance(v, np.ndarray) and v.size else None

            xb, yb, zb = bounds("x"), bounds("y"), bounds("z")
            if xb is None or yb is None or zb is None or yb.size < 2 or zb.size < 2:
                continue  # never placed on the mesh (a disabled monitor)
            grp = p.get("HDF5DataGroup") or None
            in_fsp = "Ex" in ob.results
            in_h5 = bool(grp and self.h5 is not None and grp in self.h5)
            mons.append({
                "name": ob.name.split("::")[-1],
                "full_name": ob.name,
                "kind": "monitor",
                "x": float(xb[0]),
                "y": 0.5 * float(yb[0] + yb[-1]),
                "z": 0.5 * float(zb[0] + zb[-1]),
                "ymin": float(yb[0]),
                "ymax": float(yb[-1]),
                "zmin": float(zb[0]),
                "zmax": float(zb[-1]),
                "enabled": bool(p.get("enabled", 1)),
                "has_data": in_fsp or in_h5,
                "obj": ob,
                "h5_group": grp,
            })
        mons.sort(key=lambda m: m["x"])
        return mons

    # ---------- fields in y-z planes (ports and field monitors), from the .h5 or the .fsp ----------
    def plane_source(self, name):
        """Where the recorded field of a port or field monitor is: ("h5", group), ("fsp", object) or None.
        Fields stored in the .fsp are read one frequency at a time, never as a whole."""
        for pl in list(self.ports) + list(self.field_monitors):
            if pl["name"] != name:
                continue
            grp = pl.get("h5_group")
            if self.h5 is not None and grp and grp in self.h5 and self._h5_group_at(grp, pl.get("x")):
                return "h5", grp
            ob = pl.get("dft_obj") if "dft_obj" in pl else pl.get("obj")
            if ob is not None and "Ex" in ob.results:
                return "fsp", ob
            return None
        return None

    def _h5_group_at(self, grp, x_um, tol=0.5):
        """Whether the .h5 group lies at x (um): a group named only by its order (Monitor<n>) can
        belong to another plane, e.g. when a disabled port recorded nothing."""
        cache = self.__dict__.setdefault("_h5_group_x", {})
        if grp not in cache:
            try:
                g = self.h5[grp]
                x = float(np.ravel(g["x"][()])[0])
                big = max(float(np.max(np.abs(g[c][()]))) for c in "yz" if c in g) if any(c in g for c in "yz") else 1.0
                cache[grp] = x * 1e6 if big < 1e-2 else x  # metres or um
            except Exception:
                cache[grp] = None
        gx = cache[grp]
        return gx is None or x_um is None or abs(gx - x_um) <= tol

    def field_planes(self):
        """Names of the ports and field monitors with a recorded field, ordered along x."""
        planes = [pl for pl in list(self.ports) + list(self.field_monitors) if self.plane_source(pl["name"])]
        return [pl["name"] for pl in sorted(planes, key=lambda pl: pl["x"])]

    def plane_grid(self, name):
        """(x, y, z) of the field samples of a port / field monitor, in um."""
        kind, src = self.plane_source(name)
        if kind == "h5":  # exported in um, or by Lumerical in metres
            g = self.h5[src]
            x, y, z = (np.ravel(g[c][()]).astype(float) for c in "xyz")
            scale = 1e6 if max(np.max(np.abs(y)), np.max(np.abs(z))) < 1e-2 else 1.0
            return float(x[0]) * scale, y * scale, z * scale
        r = src.results
        return float(np.ravel(r["x"])[0]) * 1e6, np.ravel(r["y"]) * 1e6, np.ravel(r["z"]) * 1e6

    def _plane_freq_index(self, ob, k):
        """Index in the monitor's own frequency list of the k-th frequency of the model."""
        if "f" not in ob.results:
            return k
        f_mon = np.ravel(ob.results["f"]).real
        freqs, _, _ = self.get_frequencies_and_wavelengths()
        if len(f_mon) == len(freqs):
            return k
        return int(np.argmin(np.abs(f_mon - freqs[min(k, len(freqs) - 1)])))

    def plane_has(self, name, comp):
        kind, src = self.plane_source(name)
        return comp in (self.h5[src] if kind == "h5" else src.results)

    def plane_field(self, name, comp, k):
        """Complex field component `comp` (Ex .. Hz) of a port / field monitor at frequency index k,
        as an (Nz, Ny) array."""
        kind, src = self.plane_source(name)
        if kind == "h5":
            return self.h5_field(src, comp, k)[:, :, 0]
        a = src.results.take_last(comp, self._plane_freq_index(src, k))  # (1, Ny, Nz)
        return np.asarray(a)[0].T

    def plane_power(self, name, k):
        """Power through a port / field monitor plane at frequency index k: the 'power' result
        Lumerical stored with the monitor, else the Poynting flux of the stored fields; or None."""
        kind, src = self.plane_source(name)
        if kind == "fsp" and "power" in src.results:
            pw = np.ravel(src.results["power"])
            return float(pw[self._plane_freq_index(src, k)].real)
        if not all(self.plane_has(name, c) for c in ("Ey", "Ez", "Hy", "Hz")):
            return None
        _, y, z = self.plane_grid(name)
        E = {c: self.plane_field(name, c, k) for c in ("Ey", "Ez", "Hy", "Hz")}
        sx = np.real(E["Ey"] * np.conj(E["Hz"]) - E["Ez"] * np.conj(E["Hy"]))
        return float(TRAPZ(TRAPZ(sx, y, axis=1), z, axis=0))

    SPECTRUM_POINTS = 126  # frequencies evaluated when a spectrum is computed from the fields

    def _spectrum_indices(self):
        """Frequency indices used for spectra computed from the fields (at most SPECTRUM_POINTS;
        the rest is interpolated, the spectra being smooth)."""
        freqs, _, _ = self.get_frequencies_and_wavelengths()
        n = len(freqs)
        return np.arange(n) if n <= self.SPECTRUM_POINTS else np.unique(
            np.linspace(0, n - 1, self.SPECTRUM_POINTS).round().astype(int))

    def _fill_spectrum(self, ks, vals):
        freqs, _, _ = self.get_frequencies_and_wavelengths()
        if len(ks) == len(freqs):
            return np.asarray(vals, float)
        return np.interp(np.arange(len(freqs)), ks, vals)

    def source_power(self):
        """Power injected by the (enabled) source at every frequency of the model, as Lumerical's
        sourcepower: the stored P0 over f_vec. None if the project holds no source spectrum."""
        if getattr(self, "_source_power", False) is not False:
            return self._source_power
        self._source_power = None
        freqs, _, _ = self.get_frequencies_and_wavelengths()
        for ob in self.fsp.objects:
            r = ob.results
            if "P0" not in r or not ob.props.get("enabled", 1):
                continue
            p0 = np.ravel(r["P0"]).real
            fv = np.ravel(r["f_vec"]).real if "f_vec" in r else np.ravel(r.get("f", np.array([]))).real
            if p0.size == 0 or not np.all(np.isfinite(p0)) or not p0.any():
                continue
            if p0.size == 1 or fv.size != p0.size:
                self._source_power = np.full(len(freqs), float(p0[0]))
            else:
                order = np.argsort(fv)
                self._source_power = np.interp(freqs, fv[order], p0[order])
            shape = self._pulse_shape(r, freqs)
            if shape is not None:
                self._source_power = self._source_power * shape
            break
        return self._source_power

    @staticmethod
    def _pulse_shape(r, freqs):
        """|s(f)|^2 / |s(f_c)|^2 of the source pulse (its stored time signal), or None.
        The stored monitor powers are not normalised to the source spectrum, but P0 is the same
        at every frequency: the power injected at f is P0 scaled by the pulse spectrum. (Without
        this, the plane right at the source shows 7 % less power at the band edges than at the
        centre, which is just the pulse spectrum.)"""
        try:
            t = np.ravel(r["time"]).real
            sig = np.ravel(r["time_signal"])
            fc = float(np.ravel(r["f"]).real[0])
        except Exception:
            return None
        if t.size < 8 or t.size != sig.size or not np.all(np.isfinite(sig)) or not fc:
            return None
        dt = np.gradient(t)

        def spec(f):
            return np.abs(np.exp(2j * np.pi * np.outer(np.atleast_1d(f), t)) @ (sig * dt)) ** 2

        s_c = spec(fc)[0]
        if not s_c:
            return None
        shape = spec(np.asarray(freqs, float)) / s_c
        # a pulse that does not cover the band (or a signal saved too coarsely) is not used
        return shape if np.all(np.isfinite(shape)) and shape.min() > 0.05 and shape.max() < 1.5 else None

    def power_reference(self):
        """(spectrum, label, absolute) the plane powers are divided by: the source power, or, when
        the recorded fields are on another scale than the source power (the fields of an .h5 field
        file carry no absolute normalisation), the power through the first plane at or after the
        source. None if neither is there."""
        if getattr(self, "_power_ref", False) is not False:
            return self._power_ref
        self._power_ref = None
        planes = self.field_planes()
        src = next((pt for pt in self.ports if pt.get("is_source") and pt["enabled"]), None)
        x_src = src["x"] if src else None
        first = next((n for n in planes if x_src is None or self.plane_grid(n)[0] >= x_src - 0.01), None)
        p_first = self.plane_power_spectrum(first) if first else None
        ps = self.source_power()
        if ps is not None:
            ratio = None
            if p_first is not None:
                with np.errstate(divide="ignore", invalid="ignore"):
                    ratio = np.nanmedian(np.abs(p_first / ps))
            if ratio is None or 1e-3 < ratio < 1e3:  # same scale: transmission = P / P_source
                self._power_ref = (ps, "P_source", True)
                return self._power_ref
        if p_first is not None and np.any(p_first):
            self._power_ref = (p_first, f"P({first})", False)
        return self._power_ref

    def plane_power_spectrum(self, name):
        """Power through a plane at every frequency of the model: the stored 'power' result, else
        the Poynting flux of the recorded fields (needs E and H). None if neither is there."""
        cache = self.__dict__.setdefault("_power_spectra", {})
        if name in cache:
            return cache[name]
        out = None
        src = self.plane_source(name)
        freqs, _, _ = self.get_frequencies_and_wavelengths()
        if src and src[0] == "fsp" and "power" in src[1].results:
            pw = np.ravel(src[1].results["power"]).real
            if len(pw) == len(freqs):
                out = pw
            elif "f" in src[1].results and len(np.ravel(src[1].results["f"])) == len(pw):
                f_mon = np.ravel(src[1].results["f"]).real
                order = np.argsort(f_mon)
                out = np.interp(freqs, f_mon[order], pw[order])
        elif src and all(self.plane_has(name, c) for c in ("Ey", "Ez", "Hy", "Hz")):
            ks = self._spectrum_indices()
            out = self._fill_spectrum(ks, [self.plane_power(name, int(k)) for k in ks])
        cache[name] = out
        return out

    def plane_transmission(self, name):
        """P / P_source of a plane at every frequency (|S_i1|^2 when all of it is in one mode), or
        P / P(first plane) when the fields have no absolute scale (see power_reference); or None."""
        pw, ref = self.plane_power_spectrum(name), self.power_reference()
        if pw is None or ref is None:
            return None
        return pw / np.where(ref[0] == 0, np.nan, ref[0])

    def plane_mode(self, name, tol=0.05):
        """(name, y, z, M, neff) of the stored mode belonging to a plane: the port's own mode, or that
        of a mode expansion monitor (or port) at the same x, within tol um. None if there is none."""
        x, _, _ = self.plane_grid(name)
        candidates = [(name, None)] if any(pt["name"] == name for pt in self.ports) else []
        candidates += [(mm["name"], mm["x"]) for mm in self.mode_monitors]
        candidates += [(pt["name"], pt["x"]) for pt in self.ports if pt["name"] != name]
        for cand, cx in candidates:
            if cx is not None and abs(cx - x) > tol:
                continue
            try:
                y, z, M, neff, _ = self.stored_mode(cand)
            except Exception:
                continue
            return cand, y, z, M, neff
        return None

    def plane_mode_transmission(self, name):
        """(mode name, eta, T_mode) over all frequencies: eta is the overlap of the recorded field with
        the stored mode of the plane, T_mode = eta * P / P_source the power in that mode (|S_i1|^2 of
        the mode). None if the plane has no stored mode or no E and H fields."""
        cache = self.__dict__.setdefault("_mode_spectra", {})
        if name in cache:
            return cache[name]
        cache[name] = None
        mode = self.plane_mode(name)
        T = self.plane_transmission(name)
        if mode is None or RegularGridInterpolator is None or \
                not all(self.plane_has(name, c) for c in ("Ey", "Ez", "Hy", "Hz")):
            return None
        mname, ym, zm, M, _ = mode
        _, y, z = self.plane_grid(name)

        def integ(F):
            return TRAPZ(TRAPZ(F, y, axis=1), z, axis=0)

        # the mode on the grid of the recorded field (done once)
        Z, Y = np.meshgrid(np.clip(z, zm[0], zm[-1]), np.clip(y, ym[0], ym[-1]), indexing="ij")
        pts = np.stack([Z, Y], -1)
        Mi = {c: RegularGridInterpolator((zm, ym), M[c].real)(pts) + 1j * RegularGridInterpolator((zm, ym), M[c].imag)(pts)
              for c in ("Ey", "Ez", "Hy", "Hz")}
        c_mm = integ(np.real(Mi["Ey"] * np.conj(Mi["Hz"]) - Mi["Ez"] * np.conj(Mi["Hy"])))
        ks = self._spectrum_indices()
        etas = []
        for k in ks:
            E = {c: self.plane_field(name, c, int(k)) for c in ("Ey", "Ez", "Hy", "Hz")}
            a = integ(E["Ey"] * np.conj(Mi["Hz"]) - E["Ez"] * np.conj(Mi["Hy"]))
            b = integ(Mi["Ey"] * np.conj(E["Hz"]) - Mi["Ez"] * np.conj(E["Hy"]))
            d = integ(np.real(E["Ey"] * np.conj(E["Hz"]) - E["Ez"] * np.conj(E["Hy"])))
            etas.append(float(np.real(a * b / c_mm) / d) if c_mm and d else np.nan)
        eta = self._fill_spectrum(ks, etas)
        cache[name] = (mname, eta, None if T is None else T * eta)
        return cache[name]

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

            # A layer drawn as one polygon over the whole patterned area (a BOX or a cladding given as
            # a big rectangle in the GDS) is a background slab, not a waveguide pattern
            all_pts = [pt for polys in gds_polys.values() for poly in polys for pt in poly]

            def covers_all(polys):
                if len(polys) != 1 or not all_pts:
                    return False
                xs, ys = [pt[0] for pt in polys[0]], [pt[1] for pt in polys[0]]
                ax = max(pt[0] for pt in all_pts) - min(pt[0] for pt in all_pts)
                ay = max(pt[1] for pt in all_pts) - min(pt[1] for pt in all_pts)
                return max(xs) - min(xs) >= 0.9 * ax and max(ys) - min(ys) >= 0.9 * ay

            slab_colors = {
                "Si_Substrate": ("#2b303a", "#5c6470"),
                "BOX": ("#253342", "#4a6582"),
                "SiO2_under": ("#2c3e50", "#5d7b93"),
                "CLAD": ("#1f3347", "#486b8a"),
            }

            core_layers = 0  # each waveguide layer gets its own color (Si and InP must be told apart)

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
                    # Patterned waveguide core or trench layer, or a slab drawn over the whole area
                    is_slab = covers_all(gds_polys[pat_lay])
                    is_core = not is_slab and (
                        "core" in lname.lower() or "sin" in lname.lower() or bool(n_pat and n_pat > 1.6))
                    if is_slab:
                        fill_c, edge_c = slab_colors.get(lname, ("#28303d", "#526075"))
                    elif is_core:
                        # red, gold, green, purple, teal, pink (blue is for trenches, slate for slabs)
                        fill_c, edge_c = LAYER_PALETTE[(0, 4, 2, 3, 5, 6)[core_layers % 6]]
                        core_layers += 1
                    else:
                        fill_c, edge_c = ("#3d7bd9", "#8fbfff")
                    for p_idx, poly in enumerate(gds_polys[pat_lay]):
                        xs = [pt[0] for pt in poly]
                        ys = [pt[1] for pt in poly]
                        items.append({
                            "name": f"{lname} ({pat_lay})" if len(gds_polys[pat_lay]) == 1 else f"{lname} ({pat_lay}) #{p_idx+1}",
                            "layer_name": lname,
                            "gds_layer": pat_lay,
                            "material": pat_mat,
                            "n": n_pat,
                            "is_background": is_slab,
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
                material = sh.get("material")
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
        if getattr(self, "_freqs", None) is None:
            self._freqs = self._find_frequencies()
        return self._freqs

    def _find_frequencies(self):
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
        if "yMatrix" not in p or "freqEHnx0" not in p:
            raise RuntimeError(f"'{port_or_mon_name}' has no stored mode: its mode was not calculated or not saved "
                               "in this project (a project saved before running, or a disabled port / monitor).")
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

    H5_WHOLE_BYTES = 512 << 20   # a field component up to this size is read whole, once
    H5_CACHE_BYTES = 512 << 20   # field components kept in memory (least recently used dropped)

    def h5_field(self, mon_group, comp, k):
        """Reads complex field component `comp` at frequency index `k` from H5 group `mon_group`.
        Lumerical stores real and imaginary parts side by side in the last axis, and compresses
        blocks that hold every frequency: reading one frequency unpacks the whole component.
        So a component is read whole once and kept (spectra then need no further reading);
        only one too large for that is read frequency by frequency."""
        if self.h5 is None or mon_group not in self.h5:
            raise KeyError(f"HDF5 group '{mon_group}' not available (open the matching .h5 file).")
        a = self.h5[mon_group][comp]
        if a.size * a.dtype.itemsize > self.H5_WHOLE_BYTES or a.shape[-1] % 2:
            pair = a[..., 2 * k:2 * k + 2]
            return pair[..., 0] + 1j * pair[..., 1]
        cache = self.__dict__.setdefault("_h5_cache", collections.OrderedDict())
        key = (self.h5_path, mon_group, comp)
        arr = cache.get(key)
        if arr is None:
            data = np.ascontiguousarray(a[()])
            ctype = np.complex64 if data.dtype == np.float32 else np.complex128
            arr = data.astype(np.float64 if ctype == np.complex128 else np.float32, copy=False).view(ctype)
            cache[key] = arr
            while len(cache) > 1 and sum(v.nbytes for v in cache.values()) > self.H5_CACHE_BYTES:
                cache.popitem(last=False)
        else:
            cache.move_to_end(key)
        return arr[..., k]

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

        P[:] = np.nan
        eta[:] = np.nan
        for i, pt in enumerate(self.ports):
            xs[i] = pt["x"]
            src = self.plane_source(pt["name"])
            if not src or src[0] != "h5":  # e.g. a disabled port: recorded nothing
                continue
            m = src[1]
            try:
                ys, zs, M, _, _ = self.stored_mode(pt["name"])
            except Exception:  # no stored mode: only the power
                ys = zs = M = None
            if M is None:
                for k in range(n_freqs):
                    pw = self.plane_power(pt["name"], k)
                    P[i, k] = np.nan if pw is None else pw
                continue
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

        valid = [i for i in range(n_ports) if np.isfinite(P[i]).any()]
        if not valid:
            raise RuntimeError("No port has a recorded field in the .h5 file.")
        Prel = P / P[valid[0]]
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

    def __init__(self, parent, view_kind, title, u_label, v_label, on_port_clicked=None, on_info=None):
        super().__init__(parent, bg=THEME["panel_bg"], highlightthickness=1, highlightbackground=THEME["border"])
        self.view_kind = view_kind  # "xy", "xz", or "yz"
        self.title = title
        self.u_label = u_label
        self.v_label = v_label
        self.on_port_clicked = on_port_clicked
        # on_info(view) is called when the pointer enters the view, or the view is zoomed, and
        # on_info(None) when the pointer leaves it: the status bar shows the stretch of the axes
        # (the header has room for the pointer position only)
        self.on_info = on_info
        self._hovered = False

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
                tooltip="Toggle y-exaggerated zoom around the waveguide cores\n"
                        "(Shift+wheel zooms x only, Ctrl/⌘+wheel zooms y only)",
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
        self.canvas.bind("<Enter>", self._on_enter)
        self.canvas.bind("<Leave>", self._on_leave)
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
            self.v_range = list(core_y_window(self.model.structures))
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
        if not self._hovered:
            self._on_enter()

    def _on_enter(self, _event=None):
        self._hovered = True
        self._notify_info()

    def _on_leave(self, _event=None):
        self._hovered = False
        self.coord_lbl.config(text="")
        self._notify_info()

    def _notify_info(self):
        if self.on_info is not None:
            self.on_info(self if self._hovered and self.model is not None else None)

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
        # Shift (also a sideways trackpad swipe) zooms the horizontal axis only, Ctrl / Command
        # the vertical one only: an axis can be stretched to make a small tilt or offset visible
        state = getattr(event, "state", 0) or 0
        if not isinstance(state, int):
            state = 0
        fu = factor if not state & 0x000C else 1.0   # Control (0x4), Command / Mod1 (0x8)
        fv = factor if not state & 0x0001 else 1.0   # Shift
        u_min, u_max = self.u_range
        v_min, v_max = self.v_range
        if (u_max - u_min) * fu < 1e-3 or (v_max - v_min) * fv < 1e-3:
            return
        self.u_range = [u_c - (u_c - u_min) * fu, u_c + (u_max - u_c) * fu]
        self.v_range = [v_c - (v_c - v_min) * fv, v_c + (v_max - v_c) * fv]
        self.redraw()

    def _draw_shape(self, uv_pts, fill, outline, width=1.0, dash=()):
        """Polygon given in (u, v), clipped to the plot area. The outline is drawn only along the
        shape's own edges, not along the border where it was cut off."""
        x0, y0, x1, y1 = self._plot_rect()
        px = [self.uv_to_px(u, v) for u, v in uv_pts]
        if fill:
            clipped = clip_polygon_to_rect(px, x0, y0, x1, y1)
            if len(clipped) >= 3:
                self.canvas.create_polygon(*[q for p in clipped for q in p], fill=fill, outline="")
        if outline:
            for i, (xa, ya) in enumerate(px):
                xb, yb = px[(i + 1) % len(px)]
                seg = clip_segment_to_rect(xa, ya, xb, yb, x0, y0, x1, y1)
                if seg:
                    self.canvas.create_line(*seg, fill=outline, width=width, dash=dash)

    def _draw_box(self, u0, u1, v0, v1, fill, outline, width=1.0, dash=()):
        self._draw_shape([(u0, v0), (u1, v0), (u1, v1), (u0, v1)], fill, outline, width, dash)

    def _clipped_line(self, xa, ya, xb, yb, **kw):
        x0, y0, x1, y1 = self._plot_rect()
        seg = clip_segment_to_rect(xa, ya, xb, yb, x0, y0, x1, y1)
        if seg:
            self.canvas.create_line(*seg, **kw)
        return seg

    def _aspect_text(self):
        """How much the vertical axis is stretched against the horizontal one, e.g. 'y ×6.3'."""
        x0, y0, x1, y1 = self._plot_rect()
        su = (x1 - x0) / max(1e-12, self.u_range[1] - self.u_range[0])
        sv = (y1 - y0) / max(1e-12, self.v_range[1] - self.v_range[0])
        ratio = sv / su
        if 0.95 <= ratio <= 1.05:
            return "1:1"
        return f"{self.v_label} ×{ratio:.3g}" if ratio > 1 else f"{self.u_label} ×{1 / ratio:.3g}"

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
            c.create_text(
                (x0 + x1) / 2,
                (y0 + y1) / 2,
                text="Open an .fsp file to visualize the model",
                font=(FONT_UI, 11),
                fill=T["text_dim"],
            )
            return

        if self._hovered:  # the stretch of the axes changes with zooming
            self._notify_info()

        # 2. Draw structures (background slabs first, then patterned/core structures)
        # Among them the larger ones first: a shape inside another one (an active layer in its
        # cladding) is then drawn on top instead of being hidden.
        def _view_area(s):
            du, dv = {"xy": ("x", "y"), "xz": ("x", "z"), "yz": ("y", "z")}[self.view_kind]
            return (s[du + "max"] - s[du + "min"]) * (s[dv + "max"] - s[dv + "min"])

        ordered_structs = sorted(self.model.structures,
                                 key=lambda s: (not s["is_background"], s["is_core"], -_view_area(s)))
        labelled = set()  # a layer split into several shapes is labelled once
        for st in ordered_structs:
            if self.view_kind == "xy":
                if st["is_background"] and st.get("gds_layer") == "":
                    # Subtle outline for full wafer background slab in XY
                    self._draw_box(st["xmin"], st["xmax"], st["ymin"], st["ymax"], "", st["edge"], dash=(2, 4))
                elif len(st["xy_poly"]) >= 3:
                    self._draw_shape(st["xy_poly"], st["fill"], st["edge"], width=1.5)
                continue
            u, v = ("x", "z") if self.view_kind == "xz" else ("y", "z")
            self._draw_box(st[u + "min"], st[u + "max"], st["zmin"], st["zmax"], st["fill"], st["edge"],
                           width=(1.5 if self.view_kind == "xz" else 1.6) if st["is_core"] else 1.0)
            wants_label = st["is_background"] or (self.view_kind == "xz" and st["is_core"])
            if not wants_label or st["layer_name"] in labelled:
                continue
            # label inside the visible part of the shape
            px0, py1 = self.uv_to_px(st[u + "min"], st["zmin"])
            px1, py0 = self.uv_to_px(st[u + "max"], st["zmax"])
            vx0, vx1 = max(x0, min(px0, px1)), min(x1, max(px0, px1))
            vy0, vy1 = max(y0, min(py0, py1)), min(y1, max(py0, py1))
            if vy1 - vy0 < 12 or vx1 - vx0 < 40:
                continue
            labelled.add(st["layer_name"])
            if self.view_kind == "xz":
                text = st["layer_name"]
                fill = T["text_muted"] if st["is_background"] else "#ffffff"
            else:
                text = st["layer_name"] + (f" (n={st['n']:.3f})" if st["n"] else "")
                fill = T["text_muted"]
            c.create_text(vx0 + 6, 0.5 * (vy0 + vy1), text=text, font=(FONT_UI, 8), fill=fill, anchor="w")

        # 3. Draw FDTD region in orange
        if self.model.fdtd_box:
            fx0, fx1, fy0, fy1, fz0, fz1 = self.model.fdtd_box
            box = {"xy": (fx0, fx1, fy0, fy1), "xz": (fx0, fx1, fz0, fz1), "yz": (fy0, fy1, fz0, fz1)}[self.view_kind]
            self._draw_box(*box, "", T["fdtd_orange"], width=1.5, dash=(5, 3))
            rx1, ry0 = self.uv_to_px(box[1], box[3])
            rx0, ry1 = self.uv_to_px(box[0], box[2])
            if rx1 > x0 + 30 and rx0 < x1 and ry0 < y1 - 10 and ry1 > y0:  # label only when the box is in view
                # bottom right corner: the port labels are at the top
                c.create_text(
                    min(x1 - 4, max(x0 + 30, rx1 - 4)),
                    max(y0 + 12, min(y1 - 3, ry1 - 3)),
                    text="FDTD",
                    font=(FONT_UI, 8, "bold"),
                    fill=T["fdtd_orange"],
                    anchor="se",
                )

        # 3b. Field monitors in CYAN: y-z planes where the field is recorded along the device
        n_mon = 0
        for mm in self.model.field_monitors:
            is_sel = mm["name"] == self.selected_port
            if mm["has_data"]:
                color, lw, dash = (T["monitor_cyan_bright"], 2.6, ()) if is_sel else (T["monitor_cyan"], 1.4, (6, 3))
                n_mon += 1
            else:  # disabled / no recorded field: faint
                color, lw, dash = "#2f6f80", 1.0, (2, 3)
            if self.view_kind in ("xy", "xz"):
                lo, hi = ("ymin", "ymax") if self.view_kind == "xy" else ("zmin", "zmax")
                px, pa = self.uv_to_px(mm["x"], mm[hi])
                _, pb = self.uv_to_px(mm["x"], mm[lo])
                if not (x0 <= px <= x1):
                    continue
                seg = self._clipped_line(px, pa, px, pb, fill=color, width=lw, dash=dash)
                if seg and mm["has_data"]:
                    self._port_hit_boxes.append((mm["name"], px - 5, min(seg[1], seg[3]), px + 5, max(seg[1], seg[3])))
            elif is_sel or mm["has_data"]:  # yz: the monitors share their frame; the selected one on top
                self._draw_box(mm["ymin"], mm["ymax"], mm["zmin"], mm["zmax"], "", color, width=lw, dash=dash)
        if self.view_kind == "yz" and n_mon:
            sel = next((mm for mm in self.model.field_monitors if mm["name"] == self.selected_port), None)
            text = (f"{sel['name']} (x={sel['x']:.4g} μm)" if sel else
                    f"{n_mon} field monitor{'s' if n_mon > 1 else ''} (cyan)")
            c.create_text(x0 + 6, y1 - 6, text=text, font=(FONT_UI, 8, "bold"),
                          fill=T["monitor_cyan_bright"] if sel else T["monitor_cyan"], anchor="sw")

        # 4. Draw PORTS (and mode expansion monitors) in YELLOW (#ffd700 / #ffee55)
        for pt in self.model.port_planes():
            is_sel = (pt["name"] == self.selected_port) or (self.selected_port == "All Ports")
            p_color = T["port_yellow_bright"] if pt["name"] == self.selected_port else T["port_yellow"]
            lw = 3.0 if pt["name"] == self.selected_port else 2.0
            if not pt["enabled"]:  # a disabled port takes no part in the simulation: dimmed
                p_color, lw = "#8a7a1a", 1.0
            short_lbl = pt["name"].replace("output_", "out").replace("input_port", "in")

            if self.view_kind in ("xy", "xz"):
                lo, hi = ("ymin", "ymax") if self.view_kind == "xy" else ("zmin", "zmax")
                px, pa = self.uv_to_px(pt["x"], pt[hi])
                _, pb = self.uv_to_px(pt["x"], pt[lo])
                if not (x0 <= px <= x1):
                    continue  # port outside the visible x range
                # Full vertical guide dash + thick yellow port span (only the part inside the plot)
                c.create_line(px, y0, px, y1, fill="#665800", width=1, dash=(2, 4))
                seg = self._clipped_line(px, pa, px, pb, fill=p_color, width=lw)
                for py in (pa, pb):
                    if y0 <= py <= y1:
                        c.create_oval(px - 3, py - 3, px + 3, py + 3, fill=p_color, outline="#000000")
                if self.view_kind == "xy" and pt["direction"]:  # direction arrow at port center
                    _, pyc = self.uv_to_px(pt["x"], pt["y"])
                    if y0 <= pyc <= y1:
                        arr_dx = 10 if pt["direction"] == "+" else -10
                        c.create_line(px, pyc, px + arr_dx, pyc, fill=p_color, width=2, arrow=tk.LAST)
                if seg:
                    self._port_hit_boxes.append((pt["name"], px - 6, min(seg[1], seg[3]), px + 6, max(seg[1], seg[3])))

            else:  # yz cross-section
                self._draw_box(pt["ymin"], pt["ymax"], pt["zmin"], pt["zmax"], "", p_color, width=lw,
                               dash=() if is_sel else (4, 2))
                py1, pz0 = self.uv_to_px(pt["ymax"], pt["zmax"])
                c.create_text(
                    min(x1, py1) - 4,
                    max(y0, pz0) + 10 + 12 * pt["index"],  # one line per port: they share this frame
                    text=f"{pt['name']} (x={pt['x']:g} μm)" + ("" if pt["enabled"] else ", disabled"),
                    font=(FONT_UI, 8, "bold"),
                    fill=p_color,
                    anchor="ne",
                )

        # Port labels in XY / XZ views (staggered so dense output ports don't overlap)
        if self.view_kind in ("xy", "xz"):
            last_px = -999
            row_idx = 0
            for pt in sorted(self.model.port_planes(), key=lambda item: item["x"]):
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

        # Field monitor labels in XY / XZ views: along the bottom, staggered like the port labels
        if self.view_kind in ("xy", "xz"):
            last_px, row_idx = -999, 0
            for mm in self.model.field_monitors:
                if not mm["has_data"]:
                    continue
                px, _ = self.uv_to_px(mm["x"], 0)
                if not (x0 <= px <= x1):
                    continue
                if px - last_px < 52:
                    row_idx = (row_idx + 1) % 3
                else:
                    row_idx, last_px = 0, px
                is_sel = mm["name"] == self.selected_port
                label = mm["name"][3:] if mm["name"].startswith(("yz_", "yz ")) else mm["name"]
                tid = c.create_text(px, y1 - 9 - row_idx * 13, text=label, font=(FONT_MONO, 8, "bold"),
                                    fill="#0f1115" if is_sel else T["monitor_cyan"])
                bx0, by0, bx1, by1 = c.bbox(tid)
                bg = c.create_rectangle(bx0 - 3, by0 - 1, bx1 + 3, by1 + 1,
                                        fill=T["monitor_cyan_bright"] if is_sel else "#0d2a33", outline=T["monitor_cyan"])
                c.tag_lower(bg, tid)
                self._port_hit_boxes.append((mm["name"], bx0 - 3, by0 - 1, bx1 + 3, by1 + 1))

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
        self.bsp = None

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
        self.redraw()  # the selection only changes the colors: the scene is kept

    PORT_HATCH_LINES = 7

    def _rebuild_3d_scene(self):
        self.faces_3d = []
        self.port_faces_3d = []
        self.bsp = None
        if self.model is None:
            self.bbox_3d = None
            return

        # Background slabs (BOX, cladding, substrate) are drawn see-through when shown: they
        # enclose the waveguide cores, which would otherwise be hidden inside them.
        show_slabs = self.show_slabs_var.get()
        for st in self.model.structures:
            if st["is_background"] and not show_slabs:
                continue
            ghost = bool(st["is_background"])
            for verts, kind, color, _tag in extruded_poly_faces_3d(st["xy_poly"], st["zmin"], st["zmax"], st["fill"],
                                                                   tag=st["name"]):
                self.faces_3d.append((verts, kind, color, ghost))

        # Port planes (in y-z at the port x), mode expansion monitors included
        for pt in self.model.port_planes():
            x = pt["x"]
            y0, y1 = pt["ymin"], pt["ymax"]
            z0, z1 = pt["zmin"], pt["zmax"]
            self.port_faces_3d.append({
                "name": pt["name"],
                "kind": "port",
                "x": x,
                "y": pt["y"],
                "z": z1,
                "direction": pt["direction"] or "+",
                "enabled": pt["enabled"],
                "verts": [(x, y0, z0), (x, y1, z0), (x, y1, z1), (x, y0, z1)],
            })
        # Field monitor planes (cyan), drawn like ports
        for mm in self.model.field_monitors:
            if not mm["has_data"]:
                continue
            x, y0, y1, z0, z1 = mm["x"], mm["ymin"], mm["ymax"], mm["zmin"], mm["zmax"]
            self.port_faces_3d.append({
                "name": mm["name"],
                "kind": "monitor",
                "x": x,
                "y": mm["y"],
                "z": z1,
                "direction": "+",
                "enabled": True,
                "verts": [(x, y0, z0), (x, y1, z0), (x, y1, z1), (x, y0, z1)],
            })

        # Bounding box from FDTD box + shown structures + ports
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

        if not (xs and ys and zs):
            self.bbox_3d = None
            return
        self.bbox_3d = (min(xs), max(xs), min(ys), max(ys), min(zs), max(zs))
        xmin, xmax, ymin, ymax, zmin, zmax = self.bbox_3d
        self._half = view_box_half_extents(xmax - xmin, ymax - ymin, zmax - zmin)
        self._build_bsp()

    def _build_bsp(self):
        """The depth-ordering tree of the scene, in the normalized view-box coordinates
        (the camera only rotates and zooms these, so the tree is built once per scene)."""
        polys, segs = [], []
        for verts, kind, color, ghost in self.faces_3d:
            nv = [self._normalize(*v) for v in verts]
            plane = polygon_plane(nv)
            if plane is None:
                continue  # zero-area face (e.g. a side face of a zero-thickness layer)
            fill_c, edge_c = face_colors(color, kind)
            for pv, pe in convex_pieces(nv, [True] * len(nv), plane[0]):
                polys.append({"v": pv, "e": pe, "plane": plane, "kind": "ghost" if ghost else "solid",
                              "fill": fill_c, "edge": edge_c, "cull": not ghost})

        for pf in self.port_faces_3d:
            nv = [self._normalize(*v) for v in pf["verts"]]
            plane = polygon_plane(nv)
            if plane is None:
                continue
            polys.append({"v": nv, "e": [True] * 4, "plane": plane, "kind": "port", "port": pf, "cull": False})
            # Diagonal hatching: a port plane is see-through (Tk on macOS has no stippled fill)
            (x, y0, z0), (_, y1, _), (_, _, z1) = pf["verts"][0], pf["verts"][1], pf["verts"][2]
            n = self.PORT_HATCH_LINES
            for k in range(1, 2 * n + 2):
                s = k / (n + 1.0)
                if s >= 2.0:
                    break
                (ua, va), (ub, vb) = ((s, 0.0), (0.0, s)) if s <= 1.0 else ((1.0, s - 1.0), (s - 1.0, 1.0))
                a = self._normalize(x, y0 + ua * (y1 - y0), z0 + va * (z1 - z0))
                b = self._normalize(x, y0 + ub * (y1 - y0), z0 + vb * (z1 - z0))
                segs.append({"a": a, "b": b, "kind": "hatch", "port": pf})

        xmin, xmax, ymin, ymax, zmin, zmax = self.model.fdtd_box or self.bbox_3d
        corners = [self._normalize(*p) for p in (
            (xmin, ymin, zmin), (xmax, ymin, zmin), (xmax, ymax, zmin), (xmin, ymax, zmin),
            (xmin, ymin, zmax), (xmax, ymin, zmax), (xmax, ymax, zmax), (xmin, ymax, zmax))]
        for i, j in ((0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4), (0, 4), (1, 5), (2, 6), (3, 7)):
            segs.append({"a": corners[i], "b": corners[j], "kind": "box"})

        self.bsp = build_bsp(polys, segs)

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

    def _eye(self):
        """Camera position in normalized coordinates: on the depth axis, EYE_DISTANCE from the center."""
        az, el = math.radians(self.azim), math.radians(self.elev)
        d = self.EYE_DISTANCE
        return d * math.cos(el) * math.sin(az), d * math.cos(el) * math.cos(az), d * math.sin(el)

    def _project_normalized(self, nx, ny, nz, cx, cy, scale):
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

        # perspective seen from the eye at depth EYE_DISTANCE (= 3.2 / 0.35)
        persp = 1.0 / max(0.4, 3.2 - depth * 0.35) * 2.8
        sx = cx + self.pan_x + px_3d * scale * self.zoom * persp
        sy = cy + self.pan_y - py_3d * scale * self.zoom * persp
        return sx, sy, depth

    def _project_point(self, x, y, z, cx, cy, scale):
        return self._project_normalized(*self._normalize(x, y, z), cx, cy, scale)

    @staticmethod
    def _edge_runs(pts, flags):
        """Polylines [x0, y0, x1, y1, ...] along the consecutive outline edges of a polygon."""
        n = len(pts)
        if all(flags):
            return [[c for p in pts + pts[:1] for c in p]]
        runs = []
        start = next(i for i in range(n) if not flags[i - 1])  # first edge after a non-outline one
        run = None
        for k in range(n):
            i = (start + k) % n
            if flags[i]:
                if run is None:
                    run = list(pts[i])
                run.extend(pts[(i + 1) % n])
            elif run is not None:
                runs.append(run)
                run = None
        if run is not None:
            runs.append(run)
        return runs

    def _port_style(self, pf):
        """(color, frame width, dash) of a port or field monitor plane."""
        if pf.get("kind") == "monitor":
            if pf["name"] == self.selected_port:
                return THEME["monitor_cyan_bright"], 2.4, ()
            return THEME["monitor_cyan"], 1.2, (5, 3)
        if not pf["enabled"]:  # a disabled port takes no part in the simulation: faint
            return "#8a7a1a", 1.2, (3, 3)
        if pf["name"] == self.selected_port:
            return THEME["port_yellow_bright"], 2.6, ()
        return THEME["port_yellow"], 1.6, ()

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

        proj_cache = {}

        def proj(v):
            p = proj_cache.get(v)
            if p is None:
                p = proj_cache[v] = self._project_normalized(*v, cx, cy, scale)[:2]
            return p

        box_color = THEME["fdtd_orange"] if self.model.fdtd_box else THEME["grid_axis"]

        # Painter's algorithm in the exact back-to-front order of the BSP tree
        for kind, item in bsp_back_to_front(self.bsp, self._eye()):
            if kind == "seg":
                (xa, ya), (xb, yb) = proj(item["a"]), proj(item["b"])
                if item["kind"] == "box":
                    c.create_line(xa, ya, xb, yb, fill=box_color, dash=(3, 3))
                else:  # port hatching
                    color, _w, dash = self._port_style(item["port"])
                    c.create_line(xa, ya, xb, yb, fill=color, width=1, dash=dash)
                continue
            pts = [proj(v) for v in item["v"]]
            if item["kind"] == "solid":
                # outline in the fill color closes the hairline seams between split fragments
                c.create_polygon(*[q for p in pts for q in p], fill=item["fill"], outline=item["fill"], width=1)
                edge_c, width, dash = item["edge"], 1.0, ()
            elif item["kind"] == "ghost":
                edge_c, width, dash = item["edge"], 1.0, (2, 3)
            else:
                edge_c, width, dash = self._port_style(item["port"])
            for run in self._edge_runs(pts, item["e"]):
                c.create_line(*run, fill=edge_c, width=width, dash=dash)

        # Port labels on top of everything, above the top edge of the port plane
        # (of the field monitors only the selected one is labelled: they are many)
        for pf in self.port_faces_3d:
            if pf.get("kind") == "monitor" and pf["name"] != self.selected_port:
                continue
            (_, y0, _), (_, y1, z1) = pf["verts"][0], pf["verts"][2]
            top_x, top_y, _ = self._project_point(pf["x"], 0.5 * (y0 + y1), z1, cx, cy, scale)
            color, _w, _d = self._port_style(pf)
            short = pf["name"].replace("output_port", "P").replace("input_port", "In")
            if not pf["enabled"]:
                short += " (off)"
            self._badge(top_x, top_y - 8, short, color, font=(FONT_MONO, 8, "bold"))

        # Dimension badges
        xmin, xmax, ymin, ymax, zmin, zmax = self.model.fdtd_box or self.bbox_3d
        p_x0 = self._project_point(xmin, ymin, zmin, cx, cy, scale)
        p_x1 = self._project_point(xmax, ymin, zmin, cx, cy, scale)
        p_y1 = self._project_point(xmax, ymax, zmin, cx, cy, scale)
        p_z1 = self._project_point(xmax, ymin, zmax, cx, cy, scale)
        self._badge(0.5 * (p_x0[0] + p_x1[0]), 0.5 * (p_x0[1] + p_x1[1]) + 14, f"Δx={xmax - xmin:g} μm", "#74c0fc")
        self._badge(0.5 * (p_x1[0] + p_y1[0]) + 24, 0.5 * (p_x1[1] + p_y1[1]) + 8, f"Δy={ymax - ymin:g} μm", "#63e6be")
        self._badge(0.5 * (p_x1[0] + p_z1[0]) + 30, 0.5 * (p_x1[1] + p_z1[1]), f"Δz={zmax - zmin:g} μm", "#ffa8a8")

        # Legend pill in top-left corner
        tid = c.create_text(
            14, 12,
            text=f"■ Ports ({len(self.port_faces_3d) - sum(pf.get('kind') == 'monitor' for pf in self.port_faces_3d)})"
                 f" in Yellow   ■ FDTD Box",
            font=(FONT_UI, 9, "bold"),
            fill=THEME["port_yellow"],
            anchor="nw",
        )
        n_mon = sum(pf.get("kind") == "monitor" for pf in self.port_faces_3d)
        if n_mon:
            c.create_text(c.bbox(tid)[2] + 12, 12, text=f"■ Field monitors ({n_mon})", font=(FONT_UI, 9, "bold"),
                          fill=THEME["monitor_cyan"], anchor="nw")

        self._draw_axes_triad(w, h)

    def _badge(self, x, y, text, color, font=(FONT_UI, 8, "bold")):
        tid = self.canvas.create_text(x, y, text=text, font=font, fill=color)
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
                for k, v in ob.props.raw_items():
                    lines.append(f"   {k:45s} = {_fmt(v)}")
                for k, v in ob.results.raw_items():
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
                for k, v in m.raw_items():
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
# ======================================================================
# Application icon: a black square on a yellow background
# ======================================================================
def app_icon_image(size=1024, margin=APP_ICON_MARGIN, rounding=APP_ICON_ROUNDING):
    """Pillow image of the icon, size x size pixels.

    macOS draws the Dock icon edge to edge, so the yellow field keeps a transparent margin
    (a fraction of the size per side) and rounded corners, like the icons of native programs.
    """
    from PIL import Image, ImageDraw
    lanczos = getattr(Image, "Resampling", Image).LANCZOS
    margin = min(max(float(margin), 0.0), 0.45)
    rounding = min(max(float(rounding), 0.0), 0.5)
    ss = 4  # drawn 4x larger and scaled down, for smooth edges
    full = size * ss
    image = Image.new("RGBA", (full, full), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    edge = round(full * margin)
    inner = full - 2 * edge
    draw.rounded_rectangle((edge, edge, full - edge - 1, full - edge - 1),
                           radius=round(rounding * inner), fill=APP_ICON_YELLOW)
    half = inner * APP_ICON_SQUARE / 2
    draw.rectangle((full / 2 - half, full / 2 - half, full / 2 + half - 1, full / 2 + half - 1), fill=APP_ICON_BLACK)
    return image.resize((size, size), lanczos)


def app_icon_photo(root, size=256, margin=APP_ICON_MARGIN):
    """Tk image of the icon (for the window, the Dock and the dialogs), or None without Pillow."""
    try:
        from PIL import ImageTk
        return ImageTk.PhotoImage(app_icon_image(size, margin), master=root)
    except Exception:
        return None


def _log_to_file():
    """A packaged application has no console (sys.stdout / sys.stderr are None): its messages
    and error reports go to LOG_FILE in the home folder instead."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        log = open(os.path.expanduser(LOG_FILE), "a", encoding="utf-8", buffering=1)
    except OSError:
        return
    log.write(f"--- {APP_NAME} {APP_VERSION} started {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    sys.stdout = sys.stdout or log
    sys.stderr = sys.stderr or log


def self_test(report, fsp=None):
    """Starts the program without user interaction and checks that every library it needs is
    there and works (the Windows build runs this on the packaged program). With an .fsp file,
    every result is also plotted. Writes a report; returns 0 if all went well, else 1."""
    lines, failed = [f"{APP_NAME} {APP_VERSION} self-test, Python {sys.version.split()[0]}, "
                     f"{sys.platform}, packaged={FROZEN}"], []

    def check(name, fn):
        try:
            detail = fn()
            lines.append(f"ok    {name}" + (f": {detail}" if detail else ""))
        except Exception as err:
            failed.append(name)
            lines.append(f"FAIL  {name}: {type(err).__name__}: {err}")

    def need(module, what):
        if module is None:
            raise ImportError(f"{what} is missing")
        return getattr(module, "__version__", "")

    try:
        import scipy
    except ImportError:
        scipy = None
    check("numpy", lambda: np.__version__ + f", sum={np.arange(10).sum()}")
    check("matplotlib", lambda: matplotlib.__version__ + f", backend {matplotlib.get_backend()}")
    check("h5py", lambda: need(h5py, "h5py"))
    check("scipy.interpolate", lambda: need(scipy, "scipy") if RegularGridInterpolator else need(None, "scipy"))
    check("Pillow icon", lambda: f"{app_icon_image(64).size}")
    root = None
    try:
        root = tk.Tk(className=APP_CLASS)
        check("Tk", lambda: f"Tk {root.tk.call('info', 'patchlevel')}")
        app = FSPGuiApp(root, initial_fsp=fsp)
        root.update()
        deadline = time.time() + 120  # the file given at the start is opened by a scheduled call
        while fsp and app.model is None and time.time() < deadline:
            root.update()
            time.sleep(0.05)
        check("window", lambda: f"{root.winfo_width()}x{root.winfo_height()}")
        check("icon image", lambda: "" if app_icon_photo(root, 64) is not None else need(None, "Pillow ImageTk"))
        if fsp:
            check("open file", lambda: f"{len(app.model.structures)} structures, {len(app.model.ports)} ports"
                                       if app.model is not None else need(None, "the model"))
            for option in app.RESULT_OPTIONS:
                def plot(option=option):
                    app.result_var.set(option)
                    app.plot_selected()
                    root.update()
                    texts = [t.get_text() for ax in app.fig.axes for t in ax.texts if t.get_text().startswith("Cannot")]
                    return texts[0].replace("\n", " ")[:160] if texts else ""
                check(f"plot: {option}", plot)
    except Exception as err:
        failed.append("start")
        lines.append(f"FAIL  start: {type(err).__name__}: {err}")
    finally:
        if root is not None:
            try:
                root.destroy()
            except tk.TclError:
                pass
    lines.append("RESULT: " + ("all ok" if not failed else "FAILED: " + ", ".join(failed)))
    with open(report, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return 1 if failed else 0


def bring_to_front(root):
    """Shows the window in front of the others, also when started from a terminal.
    lift() alone leaves it behind the terminal on macOS (and on another desktop when the
    terminal is in full screen): focus_force() activates the application as well."""
    try:
        root.deiconify()
        root.lift()
        root.focus_force()
    except tk.TclError:
        pass


def set_macos_app_name(name=APP_NAME):
    """Best effort: make macOS call the program `name` instead of Python in the menu bar and the Dock.

    Needs PyObjC (pip install pyobjc-framework-Cocoa); without it, and on other systems,
    nothing happens. Must run before the Tk root window is created. Returns success.
    """
    if sys.platform != "darwin":
        return False
    done = False
    try:
        from Foundation import NSBundle
        bundle = NSBundle.mainBundle()
        info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
        if info is not None:
            info["CFBundleName"] = name
            info["CFBundleDisplayName"] = name
            done = True
    except Exception:
        pass
    try:
        from Foundation import NSProcessInfo
        NSProcessInfo.processInfo().setProcessName_(name)
        done = True
    except Exception:
        pass
    return done


# ======================================================================
# Installation on macOS and Linux
#
# Installing copies the program files (APP_FILES) to a fixed place and adds a launcher,
# so the program starts like any other application, under its own name and icon:
#   macOS   "FSP Viewer.app" in /Applications (systemwide) or ~/Applications (only for
#           you); it appears in Launchpad and Spotlight.
#   Linux   program files in /opt/fsp_viewer and a menu entry in
#           /usr/local/share/applications (systemwide), or ~/.local/share/fsp_viewer and
#           ~/.local/share/applications (only for you).
# The launcher runs the copy with the Python interpreter that did the installation, which
# has NumPy and matplotlib. CONFIG_FILE in the home folder records that the question at
# the first start was answered and what was installed.
# ======================================================================
MAC_SYSTEM_DIR = "/Applications"
MAC_USER_DIR = "~/Applications"
LINUX_SYSTEM_DIRS = ("/opt", "/usr/local/share/applications")   # program files, menu entry
LINUX_USER_DIRS = ("~/.local/share", "~/.local/share/applications")
LINUX_COMMAND = "fsp_gui"                     # terminal command of an installation on Linux
LINUX_SYSTEM_BIN = "/usr/local/bin"           # where the command goes (systemwide / only for you)
LINUX_USER_BIN = "~/.local/bin"

THIS_SCRIPT = os.path.realpath(__file__)
MAIN_FILE = APP_FILES[0]
# A packaged application (e.g. "FSP Viewer.exe" built with PyInstaller, see build_windows.py):
# it runs without Python, so it cannot install or update itself; new versions are downloaded.
FROZEN = bool(getattr(sys, "frozen", False))
RELEASES_PAGE = APP_WEBSITE + "/releases/latest"
LOG_FILE = "~/.fsp_viewer.log"  # messages and errors of a packaged application (it has no console)


def load_config():
    """Settings from CONFIG_FILE; empty if the file is missing or unreadable."""
    try:
        with open(os.path.expanduser(CONFIG_FILE), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(config):
    """Write the settings (via a temporary file, so a crash cannot leave half a file)."""
    path = os.path.expanduser(CONFIG_FILE)
    try:
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, sort_keys=True)
        os.replace(path + ".tmp", path)
        return True
    except OSError:
        return False


def _platform_name(platform):
    return "macOS" if platform == "darwin" else "Linux" if platform.startswith("linux") else platform


def install_target(systemwide, platform=None):
    """Where an installation goes, or None on unsupported systems.

    Returns a dict with 'files' (the folder that gets the program files), 'launcher' (the
    .app bundle or the .desktop file), 'script' (the installed main program) and 'parents'
    (the folders that must be writable).
    """
    platform = platform or sys.platform
    if platform == "darwin":
        base = os.path.expanduser(MAC_SYSTEM_DIR if systemwide else MAC_USER_DIR)
        app = os.path.join(base, APP_NAME + ".app")
        files = os.path.join(app, "Contents", "Resources")
        parents = [base]
    elif platform.startswith("linux"):
        if systemwide:
            files_root, menu_dir = LINUX_SYSTEM_DIRS
        elif os.environ.get("XDG_DATA_HOME"):  # personal data folder set by the desktop
            files_root = os.environ["XDG_DATA_HOME"]
            menu_dir = os.path.join(files_root, "applications")
        else:
            files_root, menu_dir = LINUX_USER_DIRS
        files_root, menu_dir = os.path.expanduser(files_root), os.path.expanduser(menu_dir)
        files = os.path.join(files_root, APP_SLUG)
        app = os.path.join(menu_dir, APP_SLUG + ".desktop")
        bin_dir = os.path.expanduser(LINUX_SYSTEM_BIN if systemwide else LINUX_USER_BIN)
        parents = [files_root, menu_dir, bin_dir]
        return {"files": files, "launcher": app, "script": os.path.join(files, MAIN_FILE), "parents": parents,
                "command": os.path.join(bin_dir, LINUX_COMMAND)}
    else:
        return None
    return {"files": files, "launcher": app, "script": os.path.join(files, MAIN_FILE), "parents": parents}


def _can_write(path):
    """Can this user create `path` (or write into it if it exists)?"""
    while not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            return False
        path = parent
    return os.access(path, os.W_OK)


def running_installed_copy(platform=None):
    """'systemwide' or 'user' if this program is an installed copy, otherwise None."""
    for systemwide in (True, False):
        target = install_target(systemwide, platform)
        if target and os.path.realpath(target["script"]) == THIS_SCRIPT:
            return "systemwide" if systemwide else "user"
    return None


def _version_tuple(version):
    try:
        return tuple(int(part) for part in str(version).split("."))
    except ValueError:
        return (0,)


def is_newer(version, than):
    return _version_tuple(version) > _version_tuple(than)


def _admin_hint(option):
    return f"    sudo {shlex.quote(sys.executable or 'python3')} {shlex.quote(THIS_SCRIPT)} {option}"


_VERSION_RE = re.compile(r"""^APP_VERSION\s*=\s*['"]([^'"]+)['"]""", re.M)
_FILES_RE = re.compile(r"^APP_FILES\s*=\s*(\([^)]*\))", re.M)


def file_version(path):
    """APP_VERSION written in a copy of the main program, or None."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            match = _VERSION_RE.search(f.read())
        return match.group(1) if match else None
    except OSError:
        return None


def program_files(source):
    """The file names in APP_FILES of a main program given as text (a newer version may have
    more files than this one); this version's list if they cannot be read."""
    match = _FILES_RE.search(source)
    try:
        names = ast.literal_eval(match.group(1)) if match else None
    except (ValueError, SyntaxError):
        names = None
    if (isinstance(names, tuple) and names and names[0] == MAIN_FILE
            and all(isinstance(n, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", n) and not n.startswith(".")
                    for n in names)):
        return names
    return APP_FILES


def install_app(systemwide, platform=None, source_dir=None, admin_option="--install-systemwide"):
    """Install this program, or the copy in the folder `source_dir` (a downloaded update).
    Returns (success, message)."""
    platform = platform or sys.platform
    source_dir = source_dir or os.path.dirname(THIS_SCRIPT)
    main = os.path.join(source_dir, MAIN_FILE)
    version = file_version(main) or APP_VERSION
    try:
        with open(main, encoding="utf-8", errors="replace") as f:
            names = program_files(f.read())
    except OSError:
        names = APP_FILES
    missing = [n for n in names if not os.path.isfile(os.path.join(source_dir, n))]
    target = install_target(systemwide, platform)
    if target is None:
        return False, f"Installing is only supported on macOS and Linux, not on {_platform_name(platform)}."
    if missing:
        return False, f"Nothing was installed: {', '.join(missing)} is missing in {source_dir}."
    if not all(_can_write(p) for p in target["parents"]):
        if systemwide:
            return False, ("Installing for all users needs administrator rights. In a terminal, run\n"
                           f"{_admin_hint(admin_option)}\n"
                           "or install it only for you.")
        return False, f"Cannot write to {' or '.join(target['parents'])}; nothing was installed."
    previous = file_version(target["script"])
    try:
        if platform == "darwin":
            _install_mac(target, source_dir, names, version)
        else:
            _install_linux(target, source_dir, names)
    except (OSError, subprocess.SubprocessError) as err:
        return False, f"The installation failed: {err}"
    # A systemwide installation replaces a personal one, so the program is not listed twice
    other = install_target(not systemwide, platform)
    if systemwide and os.path.lexists(other["launcher"]):
        try:
            _remove_installation(other)
        except OSError:
            pass
    config = load_config()
    config.update(first_start_done=True, update_declined=None, installed={
        "version": version, "systemwide": systemwide, "launcher": target["launcher"],
        "python": sys.executable})
    config.setdefault("last_update_check", time.time())  # the first automatic check comes a week later
    save_config(config)
    scope = "for all users" if systemwide else "for you"
    if previous and previous != version:
        return True, f"{APP_NAME} is updated from {previous} to {version} ({scope}):\n    {target['launcher']}"
    where = ("from Launchpad or the Applications folder" if platform == "darwin"
             else "from the application menu (search for " + APP_NAME + ")")
    if target.get("command"):
        where += f",\nor from a terminal with the command  {LINUX_COMMAND}"
        bin_dir = os.path.dirname(target["command"])
        if not systemwide and bin_dir not in [os.path.expanduser(d) for d in os.environ.get("PATH", "").split(os.pathsep)]:
            where += (f"\n({bin_dir} is not on your PATH: add  export PATH=\"{bin_dir}:$PATH\"  "
                      "to ~/.bashrc and open a new terminal)")
    return True, (f"{APP_NAME} {version} is installed {scope}:\n    {target['launcher']}\n"
                  f"From now on it can be started {where}.")


def uninstall_app(platform=None):
    """Remove the installed copies (systemwide and personal). Returns (success, message)."""
    platform = platform or sys.platform
    if install_target(False, platform) is None:
        return False, f"Installing is only supported on macOS and Linux, not on {_platform_name(platform)}."
    removed, failed = [], []
    for systemwide in (True, False):
        target = install_target(systemwide, platform)
        if not (os.path.lexists(target["launcher"]) or os.path.exists(target["script"])):
            continue
        try:
            _remove_installation(target)
            removed.append(target["launcher"])
        except OSError:
            failed.append(target["launcher"])
    if failed:
        return False, ("Removing " + ", ".join(failed) + " needs administrator rights. In a terminal, run\n"
                       + _admin_hint("--uninstall"))
    config = load_config()
    config.pop("installed", None)
    save_config(config)
    if not removed:
        return True, f"{APP_NAME} is not installed."
    return True, "Removed " + ", ".join(removed) + "."


def _remove_installation(target):
    """Delete an installation; only folders that really contain an installed copy are removed."""
    if os.path.isdir(target["launcher"]):          # macOS .app bundle
        if not os.path.exists(target["script"]):
            raise OSError(f"{target['launcher']} does not look like a {APP_NAME} installation")
        shutil.rmtree(target["launcher"])
        return
    if os.path.lexists(target["launcher"]):        # Linux menu entry
        os.remove(target["launcher"])
        _refresh_menus(os.path.dirname(target["launcher"]))
    if target.get("command") and os.path.lexists(target["command"]):   # Linux terminal command
        os.remove(target["command"])
    if os.path.exists(target["script"]):
        shutil.rmtree(target["files"])


def _copy_program(source_dir, names, files):
    os.makedirs(files, exist_ok=True)
    for name in names:
        src, dst = os.path.join(source_dir, name), os.path.join(files, name)
        if os.path.exists(dst) and os.path.samefile(src, dst):
            continue  # running the installed copy: nothing to copy
        shutil.copyfile(src, dst)


def _make_readable(folder):
    """Let every user read (and enter) the installed files, whatever the umask was."""
    for path, _dirs, files in os.walk(folder):
        os.chmod(path, 0o755)
        for name in files:
            full = os.path.join(path, name)
            os.chmod(full, 0o755 if os.access(full, os.X_OK) else 0o644)


def _desktop_exec(args):
    """Quote arguments for the Exec key of a .desktop file (freedesktop.org rules)."""
    quoted = []
    for arg in args:
        arg = arg.replace("%", "%%")
        if re.search(r"[\s\"'\\><~|&;$*?#()`]", arg):
            arg = '"' + re.sub(r'(["`$\\])', r"\\\1", arg) + '"'
        quoted.append(arg.replace("\\", "\\\\"))  # the file format escapes backslashes once more
    return " ".join(quoted)


def _refresh_menus(menu_dir):
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", "-q", menu_dir],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def _install_linux(target, source_dir, names):
    files = target["files"]
    _copy_program(source_dir, names, files)
    lines = ["[Desktop Entry]", "Type=Application", f"Name={APP_NAME}", "GenericName=Lumerical FSP Viewer",
             f"Comment={APP_DESCRIPTION}",
             "Exec=" + _desktop_exec([sys.executable or "python3", target["script"]]) + " %f"]
    try:
        menu_icon = os.path.join(files, APP_SLUG + ".png")
        app_icon_image(256).save(menu_icon)
        lines.append(f"Icon={menu_icon}")
    except Exception:  # no Pillow: the menu entry gets the default icon
        pass
    lines += ["Terminal=false", "Categories=Science;Education;", f"StartupWMClass={APP_CLASS}"]
    os.makedirs(os.path.dirname(target["launcher"]), exist_ok=True)
    with open(target["launcher"], "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(target["launcher"], 0o755)
    os.makedirs(os.path.dirname(target["command"]), exist_ok=True)
    with open(target["command"], "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\n"
                f"# Starts {APP_NAME} with the Python interpreter that installed it.\n"
                f'exec {shlex.quote(sys.executable or "python3")} {shlex.quote(target["script"])} "$@"\n')
    os.chmod(target["command"], 0o755)
    _make_readable(files)
    _refresh_menus(os.path.dirname(target["launcher"]))


def _install_mac(target, source_dir, names, version):
    app, resources = target["launcher"], target["files"]
    macos = os.path.join(app, "Contents", "MacOS")
    os.makedirs(macos, exist_ok=True)
    _copy_program(source_dir, names, resources)
    has_icon = False
    try:
        app_icon_image(1024).save(os.path.join(resources, "AppIcon.icns"), format="ICNS")
        has_icon = True
    except Exception:  # no Pillow: the application gets the default icon
        pass
    executable = os.path.join(macos, APP_NAME)
    with open(executable, "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\n"
                f"# Starts {APP_NAME} with the Python interpreter that installed it.\n"
                f'exec {shlex.quote(sys.executable or "python3")} "$(dirname "$0")/../Resources/{MAIN_FILE}" "$@"\n')
    os.chmod(executable, 0o755)
    info = {
        "CFBundleName": APP_NAME, "CFBundleDisplayName": APP_NAME, "CFBundleExecutable": APP_NAME,
        "CFBundleIdentifier": APP_ID,
        "CFBundlePackageType": "APPL", "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleShortVersionString": version, "CFBundleVersion": version,
        "LSMinimumSystemVersion": "10.13", "NSHighResolutionCapable": True,
        # Lumerical project files can be opened with the program (Finder: Open With)
        "CFBundleDocumentTypes": [{"CFBundleTypeName": "Lumerical FDTD project", "CFBundleTypeRole": "Viewer",
                                   "CFBundleTypeExtensions": ["fsp"], "LSHandlerRank": "Alternate"}],
    }
    if has_icon:
        info["CFBundleIconFile"] = "AppIcon"
    with open(os.path.join(app, "Contents", "Info.plist"), "wb") as f:
        plistlib.dump(info, f)
    _make_readable(app)
    os.utime(app)  # lets Finder and Launchpad notice the change
    lsregister = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/"
                  "LaunchServices.framework/Support/lsregister")
    if os.path.exists(lsregister):
        subprocess.run([lsregister, "-f", app], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def installed_copy(platform=None):
    """The installation to update: {'version', 'systemwide', 'script'}, or None if not installed."""
    recorded = (load_config().get("installed") or {}).get("systemwide")
    for systemwide in ((True, False) if recorded else (False, True)):
        target = install_target(systemwide, platform)
        if target and os.path.isfile(target["script"]):
            return {"version": file_version(target["script"]), "systemwide": systemwide,
                    "script": target["script"]}
    return None


def startup_question(config, platform=None):
    """What to ask when the program starts: 'install' at the first start, 'update' when an
    older version is installed and this newer one runs from elsewhere, otherwise None."""
    platform = platform or sys.platform
    if install_target(False, platform) is None or running_installed_copy(platform):
        return None
    if not config.get("first_start_done"):
        return "install"
    installed = config.get("installed") or {}
    if (installed and os.path.lexists(installed.get("launcher", ""))
            and _version_tuple(installed.get("version")) < _version_tuple(APP_VERSION)
            and config.get("update_declined") != APP_VERSION):
        return "update"
    return None


# ---- updates from GitHub ---------------------------------------------------------
# New versions are published in the GitHub project UPDATE_REPOSITORY: the program files
# (APP_FILES) in its top folder. If the project has GitHub releases, the newest release
# is used, otherwise the newest version of the main branch. APP_VERSION in its fsp_gui.py
# decides whether it is newer than the installed copy.
GITHUB_API = "https://api.github.com"
GITHUB_RAW = "https://raw.githubusercontent.com"
UPDATE_TIMEOUT = 15          # seconds allowed for each download
MAX_DOWNLOAD = 5_000_000     # bytes; the largest program file is about 150 kB


class UpdateError(Exception):
    """No new version could be found or downloaded; the message says why."""


def _download(url):
    request = urllib.request.Request(url, headers={"User-Agent": f"{APP_SLUG}/{APP_VERSION}"})
    with urllib.request.urlopen(request, timeout=UPDATE_TIMEOUT) as response:
        data = response.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise UpdateError(f"{url} is unexpectedly large; it was not used.")
    return data


def _connection_problem(err):
    reason = getattr(err, "reason", err)
    text = f"Could not connect to GitHub to look for a new version ({reason})."
    if "CERTIFICATE_VERIFY_FAILED" in str(reason):
        text += ("\nPython cannot check GitHub's security certificate. With Python from python.org on macOS,\n"
                 "run 'Install Certificates.command' in its folder in Applications.")
    return text


def _download_program(base, name):
    """One program file from GitHub as text; UpdateError if it is missing or not valid Python."""
    try:
        data = _download(base + name)
    except urllib.error.HTTPError as err:
        raise UpdateError(f"{base}{name} could not be downloaded (HTTP error {err.code}).") from None
    except OSError as err:
        raise UpdateError(_connection_problem(err)) from None
    source = data.decode("utf-8", errors="replace")
    if name.endswith(".py"):
        try:
            compile(source, name, "exec")  # a valid Python program, not e.g. an error page
        except (SyntaxError, ValueError):
            raise UpdateError(f"The file {name} downloaded from {base} is not a valid Python program.") from None
    return source


def find_latest_version():
    """The newest version on GitHub: {'version', 'source' (the text of the main program),
    'files' (its file names), 'base' (its folder URL)}.

    Raises UpdateError when GitHub cannot be reached or the file is not a valid program.
    """
    ref = "HEAD"  # the main branch, used when the project has no releases
    try:
        release = json.loads(_download(f"{GITHUB_API}/repos/{UPDATE_REPOSITORY}/releases/latest"))
        if isinstance(release, dict) and release.get("tag_name"):
            ref = release["tag_name"]
    except (OSError, ValueError, UpdateError):  # no releases (404), API limit reached, offline, ...
        pass
    base = f"{GITHUB_RAW}/{UPDATE_REPOSITORY}/{urllib.parse.quote(ref)}/"
    source = _download_program(base, MAIN_FILE)
    match = _VERSION_RE.search(source)
    if not match:
        raise UpdateError(f"The file {MAIN_FILE} downloaded from {base} has no version number.")
    return {"version": match.group(1), "source": source, "files": program_files(source), "base": base}


def update_check_interval(config):
    """Days between the automatic looks for a new version; 0 if they are turned off."""
    if not config.get("check_updates", True):
        return 0
    try:
        return max(0, int(config.get("update_check_days", UPDATE_CHECK_DAYS)))
    except (TypeError, ValueError):
        return max(0, UPDATE_CHECK_DAYS)


def every_days_text(days):
    return "every week" if days == 7 else "every day" if days == 1 else f"every {days} days"


def update_check_due(config, now=None):
    """Is it time for the automatic look for a new version (see update_check_interval)?"""
    days = update_check_interval(config)
    if days <= 0:
        return False
    now = time.time() if now is None else now
    return now - float(config.get("last_update_check") or 0) >= days * 86400


def remember_update_check():
    config = load_config()
    config["last_update_check"] = time.time()
    save_config(config)


def install_update(latest, systemwide, platform=None):
    """Download every file of a version found by find_latest_version and install it over the
    installed copy. Nothing is changed unless all files arrive. Returns (success, message)."""
    with tempfile.TemporaryDirectory() as tmp:
        try:
            for name in latest["files"]:
                source = latest["source"] if name == MAIN_FILE else _download_program(latest["base"], name)
                with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
                    f.write(source)
        except UpdateError as err:
            return False, f"The update was not installed.\n\n{err}"
        return install_app(systemwide, platform, source_dir=tmp, admin_option="--update")


def update_installed_app(platform=None):
    """Look for a newer version on GitHub and install it over the installed copy. Returns (success, message)."""
    installed = installed_copy(platform)
    if installed is None:
        return False, (f"{APP_NAME} is not installed, so there is nothing to update. "
                       f"Install it with: python3 {MAIN_FILE} --install")
    try:
        latest = find_latest_version()
    except UpdateError as err:
        return False, str(err)
    remember_update_check()
    if not is_newer(latest["version"], installed["version"]):
        return True, (f"{APP_NAME} {installed['version']} is up to date "
                      f"(newest version on GitHub: {latest['version']}).")
    ok, message = install_update(latest, installed["systemwide"], platform)
    if ok:
        message += f"\nRestart {APP_NAME} to use version {latest['version']}."
    return ok, message


class AppDialog(tk.Toplevel):
    """Modal question in the colours of the program, with its icon and a row of buttons;
    show() returns the key of the chosen button (None if the window was closed).

    checkbox is an optional (label, tkinter.BooleanVar) pair shown above the buttons.
    """

    def __init__(self, root, title, message, buttons, note="", icon=None, checkbox=None):
        super().__init__(root)
        T = THEME
        self.root, self.choice, self.message = root, None, message
        self.title(title)
        self.resizable(False, False)
        self.transient(root)
        self.configure(bg=T["panel_bg"], padx=18, pady=16)
        top = tk.Frame(self, bg=T["panel_bg"])
        top.pack(anchor=tk.W, fill=tk.X)
        if icon is not None:
            label = tk.Label(top, image=icon, bg=T["panel_bg"])
            label.image = icon  # keep a reference, or Tk shows an empty square
            label.pack(side=tk.LEFT, anchor=tk.N, padx=(0, 16))
        tk.Label(top, text=message, justify=tk.LEFT, wraplength=440, font=(FONT_UI, 11),
                 bg=T["panel_bg"], fg=T["text"]).pack(side=tk.LEFT, anchor=tk.W)
        if checkbox:
            tk.Checkbutton(self, text=checkbox[0], variable=checkbox[1], font=(FONT_UI, 10),
                           bg=T["panel_bg"], fg=T["text"], activebackground=T["panel_bg"],
                           activeforeground=T["text"], selectcolor=T["entry_bg"],
                           highlightthickness=0).pack(anchor=tk.W, pady=(14, 0))
        row = tk.Frame(self, bg=T["panel_bg"])
        row.pack(anchor=tk.E, pady=(16, 0))
        self.buttons = {}
        for i, (key, text) in enumerate(buttons):
            self.buttons[key] = FlatButton(row, text, lambda k=key: self._choose(k),
                                           variant="primary" if i == 0 else "secondary", bold=i == 0)
            self.buttons[key].pack(side=tk.LEFT, padx=(8, 0))
        if note:
            tk.Label(self, text=note, justify=tk.LEFT, wraplength=480, font=(FONT_UI, 9),
                     bg=T["panel_bg"], fg=T["text_muted"]).pack(anchor=tk.W, pady=(14, 0))
        self.bind("<Return>", lambda event: self._choose(buttons[0][0]))
        self.bind("<Escape>", lambda event: self._choose(None))
        self.protocol("WM_DELETE_WINDOW", lambda: self._choose(None))

    def _choose(self, key):
        self.choice = key
        self.destroy()

    def show(self):
        self.update_idletasks()  # centre over the main window
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - self.winfo_reqwidth()) // 2)
        y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - self.winfo_reqheight()) // 3)
        self.geometry(f"+{x}+{y}")
        try:
            self.wait_visibility()
            self.grab_set()
            self.focus_set()
        except tk.TclError:
            pass
        self.wait_window()
        return self.choice


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
        "Simulated Field |E|² in YZ Planes (ports & monitors)",
        "Power Through YZ Planes vs x & λ (ports & monitors)",
        "Power Spectrum at Selected Plane (P / P_source)",
        "S-Parameters from Power |S_i1|² (dB)",
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
        self.app_icon = app_icon_photo(root)  # the window and Dock icon
        if self.app_icon is not None:
            try:
                self.root.iconphoto(True, self.app_icon)
            except tk.TclError:
                pass
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
        self._busy_depth = 0
        # colour map of the colour (surface) plots, and whether it runs the other way; remembered
        config = load_config()
        cmap = config.get("colormap", self.COLORMAP_DEFAULT)
        self.cmap_var = tk.StringVar(value=cmap if cmap in self.colormap_choices() else self.COLORMAP_DEFAULT)
        self.cmap_reverse_var = tk.BooleanVar(value=bool(config.get("colormap_reversed", False)))

        self._build_ui()
        self._build_menu()

        if sys.platform == "darwin":  # a .fsp file opened with the installed application in Finder
            try:
                self.root.createcommand("::tk::mac::OpenDocument", lambda *paths: paths and self.open_fsp_file(paths[0]))
            except tk.TclError:
                pass
        if initial_fsp:  # the file given on the command line
            self.root.after(100, lambda: self.open_fsp_file(initial_fsp, initial_h5))

    def _build_menu(self):
        mod = "Cmd" if sys.platform == "darwin" else "Ctrl"
        menubar = tk.Menu(self.root)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Open .fsp File…", command=self.browse_fsp, accelerator=f"{mod}+O")
        file_menu.add_command(label="Open .h5 Field File…", command=self.browse_h5)
        file_menu.add_separator()
        file_menu.add_command(label="Save Geometry as GDS File…", command=lambda: self.save_geometry("gds"))
        file_menu.add_command(label="Save Geometry as Python (gdsfactory) Script…",
                              command=lambda: self.save_geometry("py"))
        file_menu.add_separator()
        file_menu.add_command(label="Export Plot Data to Clipboard", command=self.export_data_to_clipboard,
                              **({"accelerator": "Cmd+C"} if sys.platform == "darwin" else {}))
        file_menu.add_separator()
        file_menu.add_command(label="Quit", command=self.root.quit, accelerator=f"{mod}+Q")
        menubar.add_cascade(label="File", menu=file_menu)
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label=f"About {APP_NAME}", command=self.show_about)
        help_menu.add_command(label="Check for Updates…", command=self.check_for_update_now)
        if install_target(False) is not None and not FROZEN:  # installing: macOS and Linux
            help_menu.add_separator()
            help_menu.add_command(label=f"Install {APP_NAME}…", command=self.ask_install)
            help_menu.add_command(label="Uninstall…", command=self.ask_uninstall)
        help_menu.add_separator()
        help_menu.add_command(label="GitHub Page", command=lambda: webbrowser.open(APP_WEBSITE))
        menubar.add_cascade(label="Help", menu=help_menu)
        self.root.config(menu=menubar)
        self.root.bind_all("<Command-o>" if sys.platform == "darwin" else "<Control-o>", lambda e: self.browse_fsp())
        if sys.platform != "darwin":  # macOS has Cmd+Q of its own
            self.root.bind_all("<Control-q>", lambda e: self.root.quit())

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
        # ==================================================================
        # Status Bar (bottom): what the program is doing (left) and the state of the .h5 file (right)
        # ==================================================================
        statusbar = tk.Frame(self.root, bg=T["bar_bg"])
        statusbar.pack(fill=tk.X, side=tk.BOTTOM)
        tk.Frame(self.root, bg=T["border"], height=1).pack(fill=tk.X, side=tk.BOTTOM)
        self.status_info_lbl = tk.Label(
            statusbar, text="", font=(FONT_UI, 9), bg=T["bar_bg"], fg=T["text_muted"], anchor="e"
        )
        self.status_info_lbl.pack(side=tk.RIGHT, padx=(12, 10), pady=2)
        # layers, ports and monitors of the model, and the stretch of the view under the pointer
        # (shown in the headers of the views before, where it hid the pointer position)
        self.status_model_lbl = tk.Label(
            statusbar, text="", font=(FONT_UI, 9), bg=T["bar_bg"], fg=T["port_yellow"], anchor="e"
        )
        self.status_model_lbl.pack(side=tk.RIGHT, padx=(12, 0), pady=2)
        self.status_lbl = tk.Label(
            statusbar, text="Ready: open an .fsp file", font=(FONT_UI, 9), bg=T["bar_bg"],
            fg=T["text_muted"], anchor="w"
        )
        self.status_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(10, 0), pady=2)

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
            quad_frame, "xy", "XY View (Top)", "x", "y", on_port_clicked=self.select_port,
            on_info=self._show_model_info,
        )
        self.view_xz = ModelOrthoCanvas2D(
            quad_frame, "xz", "XZ View (Side)", "x", "z", on_port_clicked=self.select_port,
            on_info=self._show_model_info,
        )
        self.view_3d = ModelView3DCanvas(
            quad_frame, on_port_clicked=self.select_port
        )
        self.view_yz = ModelOrthoCanvas2D(
            quad_frame, "yz", "YZ View (Cross-Section)", "y", "z", on_port_clicked=self.select_port,
            on_info=self._show_model_info,
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

        self._build_colormap_controls(plot_header, T["panel_bg"]).pack(side=tk.RIGHT, padx=(4, 8), pady=3)

        tk.Label(
            plot_header,
            text="Results Plot Window (5th Window)",
            font=(FONT_UI, 11, "bold"),
            bg=T["panel_bg"],
            fg=T["text"],
            anchor="w",
        ).pack(side=tk.LEFT, padx=(10, 8), pady=5)

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
    # Colour Maps of the Colour Plots
    # ==================================================================
    COLORMAP_DEFAULT = "Default"  # each plot its own: viridis for n, inferno for |E|², magma for components
    COLORMAPS = ["plasma", "viridis", "inferno", "magma", "cividis", "turbo", "jet", "Spectral",
                 "rainbow", "hot", "coolwarm", "RdBu", "bone", "gray"]

    @classmethod
    def colormap_choices(cls):
        return [cls.COLORMAP_DEFAULT] + [name for name in cls.COLORMAPS if name in matplotlib.colormaps]

    def _build_colormap_controls(self, parent, bg):
        """The colour map selector and the Reverse check button, in a frame of their own."""
        T = THEME
        frame = tk.Frame(parent, bg=bg)
        tk.Label(frame, text="Colors:", font=(FONT_UI, 9, "bold"), bg=bg, fg=T["text_muted"]).pack(side=tk.LEFT)
        cmb = ttk.Combobox(frame, textvariable=self.cmap_var, state="readonly", width=9,
                           style="Dark.TCombobox", font=(FONT_UI, 9), values=self.colormap_choices())
        cmb.pack(side=tk.LEFT, padx=(4, 4))
        cmb.bind("<<ComboboxSelected>>", lambda e: self._on_colormap_changed())
        Tooltip(cmb, "Colour map of the colour plots (fields, refractive index, propagation).\n"
                     "Default: each plot its own (viridis for n, inferno for |E|², magma for components)")
        chk = tk.Checkbutton(frame, text="Reverse", variable=self.cmap_reverse_var,
                             command=self._on_colormap_changed, font=(FONT_UI, 9),
                             bg=bg, fg=T["text"], activebackground=bg, activeforeground=T["text"],
                             selectcolor=T["entry_bg"], highlightthickness=0, bd=0)
        chk.pack(side=tk.LEFT)
        Tooltip(chk, "Run the colour scale the other way round")
        return frame

    def _cmap(self, default):
        """Name of the colour map to use for a plot whose own colour map is default."""
        name = self.cmap_var.get()
        if name not in matplotlib.colormaps:  # "Default"
            name = default
        return name + "_r" if self.cmap_reverse_var.get() else name

    def _colormesh(self, ax, *args, cmap, **kwargs):
        """ax.pcolormesh with the chosen colour map (cmap: the plot's own); the mesh remembers
        its own map, so a new choice recolours it without computing the plot again."""
        mesh = ax.pcolormesh(*args, cmap=self._cmap(cmap), **kwargs)
        mesh.fsp_default_cmap = cmap
        return mesh

    def _on_colormap_changed(self):
        config = load_config()
        config["colormap"] = self.cmap_var.get()
        config["colormap_reversed"] = bool(self.cmap_reverse_var.get())
        save_config(config)
        figures = [(self.fig, self.mpl_canvas)]
        if self.popout_win is not None and self.popout_win.winfo_exists():
            figures.append((self.popout_fig, self.popout_canvas))
        for fig, canvas in figures:
            for ax in fig.axes:
                for coll in ax.collections:
                    default = getattr(coll, "fsp_default_cmap", None)
                    if default:
                        coll.set_cmap(self._cmap(default))  # its colour bar follows
            canvas.draw_idle()

    # ==================================================================
    # Status Bar
    # ==================================================================
    STATUS_COLORS = {"info": "text_muted", "busy": "warning", "ok": "success", "warn": "warning", "error": "danger"}

    def set_status(self, text, kind="info"):
        """Show a message in the status bar; kind: info, busy, ok, warn or error."""
        self.status_lbl.config(text=text, fg=THEME[self.STATUS_COLORS[kind]])

    @contextlib.contextmanager
    def busy(self, text, done=None):
        """Say in the status bar what is being done while the block runs, with a busy cursor.

        Afterwards the status bar shows done (\u2713 done, with the time taken when it was noticeable),
        or "Ready"; when the block fails, what failed. A busy block inside another one shows its
        text while it runs, then gives the status bar back to the outer one."""
        outer = self._busy_depth == 0
        before = (self.status_lbl.cget("text"), self.status_lbl.cget("fg"))
        self._busy_depth += 1
        self.set_status(f"\u23f3 {text}\u2026", "busy")
        try:
            if outer:
                self.root.configure(cursor="watch")
                self.root.update()  # macOS draws the status bar only when the event loop runs
            else:
                self.status_lbl.update_idletasks()
        except tk.TclError:
            pass
        t0 = time.perf_counter()
        try:
            yield
        except Exception as exc:
            self.set_status(f"\u2717 {text} failed: {str(exc).splitlines()[0] if str(exc) else type(exc).__name__}",
                            "error")
            raise
        else:
            if not outer:
                self.status_lbl.config(text=before[0], fg=before[1])
            elif done:
                dt = time.perf_counter() - t0
                self.set_status(f"\u2713 {done}" + (f" ({dt:.1f} s)" if dt >= 0.1 else ""), "ok")
            else:
                self.set_status("Ready")
        finally:
            self._busy_depth -= 1
            if outer:
                try:
                    self.root.configure(cursor="")
                except tk.TclError:
                    pass

    def _show_model_info(self, view=None):
        """The model summary in the status bar; with view, also how much its axes are stretched."""
        m = self.model
        if m is None:
            self.status_model_lbl.config(text="")
            return
        text = f"{len(m.structures)} layers · {len(m.port_planes())} ports"
        n_fm = sum(mm["has_data"] for mm in m.field_monitors)
        if n_fm:
            text += f" · {n_fm} monitors"
        if view is not None:
            text += f" · {view.view_kind.upper()} view: {view._aspect_text()}"
        self.status_model_lbl.config(text=text)

    def _update_h5_status(self):
        """The right side of the status bar: which .h5 field file is open, or why there is none."""
        m = self.model
        if m is None:
            text, kind = "", "info"
        elif m.h5_path:
            text, kind = f"\u2713 .h5 field file: {os.path.basename(m.h5_path)}", "ok"
        elif h5py is None:
            text, kind = "\u26a0 h5py is not installed: .h5 field files cannot be read", "warn"
        elif m.h5_missing:
            text, kind = f"\u26a0 .h5 file not found: {os.path.basename(m.h5_missing)}", "warn"
        elif m.h5_skipped:
            text, kind = f"\u26a0 {os.path.basename(m.h5_skipped)} not opened: the project holds no results", "warn"
        else:
            text, kind = "\u26a0 No .h5 field file found", "warn"
        self.status_info_lbl.config(text=text, fg=THEME[self.STATUS_COLORS[kind]])

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
            if h5py is None:
                messagebox.showerror("Cannot Open .h5 File", "The h5py package is not installed: .h5 files cannot be read.")
                return
            try:
                with self.busy(f"Opening {os.path.basename(path)}"):
                    self.model.load_h5(path)
            except Exception as exc:
                messagebox.showerror("Error Opening .h5 File", f"Could not read {path}:\n\n{exc}")
                return
            self._refresh_after_load()

    def open_fsp_file(self, fsp_path, h5_path=None):
        t0 = time.perf_counter()
        try:
            with self.busy(f"Opening {os.path.basename(fsp_path)}"):
                model = FSPModelData(fsp_path, h5_path)
        except Exception as exc:
            messagebox.showerror("Error Opening .fsp File", f"Could not read {fsp_path}:\n\n{exc}")
            return
        self.model = model
        self._refresh_after_load(t0)

    def _refresh_after_load(self, t0=None):
        if self.model is None:
            return
        t0 = time.perf_counter() if t0 is None else t0
        # the open files are named only in the window title: long names in the toolbar pushed
        # the port and result selectors out of the window
        title = os.path.basename(self.model.fsp_path)
        if self.model.h5_path:
            title += f" ({os.path.basename(self.model.h5_path)})"
        self.root.title(f"{APP_NAME} — {title}")

        # Populate Port dropdown
        port_choices = ["All Ports"] + [pt["name"] for pt in self.model.ports]
        for mm in self.model.mode_monitors:
            port_choices.append(mm["name"])
        for mm in self.model.field_monitors:  # y-z planes with a recorded field
            if mm["has_data"]:
                port_choices.append(mm["name"])
        self.cmb_port["values"] = port_choices
        default_port = self.model.ports[0]["name"] if self.model.ports else "All Ports"
        self.port_var.set("All Ports" if len(self.model.ports) > 1 else default_port)

        # Populate Wavelength dropdown
        freqs, lam, kc = self.model.get_frequencies_and_wavelengths()
        lam_choices = [f"Center ({lam[kc]:.1f} nm)"] + [f"{l:.1f} nm (#{idx})" for idx, l in enumerate(lam)]
        self.cmb_lam["values"] = lam_choices
        self.lam_var.set(lam_choices[0])

        self._update_h5_status()
        self._show_model_info()

        # Update 4 views and Parameter Editor
        sel_port = self.port_var.get()
        with self.busy("Drawing the model views"):
            self.view_xy.set_model(self.model, sel_port)
            self.view_xz.set_model(self.model, sel_port)
            self.view_yz.set_model(self.model, sel_port)
            self.view_3d.set_model(self.model, sel_port)
            self.param_editor.set_model(self.model)

        # Automatically plot initial result in the 5th window
        if not self.plot_selected():
            return  # the status bar says why the plot failed
        m = self.model
        n_mon = len(m.mode_monitors) + sum(mm["has_data"] for mm in m.field_monitors)
        self.set_status(f"\u2713 Loaded {os.path.basename(m.fsp_path)}: {len(m.structures)} structures, "
                        f"{len(m.ports)} ports, {n_mon} monitors ({time.perf_counter() - t0:.1f} s)", "ok")

    def select_port(self, port_name):
        if port_name in self.cmb_port["values"]:
            self.port_var.set(port_name)
        self._on_port_dropdown_changed()

    def _on_port_dropdown_changed(self, _event=None):
        sel_port = self.port_var.get()
        # a field monitor has no stored mode: show its recorded field instead
        if self.model is not None and any(mm["name"] == sel_port for mm in self.model.field_monitors) \
                and self.result_var.get() not in ("Simulated Field |E|² in YZ Planes (ports & monitors)", "Power Through YZ Planes vs x & λ (ports & monitors)",
                                                  "Power Spectrum at Selected Plane (P / P_source)", "S-Parameters from Power |S_i1|² (dB)",
                                                  "Top-View Layout & Taper Zoom (XY)"):
            self.result_var.set("Simulated Field |E|² in YZ Planes (ports & monitors)")
        with self.busy(f"Showing {sel_port} in the model views"):
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
            self._render_with_status(self.popout_fig)
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
        self._build_colormap_controls(top, THEME["bar_bg"]).pack(side=tk.RIGHT, padx=6)

        self.popout_fig = Figure(figsize=(10.2, 6.8), dpi=100, facecolor="#12151a")
        self.popout_canvas = FigureCanvasTkAgg(self.popout_fig, master=win)
        self.popout_canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        tb_frame = tk.Frame(win, bg=THEME["bar_bg"])
        tb_frame.pack(fill=tk.X, side=tk.BOTTOM)
        tb = NavigationToolbar2Tk(self.popout_canvas, tb_frame, pack_toolbar=False)
        tb.update()
        tb.pack(side=tk.LEFT, fill=tk.X)

        self._render_with_status(self.popout_fig)
        self.popout_canvas.draw_idle()

    def plot_selected(self):
        """Plot the selected result (also in the separate window); False if it could not be plotted."""
        if self.model is None:
            self._draw_empty_plot("Please open an .fsp file first.")
            return False
        ok = self._render_with_status(self.fig)
        self.mpl_canvas.draw_idle()
        if self.popout_win is not None and self.popout_win.winfo_exists():
            self._render_onto_figure(self.popout_fig)
            self.popout_canvas.draw_idle()
        return ok

    def _render_with_status(self, fig: Figure):
        """Render the selected result onto fig, saying so in the status bar; False if it failed."""
        res_kind = self.result_var.get()
        with self.busy(f"Computing {res_kind}", done=f"Plotted {res_kind}"):
            error = self._render_onto_figure(fig)
        if error:
            self.set_status(f"\u2717 Cannot plot {res_kind}: {error.splitlines()[0]}", "error")
        return not error

    def _get_target_ports(self, max_all=4):
        """Returns list of port/monitor names to plot based on the Port dropdown."""
        sel = self.port_var.get()
        # enabled ports with a stored mode, and the enabled mode expansion monitors, along x
        planes = [pt for pt in self.model.ports if pt.get("mode_exp_obj") is not None and pt["enabled"]]
        planes += [mm for mm in self.model.mode_monitors if mm["enabled"] and mm["placed"]]
        all_names = [pl["name"] for pl in sorted(planes, key=lambda pl: pl["x"])]
        if not all_names:
            all_names = [pt["name"] for pt in self.model.ports if pt.get("mode_exp_obj") is not None]
        if not all_names:
            all_names = [pt["name"] for pt in self.model.ports]
        if sel == "All Ports" or not sel:
            if len(all_names) > max_all:
                # Pick representative subset like fsp_plots.py (first, second, middle, last)
                idxs = np.linspace(0, len(all_names) - 1, max_all, dtype=int)
                return [all_names[i] for i in idxs]
            return all_names
        if any(mm["name"] == sel for mm in self.model.field_monitors):
            raise RuntimeError(f"'{sel}' is a field monitor: it holds the simulated field, not a stored mode.\n"
                               "Choose a port, or the result 'Simulated Field |E|² in YZ Planes'.")
        return [sel]

    def _render_onto_figure(self, fig: Figure):
        """Draw the selected result onto fig; returns None, or the reason it could not be plotted."""
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
            elif res_kind == "Simulated Field |E|² in YZ Planes (ports & monitors)":
                self._plot_simulated_port_fields(fig, port_sel, k_freq, lam)
            elif res_kind == "Power Through YZ Planes vs x & λ (ports & monitors)":
                self._plot_plane_power(fig, port_sel, k_freq, lam)
            elif res_kind == "Power Spectrum at Selected Plane (P / P_source)":
                self._plot_plane_spectrum(fig, port_sel, k_freq, lam)
            elif res_kind == "S-Parameters from Power |S_i1|² (dB)":
                self._plot_s_parameters(fig, k_freq, lam)
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
            return str(exc) or type(exc).__name__
        return None

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

            im_n = self._colormesh(axs[0, j], y, z, M["n"], shading="auto", cmap="viridis", vmin=1.0, vmax=max(2.0, float(M["n"].max())))
            x_val = port_x_map.get(pname, 0.0)
            neff_str = f", n_eff={neff:.4f}" if neff else ""
            axs[0, j].set_title(f"{pname} (x={x_val:.1f} μm)\nRefractive index{neff_str}", fontsize=9)

            im_e = self._colormesh(axs[1, j], y, z, E2_norm, shading="auto", cmap="inferno", vmin=0.0, vmax=1.0)
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
                im = self._colormesh(ax, y, z, mat, shading="auto", cmap="viridis")
                ax.set_title(f"{pname}\nRefractive index n(y, z)", fontsize=9.5)
            else:
                E2 = sum(np.abs(M[f"E{c}"]) ** 2 for c in "xyz")
                mat = E2 / max(float(E2.max()), 1e-30)
                im = self._colormesh(ax, y, z, mat, shading="auto", cmap="inferno", vmin=0.0, vmax=1.0)
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
        """|E|² recorded in the y-z planes of the ports and field monitors, from the .h5 file or
        from the .fsp itself (read one frequency at a time)."""
        planes = self.model.field_planes()
        if not planes:
            raise RuntimeError("No recorded field in any port or monitor plane: the project holds no "
                               "results, and no matching .h5 file is open.")
        if port_sel in planes:
            show = [port_sel]
        elif port_sel and port_sel != "All Ports":
            raise RuntimeError(f"'{port_sel}' has no recorded field. Planes with fields: {', '.join(planes)}")
        elif len(planes) > 8:
            show = [planes[i] for i in np.linspace(0, len(planes) - 1, 8).astype(int)]
        else:
            show = planes

        ncols = min(4, len(show))
        nrows = -(-len(show) // ncols)
        axs = fig.subplots(nrows, ncols, squeeze=False)
        self._style_mpl_axes(axs)
        for ax in axs.ravel()[len(show):]:
            ax.set_visible(False)

        im = None
        first_export = None
        port_names = {pt["name"] for pt in self.model.ports}
        for j, name in enumerate(show):
            ax = axs.ravel()[j]
            x, y, z = self.model.plane_grid(name)
            E2 = sum(np.abs(self.model.plane_field(name, c, k_freq)) ** 2
                     for c in ("Ex", "Ey", "Ez") if self.model.plane_has(name, c))
            if y[0] >= -1e-6 and len(y) > 1:  # symmetric simulation: only y >= 0 recorded
                yy = np.concatenate([-y[:0:-1], y])
                E2f = np.concatenate([E2[:, :0:-1], E2], axis=1)
            else:
                yy, E2f = y, E2
            E2_norm = E2f / max(float(E2f.max()), 1e-300)
            im = self._colormesh(ax, yy, z, E2_norm, shading="auto", cmap="inferno", vmin=0.0, vmax=1.0)
            if name in port_names:
                try:
                    ys, zs, M, _, _ = self.model.stored_mode(name)
                    ax.contour(ys, zs, M["n"], levels=[1.2, 1.445, 1.7], colors="w", linewidths=0.5)
                except Exception:
                    pass
            ax.set_xlim(yy[0], yy[-1])
            ax.set_ylim(z[0], z[-1])
            ax.set_aspect("equal")
            color = THEME["port_yellow"] if name in port_names else THEME["monitor_cyan"]
            ax.set_title(f"{name.replace('output_', '')}\nx = {x:.2f} μm", fontsize=9, color=color)
            ax.set_xlabel("y (μm)")
            if j % ncols == 0:
                ax.set_ylabel("z (μm)")
            if first_export is None:
                first_export = (name, yy, z, E2_norm)

        if im is not None:
            cb = fig.colorbar(im, ax=axs.ravel()[:len(show)].tolist(), shrink=0.8)
            cb.set_label("|E|² (norm.)", color=THEME["text"])
            cb.ax.yaxis.set_tick_params(color=THEME["text_muted"], labelcolor=THEME["text_muted"])

        fig.suptitle(
            f"Simulated |E|² (each plane normalised), λ = {lam[k_freq]:.1f} nm",
            color=THEME["text"],
            fontsize=10.5,
            fontweight="bold",
        )
        fig.set_layout_engine("constrained")
        if first_export:
            pname, yy, z, mat = first_export
            self._store_grid_export(
                f"Simulated |E|^2 ({pname}, lambda={lam[k_freq]:.1f} nm)",
                yy,
                z,
                [("E2_norm", mat)],
            )

    def _plot_plane_power(self, fig: Figure, port_sel: str, k_freq: int, lam: np.ndarray):
        """Power through the port and field monitor planes along x, relative to the first plane:
        at the chosen wavelength, and (when the planes store their power spectrum) against λ."""
        planes = []
        for name in self.model.field_planes():
            pw = self.model.plane_power(name, k_freq)
            if pw is not None:
                x, _, _ = self.model.plane_grid(name)
                planes.append((name, x, pw))
        if not planes:
            raise RuntimeError("No port or monitor plane holds a power result or the E and H fields "
                               "needed to compute it.")
        ref_name, _, ref = planes[0]
        pref = self.model.power_reference()
        ps = None
        if pref is not None and pref[0][k_freq]:
            ps = pref[0]  # P / P_source (the transmission), or P / P(first plane at the source)
            ref_name, ref = ("source" if pref[2] else pref[1][2:-1]), float(ps[k_freq])
        if not ref:
            raise RuntimeError(f"The first plane ({ref_name}) carries no power.")
        port_names = {pt["name"] for pt in self.model.ports}
        spectra = {name: self.model.plane_power_spectrum(name) for name, _, _ in planes}
        has_spectra = all(v is not None for v in spectra.values())

        axs = fig.subplots(1, 2 if has_spectra else 1, squeeze=False)[0]
        self._style_mpl_axes(list(axs))
        a1 = axs[0]
        xs = [x for _, x, _ in planes]
        rel = [pw / ref for _, _, pw in planes]
        a1.plot(xs, rel, "-", color=THEME["text_muted"], lw=1.2)
        for i, ((name, x, _), r) in enumerate(zip(planes, rel)):
            is_port = name in port_names
            a1.plot([x], [r], "s" if is_port else "o", ms=8 if name == port_sel else 6,
                    color=THEME["port_yellow"] if is_port else THEME["monitor_cyan"],
                    markeredgecolor="#ffffff" if name == port_sel else "none")
            a1.annotate(name, (x, r), textcoords="offset points", xytext=(0, 9 if i % 2 == 0 else -16),
                        ha="center", fontsize=7.5, color=THEME["text_muted"])
        a1.set_xlabel("x (μm)")
        a1.set_ylabel(f"P / P({ref_name})")
        a1.set_title(f"Power through the planes along x, λ = {lam[k_freq]:.1f} nm", fontsize=10)
        a1.grid(alpha=0.25, color=THEME["grid_axis"])
        a1.set_ylim(bottom=0, top=max(1.1, max(rel) * 1.15))

        rows = ["plane\tx_um\tP_over_P_first"] + [f"{n}\t{x:.4f}\t{r:.6g}" for (n, x, _), r in zip(planes, rel)]
        if has_spectra:
            a2 = axs[1]
            _, lam_all, _ = self.model.get_frequencies_and_wavelengths()
            order = np.argsort(lam_all)
            ref_spec = ps if ps is not None else spectra[ref_name]
            cmap = matplotlib.colormaps["viridis"]
            shown = [pl for pl in planes if ref_name == "source" or pl[0] != ref_name]
            for i, (name, x, _) in enumerate(shown, start=1):
                t = spectra[name] / np.where(ref_spec == 0, np.nan, ref_spec)
                a2.plot(lam_all[order], t[order], lw=1.4, color=cmap(i / max(1, len(planes) - 1)),
                        label=f"{name} (x={x:.4g} μm)")
            a2.axvline(lam[k_freq], color=THEME["text_dim"], ls=":", lw=1)
            a2.set_xlabel("Wavelength λ (nm)")
            a2.set_ylabel(f"P / P({ref_name})")
            a2.set_title("Power spectrum through each plane", fontsize=10)
            a2.grid(alpha=0.25, color=THEME["grid_axis"])
            a2.legend(fontsize=7.5, facecolor="#1a1e25", edgecolor=THEME["border"], labelcolor=THEME["text"],
                      loc="best", framealpha=0.85)
        fig.set_layout_engine("constrained")
        self.last_exported_tsv = "\n".join(rows)
        self.last_exported_desc = f"Power through {len(planes)} planes, lambda={lam[k_freq]:.1f} nm"

    def _spectrum_target(self, port_sel):
        """The plane(s) whose spectrum is shown: the selected port / field monitor, or for a mode
        expansion monitor the field plane at its x; all planes for 'All Ports'."""
        planes = self.model.field_planes()
        if not planes:
            raise RuntimeError("No port or monitor plane holds a recorded field or power.")
        if not port_sel or port_sel == "All Ports":
            return planes
        if port_sel in planes:
            return [port_sel]
        mm = next((m for m in self.model.mode_monitors if m["name"] == port_sel), None)
        if mm is not None:
            same_x = [n for n in planes if abs(self.model.plane_grid(n)[0] - mm["x"]) < 0.05]
            if same_x:
                return same_x[:1]
        raise RuntimeError(f"'{port_sel}' has no recorded field or power. Planes with data: {', '.join(planes)}")

    def _plot_plane_spectrum(self, fig: Figure, port_sel: str, k_freq: int, lam: np.ndarray):
        """Transmission P / P_source of the selected plane (or of all planes) against wavelength,
        with the part in the stored mode of the plane where there is one."""
        names = self._spectrum_target(port_sel)
        _, lam_all, _ = self.model.get_frequencies_and_wavelengths()
        order = np.argsort(lam_all)
        lam_s = lam_all[order]
        pref = self.model.power_reference()
        if pref is None:
            raise RuntimeError("Neither a source power nor a plane at the source to normalise the powers to.")
        a1, a2 = fig.subplots(1, 2)
        self._style_mpl_axes([a1, a2])
        cmap = matplotlib.colormaps["viridis"]
        ref_label = f"P / {pref[1]}"
        header = ["wavelength_nm"]
        cols = []
        for i, name in enumerate(names):
            T = self.model.plane_transmission(name)
            if T is None:
                continue
            color = cmap(i / max(1, len(names) - 1)) if len(names) > 1 else THEME["monitor_cyan"]
            x = self.model.plane_grid(name)[0]
            a1.plot(lam_s, T[order], lw=1.8, color=color, label=f"{name} (x={x:.4g} μm)")
            a2.plot(lam_s, 10 * np.log10(np.clip(T[order], 1e-12, None)), lw=1.8, color=color, label=name)
            header.append(f"{name}_P_over_{'Psource' if pref[2] else 'Pref'}")
            cols.append(T[order])
            mt = self.model.plane_mode_transmission(name)
            if mt is not None and mt[2] is not None:
                a1.plot(lam_s, mt[2][order], ls="--", lw=1.4, color=color, label=f"{name}: in mode of {mt[0]}")
                a2.plot(lam_s, 10 * np.log10(np.clip(mt[2][order], 1e-12, None)), ls="--", lw=1.4, color=color)
                header.append(f"{name}_mode_{mt[0]}")
                cols.append(mt[2][order])
        if not cols:
            raise RuntimeError("The selected plane holds no power spectrum (no 'power' result, and no E and H fields).")
        for ax in (a1, a2):
            ax.axvline(lam[k_freq], color=THEME["text_dim"], ls=":", lw=1)
            ax.set_xlabel("Wavelength λ (nm)")
            ax.grid(alpha=0.25, color=THEME["grid_axis"])
        a1.set_ylabel(ref_label)
        a1.set_ylim(bottom=0)
        a1.set_title("Solid: all power · dashed: in the stored mode", fontsize=9.5)
        a2.set_ylabel(f"{ref_label} (dB)")
        a2.set_title("Transmission (dB)", fontsize=9.5)
        a1.legend(fontsize=7.5, facecolor="#1a1e25", edgecolor=THEME["border"], labelcolor=THEME["text"], loc="best")
        note = "" if pref[2] else f"  (relative to {pref[1][2:-1]}: the field file has no absolute power scale)"
        fig.suptitle(f"Power spectrum through {names[0] if len(names) == 1 else f'{len(names)} planes'}{note}",
                     color=THEME["text"], fontsize=10.5, fontweight="bold")
        fig.set_layout_engine("constrained")
        rows = ["\t".join(header)] + ["\t".join([f"{l:.3f}"] + [f"{c[j]:.6g}" for c in cols]) for j, l in enumerate(lam_s)]
        self.last_exported_tsv = "\n".join(rows)
        self.last_exported_desc = f"Power spectrum ({len(cols)} curves x {len(lam_s)} wavelengths)"

    def _plot_s_parameters(self, fig: Figure, k_freq: int, lam: np.ndarray):
        """|S_i1|^2 of every plane from the stored powers: P_i / P_source (all modes) and, where the
        plane has a stored mode, the power in that mode; S11 estimated from the power balance."""
        pref = self.model.power_reference()
        if pref is None:
            raise RuntimeError("The project stores no source power spectrum (P0 of the source) and no plane "
                               "at the source: S-parameters cannot be normalised.")
        absolute = pref[2]
        ref_plane = None if absolute else pref[1][2:-1]
        planes = [n for n in self.model.field_planes() if self.model.plane_transmission(n) is not None]
        if not planes:
            raise RuntimeError("No plane holds a power spectrum.")
        _, lam_all, _ = self.model.get_frequencies_and_wavelengths()
        order = np.argsort(lam_all)
        lam_s = lam_all[order]
        src = next((pt for pt in self.model.ports if pt.get("is_source") and pt["enabled"]), None)
        x_src = src["x"] if src else None

        a1, a2 = fig.subplots(1, 2)
        self._style_mpl_axes([a1, a2])
        cmap = matplotlib.colormaps["viridis"]
        header, cols = ["wavelength_nm"], []
        downstream = [n for n in planes if n != ref_plane and (x_src is None or self.model.plane_grid(n)[0] > x_src + 0.01)]
        for i, name in enumerate(downstream):
            T = self.model.plane_transmission(name)
            color = cmap(i / max(1, len(downstream) - 1))
            x = self.model.plane_grid(name)[0]
            a1.plot(lam_s, 10 * np.log10(np.clip(T[order], 1e-12, None)), lw=1.6, color=color, label=f"{name} (x={x:.4g} μm)")
            header += [f"|S|^2_{name}", f"|S|_{name}"]
            cols += [T[order], np.sqrt(np.clip(T[order], 0, None))]
            mt = self.model.plane_mode_transmission(name)
            if mt is not None and mt[2] is not None:
                a2.plot(lam_s, 10 * np.log10(np.clip(mt[2][order], 1e-12, None)), lw=1.8, color=color,
                        label=f"{name} → mode of {mt[0]}")
                header += [f"|S|^2_mode_{name}", f"|S|_mode_{name}"]
                cols += [mt[2][order], np.sqrt(np.clip(mt[2][order], 0, None))]
        if downstream and x_src is not None and absolute:
            # power balance at the first plane after the source: P = P_source - P_reflected
            first = downstream[0]
            R = 1.0 - self.model.plane_transmission(first)
            a2.plot(lam_s, 10 * np.log10(np.clip(R[order], 1e-12, None)), color=THEME["warning"], lw=1.4, ls="--",
                    label=f"|S11|² ≈ 1 − P({first})/P_source (power balance)")
            header += ["|S11|^2_estimate"]
            cols += [R[order]]
        for ax in (a1, a2):
            ax.axvline(lam[k_freq], color=THEME["text_dim"], ls=":", lw=1)
            ax.set_xlabel("Wavelength λ (nm)")
            ax.set_ylabel("dB")
            ax.grid(alpha=0.25, color=THEME["grid_axis"])
            if ax.get_legend_handles_labels()[0]:
                ax.legend(fontsize=7.5, facecolor="#1a1e25", edgecolor=THEME["border"], labelcolor=THEME["text"], loc="best")
        a1.set_title(f"|S_i1|² = P_i / {pref[1]} (all power)", fontsize=9.5)
        a2.set_title("|S_i1|² in the stored mode; |S11|²" if absolute else "|S_i1|² in the stored mode", fontsize=9.5)
        fig.suptitle("S-parameter magnitudes from the stored powers" + ("" if absolute else
                     f"  (relative to {ref_plane}: the field file has no absolute power scale, so no |S11|²)"),
                     color=THEME["text"], fontsize=10.5, fontweight="bold")
        fig.set_layout_engine("constrained")
        rows = ["\t".join(header)] + ["\t".join([f"{l:.3f}"] + [f"{c[j]:.6g}" for c in cols]) for j, l in enumerate(lam_s)]
        self.last_exported_tsv = "\n".join(rows)
        self.last_exported_desc = f"S-parameter magnitudes ({len(cols)} columns x {len(lam_s)} wavelengths)"

    def _plot_power_and_mode_coupling(self, fig: Figure, port_sel: str):
        xs, freqs, lam, P, Prel, eta, Tmode = self.model.compute_power_and_mode_coupling()
        a1, a2 = fig.subplots(1, 2)
        self._style_mpl_axes([a1, a2])

        start_idx = 1 if len(xs) > 1 else 0
        shown = range(len(freqs)) if len(freqs) <= 9 else np.unique(np.linspace(0, len(freqs) - 1, 7).round().astype(int))
        for k in shown:  # at most 7 wavelengths of a long list, to keep the legend readable
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
        indices = [i for i in indices if np.isfinite(Prel[i]).any()]  # ports that recorded a field
        if not indices:
            raise RuntimeError(f"{'This port has' if port_sel != 'All Ports' else 'The ports have'} no recorded "
                               "field in the .h5 file (a disabled port records nothing).")

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
        def is_xz(grp):  # a y-normal plane: many x, one y
            try:
                g = self.model.h5[grp]
                return g["x"].size > 1 and g["y"].size == 1 and g["z"].size > 1 and "Ex" in g
            except Exception:
                return False
        mon_info = self.model.dft_monitors.get("dft_xz_propagation")
        h5_grp = mon_info["h5_group"] if mon_info and mon_info["h5_group"] else None
        if not h5_grp or not is_xz(h5_grp):
            h5_grp = next((g for g in self.model.h5 if is_xz(g)), None)
        if h5_grp is None:
            raise RuntimeError("The .h5 file holds no XZ field monitor (a y-normal plane): the XZ monitor "
                               "was disabled or not recorded.")

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
            im = self._colormesh(ax, x[sel], z, dB[:, sel], shading="auto", cmap="inferno", vmin=-35, vmax=0, rasterized=True)
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
        core_lo, core_hi = core_y_window(self.model.structures)

        for ax, ylim in ((a1, (min(ys_all) * 1.05, max(ys_all) * 1.05)), (a2, (core_lo, core_hi))):
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
                core_hi - 0.06 * (core_hi - core_lo),
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
            im = self._colormesh(ax, y, z, arr, shading="auto", cmap="magma")
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
    # Installation and Updates
    # ==================================================================
    def dialog_icon(self):
        if getattr(self, "_dialog_icon", None) is None:
            self._dialog_icon = app_icon_photo(self.root, 64, margin=0)
        return self._dialog_icon

    def message(self, title, text, buttons=(("ok", "OK"),), note=""):
        return AppDialog(self.root, title, text, list(buttons), note=note, icon=self.dialog_icon()).show()

    def _weekly_checkbox(self, checked):
        """(label, variable) for the 'look for new versions' checkbox, or None if checks are off."""
        self.weekly = None
        days = update_check_interval(dict(load_config(), check_updates=True))
        if days <= 0:
            return None
        self.weekly = tk.BooleanVar(self.root, value=checked)
        return (f"Look for new versions on GitHub {every_days_text(days)}", self.weekly)

    def startup_check(self, platform=None):
        """At the first start ask whether to install the program; later offer to update the
        installed copy, from this copy if it is newer, or else once a week from GitHub."""
        platform = platform or sys.platform
        config = load_config()
        question = startup_question(config, platform)
        if question is None:
            if not config.get("first_start_done"):
                config["first_start_done"] = True
                save_config(config)
            if update_check_due(config) and (installed_copy(platform) or FROZEN
                                             or install_target(False, platform) is None):
                self.check_for_update(platform)
            return
        if question == "install":
            self.ask_install(platform, first_start=True)
            return
        installed = config["installed"]
        choice = AppDialog(
            self.root, f"Update {APP_NAME}?",
            f"{APP_NAME} {installed.get('version')} is installed on this computer.\n\n"
            f"Update it to version {APP_VERSION}, the version you are running now?",
            [("update", "Update"), ("later", "Not now")],
            note="If you choose Not now, you are not asked again for this version; "
                 f"Help > Install {APP_NAME} updates it later.", icon=self.dialog_icon()).show()
        if choice == "update":
            self.message(f"Update {APP_NAME}", install_app(bool(installed.get("systemwide")), platform)[1])
        else:
            config = load_config()
            config["update_declined"] = APP_VERSION
            save_config(config)

    def ask_install(self, platform=None, first_start=False):
        """Ask where to install the program (Help > Install, and the question at the first start)."""
        platform = platform or sys.platform
        if install_target(False, platform) is None:
            self.message(f"Install {APP_NAME}",
                         f"Installing is only supported on macOS and Linux, not on {_platform_name(platform)}.")
            return
        mac = platform == "darwin"
        home = os.path.expanduser("~")

        def place(systemwide):  # folder of the launcher, with the home folder shown as ~
            folder = os.path.dirname(install_target(systemwide, platform)["launcher"])
            return "~" + folder[len(home):] if folder.startswith(home + os.sep) else folder

        choice = AppDialog(
            self.root, f"Install {APP_NAME}?",
            (f"Welcome to {APP_NAME} {APP_VERSION}!\n\n" if first_start else "")
            + f"Install {APP_NAME} {APP_VERSION}? It is then listed in "
            + ("Launchpad and the Applications folder" if mac else "the application menu")
            + f" and starts like any other application, as {APP_NAME} with its own icon, without the terminal.\n\n"
            f"Install systemwide: for all users of this computer\n    ({place(True)}"
            + ("" if mac else ", needs administrator rights") + ")\n"
            f"Only for me: just for your user account\n    ({place(False)})",
            [("systemwide", "Install systemwide"), ("user", "Only for me"), ("no", "Don't install")],
            note=("This question is asked only once. " if first_start else "")
                 + f"Help > Install {APP_NAME} and Help > Uninstall do the same later.",
            icon=self.dialog_icon(), checkbox=self._weekly_checkbox(load_config().get("check_updates", True))).show()
        message = None
        if choice in ("systemwide", "user"):
            message = install_app(choice == "systemwide", platform)[1]
        config = load_config()
        config["first_start_done"] = True
        if self.weekly is not None and choice is not None:
            config["check_updates"] = bool(self.weekly.get())
        save_config(config)
        if message:
            self.message(f"Install {APP_NAME}", message)

    def ask_uninstall(self, platform=None):
        if installed_copy(platform) is None:
            self.message(f"Uninstall {APP_NAME}", f"{APP_NAME} is not installed.")
            return
        if self.message(f"Uninstall {APP_NAME}?",
                        f"Remove the installed {APP_NAME} from this computer?\n\n"
                        "Your .fsp files and the files you saved are not touched.",
                        [("no", "Keep it"), ("yes", "Uninstall")]) == "yes":
            self.message(f"Uninstall {APP_NAME}", uninstall_app(platform)[1])

    def check_for_update(self, platform=None, interactive=False):
        """Look for a new version on GitHub without blocking the window; offer it if there is one.

        interactive (Help > Check for Updates): also say so when there is no new version or
        GitHub cannot be reached, and offer a version the user skipped before.
        """
        result = {}

        def look():  # runs in a background thread: no Tk calls here
            try:
                result["latest"] = find_latest_version()
            except Exception as err:  # offline, GitHub unreachable, ...: try again at the next start
                result["error"] = err

        # Free unused Tk objects (e.g. of closed dialogs) here in the main thread: if Python's
        # garbage collector ran into them in the background thread, Tcl would abort the program.
        gc.collect()
        thread = threading.Thread(target=look, daemon=True)
        thread.start()

        def wait():
            if thread.is_alive():
                self.root.after(200, wait)
                return
            if interactive:
                self._looking_for_update = False
                self.root.configure(cursor="")
                self.set_status("Ready")
            if "latest" in result:
                (self.report_update if interactive else self.offer_update)(result["latest"], platform)
            elif interactive:
                self.message("Check for Updates", f"Could not look for a new version.\n\n{result['error']}")

        self.root.after(200, wait)
        return thread

    def check_for_update_now(self, platform=None):
        """Help > Check for Updates: look on GitHub now and say what was found."""
        if getattr(self, "_looking_for_update", False):
            return None
        self._looking_for_update = True
        self.root.configure(cursor="watch")
        self.set_status("\u23f3 Looking for a new version on GitHub\u2026", "busy")
        return self.check_for_update(platform, interactive=True)

    def report_update(self, latest, platform=None):
        """Answer Help > Check for Updates: offer the new version, or say that there is none."""
        installed = installed_copy(platform)
        if installed is not None and is_newer(latest["version"], installed["version"]):
            self.offer_update(latest, platform, asked=True)
            return
        remember_update_check()
        if installed is None and is_newer(latest["version"], APP_VERSION):
            self.offer_download(latest, asked=True)
            return
        current = installed["version"] if installed is not None else APP_VERSION
        self.message("Check for Updates", f"{APP_NAME} {current} is up to date.\n\n"
                                          f"The newest version on GitHub is {latest['version']}.")

    def offer_update(self, latest, platform=None, asked=False):
        """Ask whether to install the newer version found on GitHub; install it and offer a restart.

        asked: the user chose Check for Updates, so a version skipped before is offered too.
        """
        remember_update_check()
        config = load_config()
        installed = installed_copy(platform)
        if installed is None:  # a packaged application, or a copy that is not installed
            self.offer_download(latest, asked)
            return
        if (not is_newer(latest["version"], installed["version"])
                or (not asked and config.get("skipped_version") == latest["version"])):
            return
        choice = AppDialog(
            self.root, f"Update {APP_NAME}?",
            f"A new version of {APP_NAME} is available.\n\n"
            f"    Installed version:  {installed['version']}\n"
            f"    New version:  {latest['version']}\n\n"
            "Download and install it now?",
            [("update", "Update now"), ("later", "Not now"), ("skip", "Skip this version")],
            note=f"From github.com/{UPDATE_REPOSITORY}. Help > Check for Updates looks for a new version at any time.",
            icon=self.dialog_icon(), checkbox=self._weekly_checkbox(config.get("check_updates", True))).show()
        config = load_config()
        if self.weekly is not None and choice is not None:
            config["check_updates"] = bool(self.weekly.get())
        if choice == "skip":
            config["skipped_version"] = latest["version"]
        save_config(config)
        if choice != "update":
            return
        self.root.configure(cursor="watch")
        self.root.update()
        try:
            ok, message = install_update(latest, installed["systemwide"], platform)
        finally:
            self.root.configure(cursor="")
        if not ok:
            self.message(f"Update {APP_NAME}", message)
            return
        if self.message(f"Restart {APP_NAME}?",
                        f"{message}\n\nRestart now to use version {latest['version']}? "
                        "The file that is open now is opened again.",
                        [("restart", "Restart now"), ("later", "Later")]) == "restart":
            self.restart(installed["script"])

    def offer_download(self, latest, asked=False):
        """A newer version for a copy that cannot update itself (the Windows application, or a
        copy that is not installed): offer the download page of the newest GitHub release."""
        config = load_config()
        if not is_newer(latest["version"], APP_VERSION) or (
                not asked and config.get("skipped_version") == latest["version"]):
            return
        if FROZEN and sys.platform.startswith("win"):
            how = (f"Download FSP-Viewer-{latest['version']}-windows.zip from the release page, unpack it "
                   f"and start '{APP_NAME}.exe' in the new folder (the old folder can then be deleted).")
        elif install_target(False) is not None:
            how = (f"Download it from GitHub, or install {APP_NAME} (Help > Install), "
                   "which then updates itself.")
        else:
            how = "Download it from the GitHub release page."
        choice = AppDialog(
            self.root, f"New version of {APP_NAME}",
            f"{APP_NAME} {latest['version']} is available; you are using version {APP_VERSION}.\n\n{how}",
            [("open", "Open Download Page"), ("later", "Not now"), ("skip", "Skip this version")],
            note=f"From github.com/{UPDATE_REPOSITORY}. Help > Check for Updates looks for a new version at any time.",
            icon=self.dialog_icon(), checkbox=self._weekly_checkbox(config.get("check_updates", True))).show()
        config = load_config()
        if self.weekly is not None and choice is not None:
            config["check_updates"] = bool(self.weekly.get())
        if choice == "skip":
            config["skipped_version"] = latest["version"]
        save_config(config)
        if choice == "open":
            webbrowser.open(RELEASES_PAGE)

    def restart(self, script):
        """Close the window and run `script` (the updated program) in place of this process,
        with the files that are open now."""
        args = [sys.executable, script]
        if self.model is not None:
            args.append(self.model.fsp_path)
            if self.model.h5_path:
                args.append(self.model.h5_path)
        self.root.destroy()
        os.execv(sys.executable, args)

    def show_about(self):
        installed = installed_copy()
        where = (f"Installed version: {installed['version']}" if installed else "Not installed on this computer")
        choice = self.message(
            f"About {APP_NAME}",
            f"{APP_NAME} {APP_VERSION}\n{APP_DESCRIPTION}\n\n"
            "Views Ansys Lumerical FDTD project files (.fsp) and their monitor data (.h5) without a "
            "Lumerical license, and saves the geometry as GDS or as a gdsfactory script.\n\n"
            f"{where}\n{APP_WEBSITE}", [("ok", "OK"), ("github", "Open GitHub Page")])
        if choice == "github":
            webbrowser.open(APP_WEBSITE)

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
            with self.busy(f"Saving geometry to {os.path.basename(path)}"):
                fsp_export.save(self.model.structures, path, self.model.fsp_path)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Error Saving Geometry", f"Could not save {path}:\n\n{exc}")
            return None
        n_layers = len(fsp_export.layer_table(shapes))
        text = f"✓ Saved geometry: {os.path.basename(path)} ({len(shapes)} shapes on {n_layers} GDS layers"
        if kind == "py":
            n_poly = sum(fsp_export.decompose(sh["points"]) is None for sh in shapes)
            text += ", all from rectangles and tapers" if not n_poly else f", {n_poly} as polygons"
        self.set_status(text + ")", "ok")
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
        self.set_status(f"✓ Copied to clipboard: {self.last_exported_desc} ({n_lines} lines)", "ok")


def main(argv=None):
    """Start the program. --install, --install-systemwide, --uninstall and --update work without a window."""
    import argparse
    parser = argparse.ArgumentParser(prog=os.path.basename(THIS_SCRIPT),
                                     description=f"{APP_NAME} {APP_VERSION} - {APP_DESCRIPTION}.")
    parser.add_argument("fsp", nargs="?", help="Lumerical FDTD project file (.fsp) to open at once")
    parser.add_argument("h5", nargs="?", help="its monitor field file (.h5); by default the .h5 of the same name")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--install", action="store_true",
                        help="install the application for the current user, then exit")
    action.add_argument("--install-systemwide", action="store_true",
                        help="install the application for all users (e.g. with sudo), then exit")
    action.add_argument("--uninstall", action="store_true", help="remove the installed application, then exit")
    action.add_argument("--update", action="store_true",
                        help="install a newer version from GitHub over the installed one, then exit")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VERSION}")
    parser.add_argument("--self-test", metavar="REPORT", help=argparse.SUPPRESS)  # used by build_windows.py
    if FROZEN:
        _log_to_file()
    args, _ = parser.parse_known_args(argv)
    if args.self_test:
        return self_test(args.self_test, os.path.abspath(args.fsp) if args.fsp else None)  # macOS may add an argument such as -psn_0_123 from Finder
    if args.install or args.install_systemwide or args.uninstall or args.update:
        if args.update:
            ok, message = update_installed_app()
        elif args.uninstall:
            ok, message = uninstall_app()
        else:
            ok, message = install_app(args.install_systemwide)
        print(message)
        return 0 if ok else 1
    # File names are relative to the folder the program was started from
    fsp_arg = os.path.abspath(args.fsp) if args.fsp and not args.fsp.startswith("-psn") else None
    h5_arg = os.path.abspath(args.h5) if args.h5 else None
    try:  # started from Launchpad or Finder the current folder is '/': files would go there
        if os.getcwd() == os.path.abspath(os.sep) or not os.access(os.getcwd(), os.W_OK):
            os.chdir(os.path.expanduser("~"))
    except OSError:
        pass
    set_macos_app_name(APP_NAME)
    root = tk.Tk(className=APP_CLASS)
    app = FSPGuiApp(root, initial_fsp=fsp_arg, initial_h5=h5_arg)
    root.after(100, lambda: bring_to_front(root))
    root.after(600, app.startup_check)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
