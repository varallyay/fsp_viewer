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
import platform as pyplatform
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
import zipfile
from tkinter import filedialog, messagebox, ttk
import tkinter.font as tkfont

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
APP_VERSION = "0.2.9"           # See the version history in README.md
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
UPDATE_BRANCH = "HEAD"          # Python copies update from this branch (HEAD: the main branch); applications from releases

__version__ = APP_VERSION

Z0 = 376.730313668  # impedance of free space (ohm)

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

# ======================================================================
# Sizes on the screen: fonts, gaps and margins
# ======================================================================
# The sizes in the program are pixels of a macOS screen, where a point of a font is one pixel.
# Windows and Linux count more pixels per point (96 per inch instead of 72, and more on a scaled
# display), so there the sizes are multiplied by UI_SCALE, and the fonts are given in pixels: text
# and the gaps around it keep the proportions they have on the Mac. The fonts are named Tk fonts,
# so a new font or font size (File > Settings) changes the text in the open windows at once.
FONT_SIZE_DEFAULT = 10      # size of the normal text (pt); the settings can change it, all text follows
UI_SCALE_CHOICES = (100, 125, 150, 175, 200)   # interface scales (%) offered besides "Automatic"
LAYOUT_DEFAULTS = {"split_plot": 0.55,     # width of the views (and the parameters) : the whole width
                   "split_editor": 0.75}   # height of the four views : the left side
ALL_OBJECTS = "All objects"  # the choice of the Object selector for the analyses of the whole model
UI_SCALE = 1.0              # set by init_ui_scale() when the main window opens
UI = {"font": FONT_UI, "mono": FONT_MONO, "size": FONT_SIZE_DEFAULT}
_FONTS = {}                 # the named fonts made by ui_font(): (size, weight, mono) -> tkinter.font.Font


def dpx(n):
    """A size in pixels of the Mac (gap, padding, window size) in pixels of this screen."""
    return int(round(n * UI_SCALE))


def text_scale():
    return UI_SCALE * UI["size"] / FONT_SIZE_DEFAULT


def tx(n):
    """A size that goes with the text (a margin for tick labels, a line of labels) in pixels of this screen."""
    return int(round(n * text_scale()))


def ui_font(size, bold=False, mono=False):
    """The named font of the program for text of `size` points at the standard font size."""
    weight = "bold" if bold else "normal"
    key = (size, weight, mono)
    font = _FONTS.get(key)
    if font is None:
        font = _FONTS[key] = tkfont.Font(family=UI["mono" if mono else "font"], size=-max(6, tx(size)),
                                         weight=weight)
    return font


def mono_font(size, bold=False):
    return ui_font(size, bold, mono=True)


def set_fonts(family=None, mono=None, size=None):
    """Change the fonts or the font size; every widget and canvas text using them follows."""
    if family:
        UI["font"] = family
    if mono:
        UI["mono"] = mono
    if size:
        UI["size"] = size
    for (sz, _weight, is_mono), font in _FONTS.items():
        font.configure(family=UI["mono" if is_mono else "font"], size=-max(6, tx(sz)))


def system_ui_scale(root):
    """How many times more pixels this screen has per point than the Mac (1 on macOS, which scales itself)."""
    if sys.platform == "darwin":
        return 1.0
    try:
        return min(4.0, max(1.0, round(root.winfo_fpixels("1i") / 96, 2)))
    except tk.TclError:
        return 1.0


def init_ui_scale(root, setting="auto"):
    """Set UI_SCALE from the setting: "auto" (from the screen) or a percentage."""
    global UI_SCALE
    try:
        UI_SCALE = min(4.0, max(0.5, float(setting) / 100))
    except (TypeError, ValueError):
        UI_SCALE = system_ui_scale(root)
    return UI_SCALE


def enable_windows_dpi_awareness():
    """Tell Windows that the program scales itself. Otherwise a scaled display (125 %, 150 %) gets a
    stretched, blurred picture of the window. Call it before the first Tk window opens."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # system DPI aware (Windows 8.1 and newer)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()      # Windows Vista, 7
    except Exception:
        pass


class FigureCanvas(FigureCanvasTkAgg):
    """Matplotlib canvas that draws the plots at the scale of the rest of the window (matplotlib
    by itself would follow the system's DPI on Windows and Linux, and ignore the interface scale)."""

    def _update_device_pixel_ratio(self, event=None):
        try:
            if self._set_device_pixel_ratio(UI_SCALE):
                w, h = self.get_width_height(physical=True)
                self._tkcanvas.configure(width=w, height=h)
        except AttributeError:  # another matplotlib version: its own way
            super()._update_device_pixel_ratio(event)

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
        for cache in ("_h5_cache", "_h5_group_x", "_power_spectra", "_power_ref", "_expansions",
                      "_h_scale"):  # results of the previous file
            self.__dict__.pop(cache, None)
        if hasattr(self, "ports"):  # an .h5 opened later: which monitors have a field now
            self.field_monitors = self._extract_field_monitors()

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
    # Every port and monitor keeps its own list of frequencies: Lumerical spaces those of the ports
    # evenly in frequency, and those of the monitors (by default) evenly in wavelength, in the
    # other order. A field is therefore looked up by frequency, never by its position in the list.
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

    def plane_object(self, name):
        """The .fsp object of a port (its DFT monitor) or field monitor."""
        for pl in list(self.ports) + list(self.field_monitors):
            if pl["name"] == name:
                return pl.get("dft_obj") if "dft_obj" in pl else pl.get("obj")
        return None

    @staticmethod
    def object_freqs(ob):
        """The frequencies (Hz) an object recorded at, from its own 'f' result; None if it has none."""
        if ob is None or "f" not in ob.results:
            return None
        f = np.ravel(ob.results["f"]).real
        return f if f.size else None

    def plane_freqs(self, name):
        f = self.object_freqs(self.plane_object(name))
        return f if f is not None else self.get_frequencies_and_wavelengths()[0]

    def freq_index(self, f_own, k):
        """Index in the list f_own of the frequency nearest to the k-th frequency of the model."""
        freqs = self.get_frequencies_and_wavelengths()[0]
        return int(np.argmin(np.abs(np.asarray(f_own) - freqs[min(k, len(freqs) - 1)])))

    def plane_k(self, name, k):
        return self.freq_index(self.plane_freqs(name), k)

    def plane_wavelength(self, name, k):
        """The wavelength (nm) of the plane's own frequency that is used for the model's k-th one."""
        return C0 / self.plane_freqs(name)[self.plane_k(name, k)] * 1e9

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

    def plane_has(self, name, comp):
        kind, src = self.plane_source(name)
        return comp in (self.h5[src] if kind == "h5" else src.results)

    def _plane_field_own(self, name, comp, kk):
        """Complex field component of a plane at index kk of its own frequency list, as (Nz, Ny)."""
        kind, src = self.plane_source(name)
        if kind == "h5":
            return self.h5_field(src, comp, kk)[:, :, 0]
        return np.asarray(src.results.take_last(comp, kk))[0].T  # (1, Ny, Nz)

    def plane_field(self, name, comp, k):
        """Complex field component `comp` (Ex .. Hz) of a port / field monitor at the model's
        frequency k (the nearest of its own), as an (Nz, Ny) array."""
        return self._plane_field_own(name, comp, self.plane_k(name, k))

    def _plane_power_own(self, name, kk):
        _, y, z = self.plane_grid(name)
        E = {c: self._plane_field_own(name, c, kk) for c in ("Ey", "Ez", "Hy", "Hz")}
        sx = np.real(E["Ey"] * np.conj(E["Hz"]) - E["Ez"] * np.conj(E["Hy"]))
        return float(TRAPZ(TRAPZ(sx, y, axis=1), z, axis=0))

    def plane_power(self, name, k):
        """Power through a port / field monitor plane at the model's frequency k: the 'power' result
        Lumerical stored with the monitor, else the Poynting flux of the stored fields; or None.
        (In the units of the stored fields: only ratios of powers are meaningful.)"""
        kind, src = self.plane_source(name)
        kk = self.plane_k(name, k)
        if kind == "fsp" and "power" in src.results:
            return float(np.ravel(src.results["power"])[kk].real)
        if not all(self.plane_has(name, c) for c in ("Ey", "Ez", "Hy", "Hz")):
            return None
        return self._plane_power_own(name, kk)

    SPECTRUM_POINTS = 126  # frequencies evaluated when a spectrum is computed from the fields

    def _own_indices(self, name):
        """Indices of the plane's own frequencies used for a spectrum (at most SPECTRUM_POINTS; the
        rest is interpolated, the spectra being smooth)."""
        n = len(self.plane_freqs(name))
        return np.arange(n) if n <= self.SPECTRUM_POINTS else np.unique(
            np.linspace(0, n - 1, self.SPECTRUM_POINTS).round().astype(int))

    def _on_model_freqs(self, f_own, values):
        """Values given at the frequencies f_own, interpolated to every frequency of the model."""
        freqs = self.get_frequencies_and_wavelengths()[0]
        f_own, values = np.asarray(f_own, float), np.asarray(values, float)
        if f_own.size == 1:
            return np.full(len(freqs), values[0])
        order = np.argsort(f_own)
        return np.interp(freqs, f_own[order], values[order])

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
        file are normalised to the source spectrum, the stored P0 is not), the power through the
        first plane at or after the source (the injected power, less the tiny reflected part).
        None if neither is there."""
        if getattr(self, "_power_ref", False) is not False:
            return self._power_ref
        self._power_ref = None
        planes = self.field_planes()
        src = self.source_port()
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

    def source_port(self):
        return next((pt for pt in self.ports if pt.get("is_source") and pt["enabled"]), None)

    def plane_power_spectrum(self, name):
        """Power through a plane at every frequency of the model: the stored 'power' result, else
        the Poynting flux of the recorded fields (needs E and H). None if neither is there."""
        cache = self.__dict__.setdefault("_power_spectra", {})
        if name in cache:
            return cache[name]
        out = None
        src = self.plane_source(name)
        f_own = self.plane_freqs(name)
        if src and src[0] == "fsp" and "power" in src[1].results:
            pw = np.ravel(src[1].results["power"]).real
            if len(pw) == len(f_own):
                out = self._on_model_freqs(f_own, pw)
        elif src and all(self.plane_has(name, c) for c in ("Ey", "Ez", "Hy", "Hz")):
            ks = self._own_indices(name)
            out = self._on_model_freqs(f_own[ks], [self._plane_power_own(name, int(kk)) for kk in ks])
        cache[name] = out
        return out

    def plane_power_stored(self, name):
        """Whether the power of the plane is a result stored in the .fsp (else computed from E and H)."""
        src = self.plane_source(name)
        return bool(src and src[0] == "fsp" and "power" in src[1].results)

    def plane_transmission(self, name):
        """T of a plane at every frequency: P / P_source (|S_i1|^2 when all of it is in one mode), or
        P / P(first plane) when the fields have no absolute scale (see power_reference); or None."""
        pw, ref = self.plane_power_spectrum(name), self.power_reference()
        if pw is None or ref is None:
            return None
        return pw / np.where(ref[0] == 0, np.nan, ref[0])

    # ---------- expansion of the recorded field on the stored modes (as Lumerical's mode expansion) ----------
    def h_scale(self, name):
        """How many times larger the recorded H of a plane is than in SI units. Lumerical writes the H
        of its .h5 field files multiplied by Z0 (in the units of E). Told apart by the ratio of E to H:
        in SI it is Z0/n (about 100 to 377 ohm), multiplied by Z0 it is 1/n (0.25 to 1)."""
        cache = self.__dict__.setdefault("_h_scale", {})
        if name not in cache:
            kk = self._own_indices(name)[len(self._own_indices(name)) // 2]
            e = sum(np.sum(np.abs(self._plane_field_own(name, c, kk)) ** 2) for c in ("Ey", "Ez"))
            h = sum(np.sum(np.abs(self._plane_field_own(name, c, kk)) ** 2) for c in ("Hy", "Hz"))
            ratio = np.sqrt(e / h) if h else 1.0
            cache[name] = Z0 if ratio < 10 else 1.0
        return cache[name]

    def plane_mode_owners(self, name, tol=0.05):
        """The ports and mode expansion monitors whose stored modes the field of a plane can be
        expanded on: the port itself, and those at the same x (within tol um)."""
        x = self.plane_grid(name)[0]
        owners = [name] if any(pt["name"] == name and self.stored_modes(name) for pt in self.ports) else []
        for o in [pt["name"] for pt in self.ports] + [mm["name"] for mm in self.mode_monitors]:
            if o != name and o not in owners and self.stored_modes(o) and abs(self._owner_x(o) - x) <= tol:
                owners.append(o)
        return owners

    def _owner_x(self, owner):
        for pl in list(self.ports) + list(self.mode_monitors):
            if pl["name"] == owner:
                return pl["x"]
        return np.inf

    def plane_expansion(self, name, owner=None, mode=0):
        """Expansion of the field recorded in a plane on a stored mode (of the port itself or of a
        port / mode expansion monitor at the same x), as Lumerical's 'expansion for port monitor':
        dict with P_forward and P_backward, the power carried by the mode towards +x and towards -x,
        and T_forward, T_backward, those divided by the power reference (see power_reference), at
        every frequency of the model; mode_number, neff, owner. None if not possible."""
        owner = owner or name
        key = (name, owner, mode)
        cache = self.__dict__.setdefault("_expansions", {})
        if key in cache:
            return cache[key]
        cache[key] = None
        if RegularGridInterpolator is None or not self.plane_source(name) or \
                not all(self.plane_has(name, c) for c in ("Ey", "Ez", "Hy", "Hz")):
            return None
        try:
            ym, zm, M, neff, _ = self.stored_mode(owner, mode)
        except Exception:
            return None
        _, y, z = self.plane_grid(name)

        def integ(F):
            return TRAPZ(TRAPZ(F, y, axis=1), z, axis=0)

        # the mode on the grid of the recorded field (zero outside the mode's own grid), in SI units
        Z, Y = np.meshgrid(z, y, indexing="ij")
        pts = np.stack([Z, Y], -1)
        Mi = {}
        for c in ("Ey", "Ez", "Hy", "Hz"):
            re = RegularGridInterpolator((zm, ym), M[c].real, bounds_error=False, fill_value=0.0)(pts)
            im = RegularGridInterpolator((zm, ym), M[c].imag, bounds_error=False, fill_value=0.0)(pts)
            Mi[c] = re + 1j * im
        mode_scale = self.mode_h_scale(owner, mode)
        Mi["Hy"], Mi["Hz"] = Mi["Hy"] / mode_scale, Mi["Hz"] / mode_scale
        mm = integ(Mi["Ey"] * np.conj(Mi["Hz"]) - Mi["Ez"] * np.conj(Mi["Hy"]))
        if not mm:
            return None
        if mm.real < 0:  # a mode stored for the -x direction: its +x twin has the transverse H reversed
            Mi["Hy"], Mi["Hz"], mm = -Mi["Hy"], -Mi["Hz"], -mm
        n_mode = 0.5 * mm.real  # power of the mode
        s = self.h_scale(name)
        f_own = self.plane_freqs(name)
        ks = self._own_indices(name)
        fwd, bwd = [], []
        for kk in ks:
            E = {c: self._plane_field_own(name, c, int(kk)) for c in ("Ey", "Ez", "Hy", "Hz")}
            E["Hy"], E["Hz"] = E["Hy"] / s, E["Hz"] / s
            A = integ(E["Ey"] * np.conj(Mi["Hz"]) - E["Ez"] * np.conj(Mi["Hy"]))   # (a + b) mm
            B = integ(np.conj(Mi["Ey"]) * E["Hz"] - np.conj(Mi["Ez"]) * E["Hy"])   # (a - b) mm*
            a = 0.5 * (A / mm + B / np.conj(mm))
            b = 0.5 * (A / mm - B / np.conj(mm))
            # in the units of plane_power_spectrum (Re of E x H* of the stored fields)
            fwd.append(abs(a) ** 2 * n_mode * 2 * s)
            bwd.append(abs(b) ** 2 * n_mode * 2 * s)
        P_f, P_b = self._on_model_freqs(f_own[ks], fwd), self._on_model_freqs(f_own[ks], bwd)
        ref = self.power_reference()
        refs = np.where(ref[0] == 0, np.nan, ref[0]) if ref is not None else None
        numbers = self.stored_modes(owner)
        cache[key] = {
            "P_forward": P_f, "P_backward": P_b,
            "T_forward": P_f / refs if refs is not None else None,
            "T_backward": P_b / refs if refs is not None else None,
            "mode_number": numbers[mode][0], "neff": neff, "owner": owner,
        }
        return cache[key]

    def mode_h_scale(self, owner, mode=0):
        """As h_scale, for a stored mode (SI units in the projects seen so far)."""
        _, _, M, _, _ = self.stored_mode(owner, mode)
        e = sum(np.sum(np.abs(M[c]) ** 2) for c in ("Ey", "Ez"))
        h = sum(np.sum(np.abs(M[c]) ** 2) for c in ("Hy", "Hz"))
        return Z0 if h and np.sqrt(e / h) < 10 else 1.0

    def port_outgoing(self, port_name, mode=0):
        """Power (spectrum, model frequencies) leaving the device through a port in one of its stored
        modes: towards -x at a port that injects towards +x, and the other way; and the incident
        power (towards the device). (outgoing, incident, expansion) or None."""
        pt = next((p for p in self.ports if p["name"] == port_name), None)
        ex = self.plane_expansion(port_name, port_name, mode) if pt else None
        if ex is None:
            return None
        if pt["direction"] == "+":
            return ex["P_backward"], ex["P_forward"], ex
        return ex["P_forward"], ex["P_backward"], ex

    # ---------- other monitors: planes along the device (x-y, x-z), from the .h5 ----------
    def other_monitors(self):
        """2D monitors with an .h5 field whose plane is not normal to x (e.g. xz, xy_si): list of
        dicts with name, group, the axes of the plane (u, v) and the normal axis."""
        if self.h5 is None:
            return []
        out = []
        for ob in self.fsp.objects:
            grp = ob.props.get("HDF5DataGroup")
            if not grp or grp not in self.h5 or not ob.props.get("enabled", 1) or "monitorShape" not in ob.props:
                continue
            g = self.h5[grp]
            try:
                sizes = {c: int(np.size(g[c])) for c in "xyz"}
            except Exception:
                continue
            flat = [c for c in "xyz" if sizes[c] == 1]
            if len(flat) != 1 or flat[0] == "x":
                continue
            u, v = [c for c in "xyz" if c != flat[0]]
            out.append({"name": ob.name.split("::")[-1], "obj": ob, "group": grp, "normal": flat[0], "u": u, "v": v})
        return out

    def monitor_field(self, mon, comp, k):
        """Field component of an other_monitors() plane at the model's frequency k (the nearest of
        its own), as (N_v, N_u); and the wavelength (nm) used."""
        f_own = self.object_freqs(mon["obj"])
        kk = self.freq_index(f_own, k) if f_own is not None else 0
        a = self.h5_field(mon["group"], comp, kk)  # (z, y, x)
        a = {"y": a[:, 0, :], "z": a[0, :, :]}[mon["normal"]]  # (z, x) or (y, x)
        lam = C0 / f_own[kk] * 1e9 if f_own is not None else None
        return a, lam

    def monitor_grid(self, mon):
        g = self.h5[mon["group"]]
        u, v = np.ravel(g[mon["u"]][()]).astype(float), np.ravel(g[mon["v"]][()]).astype(float)
        scale = 1e6 if max(np.max(np.abs(u)), np.max(np.abs(v))) < 1e-2 else 1.0
        return u * scale, v * scale

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

    def _mode_object(self, name):
        """The object holding the stored modes of a port or mode expansion monitor, or None."""
        for pt in self.ports:
            if pt["name"] == name:
                return pt["mode_exp_obj"]
        for mm in self.mode_monitors:
            if mm["name"] == name:
                return mm["obj"]
        try:
            return self.fsp.get(f"::{name} Mode Expansion Monitor")
        except Exception:
            return None

    def _mode_layout(self, p):
        """[(index of E, index of H)] of the stored modes, and the index of the refractive index, from
        the names of the stored data: E1, H1, E2, H2, ..., index (or E, H, index)."""
        e, h, idx_n = {}, {}, None
        for i in range(int(p.get("dataCount", 3))):
            nm = str(p.get(f"dataEHnName{i}", ""))
            if nm == "index":
                idx_n = i
            elif nm[:1] in ("E", "H"):
                (e if nm[0] == "E" else h).setdefault(nm[1:] or "1", i)
        pairs = [(e[k], h[k]) for k in e if k in h]
        return pairs or [(0, 1)], (2 if idx_n is None else idx_n)

    def stored_modes(self, name):
        """The modes stored with a port or mode expansion monitor: [(mode number, n_eff)], in the
        stored order; [] if it has none (not calculated, or not saved)."""
        ob = self._mode_object(name)
        if ob is None or "yMatrix" not in ob.props or "freqEHnx0" not in ob.props:
            return []
        p = ob.props
        pairs, _ = self._mode_layout(p)
        numbers = np.ravel(p.get("modeNumbers", np.arange(1, len(pairs) + 1))).astype(int)
        neffs = np.ravel(p.get("neff", np.array([])))
        return [(int(numbers[i]) if i < numbers.size else i + 1, float(neffs[i].real) if i < neffs.size else None)
                for i in range(len(pairs))]

    def stored_mode(self, port_or_mon_name, mode=0):
        """
        The cross-section refractive index n(z, y) and the fields Ex..Hz(z, y) of a stored mode
        (mode: its place among the stored modes, 0 = the first) of a port or mode expansion
        monitor, from `::<port> Mode Expansion Monitor` or the monitor itself.
        Returns (y, z, M, neff, mode wavelength in nm).
        """
        target_ob = self._mode_object(port_or_mon_name)
        p = target_ob.props if target_ob is not None else {}
        if "yMatrix" not in p or "freqEHnx0" not in p:
            raise RuntimeError(f"'{port_or_mon_name}' has no stored mode: its mode was not calculated or not saved "
                               "in this project (a project saved before running, or a disabled port / monitor).")
        y = p["yMatrix"].ravel() * 1e6
        z = p["zMatrix"].ravel() * 1e6
        pairs, idx_n = self._mode_layout(p)
        idx_e, idx_h = pairs[min(mode, len(pairs) - 1)]
        g = lambda c, i: p[f"freqEHn{c}{i}"][0, :, :, 0].T  # shape (Nz, Ny)
        M = {f"E{c}": g(c, idx_e) for c in "xyz"} | {f"H{c}": g(c, idx_h) for c in "xyz"}
        M["n"] = np.abs(g("x", idx_n).real)
        modes = self.stored_modes(port_or_mon_name)
        neff = modes[min(mode, len(modes) - 1)][1] if modes else None
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
            font=ui_font(size, bold),
            padx=dpx(padx),
            pady=dpx(pady),
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
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + dpx(6)
        self._tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(
            tw,
            text=self.text,
            justify=tk.LEFT,
            bg="#262c36",
            fg=THEME["text"],
            font=ui_font(10),
            padx=dpx(8),
            pady=dpx(5),
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
        padding=dpx(4),
    )
    style.map(
        "Dark.TCombobox",
        fieldbackground=[("readonly", THEME["entry_bg"])],
        foreground=[("readonly", THEME["text"])],
        bordercolor=[("focus", THEME["accent"])],
        background=[("active", THEME["btn_hover"])],
    )
    style.configure(
        "Dark.TSpinbox",
        fieldbackground=THEME["entry_bg"],
        background=THEME["btn_bg"],
        foreground=THEME["text"],
        arrowcolor=THEME["text_muted"],
        bordercolor=THEME["border"],
        lightcolor=THEME["entry_bg"],
        darkcolor=THEME["entry_bg"],
        insertcolor=THEME["text"],
        padding=dpx(3),
    )
    style.map("Dark.TSpinbox", bordercolor=[("focus", THEME["accent"])], background=[("active", THEME["btn_hover"])])
    root.option_add("*TCombobox*Listbox.background", THEME["bar_bg"])
    root.option_add("*TCombobox*Listbox.foreground", THEME["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", THEME["accent"])
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
    root.option_add("*TCombobox*Listbox.font", str(ui_font(11)))


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
            font=ui_font(11, bold=True),
            bg=THEME["panel_bg"],
            fg=THEME["text"],
            anchor="w",
        ).pack(side=tk.LEFT, padx=(10, 6), pady=4)

        self.coord_lbl = tk.Label(
            header,
            text="",
            anchor="w",
            font=mono_font(9),
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

        # margins around the plot (Mac pixels at the standard font size): tick labels and axis titles
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
        x0 = tx(self.margin_left)
        y0 = tx(self.margin_top)
        x1 = max(x0 + 20, w - dpx(self.margin_right))
        y1 = max(y0 + 20, h - tx(self.margin_bottom))
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
        su = nice_tick_step(self.u_range[1] - self.u_range[0], x1 - x0, tx(68))
        sv = nice_tick_step(self.v_range[1] - self.v_range[0], y1 - y0, tx(36))
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
        tick_font = ui_font(8)

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
                        c.create_text(px, y1 + tx(10), text=label, font=tick_font, fill=T["text_muted"])
                    else:
                        c.create_text(x0 - dpx(5), py, text=label, font=tick_font, fill=T["text_muted"], anchor="e")

        # Axis titles
        c.create_text(
            (x0 + x1) / 2, y1 + tx(22), text=f"{self.u_label} (μm)", font=ui_font(9, bold=True), fill=T["text_muted"]
        )
        c.create_text(
            tx(15), (y0 + y1) / 2, text=f"{self.v_label}\n(μm)", font=ui_font(9, bold=True), fill=T["text_muted"],
            justify=tk.CENTER,
        )

        if self.model is None:
            c.create_text(
                (x0 + x1) / 2,
                (y0 + y1) / 2,
                text="Open an .fsp file to visualize the model",
                font=ui_font(11),
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
            if vy1 - vy0 < tx(12) or vx1 - vx0 < tx(40):
                continue
            labelled.add(st["layer_name"])
            if self.view_kind == "xz":
                text = st["layer_name"]
                fill = T["text_muted"] if st["is_background"] else "#ffffff"
            else:
                text = st["layer_name"] + (f" (n={st['n']:.3f})" if st["n"] else "")
                fill = T["text_muted"]
            c.create_text(vx0 + dpx(6), 0.5 * (vy0 + vy1), text=text, font=ui_font(8), fill=fill, anchor="w")

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
                    min(x1 - dpx(4), max(x0 + tx(30), rx1 - dpx(4))),
                    max(y0 + tx(12), min(y1 - dpx(3), ry1 - dpx(3))),
                    text="FDTD",
                    font=ui_font(8, bold=True),
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
            c.create_text(x0 + dpx(6), y1 - dpx(6), text=text, font=ui_font(8, bold=True),
                          fill=T["monitor_cyan_bright"] if sel else T["monitor_cyan"], anchor="sw")

        # 4. Draw PORTS (and mode expansion monitors) in YELLOW (#ffd700 / #ffee55)
        for pt in self.model.port_planes():
            is_sel = (pt["name"] == self.selected_port) or (self.selected_port == ALL_OBJECTS)
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
                        c.create_oval(px - dpx(3), py - dpx(3), px + dpx(3), py + dpx(3), fill=p_color, outline="#000000")
                if self.view_kind == "xy" and pt["direction"]:  # direction arrow at port center
                    _, pyc = self.uv_to_px(pt["x"], pt["y"])
                    if y0 <= pyc <= y1:
                        arr_dx = dpx(10) if pt["direction"] == "+" else -dpx(10)
                        c.create_line(px, pyc, px + arr_dx, pyc, fill=p_color, width=2, arrow=tk.LAST)
                if seg:
                    self._port_hit_boxes.append((pt["name"], px - 6, min(seg[1], seg[3]), px + 6, max(seg[1], seg[3])))

            else:  # yz cross-section
                self._draw_box(pt["ymin"], pt["ymax"], pt["zmin"], pt["zmax"], "", p_color, width=lw,
                               dash=() if is_sel else (4, 2))
                py1, pz0 = self.uv_to_px(pt["ymax"], pt["zmax"])
                c.create_text(
                    min(x1, py1) - dpx(4),
                    max(y0, pz0) + tx(10) + tx(12) * pt["index"],  # one line per port: they share this frame
                    text=f"{pt['name']} (x={pt['x']:g} μm)" + ("" if pt["enabled"] else ", disabled"),
                    font=ui_font(8, bold=True),
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
                if px - last_px < tx(34):
                    row_idx = (row_idx + 1) % 3
                else:
                    row_idx = 0
                    last_px = px
                lbl_y = y0 + tx(10) + row_idx * tx(13)
                is_sel = pt["name"] == self.selected_port
                short_lbl = pt["name"].replace("output_port", "P").replace("input_port", "In")
                if not pt["enabled"]:
                    short_lbl += " (off)"
                tid = c.create_text(
                    px,
                    lbl_y,
                    text=short_lbl,
                    font=mono_font(8, bold=True),
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
                if px - last_px < tx(52):
                    row_idx = (row_idx + 1) % 3
                else:
                    row_idx, last_px = 0, px
                is_sel = mm["name"] == self.selected_port
                label = mm["name"][3:] if mm["name"].startswith(("yz_", "yz ")) else mm["name"]
                tid = c.create_text(px, y1 - tx(9) - row_idx * tx(13), text=label, font=mono_font(8, bold=True),
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
            font=ui_font(11, bold=True),
            bg=THEME["panel_bg"],
            fg=THEME["text"],
            anchor="w",
        ).pack(side=tk.LEFT, padx=(10, 6), pady=4)

        tk.Label(
            header,
            text="drag to rotate · scroll to zoom",
            font=ui_font(9),
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
                cx, cy - tx(8), text="3D Viewport", font=ui_font(13, bold=True), fill=THEME["text_muted"]
            )
            c.create_text(
                cx, cy + tx(14), text="Open an .fsp file to inspect 3D geometry & ports",
                font=ui_font(10), fill=THEME["text_dim"],
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
            self._badge(top_x, top_y - tx(8), short, color, font=mono_font(8, bold=True))

        # Dimension badges
        xmin, xmax, ymin, ymax, zmin, zmax = self.model.fdtd_box or self.bbox_3d
        p_x0 = self._project_point(xmin, ymin, zmin, cx, cy, scale)
        p_x1 = self._project_point(xmax, ymin, zmin, cx, cy, scale)
        p_y1 = self._project_point(xmax, ymax, zmin, cx, cy, scale)
        p_z1 = self._project_point(xmax, ymin, zmax, cx, cy, scale)
        self._badge(0.5 * (p_x0[0] + p_x1[0]), 0.5 * (p_x0[1] + p_x1[1]) + tx(14), f"Δx={xmax - xmin:g} μm", "#74c0fc")
        self._badge(0.5 * (p_x1[0] + p_y1[0]) + tx(24), 0.5 * (p_x1[1] + p_y1[1]) + tx(8), f"Δy={ymax - ymin:g} μm", "#63e6be")
        self._badge(0.5 * (p_x1[0] + p_z1[0]) + tx(30), 0.5 * (p_x1[1] + p_z1[1]), f"Δz={zmax - zmin:g} μm", "#ffa8a8")

        # Legend pill in top-left corner
        tid = c.create_text(
            dpx(14), dpx(12),
            text=f"■ Ports ({len(self.port_faces_3d) - sum(pf.get('kind') == 'monitor' for pf in self.port_faces_3d)})"
                 f" in Yellow   ■ FDTD Box",
            font=ui_font(9, bold=True),
            fill=THEME["port_yellow"],
            anchor="nw",
        )
        n_mon = sum(pf.get("kind") == "monitor" for pf in self.port_faces_3d)
        if n_mon:
            c.create_text(c.bbox(tid)[2] + tx(12), dpx(12), text=f"■ Field monitors ({n_mon})", font=ui_font(9, bold=True),
                          fill=THEME["monitor_cyan"], anchor="nw")

        self._draw_axes_triad(w, h)

    def _badge(self, x, y, text, color, font=None):
        tid = self.canvas.create_text(x, y, text=text, font=font or ui_font(8, bold=True), fill=color)
        bx0, by0, bx1, by1 = self.canvas.bbox(tid)
        bg = self.canvas.create_rectangle(bx0 - dpx(4), by0 - 1, bx1 + dpx(4), by1 + 1, fill=THEME["app_bg"], outline="")
        self.canvas.tag_lower(bg, tid)

    def _draw_axes_triad(self, w, h):
        ox, oy = dpx(42), h - dpx(34)
        axis_len = dpx(24)
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
                font=ui_font(9, bold=True),
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
            font=ui_font(11, bold=True),
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
            font=mono_font(10),
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
            header, text="Filter:", font=ui_font(9), bg=THEME["panel_bg"], fg=THEME["text_muted"]
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
            font=mono_font(10),
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
            font=mono_font(10),
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
        self.text.tag_configure("section", foreground="#6aa1ff", font=mono_font(10, bold=True))
        self.text.tag_configure("port_line", foreground=THEME["port_yellow"], font=mono_font(10, bold=True))
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
    try:  # a crash (segmentation fault) prints where it happened
        import faulthandler
        faulthandler.enable(all_threads=True)
    except Exception:
        pass  # no stderr (a windowed program on Windows, macOS)

    def write_report(last=None):
        with open(report, "w", encoding="utf-8") as f:
            f.write("\n".join(lines + ([last] if last else [])) + "\n")

    def check(name, fn):
        try:
            detail = fn()
            lines.append(f"ok    {name}" + (f": {detail}" if detail else ""))
        except Exception as err:
            failed.append(name)
            lines.append(f"FAIL  {name}: {type(err).__name__}: {err}")
        write_report("(running: a crash after this line ends the report here)")

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
        enable_windows_dpi_awareness()
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
        check("updates", lambda: (f"from the GitHub releases ({packaged_kind() or 'not from a release package'})"
                                  if FROZEN else "from the main branch on GitHub"))
        if fsp:
            check("open file", lambda: f"{len(app.model.structures)} structures, {len(app.model.ports)} ports"
                                       if app.model is not None else need(None, "the model"))
            # every result of every object (the analyses of the whole model once, with All objects)
            for obj in app.cmb_port["values"]:
                app.port_var.set(obj)
                app._update_result_choices(prefer_object=True)
                for option in app.cmb_result["values"]:
                    if option == app.RESULT_SEPARATOR:
                        break
                    def plot(option=option):
                        app.result_var.set(option)
                        ok = app.plot_selected()
                        root.update()
                        if not ok:
                            texts = [t.get_text() for ax in app.fig.axes for t in ax.texts]
                            raise RuntimeError(" ".join(texts).replace("\n", " ")[:200])
                        return ""
                    check(f"plot: {obj}: {option}", plot)
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
    write_report()
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
# Written by the swap script when a downloaded version could not be put in place; shown at the next start
UPDATE_FAILED_FILE = "~/.fsp_viewer_update_failed.txt"


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
    if FROZEN or install_target(False, platform) is None or running_installed_copy(platform):
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
# New versions are published in the GitHub project UPDATE_REPOSITORY in two ways:
#  - Python copies (fsp_gui.py run with Python) update from the program files (APP_FILES)
#    in the top folder of its main branch: a new fsp_gui.py there is a new version.
#  - Packaged copies (built by build_app.py: the Windows .zip, the macOS .dmg, the Linux
#    .deb and .rpm) update from the newest GitHub release, which carries them as assets.
# APP_VERSION in fsp_gui.py, or the version tag of the release, decides what is newer.
GITHUB_API = "https://api.github.com"
GITHUB_RAW = "https://raw.githubusercontent.com"
UPDATE_TIMEOUT = 15          # seconds allowed for each download
MAX_DOWNLOAD = 5_000_000     # bytes; the largest program file is about 150 kB
MAX_PACKAGE = 2_000_000_000  # bytes; a packaged release is about 100 MB


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
    """The newest Python version on GitHub, on its main branch: {'version', 'source' (the text
    of the main program), 'files' (its file names), 'base' (its folder URL)}.

    Raises UpdateError when GitHub cannot be reached or the file is not a valid program.
    """
    base = f"{GITHUB_RAW}/{UPDATE_REPOSITORY}/{urllib.parse.quote(UPDATE_BRANCH)}/"
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


# ---- packaged copies: updates from GitHub releases ---------------------------------
# build_app.py packs the program with PyInstaller for each system, and the workflow
# .github/workflows/release.yml attaches the packages to the GitHub release of a version
# tag. A packaged copy downloads the package made for it from the newest release and puts
# it in its own place:
#   zip  Windows: the folder "FSP Viewer" is swapped for the new one after the program exits
#   dmg  macOS: "FSP Viewer.app" is swapped for the one in the disk image after it exits
#   deb  Linux: the package manager installs the new package (asks for the password)
#   rpm
LINUX_PACKAGE = "fsp-viewer"           # name of the Linux packages and of their terminal command
LINUX_PACKAGE_DIR = "/opt/fsp-viewer"  # where they put the program
PACKAGE_FORMAT_FILE = "package-format"  # next to the packaged Linux program: "deb" or "rpm"
RELAUNCH_ENV = {"PYINSTALLER_RESET_ENVIRONMENT": "1"}  # a restarted packaged program starts afresh


def machine_arch():
    """The processor: 'x86_64' or 'arm64' (others as Python names them)."""
    machine = pyplatform.machine().lower()
    return {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(machine, machine)


def release_asset_name(version, kind, arch=None):
    """The file name of a package in a GitHub release (build_app.py makes them with these names)."""
    arch = arch or machine_arch()
    slug = APP_NAME.replace(" ", "-")
    if kind == "zip":
        return f"{slug}-{version}-windows.zip"
    if kind == "dmg":
        return f"{slug}-{version}-macos-{arch}.dmg"
    if kind == "deb":
        return f"{LINUX_PACKAGE}_{version}_{ {'x86_64': 'amd64'}.get(arch, arch)}.deb"
    if kind == "rpm":
        return f"{LINUX_PACKAGE}-{version}-1.{ {'arm64': 'aarch64'}.get(arch, arch)}.rpm"
    raise ValueError(f"unknown package kind {kind!r}")


def packaged_kind(platform=None):
    """How this packaged program came: 'zip' (Windows), 'dmg' (macOS), 'deb' or 'rpm' (Linux),
    or None (a Python copy, or a packaged program that was not built for a release)."""
    if not FROZEN:
        return None
    platform = platform or sys.platform
    if platform.startswith("win"):
        return "zip"
    if platform == "darwin":
        return "dmg" if mac_app_bundle() else None
    try:
        with open(os.path.join(os.path.dirname(os.path.realpath(sys.executable)), PACKAGE_FORMAT_FILE),
                  encoding="utf-8") as f:
            kind = f.read().strip()
        return kind if kind in ("deb", "rpm") else None
    except OSError:
        return None


def mac_app_bundle():
    """The .app bundle this packaged program runs from on macOS, or None."""
    app = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(sys.executable))))
    return app if app.endswith(".app") else None


def find_latest_release():
    """The newest GitHub release: {'version', 'tag', 'page' (its web page), 'assets'
    ({file name: (download URL, size)})}. Raises UpdateError if it cannot be found."""
    url = f"{GITHUB_API}/repos/{UPDATE_REPOSITORY}/releases/latest"
    try:
        release = json.loads(_download(url))
    except urllib.error.HTTPError as err:
        if err.code == 404:
            raise UpdateError(f"No release of {APP_NAME} is published on GitHub yet.") from None
        raise UpdateError(f"GitHub did not answer the question for the newest release (HTTP error {err.code}).") from None
    except OSError as err:
        raise UpdateError(_connection_problem(err)) from None
    except ValueError:
        raise UpdateError("GitHub gave an answer that could not be read; try again later.") from None
    tag = release.get("tag_name") if isinstance(release, dict) else None
    version = (tag or "").lstrip("vV")
    if not tag or _version_tuple(version) == (0,):
        raise UpdateError(f"The newest release on GitHub has no version number ({tag!r}).")
    assets = {a["name"]: (a["browser_download_url"], int(a.get("size") or 0))
              for a in release.get("assets") or [] if a.get("name") and a.get("browser_download_url")}
    return {"version": version, "tag": tag, "page": release.get("html_url") or RELEASES_PAGE, "assets": assets}


def find_update():
    """The newest version for this copy: a release for a packaged copy, the main branch for a Python copy."""
    return find_latest_release() if FROZEN else find_latest_version()


def download_file(url, path, progress=None, size=0):
    """Download url into the file path (via path + '.part'); progress(done, total) is called
    along the way (from this thread). Raises UpdateError."""
    request = urllib.request.Request(url, headers={"User-Agent": f"{APP_SLUG}/{APP_VERSION}"})
    part = path + ".part"
    try:
        with urllib.request.urlopen(request, timeout=UPDATE_TIMEOUT) as response, open(part, "wb") as f:
            total = int(response.headers.get("Content-Length") or size or 0)
            done = 0
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                done += len(chunk)
                if done > MAX_PACKAGE:
                    raise UpdateError(f"{url} is unexpectedly large; it was not used.")
                f.write(chunk)
                if progress:
                    progress(done, total)
        if total and done != total:
            raise UpdateError(f"The download of {os.path.basename(path)} was cut off ({done} of {total} bytes).")
        os.replace(part, path)
    except urllib.error.HTTPError as err:
        raise UpdateError(f"{url} could not be downloaded (HTTP error {err.code}).") from None
    except OSError as err:
        raise UpdateError(_connection_problem(err) if isinstance(err, urllib.error.URLError)
                          else f"The download of {os.path.basename(path)} failed: {err}") from None
    finally:
        if os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass


def download_folder():
    """Where a downloaded package is kept: the Downloads folder, or the home folder."""
    downloads = os.path.expanduser("~/Downloads")
    return downloads if os.path.isdir(downloads) and os.access(downloads, os.W_OK) else os.path.expanduser("~")


def _restart_args(files):
    return [os.path.realpath(sys.executable)] + [f for f in files if f]


def prepare_windows_swap(zip_path, files=(), relaunch=True, exe=None, pid=None):
    """Unpack the new Windows package next to this program's folder and write the script that
    swaps the folders once this program (or the process pid) has exited, and starts the new one
    if relaunch. exe: the program to replace (this one). Returns the command that runs the
    script; raises UpdateError if this is not possible."""
    exe = os.path.realpath(exe or sys.executable)
    app_dir = os.path.dirname(exe)
    parent = os.path.dirname(app_dir)
    if not (os.access(parent, os.W_OK) and os.access(app_dir, os.W_OK)):
        raise UpdateError(f"{app_dir} cannot be changed by your user account.")
    stage = app_dir + ".update"
    shutil.rmtree(stage, ignore_errors=True)
    try:
        with zipfile.ZipFile(zip_path) as z:
            for member in z.namelist():  # every file must stay inside the folder
                target = os.path.realpath(os.path.join(stage, member))
                if not target.startswith(os.path.realpath(stage) + os.sep):
                    raise UpdateError(f"{os.path.basename(zip_path)} holds an unexpected file: {member}")
            z.extractall(stage)
    except (OSError, ValueError, zipfile.BadZipFile) as err:
        shutil.rmtree(stage, ignore_errors=True)
        raise UpdateError(f"{os.path.basename(zip_path)} could not be unpacked: {err}") from None
    new_dir = os.path.join(stage, APP_NAME)
    exe_name = os.path.basename(exe)
    if not os.path.isfile(os.path.join(new_dir, exe_name)):
        shutil.rmtree(stage, ignore_errors=True)
        raise UpdateError(f"{os.path.basename(zip_path)} does not hold {APP_NAME}\\{exe_name}.")

    def ps(text):  # a PowerShell string literal
        return "'" + str(text).replace("'", "''") + "'"

    args = " ".join('"' + f.replace('"', '') + '"' for f in files if f)
    start = (f"Start-Process -FilePath (Join-Path $app {ps(exe_name)}) -WorkingDirectory $app"
             + (f" -ArgumentList {ps(args)}" if args else ""))
    failed_head = (f"{APP_NAME} could not be replaced by the downloaded version, so the previous version "
                   f"is still in use.\n\nFolder: {app_dir}\nReason: ")
    failed_tail = (f"\n\nThe new version is kept in {new_dir}. Close {APP_NAME} and replace the folder "
                   f"{app_dir} with it by hand, or try Help > Check for Updates again.")
    script = os.path.join(stage, "swap.ps1")
    with open(script, "w", encoding="utf-8-sig") as f:
        f.write(f"""# Written by {APP_NAME} {APP_VERSION}: puts the new version in place after the program has exited
$ErrorActionPreference = 'Stop'
$app = {ps(app_dir)}
$new = {ps(new_dir)}
$stage = {ps(stage)}
$old = $app + '.old'
$failed = {ps(os.path.expanduser(UPDATE_FAILED_FILE))}
$env:PYINSTALLER_RESET_ENVIRONMENT = '1'
# Windows cannot rename a folder that is the current folder of a process: never stand in it
Set-Location -LiteralPath (Split-Path $app -Parent)
try {{ Wait-Process -Id {pid or os.getpid()} }} catch {{ }}  # also when the program is closed hours later
if (Test-Path -LiteralPath $old) {{ Remove-Item -LiteralPath $old -Recurse -Force -ErrorAction SilentlyContinue }}
$moved = $false
$problem = 'unknown'
for ($i = 0; $i -lt 60 -and -not $moved; $i++) {{
    try {{ Rename-Item -LiteralPath $app -NewName (Split-Path $old -Leaf); $moved = $true }}
    catch {{ $problem = $_.Exception.Message; Start-Sleep -Milliseconds 500 }}
}}
if ($moved) {{
    try {{ Move-Item -LiteralPath $new -Destination $app }}
    catch {{ $problem = $_.Exception.Message; Rename-Item -LiteralPath $old -NewName (Split-Path $app -Leaf); $moved = $false }}
}}
if (-not $moved) {{  # reported by the program at its next start; the new version is kept in $stage
    try {{ Set-Content -LiteralPath $failed -Encoding UTF8 -Value ({ps(failed_head)} + $problem + {ps(failed_tail)}) }} catch {{ }}
}}
{start if relaunch else "# not restarted: the new version runs at the next start"}
if ($moved) {{
    Remove-Item -LiteralPath $old -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
}}
""")
    return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", script]


def prepare_mac_swap(dmg_path, files=(), relaunch=True, app=None, pid=None):
    """Copy the new FSP Viewer.app out of the disk image next to this one and write the script
    that swaps the bundles once this program (or the process pid) has exited, and opens the new
    one if relaunch. app: the bundle to replace (this one). Returns the command that runs the
    script; raises UpdateError if this is not possible."""
    app = app or mac_app_bundle()
    if app is None:
        raise UpdateError(f"{APP_NAME} does not run from an application bundle.")
    parent = os.path.dirname(app)
    if "/AppTranslocation/" in app or not (os.access(parent, os.W_OK) and os.access(app, os.W_OK)):
        raise UpdateError(f"{app} cannot be replaced from here (move {APP_NAME} into the Applications "
                          "folder first, or the folder needs administrator rights).")
    stage = tempfile.mkdtemp(prefix=f".{APP_SLUG}-update-", dir=parent)
    mount = tempfile.mkdtemp(prefix=f"{APP_SLUG}-dmg-")
    try:
        subprocess.run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-quiet",
                        "-mountpoint", mount, dmg_path], check=True, capture_output=True, timeout=300)
        try:
            source = os.path.join(mount, APP_NAME + ".app")
            if not os.path.isdir(source):
                raise UpdateError(f"{os.path.basename(dmg_path)} does not hold {APP_NAME}.app.")
            subprocess.run(["ditto", source, os.path.join(stage, APP_NAME + ".app")],
                           check=True, capture_output=True, timeout=600)
        finally:
            subprocess.run(["hdiutil", "detach", mount, "-force", "-quiet"], capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as err:
        shutil.rmtree(stage, ignore_errors=True)
        detail = getattr(err, "stderr", b"") or b""
        raise UpdateError(f"{os.path.basename(dmg_path)} could not be opened: "
                          f"{detail.decode(errors='replace').strip() or err}") from None
    except UpdateError:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    finally:
        try:
            os.rmdir(mount)
        except OSError:
            pass
    q = shlex.quote
    open_cmd = "open " + q(app) + (" --args " + " ".join(q(f) for f in files if f) if any(files) else "")
    failed = (f"{APP_NAME} could not be replaced by the downloaded version, so the previous version is "
              f"still in use.\n\nApplication: {app}\n\nTry Help > Check for Updates again, or install it "
              f"from {RELEASES_PAGE} by hand.")
    script = os.path.join(stage, "swap.sh")
    with open(script, "w", encoding="utf-8") as f:
        f.write(f"""#!/bin/sh
# Written by {APP_NAME} {APP_VERSION}: puts the new version in place after the program has exited
cd {q(parent)} || cd /
while kill -0 {pid or os.getpid()} 2>/dev/null; do sleep 0.3; done
moved=0
if mv {q(app)} {q(os.path.join(stage, "old.app"))}; then
    if mv {q(os.path.join(stage, APP_NAME + ".app"))} {q(app)}; then
        moved=1
        /System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f {q(app)} >/dev/null 2>&1
    else
        mv {q(os.path.join(stage, "old.app"))} {q(app)}
    fi
fi
[ "$moved" = 1 ] || printf '%s\n' {q(failed)} > {q(os.path.expanduser(UPDATE_FAILED_FILE))}
{open_cmd if relaunch else ": the new version runs at the next start"}
rm -rf {q(stage)}
""")
    return ["/bin/sh", script]


def linux_install_command(kind, path):
    """The command (without pkexec / sudo) that installs a downloaded .deb or .rpm package."""
    def which(name):
        return shutil.which(name) or shutil.which(name, path="/usr/sbin:/usr/bin:/sbin:/bin")
    if kind == "deb":
        if which("apt-get"):
            return [which("apt-get"), "install", "-y", "--allow-downgrades", path]
        return [which("dpkg") or "dpkg", "-i", path]
    if which("dnf"):
        return [which("dnf"), "install", "-y", "--nogpgcheck", path]
    if which("zypper"):
        return [which("zypper"), "--non-interactive", "install", "--allow-unsigned-rpm", path]
    if which("yum"):
        return [which("yum"), "install", "-y", "--nogpgcheck", path]
    return [which("rpm") or "rpm", "-U", "--replacepkgs", path]


def install_linux_package(kind, path):
    """Install a downloaded .deb / .rpm with administrator rights (pkexec asks for the password
    in a window). Returns (success, message)."""
    cmd = linux_install_command(kind, path)
    manual = "sudo " + " ".join(shlex.quote(c if i else os.path.basename(c)) for i, c in enumerate(cmd))
    if os.geteuid() != 0:
        pkexec = shutil.which("pkexec")
        if not pkexec or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return False, ("Installing the package needs administrator rights. In a terminal, run\n"
                           f"    {manual}")
        cmd = [pkexec] + cmd
    try:
        result = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=1800)
    except (OSError, subprocess.SubprocessError) as err:
        return False, f"The package could not be installed ({err}). In a terminal, run\n    {manual}"
    if result.returncode == 0:
        return True, f"{os.path.basename(path)} is installed."
    if result.returncode in (126, 127) and cmd[0].endswith("pkexec"):
        why = "the password was not given"
    else:
        lines = (result.stderr or result.stdout).decode(errors="replace").strip().splitlines()
        why = lines[-1] if lines else f"exit code {result.returncode}"
    return False, f"The package was not installed ({why}). In a terminal, run\n    {manual}"


def start_detached(cmd):
    """Run cmd in the background so that it outlives this program.

    It runs in the home folder, not in the current folder: started from Explorer, a Windows
    program runs in its own folder, and Windows cannot rename a folder that is the current
    folder of a process, so an inherited one would block the update swap."""
    env = dict(os.environ, **RELAUNCH_ENV)
    home = os.path.expanduser("~")
    cwd = home if os.path.isdir(home) else tempfile.gettempdir()
    if sys.platform.startswith("win"):
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(cmd, creationflags=flags, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    else:
        subprocess.Popen(cmd, start_new_session=True, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)


# ======================================================================
# Pictures to the Clipboard (Tk itself copies only text)
# ======================================================================
PICTURE_DPI = 200   # resolution of a copied plot (dots per inch of the figure)


def figure_png(fig, dpi=PICTURE_DPI):
    """The figure as PNG bytes, on its own background colour."""
    import io
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, facecolor=fig.get_facecolor())
    return buf.getvalue()


def copy_png_to_clipboard(png):
    """Put a PNG picture on the system clipboard; raises RuntimeError with the reason if it cannot.
    macOS: through osascript; Windows: as a bitmap (CF_DIB) and as PNG, for programs that take
    either; Linux: through wl-copy (Wayland) or xclip (X11), one of which must be installed."""
    if sys.platform.startswith("win"):
        _copy_png_windows(png)
        return
    fd, path = tempfile.mkstemp(suffix=".png", prefix="fsp_viewer_")
    with os.fdopen(fd, "wb") as f:
        f.write(png)
    try:
        if sys.platform == "darwin":
            cmd = ["osascript", "-e", f'set the clipboard to (read (POSIX file "{path}") as «class PNGf»)']
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or "osascript failed")
            return
        tools = []
        if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
            tools.append(["wl-copy", "--type", "image/png"])
        if shutil.which("xclip"):
            tools.append(["xclip", "-selection", "clipboard", "-t", "image/png", "-i"])
        if not tools:
            raise RuntimeError("copying a picture needs xclip (X11) or wl-clipboard (Wayland); "
                               "install one, e.g. sudo apt install xclip")
        with open(path, "rb") as f:  # both keep running in the background to hand the picture over
            result = subprocess.run(tools[0], stdin=f, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                    timeout=20)
        if result.returncode:
            raise RuntimeError(result.stderr.decode(errors="replace").strip() or f"{tools[0][0]} failed")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _copy_png_windows(png):
    import ctypes
    from ctypes import wintypes
    import matplotlib.image as mpimg
    import io
    rgba = mpimg.imread(io.BytesIO(png), format="png")  # (h, w, 3 or 4), floats 0..1
    if rgba.dtype != np.uint8:
        rgba = (np.clip(rgba, 0, 1) * 255 + 0.5).astype(np.uint8)
    if rgba.shape[2] == 3:
        rgba = np.concatenate([rgba, np.full(rgba.shape[:2] + (1,), 255, np.uint8)], axis=2)
    h, w = rgba.shape[:2]
    pixels = np.ascontiguousarray(rgba[::-1, :, [2, 1, 0, 3]]).tobytes()  # BGRA rows, bottom row first
    dib = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 32, 0, len(pixels), 7874, 7874, 0, 0) + pixels  # 200 dpi

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes, user32.OpenClipboard.restype = [wintypes.HWND], wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes, user32.SetClipboardData.restype = [wintypes.UINT, wintypes.HANDLE], wintypes.HANDLE
    user32.RegisterClipboardFormatW.argtypes, user32.RegisterClipboardFormatW.restype = [wintypes.LPCWSTR], wintypes.UINT
    kernel32.GlobalAlloc.argtypes, kernel32.GlobalAlloc.restype = [wintypes.UINT, ctypes.c_size_t], wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes, kernel32.GlobalLock.restype = [wintypes.HGLOBAL], wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes, kernel32.GlobalUnlock.restype = [wintypes.HGLOBAL], wintypes.BOOL
    kernel32.GlobalFree.argtypes, kernel32.GlobalFree.restype = [wintypes.HGLOBAL], wintypes.HGLOBAL

    def put(fmt, data):
        handle = kernel32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE: the clipboard takes it over
        if not handle:
            raise RuntimeError("not enough memory for the picture")
        ptr = kernel32.GlobalLock(handle)
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(fmt, handle):
            kernel32.GlobalFree(handle)
            raise RuntimeError(f"the clipboard refused the picture (error {ctypes.get_last_error()})")

    for _ in range(10):  # another program may hold the clipboard for a moment
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("the clipboard is in use by another program")
    try:
        user32.EmptyClipboard()
        put(8, dib)  # CF_DIB
        put(user32.RegisterClipboardFormatW("PNG"), png)
    finally:
        user32.CloseClipboard()


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
        self.configure(bg=T["panel_bg"], padx=dpx(18), pady=dpx(16))
        top = tk.Frame(self, bg=T["panel_bg"])
        top.pack(anchor=tk.W, fill=tk.X)
        if icon is not None:
            label = tk.Label(top, image=icon, bg=T["panel_bg"])
            label.image = icon  # keep a reference, or Tk shows an empty square
            label.pack(side=tk.LEFT, anchor=tk.N, padx=(0, dpx(16)))
        tk.Label(top, text=message, justify=tk.LEFT, wraplength=tx(440), font=ui_font(11),
                 bg=T["panel_bg"], fg=T["text"]).pack(side=tk.LEFT, anchor=tk.W)
        if checkbox:
            tk.Checkbutton(self, text=checkbox[0], variable=checkbox[1], font=ui_font(10),
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
            tk.Label(self, text=note, justify=tk.LEFT, wraplength=tx(480), font=ui_font(9),
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


class SettingsDialog(AppDialog):
    """Settings of the program: fonts, font size, interface scale, the layout of the window at the
    start and the colour map of the colour plots. OK saves them and uses them at once (the interface
    scale at the next start)."""

    def __init__(self, app):
        tk.Toplevel.__init__(self, app.root)
        T = THEME
        self.app, self.root, self.choice = app, app.root, None
        self.title("Settings")
        self.resizable(False, False)
        self.transient(app.root)
        self.configure(bg=T["panel_bg"], padx=dpx(20), pady=dpx(16))
        config = load_config()
        self.families = sorted({f for f in tkfont.families(app.root) if not f.startswith("@")}, key=str.lower)
        self.auto_label = f"Automatic ({round(system_ui_scale(app.root) * 100)} %)"
        self.font_var = tk.StringVar(value=UI["font"])
        self.mono_var = tk.StringVar(value=UI["mono"])
        self.size_var = tk.StringVar(value=str(UI["size"]))
        scale = config.get("ui_scale", "auto")
        self.scale_var = tk.StringVar(value=self.auto_label if scale == "auto" else f"{scale} %")
        self.split_plot_var = tk.StringVar(value=str(round(app.layout["split_plot"] * 100)))
        self.split_editor_var = tk.StringVar(value=str(round(app.layout["split_editor"] * 100)))
        self.cmap_var = tk.StringVar(value=app.cmap_var.get())
        self.cmap_reverse_var = tk.BooleanVar(value=app.cmap_reverse_var.get())

        grid = tk.Frame(self, bg=T["panel_bg"])
        grid.pack(fill=tk.X)
        self._row = 0

        def section(text):
            tk.Label(grid, text=text, font=ui_font(11, bold=True), bg=T["panel_bg"], fg=T["text"]).grid(
                row=self._row, column=0, columnspan=3, sticky="w", pady=(dpx(10) if self._row else 0, dpx(4)))
            self._row += 1

        def row(label, widget, hint=""):
            tk.Label(grid, text=label, font=ui_font(10), bg=T["panel_bg"], fg=T["text_muted"]).grid(
                row=self._row, column=0, sticky="w", padx=(dpx(12), dpx(10)), pady=dpx(3))
            widget.grid(row=self._row, column=1, sticky="w", pady=dpx(3))
            if hint:
                tk.Label(grid, text=hint, font=ui_font(9), bg=T["panel_bg"], fg=T["text_dim"]).grid(
                    row=self._row, column=2, sticky="w", padx=(dpx(8), 0))
            self._row += 1

        def combo(var, values, width, readonly=True):
            return ttk.Combobox(grid, textvariable=var, values=values, width=width, font=ui_font(10),
                                style="Dark.TCombobox", state="readonly" if readonly else "normal", height=16)

        def spin(var, lo, hi):
            return ttk.Spinbox(grid, textvariable=var, from_=lo, to=hi, increment=1, width=5, font=ui_font(10),
                               style="Dark.TSpinbox")

        section("Text")
        row("Font:", combo(self.font_var, self.families, 28, readonly=False))
        row("Monospaced font:", combo(self.mono_var, self.families, 28, readonly=False),
            "parameters, labels in the views")
        row("Font size:", spin(self.size_var, 6, 24), f"pt (standard: {FONT_SIZE_DEFAULT})")

        section("Window")
        row("Interface scale:", combo(self.scale_var, [self.auto_label] + [f"{p} %" for p in UI_SCALE_CHOICES], 18),
            "size of everything; used at the next start")
        row("Views : results plot:", spin(self.split_plot_var, 20, 80), "% of the width for the views")
        row("Views : model parameters:", spin(self.split_editor_var, 20, 85), "% of the height for the views")
        FlatButton(grid, "Use Current Layout", self._use_current_layout, size=9, padx=8, pady=3,
                   tooltip="Take the divider positions of the window as it is now").grid(
            row=self._row, column=1, columnspan=2, sticky="w", pady=(dpx(2), 0))
        self._row += 1

        section("Plots")
        cmap_row = tk.Frame(grid, bg=T["panel_bg"])
        combo_cmap = ttk.Combobox(cmap_row, textvariable=self.cmap_var, values=app.colormap_choices(), width=12,
                                  font=ui_font(10), style="Dark.TCombobox", state="readonly")
        combo_cmap.pack(side=tk.LEFT)
        tk.Checkbutton(cmap_row, text="Reverse", variable=self.cmap_reverse_var, font=ui_font(10),
                       bg=T["panel_bg"], fg=T["text"], activebackground=T["panel_bg"], activeforeground=T["text"],
                       selectcolor=T["entry_bg"], highlightthickness=0, bd=0).pack(side=tk.LEFT, padx=(dpx(8), 0))
        row("Colour map:", cmap_row, "of the colour plots (Default: each plot its own)")

        buttons = tk.Frame(self, bg=T["panel_bg"])
        buttons.pack(fill=tk.X, pady=(dpx(18), 0))
        FlatButton(buttons, "Restore Defaults", self._restore_defaults).pack(side=tk.LEFT)
        FlatButton(buttons, "OK", self._ok, variant="primary", bold=True).pack(side=tk.RIGHT, padx=(dpx(8), 0))
        FlatButton(buttons, "Cancel", lambda: self._choose(None)).pack(side=tk.RIGHT)
        self.bind("<Return>", lambda event: self._ok())
        self.bind("<Escape>", lambda event: self._choose(None))
        self.protocol("WM_DELETE_WINDOW", lambda: self._choose(None))

    def _use_current_layout(self):
        layout = self.app.current_layout()
        self.split_plot_var.set(str(round(layout["split_plot"] * 100)))
        self.split_editor_var.set(str(round(layout["split_editor"] * 100)))

    def _restore_defaults(self):
        self.font_var.set(FONT_UI)
        self.mono_var.set(FONT_MONO)
        self.size_var.set(str(FONT_SIZE_DEFAULT))
        self.scale_var.set(self.auto_label)
        self.split_plot_var.set(str(round(LAYOUT_DEFAULTS["split_plot"] * 100)))
        self.split_editor_var.set(str(round(LAYOUT_DEFAULTS["split_editor"] * 100)))
        self.cmap_var.set(self.app.COLORMAP_DEFAULT)
        self.cmap_reverse_var.set(False)

    def _family(self, var, what):
        """The font family typed or chosen (spelled as the system spells it), or None after a message."""
        name = var.get().strip()
        match = next((f for f in self.families if f.lower() == name.lower()), None)
        if match is None:
            messagebox.showerror("Settings", f"There is no {what} called “{name}” on this computer.", parent=self)
        return match

    def values(self):
        """The settings in the dialog, or None (after a message) if one is not valid."""
        family = self._family(self.font_var, "font")
        mono = family and self._family(self.mono_var, "font")
        if not mono:
            return None
        try:
            size = int(self.size_var.get())
            split_plot = int(self.split_plot_var.get())
            split_editor = int(self.split_editor_var.get())
        except ValueError:
            messagebox.showerror("Settings", "The font size and the layout are whole numbers.", parent=self)
            return None
        scale = self.scale_var.get()
        return {
            "font_family": family, "font_mono": mono, "font_size": min(24, max(6, size)),
            "ui_scale": "auto" if scale == self.auto_label else int(scale.split()[0]),
            "split_plot": min(80, max(20, split_plot)) / 100, "split_editor": min(85, max(20, split_editor)) / 100,
            "colormap": self.cmap_var.get(), "colormap_reversed": bool(self.cmap_reverse_var.get()),
        }

    def _ok(self):
        values = self.values()
        if values is None:
            return
        self._choose("ok")
        self.app.apply_settings(values)


class FSPGuiApp:
    """
    Main GUI combining:
      1. 4-View Geometry Visualization Field (Up-Left: XY, Bottom-Left: XZ, Top-Right: 3D, Bottom-Right: YZ)
         with yellow ports
      2. Model Parameters Editor
      3. Plot button + Port dropdown + Results dropdown + Export Data to Clipboard button
      4. Fifth Larger Plot Window (embedded large right pane + optional standalone window)
    """

    def __init__(self, root, initial_fsp=None, initial_h5=None):
        self.root = root
        self.root.title(APP_NAME)
        self.app_icon = app_icon_photo(root)  # the window and Dock icon
        if self.app_icon is not None:
            try:
                self.root.iconphoto(True, self.app_icon)
            except tk.TclError:
                pass
        config = load_config()
        init_ui_scale(root, config.get("ui_scale", "auto"))
        set_fonts(config.get("font_family") or FONT_UI, config.get("font_mono") or FONT_MONO,
                  self._font_size_setting(config.get("font_size")))
        self.layout = {key: self._split_setting(config.get(key), default) for key, default in LAYOUT_DEFAULTS.items()}
        # the window fits on the screen (a laptop with a scaled display has fewer points than the Mac)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = min(dpx(1640), int(sw * 0.95)), min(dpx(960), int(sh * 0.88))
        self.root.geometry(f"{w}x{h}")
        self.root.minsize(min(dpx(1220), w), min(dpx(740), h))
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
        cmap =config.get("colormap", self.COLORMAP_DEFAULT)
        self.cmap_var = tk.StringVar(value=cmap if cmap in self.colormap_choices() else self.COLORMAP_DEFAULT)
        self.cmap_reverse_var = tk.BooleanVar(value=bool(config.get("colormap_reversed", False)))

        self._build_ui()
        self._build_menu()
        self.root.after(150, self.apply_layout)  # once the window has its size

        if sys.platform == "darwin":  # a .fsp file opened with the installed application in Finder
            try:
                self.root.createcommand("::tk::mac::OpenDocument", lambda *paths: paths and self.open_fsp_file(paths[0]))
                # Settings… (Cmd+,) in the application menu, the menu with the name of the program
                self.root.createcommand("::tk::mac::ShowPreferences", self.show_settings)
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
        if sys.platform != "darwin":  # macOS: in the application menu (::tk::mac::ShowPreferences)
            file_menu.add_separator()
            file_menu.add_command(label="Settings…", command=self.show_settings, accelerator="Ctrl+,")
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
        if sys.platform != "darwin":
            self.root.bind_all("<Control-comma>", lambda e: self.show_settings())
        if sys.platform != "darwin":  # macOS has Cmd+Q of its own
            self.root.bind_all("<Control-q>", lambda e: self.root.quit())

    def _build_ui(self):
        T = THEME

        # ==================================================================
        # Top Control & Toolbar Strip
        # ==================================================================
        toolbar = tk.Frame(self.root, bg=T["bar_bg"], padx=dpx(12), pady=dpx(8))
        toolbar.pack(fill=tk.X, side=tk.TOP)
        self.toolbar = toolbar

        # App branding + Open buttons; the branding gives way when the toolbar is too narrow
        # (a packed row hides its last widgets first: the Port selector)
        self._brand = [
            (tk.Label(toolbar, text="◆", font=ui_font(16), bg=T["bar_bg"], fg=T["port_yellow"]), (0, 6)),
            (tk.Label(toolbar, text="FSP Viewer & Plotter", font=ui_font(13, bold=True), bg=T["bar_bg"],
                      fg=T["text"]), (0, 12)),
        ]
        for label, pad in self._brand:
            label.pack(side=tk.LEFT, padx=(dpx(pad[0]), dpx(pad[1])))
        self._brand_shown = True
        toolbar.bind("<Configure>", lambda e: self.root.after_idle(self._fit_toolbar))

        self.btn_open_fsp = FlatButton(
            toolbar,
            "📂 Open .fsp…",
            self.browse_fsp,
            variant="primary",
            size=10,
            bold=True,
            padx=12,
            pady=5,
            tooltip="Open an Ansys Lumerical FDTD project file (.fsp)",
        )
        self.btn_open_fsp.pack(side=tk.LEFT, padx=(0, 6))

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
            font=ui_font(10),
            values=["Center λ"],
        )
        self.cmb_lam.pack(side=tk.RIGHT, padx=(4, 4))
        self.cmb_lam.bind("<<ComboboxSelected>>", lambda e: self.plot_selected())
        Tooltip(self.cmb_lam, "Select wavelength λ for single-wavelength field plots")

        # Result dropdown menu: the results of the selected object that the open files hold or
        # that can be calculated from them, then the analyses of the whole model
        self.result_var = tk.StringVar(value="")
        self._result_funcs = {}
        self.cmb_result = ttk.Combobox(
            toolbar,
            textvariable=self.result_var,
            state="readonly",
            width=self.RESULT_WIDTH,
            style="Result.Dark.TCombobox",
            font=ui_font(10),
            values=[],
            height=24,
        )
        self.cmb_result.pack(side=tk.RIGHT, padx=(4, 4))
        self.cmb_result.bind("<<ComboboxSelected>>", self._on_result_selected)
        self._wide_popdown(self.cmb_result, "Result.Dark.TCombobox")
        Tooltip(self.cmb_result, "The results of the selected object, named as in Lumerical (getresult):\n"
                                 "[fsp] stored in the .fsp file, [h5] stored in the .h5 field file,\n"
                                 "[calc] calculated by FSP Viewer from them.\n"
                                 "Below the line: analyses of the whole model.")
        tk.Label(
            toolbar, text="Result:", font=ui_font(10, bold=True), bg=T["bar_bg"], fg=T["text_muted"]
        ).pack(side=tk.RIGHT, padx=(8, 2))

        # Object dropdown menu: ports, monitors, FDTD::ports (S-parameters)
        self.port_var = tk.StringVar(value=ALL_OBJECTS)
        self.cmb_port = ttk.Combobox(
            toolbar,
            textvariable=self.port_var,
            state="readonly",
            width=16,
            style="Object.Dark.TCombobox",
            font=ui_font(10),
            values=[ALL_OBJECTS],
            height=24,
        )
        self.cmb_port.pack(side=tk.RIGHT, padx=(4, 4))
        self.cmb_port.bind("<<ComboboxSelected>>", self._on_port_dropdown_changed)
        self._wide_popdown(self.cmb_port, "Object.Dark.TCombobox")
        Tooltip(self.cmb_port, "The port or monitor whose results are listed (named as in the model);\n"
                               "FDTD::ports holds the S-parameters. A click on a port or monitor in the\n"
                               "views selects it too.")
        tk.Label(
            toolbar, text="Object:", font=ui_font(10, bold=True), bg=T["bar_bg"], fg=T["port_yellow"]
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
            statusbar, text="", font=ui_font(9), bg=T["bar_bg"], fg=T["text_muted"], anchor="e"
        )
        self.status_info_lbl.pack(side=tk.RIGHT, padx=(12, 10), pady=2)
        # layers, ports and monitors of the model, and the stretch of the view under the pointer
        # (shown in the headers of the views before, where it hid the pointer position)
        self.status_model_lbl = tk.Label(
            statusbar, text="", font=ui_font(9), bg=T["bar_bg"], fg=T["port_yellow"], anchor="e"
        )
        self.status_model_lbl.pack(side=tk.RIGHT, padx=(12, 0), pady=2)
        self.status_lbl = tk.Label(
            statusbar, text="Ready: open an .fsp file", font=ui_font(9), bg=T["bar_bg"],
            fg=T["text_muted"], anchor="w"
        )
        self.status_lbl.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(10, 0), pady=2)

        self.main_paned = tk.PanedWindow(
            self.root, orient=tk.HORIZONTAL, bg=T["app_bg"], sashwidth=dpx(6), bd=0, opaqueresize=True
        )
        self.main_paned.pack(fill=tk.BOTH, expand=True, padx=dpx(6), pady=(dpx(4), dpx(6)))

        # Left vertical split (4-View Visualization Field + Parameter Editor)
        self.left_paned = tk.PanedWindow(
            self.main_paned, orient=tk.VERTICAL, bg=T["app_bg"], sashwidth=dpx(6), bd=0, opaqueresize=True
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

        self.left_paned.add(quad_frame, minsize=dpx(360), height=dpx(560))
        self.left_paned.add(self.param_editor, minsize=dpx(180), height=dpx(320))

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

        FlatButton(
            plot_header,
            "Copy Picture",
            lambda: self.copy_picture_to_clipboard(self.fig),
            variant="ghost",
            size=9,
            padx=8,
            pady=3,
            tooltip=f"Copy the plot as a picture ({PICTURE_DPI} dpi) to the clipboard, to paste it into a document",
        ).pack(side=tk.RIGHT, padx=2, pady=3)

        self._build_colormap_controls(plot_header, T["panel_bg"]).pack(side=tk.RIGHT, padx=(4, 8), pady=3)

        tk.Label(
            plot_header,
            text="Results Plot",
            font=ui_font(11, bold=True),
            bg=T["panel_bg"],
            fg=T["text"],
            anchor="w",
        ).pack(side=tk.LEFT, padx=(10, 8), pady=5)

        # Matplotlib Figure & Canvas
        self.fig = Figure(figsize=(7.6, 6.4), dpi=100, facecolor="#12151a")
        self.mpl_canvas = FigureCanvas(self.fig, master=self.plot_panel)
        self.mpl_canvas.get_tk_widget().configure(bg="#12151a", highlightthickness=0)
        self.mpl_canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        toolbar_frame = tk.Frame(self.plot_panel, bg=T["bar_bg"])
        toolbar_frame.pack(fill=tk.X, side=tk.BOTTOM)
        self.mpl_toolbar = NavigationToolbar2Tk(self.mpl_canvas, toolbar_frame, pack_toolbar=False)
        self.mpl_toolbar.update()
        self.mpl_toolbar.pack(side=tk.LEFT, fill=tk.X)

        self.main_paned.add(self.left_paned, minsize=dpx(580), width=dpx(860))
        self.main_paned.add(self.plot_panel, minsize=dpx(480), width=dpx(760))

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
        tk.Label(frame, text="Colors:", font=ui_font(9, bold=True), bg=bg, fg=T["text_muted"]).pack(side=tk.LEFT)
        cmb = ttk.Combobox(frame, textvariable=self.cmap_var, state="readonly", width=9,
                           style="Dark.TCombobox", font=ui_font(9), values=self.colormap_choices())
        cmb.pack(side=tk.LEFT, padx=(4, 4))
        cmb.bind("<<ComboboxSelected>>", lambda e: self._on_colormap_changed())
        Tooltip(cmb, "Colour map of the colour plots (fields, refractive index, propagation).\n"
                     "Default: each plot its own (viridis for n, inferno for |E|², magma for components)")
        chk = tk.Checkbutton(frame, text="Reverse", variable=self.cmap_reverse_var,
                             command=self._on_colormap_changed, font=ui_font(9),
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
    # Layout and Settings
    # ==================================================================
    @staticmethod
    def _font_size_setting(value):
        try:
            return min(24, max(6, int(value)))
        except (TypeError, ValueError):
            return FONT_SIZE_DEFAULT

    @staticmethod
    def _split_setting(value, default):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return default
        return value if 0.15 <= value <= 0.85 else default

    RESULT_WIDTH, RESULT_WIDTH_NARROW = 40, 26  # characters of the Result selector (narrow: tight toolbar)

    def _fit_toolbar(self):
        """When the controls of the toolbar do not fit, first hide the branding, then narrow the
        Result selector; undo it when there is room again."""
        try:
            width, need = self.toolbar.winfo_width(), self.toolbar.winfo_reqwidth()
        except tk.TclError:
            return
        if width <= 1:
            return
        brand = sum(label.winfo_reqwidth() + dpx(pad[0] + pad[1]) for label, pad in self._brand)
        result = (self.RESULT_WIDTH - self.RESULT_WIDTH_NARROW) * ui_font(10).measure("0")
        narrowed = int(str(self.cmb_result.cget("width"))) < self.RESULT_WIDTH
        full = need + (0 if self._brand_shown else brand) + (result if narrowed else 0)
        show_brand, narrow = full <= width, full - brand > width
        if show_brand != self._brand_shown:
            for label, pad in self._brand:
                if show_brand:
                    label.pack(side=tk.LEFT, padx=(dpx(pad[0]), dpx(pad[1])), before=self.btn_open_fsp)
                else:
                    label.pack_forget()
            self._brand_shown = show_brand
        if narrow != narrowed:
            self.cmb_result.configure(width=self.RESULT_WIDTH_NARROW if narrow else self.RESULT_WIDTH)

    def apply_layout(self):
        """Put the dividers between the views, the parameters and the results plot where the
        settings say."""
        try:
            self.root.update_idletasks()
            w, h = self.main_paned.winfo_width(), self.left_paned.winfo_height()
            if w > 50:
                self.main_paned.sash_place(0, int(w * self.layout["split_plot"]), 0)
            if h > 50:
                self.left_paned.sash_place(0, 0, int(h * self.layout["split_editor"]))
        except tk.TclError:
            pass

    def current_layout(self):
        """Where the dividers are now, as fractions of the width and the height."""
        layout = dict(self.layout)
        try:
            w, h = self.main_paned.winfo_width(), self.left_paned.winfo_height()
            if w > 50:
                layout["split_plot"] = self.main_paned.sash_coord(0)[0] / w
            if h > 50:
                layout["split_editor"] = self.left_paned.sash_coord(0)[1] / h
        except tk.TclError:
            pass
        return layout

    def show_settings(self):
        """File > Settings (macOS: FSP Viewer > Settings): fonts, interface scale, layout, colour map."""
        if getattr(self, "_settings", None) is not None and self._settings.winfo_exists():
            self._settings.lift()
            return
        self._settings = SettingsDialog(self)
        self._settings.show()
        self._settings = None

    def apply_settings(self, values):
        """Save and use the settings of the Settings dialog (a dict, see SettingsDialog.values)."""
        config = load_config()
        old_scale = config.get("ui_scale", "auto")
        config.update({
            "font_family": values["font_family"], "font_mono": values["font_mono"],
            "font_size": values["font_size"], "ui_scale": values["ui_scale"],
            "split_plot": round(values["split_plot"], 3), "split_editor": round(values["split_editor"], 3),
        })
        save_config(config)
        # fonts: every widget and canvas text follows its named font; the views redraw their margins
        set_fonts(values["font_family"], values["font_mono"], values["font_size"])
        for view in (self.view_xy, self.view_xz, self.view_yz, self.view_3d):
            view.redraw()
        self.layout = {"split_plot": values["split_plot"], "split_editor": values["split_editor"]}
        self.apply_layout()
        self.root.after_idle(self._fit_toolbar)
        self.cmap_var.set(values["colormap"])
        self.cmap_reverse_var.set(values["colormap_reversed"])
        self._on_colormap_changed()  # saves them, and recolours the plots
        if str(values["ui_scale"]) != str(old_scale):
            choice = self.message(
                "Interface Scale",
                f"The new interface scale is used the next time {APP_NAME} starts.\n\nRestart now? "
                "The file that is open now is opened again.",
                [("restart", "Restart Now"), ("later", "Later")])
            if choice == "restart":
                self.restart(THIS_SCRIPT)

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

        # Populate the Object dropdown (the Result dropdown follows the object)
        self.cmb_port["values"] = self._object_choices()
        self.port_var.set(ALL_OBJECTS)
        self.result_var.set("")
        self._update_result_choices()

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
        self._update_result_choices(prefer_object=_event is not None)
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
    # The Object and Result Selectors
    # ==================================================================
    # Results are named as in Lumerical (getresult): E, H, P, T, 'expansion for port monitor',
    # 'mode profiles', S, and the datasets stored in the .fsp under their stored names (time_signal,
    # P0). The tag says where a result comes from: [fsp] the .fsp file, [h5] the .h5 field file,
    # [calc] calculated by this program from them (Lumerical calculates these too, when asked).
    RESULT_SEPARATOR = "──────── whole model ────────"
    PORTS_OBJECT = "FDTD::ports"

    def _wide_popdown(self, cmb, style):
        """Make the dropdown list of a combobox as wide as its longest entry (extending to the left)."""
        def post():
            values = cmb.cget("values") or ()
            font = ui_font(10)
            need = max((font.measure(str(v)) for v in values), default=0) + dpx(40)
            extra = max(0, need - cmb.winfo_width())
            ttk.Style(self.root).configure(style, postoffset=(-extra, 0, extra, 0))
        cmb.configure(postcommand=post)

    def _lum_name(self, obj):
        """The name of an object as Lumerical's getresult takes it."""
        pt = next((p for p in self.model.ports if p["name"] == obj), None) if self.model else None
        if pt is not None:
            return pt["full_name"].replace("::model::", "", 1)
        return obj

    def _object_choices(self):
        """All objects, the ports, FDTD::ports (when S-parameters can be calculated), the mode expansion
        monitors, the field monitors in y-z planes and the other 2D monitors with a field."""
        m = self.model
        choices = [ALL_OBJECTS] + [pt["name"] for pt in m.ports]
        if any(m.port_outgoing(pt["name"]) for pt in m.ports if pt.get("is_source")):
            choices.append(self.PORTS_OBJECT)
        choices += [mm["name"] for mm in m.mode_monitors if mm["placed"]]
        choices += [mm["name"] for mm in m.field_monitors if mm["has_data"]]
        choices += [mon["name"] for mon in m.other_monitors() if mon["name"] not in choices]
        return choices

    def _object_entries(self, obj):
        """(label, plot function) of the results of one object."""
        m = self.model
        out = []
        planes = m.field_planes()
        port = next((p for p in m.ports if p["name"] == obj), None)
        if obj in planes:
            tag = "[h5]" if m.plane_source(obj)[0] == "h5" else "[fsp]"
            has_e = any(m.plane_has(obj, c) for c in ("Ex", "Ey", "Ez"))
            has_h = any(m.plane_has(obj, c) for c in ("Hx", "Hy", "Hz"))
            eh = all(m.plane_has(obj, c) for c in ("Ey", "Ez", "Hy", "Hz"))
            if has_e:
                out.append((f"E — |E|² in the plane at λ {tag}", lambda fig, k: self._plot_plane_field(fig, obj, "E", k)))
            if has_h:
                out.append((f"H — |H|² in the plane at λ {tag}", lambda fig, k: self._plot_plane_field(fig, obj, "H", k)))
            if eh and all(m.plane_has(obj, c) for c in ("Ex", "Hx")):
                out.append(("P — Poynting vector at λ [calc]", lambda fig, k: self._plot_plane_field(fig, obj, "P", k)))
            if m.plane_power_spectrum(obj) is not None and m.power_reference() is not None:
                out.append((f"T — transmission vs λ {'[fsp]' if m.plane_power_stored(obj) else '[calc]'}",
                            lambda fig, k: self._plot_plane_T(fig, obj, k)))
            for owner in m.plane_mode_owners(obj) if eh else []:
                label = ("expansion for port monitor — T_forward, T_backward [calc]" if owner == obj
                         else f"expansion on the modes of {owner} (same x) [calc]")
                out.append((label, lambda fig, k, owner=owner: self._plot_expansion(fig, obj, owner, k)))
        if m.stored_modes(obj):
            out.append(("mode profiles — |E|² and index n [fsp]", lambda fig, k: self._plot_mode_profiles(fig, obj)))
            out.append(("mode profiles — Ex … Hz [fsp]", lambda fig, k: self._plot_mode_components(fig, obj)))
        if port is not None and self._source_object(obj) is not None:
            out.append(("time_signal, P0 — source pulse and spectrum [fsp]", lambda fig, k: self._plot_source_pulse(fig, obj)))
        mm = next((mon for mon in m.mode_monitors if mon["name"] == obj), None)
        if mm is not None:
            for plane in planes:
                if obj in m.plane_mode_owners(plane):
                    out.append((f"expansion of {plane} (same x) — T_forward, T_backward [calc]",
                                lambda fig, k, plane=plane: self._plot_expansion(fig, plane, obj, k)))
        mon = next((mon for mon in m.other_monitors() if mon["name"] == obj), None)
        if mon is not None:
            g = m.h5[mon["group"]]
            f_mon = m.object_freqs(mon["obj"])
            # a monitor set to record one frequency (e.g. to keep a long x-y plane small) has only that
            at = f"at {C0 / f_mon[0] * 1e9:.1f} nm only" if f_mon is not None and f_mon.size == 1 else "at λ"
            if any(c in g for c in ("Ex", "Ey", "Ez")):
                out.append((f"E — |E|² in the plane {at} [h5]", lambda fig, k: self._plot_monitor_2d(fig, mon, "E", k)))
            if any(c in g for c in ("Hx", "Hy", "Hz")):
                out.append((f"H — |H|² in the plane {at} [h5]", lambda fig, k: self._plot_monitor_2d(fig, mon, "H", k)))
        if obj == self.PORTS_OBJECT:
            out.append(("S — |S|² of the port modes [calc]", lambda fig, k: self._plot_port_S(fig, k)))
        return out

    def _model_entries(self):
        """(label, plot function) of the analyses of the whole model."""
        m = self.model
        out = []
        planes = m.field_planes()
        if any(m.plane_power(n, 0) is not None for n in planes):
            out.append(("All planes — T along x and vs λ [calc]", lambda fig, k: self._plot_plane_power(fig, self.port_var.get(), k)))
        if planes:
            out.append(("All planes — E: |E|² at λ [h5]" if m.h5 is not None else "All planes — E: |E|² at λ [fsp]",
                        lambda fig, k: self._plot_simulated_port_fields(fig, ALL_OBJECTS, k)))
        if m.power_reference() is not None and any(m.plane_transmission(n) is not None for n in planes):
            out.append(("All planes — |S_i1|² from their power [calc]", lambda fig, k: self._plot_s_parameters(fig, k)))
        if any(m.stored_modes(pt["name"]) for pt in m.ports) or m.mode_monitors:
            out.append(("All ports — mode profiles [fsp]", lambda fig, k: self._plot_cross_sections_and_modes(fig, ALL_OBJECTS)))
        if m.structures:
            out.append(("Top view — structures, FDTD region, ports [fsp]", lambda fig, k: self._plot_layout_top_view(fig, self.port_var.get())))
        src = m.source_port()
        if src is not None and self._source_object(src["name"]) is not None:
            out.append(("Source — time_signal, P0 [fsp]", lambda fig, k: self._plot_source_pulse(fig, src["name"])))
        return out

    def _update_result_choices(self, prefer_object=False):
        """List the results of the selected object, then the analyses of the whole model. A result of
        the object keeps its name for the new object (E stays E); an analysis of the whole model stays
        chosen unless prefer_object (the object was chosen in its dropdown: show its own results)."""
        if self.model is None:
            return
        obj = self.port_var.get()
        own = [] if obj == ALL_OBJECTS else self._object_entries(obj)
        model = self._model_entries()
        entries = own + ([(self.RESULT_SEPARATOR, None)] if own and model else []) + model
        self._result_funcs = {label: func for label, func in entries if func is not None}
        self.cmb_result["values"] = [label for label, _ in entries]
        own_labels, model_labels = [lb for lb, _ in own], [lb for lb, _ in model]
        current = self.result_var.get()
        if current in own_labels or (current in model_labels and not (prefer_object and own_labels)):
            return
        name = current.split(" — ")[0]
        same = next((lb for lb in own_labels if lb.split(" — ")[0] == name), None)
        self.result_var.set(same or (own_labels or model_labels or [""])[0])

    def _on_result_selected(self, _event=None):
        if self.result_var.get() == self.RESULT_SEPARATOR:  # the line is not a result
            self.result_var.set(getattr(self, "_last_result", ""))
            return
        self.plot_selected()

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
        win.geometry(f"{min(dpx(1080), int(self.root.winfo_screenwidth() * 0.9))}x"
                     f"{min(dpx(760), int(self.root.winfo_screenheight() * 0.85))}")
        win.configure(bg=THEME["panel_bg"])
        self.popout_win = win

        top = tk.Frame(win, bg=THEME["bar_bg"], padx=10, pady=6)
        top.pack(fill=tk.X, side=tk.TOP)
        tk.Label(
            top,
            textvariable=self.result_var,
            font=ui_font(12, bold=True),
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
        FlatButton(
            top,
            "Copy Picture",
            lambda: self.copy_picture_to_clipboard(self.popout_fig),
            variant="secondary",
            size=10,
            padx=12,
            pady=4,
            tooltip=f"Copy the plot as a picture ({PICTURE_DPI} dpi) to the clipboard",
        ).pack(side=tk.RIGHT, padx=6)
        self._build_colormap_controls(top, THEME["bar_bg"]).pack(side=tk.RIGHT, padx=6)

        self.popout_fig = Figure(figsize=(10.2, 6.8), dpi=100, facecolor="#12151a")
        self.popout_canvas = FigureCanvas(self.popout_fig, master=win)
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
        self._last_result = self.result_var.get()
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
        """The ports (with a stored mode) and mode expansion monitors shown by the mode profiles of
        the whole model: all of them along x, or a representative max_all of a long list."""
        planes = [pt for pt in self.model.ports if self.model.stored_modes(pt["name"]) and pt["enabled"]]
        planes += [mm for mm in self.model.mode_monitors if mm["enabled"] and mm["placed"]]
        names = [pl["name"] for pl in sorted(planes, key=lambda pl: pl["x"])] or \
            [pt["name"] for pt in self.model.ports if self.model.stored_modes(pt["name"])]
        if len(names) > max_all:  # first, second, middle, last ...
            return [names[i] for i in np.linspace(0, len(names) - 1, max_all, dtype=int)]
        return names

    def _render_onto_figure(self, fig: Figure):
        """Draw the selected result onto fig; returns None, or the reason it could not be plotted."""
        fig.clear()
        res_kind = self.result_var.get()
        func = self._result_funcs.get(res_kind)
        try:
            if func is None:
                raise RuntimeError("Choose a result: this one is not available for the selected object.")
            func(fig, self._selected_freq_index())
        except Exception as exc:
            fig.clear()
            ax = fig.add_subplot(111)
            self._style_mpl_axes(ax)
            ax.text(0.5, 0.5, f"Cannot plot '{res_kind}':\n\n{exc}", color=THEME["warning"], fontsize=10.5,
                    ha="center", va="center", transform=ax.transAxes, wrap=True)
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

    def _plot_simulated_port_fields(self, fig: Figure, port_sel: str, k_freq: int):
        """|E|² recorded in the y-z planes of the ports and field monitors, from the .h5 file or
        from the .fsp itself (read one frequency at a time)."""
        lam = self.model.get_frequencies_and_wavelengths()[1]
        planes = self.model.field_planes()
        if not planes:
            raise RuntimeError("No recorded field in any port or monitor plane: the project holds no "
                               "results, and no matching .h5 file is open.")
        if port_sel in planes:
            show = [port_sel]
        elif port_sel and port_sel != ALL_OBJECTS:
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

    def _plot_plane_power(self, fig: Figure, port_sel: str, k_freq: int):
        """Power through the port and field monitor planes along x, relative to the first plane:
        at the chosen wavelength, and (when the planes store their power spectrum) against λ.
        Where a plane has a stored mode (its own, or of a port at the same x), the power in the
        first mode is marked too (open diamond)."""
        lam = self.model.get_frequencies_and_wavelengths()[1]
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
        mode_pts = []  # power in the first stored mode of the planes that have one (T_forward)
        if ps is not None:
            for name, x, _ in planes:
                owners = self.model.plane_mode_owners(name)
                ex = self.model.plane_expansion(name, owners[0], 0) if owners else None
                if ex is not None and ex["T_forward"] is not None:
                    mode_pts.append((name, x, float(ex["T_forward"][k_freq]), ex))
        if mode_pts:
            a1.plot([x for _, x, _, _ in mode_pts], [t for _, _, t, _ in mode_pts], "D", ms=9, mfc="none",
                    mec=THEME["warning"], mew=1.4,
                    label=f"in mode {mode_pts[0][3]['mode_number']} (n_eff={mode_pts[0][3]['neff']:.4f}) of the port at that x")
            a1.legend(fontsize=7.5, facecolor="#1a1e25", edgecolor=THEME["border"], labelcolor=THEME["text"], loc="lower left")
        a1.set_xlabel("x (μm)")
        a1.set_ylabel(f"P / P({ref_name})")
        a1.set_title(f"Power through the planes along x, λ = {lam[k_freq]:.1f} nm", fontsize=10)
        a1.grid(alpha=0.25, color=THEME["grid_axis"])
        a1.set_ylim(bottom=0, top=max(1.1, max(rel) * 1.15))

        in_mode = {n: t for n, _, t, _ in mode_pts}
        rows = ["plane\tx_um\tP_over_P_first\tT_forward_mode1"] + [
            f"{n}\t{x:.4f}\t{r:.6g}\t{in_mode[n]:.6g}" if n in in_mode else f"{n}\t{x:.4f}\t{r:.6g}\t"
            for (n, x, _), r in zip(planes, rel)]
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

    def _plot_s_parameters(self, fig: Figure, k_freq: int):
        """|S_i1|^2 of every plane from the stored powers: P_i / P_source (all modes) and, where the
        plane has a stored mode, the power in that mode; S11 estimated from the power balance."""
        pref = self.model.power_reference()
        if pref is None:
            raise RuntimeError("The project stores no source power spectrum (P0 of the source) and no plane "
                               "at the source: S-parameters cannot be normalised.")
        lam = self.model.get_frequencies_and_wavelengths()[1]
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
            owners = self.model.plane_mode_owners(name)
            ex = self.model.plane_expansion(name, owners[0], 0) if owners else None
            if ex is not None and ex["T_forward"] is not None:
                tf = ex["T_forward"][order]
                a2.plot(lam_s, 10 * np.log10(np.clip(tf, 1e-12, None)), lw=1.8, color=color,
                        label=f"{name} → mode {ex['mode_number']} of {ex['owner']}")
                header += [f"|S|^2_mode_{name}", f"|S|_mode_{name}"]
                cols += [tf, np.sqrt(np.clip(tf, 0, None))]
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

    def _source_object(self, port_name):
        """The stored source of a port (its '<port> Mode Source', with time_signal), or None."""
        pt = next((p for p in self.model.ports if p["name"] == port_name), None)
        ob = pt.get("mode_src_obj") if pt else None
        return ob if ob is not None and "time" in ob.results and "time_signal" in ob.results else None

    def _plot_source_pulse(self, fig: Figure, port_name):
        """The stored time signal of the source of a port, and its stored P0 against λ."""
        src_ob = self._source_object(port_name)
        if src_ob is None:
            raise RuntimeError(f"No time signal of the source of {port_name} is stored in this .fsp file.")

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

    # ------------------------------------------------------------------
    # Results of One Object (named as in Lumerical)
    # ------------------------------------------------------------------
    def _legend(self, ax, **kw):
        if ax.get_legend_handles_labels()[0]:
            ax.legend(**dict(dict(fontsize=7.5, facecolor="#1a1e25", edgecolor=THEME["border"],
                                  labelcolor=THEME["text"], loc="best", framealpha=0.85), **kw))

    @staticmethod
    def _mirror_y(y, A):
        """A field recorded in a symmetric simulation (only y >= 0) on the whole y range."""
        if y[0] >= -1e-6 and len(y) > 1:
            return np.concatenate([-y[:0:-1], y]), np.concatenate([A[:, :0:-1], A], axis=1)
        return y, A

    @staticmethod
    def _zoom_to_field(axs, y, z, A, frac=1e-3, pad=0.6):
        """Show only the part of the plane where the field is (above frac of its peak, with a margin
        of pad um), when that is clearly smaller than the plane; the data stays whole."""
        rows, cols = np.nonzero(np.abs(A) > frac * float(np.abs(A).max()))
        if not rows.size:
            return
        y0, y1 = y[cols.min()] - pad, y[cols.max()] + pad
        z0, z1 = z[rows.min()] - pad, z[rows.max()] + pad
        if (y1 - y0) < 0.7 * (y[-1] - y[0]) or (z1 - z0) < 0.7 * (z[-1] - z[0]):
            for ax in axs:
                ax.set_xlim(max(y0, y[0]), min(y1, y[-1]))
                ax.set_ylim(max(z0, z[0]), min(z1, z[-1]))

    def _index_contour(self, ax, plane):
        """Outline the materials (stored refractive index) of the port at the plane's x."""
        owners = self.model.plane_mode_owners(plane)
        if not owners:
            return
        try:
            ys, zs, M, _, _ = self.model.stored_mode(owners[0])
            ax.contour(ys, zs, M["n"], levels=[1.2, 1.445, 1.7, 2.5], colors="w", linewidths=0.5, alpha=0.7)
        except Exception:
            pass

    def _plot_plane_field(self, fig: Figure, name, quantity, k):
        """E or H (|E|² and its components) or P (the Poynting vector) recorded in a y-z plane."""
        m = self.model
        x, y, z = m.plane_grid(name)
        lam = m.plane_wavelength(name, k)
        if quantity == "P":
            F = {c: m.plane_field(name, c, k) for c in ("Ex", "Ey", "Ez", "Hx", "Hy", "Hz")}
            s = m.h_scale(name)
            Hc = {c: np.conj(F[c]) / s for c in ("Hx", "Hy", "Hz")}
            P = {"Px": 0.5 * np.real(F["Ey"] * Hc["Hz"] - F["Ez"] * Hc["Hy"]),
                 "Py": 0.5 * np.real(F["Ez"] * Hc["Hx"] - F["Ex"] * Hc["Hz"]),
                 "Pz": 0.5 * np.real(F["Ex"] * Hc["Hy"] - F["Ey"] * Hc["Hx"])}
            big = max(float(np.abs(v).max()) for v in P.values()) or 1.0
            panels = [(c, v / big) for c, v in P.items()]
            axs = fig.subplots(1, 3, squeeze=False).ravel()
            cmap, vmin, vmax, cb_label = "coolwarm", -1.0, 1.0, "P / max |P|"
        else:
            comps = [c for c in (quantity + "x", quantity + "y", quantity + "z") if m.plane_has(name, c)]
            F2 = {c: np.abs(m.plane_field(name, c, k)) ** 2 for c in comps}
            total = sum(F2.values())
            big = float(total.max()) or 1.0
            panels = [(f"|{quantity}|²", total / big)] + [(f"|{c}|²", F2[c] / big) for c in comps]
            axs = fig.subplots(2, 2, squeeze=False).ravel()
            cmap, vmin, vmax, cb_label = "inferno", 0.0, 1.0, f"/ max |{quantity}|²"
        self._style_mpl_axes(list(axs))
        for ax in axs[len(panels):]:
            ax.set_visible(False)
        im, export = None, []
        for ax, (title, A) in zip(axs, panels):
            yy, AA = self._mirror_y(y, A)
            im = self._colormesh(ax, yy, z, AA, shading="auto", cmap=cmap, vmin=vmin, vmax=vmax)
            self._index_contour(ax, name)
            ax.set_xlim(yy[0], yy[-1])
            ax.set_ylim(z[0], z[-1])
            ax.set_aspect("equal")
            ax.set_title(title, fontsize=9.5)
            ax.set_xlabel("y (μm)")
            ax.set_ylabel("z (μm)")
            export.append((title.replace("|", "abs_").replace("²", "2"), AA))
        yy, _ = self._mirror_y(y, panels[0][1])
        extent = sum(np.abs(A) for _, A in panels)
        self._zoom_to_field(axs[:len(panels)], yy, z, self._mirror_y(y, extent)[1])
        cb = fig.colorbar(im, ax=list(axs[:len(panels)]), shrink=0.8)
        cb.set_label(cb_label, color=THEME["text"])
        cb.ax.yaxis.set_tick_params(color=THEME["text_muted"], labelcolor=THEME["text_muted"])
        fig.suptitle(f"{self._lum_name(name)} › {quantity}    λ = {lam:.1f} nm, x = {x:.4g} μm",
                     color=THEME["text"], fontsize=10.5, fontweight="bold")
        fig.set_layout_engine("constrained")
        yy, _ = self._mirror_y(y, panels[0][1])
        self._store_grid_export(f"{self._lum_name(name)} {quantity}, lambda={lam:.2f} nm", yy, z, export)

    def _plot_plane_T(self, fig: Figure, name, k):
        """T of a plane against λ, linear and in dB; dashed: the part in each stored mode of the port
        at the plane (T_forward)."""
        m = self.model
        T = m.plane_transmission(name)
        pref = m.power_reference()
        if T is None or pref is None:
            raise RuntimeError("The plane holds no power spectrum, or there is nothing to normalise it to.")
        lam = m.get_frequencies_and_wavelengths()[1]
        order = np.argsort(lam)
        lam_s = lam[order]
        a1, a2 = fig.subplots(1, 2)
        self._style_mpl_axes([a1, a2])
        header, cols = ["wavelength_nm", "T"], [T[order]]
        a1.plot(lam_s, T[order], lw=2, color=THEME["monitor_cyan"], label="T (all power through the plane)")
        a2.plot(lam_s, 10 * np.log10(np.clip(T[order], 1e-12, None)), lw=2, color=THEME["monitor_cyan"], label="T")
        colors = ["#ffd700", "#ff922b", "#c7a8ff", "#7ee2b0"]
        i = 0
        for owner in m.plane_mode_owners(name):
            for mode, (num, neff) in enumerate(m.stored_modes(owner)):
                ex = m.plane_expansion(name, owner, mode)
                if ex is None or ex["T_forward"] is None or not np.nanmax(ex["T_forward"]) > 1e-6:
                    continue
                tf = ex["T_forward"][order]
                lbl = f"in mode {num} of {owner} (n_eff={neff:.4f})"
                a1.plot(lam_s, tf, ls="--", lw=1.6, color=colors[i % 4], label=lbl)
                a2.plot(lam_s, 10 * np.log10(np.clip(tf, 1e-12, None)), ls="--", lw=1.6, color=colors[i % 4], label=lbl)
                header.append(f"T_forward_mode{num}_{owner}")
                cols.append(tf)
                i += 1
        for ax in (a1, a2):
            ax.axvline(lam[k], color=THEME["text_dim"], ls=":", lw=1)
            ax.set_xlabel("Wavelength λ (nm)")
            ax.grid(alpha=0.25, color=THEME["grid_axis"])
        a1.set_ylabel("T")
        a2.set_ylabel("T (dB)")
        self._legend(a1)
        norm = (f"T = P / {pref[1]}" if pref[2] else
                f"T = P / {pref[1]} (the power injected by the source; the .h5 fields are normalised to the source spectrum)")
        a1.set_title(norm if pref[2] else f"T = P / {pref[1]}", fontsize=9.5)
        a2.set_title("in dB", fontsize=9.5)
        fig.suptitle(f"{self._lum_name(name)} › T", color=THEME["text"], fontsize=10.5, fontweight="bold")
        fig.set_layout_engine("constrained")
        self._store_columns(f"{self._lum_name(name)} T ({norm})", lam_s, header, cols)

    def _plot_expansion(self, fig: Figure, name, owner, k):
        """T_forward (towards +x) and T_backward (towards -x) of each stored mode of `owner` in the
        field recorded in the plane `name`."""
        m = self.model
        lam = m.get_frequencies_and_wavelengths()[1]
        order = np.argsort(lam)
        lam_s = lam[order]
        a1, a2 = fig.subplots(1, 2)
        self._style_mpl_axes([a1, a2])
        header, cols = ["wavelength_nm"], []
        T = m.plane_transmission(name)
        if T is not None:
            a1.plot(lam_s, T[order], color=THEME["text_muted"], lw=1.2, ls=":", label="T (all power)")
        colors = ["#ffd700", "#ff922b", "#c7a8ff", "#7ee2b0"]
        for mode, (num, neff) in enumerate(m.stored_modes(owner)):
            ex = m.plane_expansion(name, owner, mode)
            if ex is None or ex["T_forward"] is None:
                continue
            tf, tb = ex["T_forward"][order], ex["T_backward"][order]
            c = colors[mode % 4]
            a1.plot(lam_s, tf, color=c, lw=1.8, label=f"T_forward, mode {num} (n_eff={neff:.4f})")
            a2.plot(lam_s, 10 * np.log10(np.clip(tf, 1e-10, None)), color=c, lw=1.8, label=f"T_forward, mode {num}")
            a2.plot(lam_s, 10 * np.log10(np.clip(tb, 1e-10, None)), color=c, lw=1.4, ls="--", label=f"T_backward, mode {num}")
            header += [f"T_forward_mode{num}", f"T_backward_mode{num}"]
            cols += [tf, tb]
        if not cols:
            raise RuntimeError(f"The field in {name} could not be expanded on the modes of {owner}.")
        for ax in (a1, a2):
            ax.axvline(lam[k], color=THEME["text_dim"], ls=":", lw=1)
            ax.set_xlabel("Wavelength λ (nm)")
            ax.grid(alpha=0.25, color=THEME["grid_axis"])
            self._legend(ax)
        a1.set_ylabel("T")
        a2.set_ylabel("T (dB)")
        a2.set_ylim(bottom=max(-100, a2.get_ylim()[0]))
        pref = m.power_reference()
        a1.set_title(f"Power in the modes / {pref[1] if pref else 'reference'}  (forward: +x)", fontsize=9.5)
        a2.set_title("in dB (dashed: towards −x)", fontsize=9.5)
        is_port = owner == name and any(pt["name"] == name for pt in m.ports)
        title = (f"{self._lum_name(name)} › expansion for port monitor" if is_port
                 else f"{name}: expansion on the modes of {self._lum_name(owner)}")
        fig.suptitle(title, color=THEME["text"], fontsize=10.5, fontweight="bold")
        fig.set_layout_engine("constrained")
        self._store_columns(title, lam_s, header, cols)

    def _plot_mode_profiles(self, fig: Figure, owner):
        """The stored refractive index and the |E|² of each stored mode of a port / mode monitor."""
        m = self.model
        modes = m.stored_modes(owner)[:4]
        axs = fig.subplots(1, 1 + len(modes), squeeze=False).ravel()
        self._style_mpl_axes(list(axs))
        y, z, M, _, mode_lam = m.stored_mode(owner, 0)
        im_n = self._colormesh(axs[0], y, z, M["n"], shading="auto", cmap="viridis")
        axs[0].set_title("index n", fontsize=9.5)
        export = [("n", M["n"])]
        im_e = None
        for j, (num, neff) in enumerate(modes, start=1):
            _, _, Mj, _, _ = m.stored_mode(owner, j - 1)
            E2 = sum(np.abs(Mj[f"E{c}"]) ** 2 for c in "xyz")
            E2 = E2 / max(float(E2.max()), 1e-300)
            im_e = self._colormesh(axs[j], y, z, E2, shading="auto", cmap="inferno", vmin=0.0, vmax=1.0)
            axs[j].contour(y, z, M["n"], levels=[1.2, 1.445, 1.7, 2.5], colors="w", linewidths=0.5, alpha=0.7)
            axs[j].set_title(f"mode {num}: |E|², n_eff = {neff:.4f}" if neff else f"mode {num}: |E|²", fontsize=9.5)
            export.append((f"E2_mode{num}", E2))
        self._zoom_to_field(axs, y, z, sum(A for name, A in export[1:]))
        for ax in axs:
            ax.set_aspect("equal")
            ax.set_xlabel("y (μm)")
        axs[0].set_ylabel("z (μm)")
        for im, ax, label in ((im_n, axs[0], "n"), (im_e, list(axs[1:]), "|E|² (norm.)")):
            if im is not None:
                cb = fig.colorbar(im, ax=ax, shrink=0.7)
                cb.set_label(label, color=THEME["text"])
                cb.ax.yaxis.set_tick_params(color=THEME["text_muted"], labelcolor=THEME["text_muted"])
        at = f" (calculated at λ = {mode_lam:.1f} nm)" if mode_lam else ""
        fig.suptitle(f"{self._lum_name(owner)} › mode profiles{at}", color=THEME["text"], fontsize=10.5, fontweight="bold")
        fig.set_layout_engine("constrained")
        self._store_grid_export(f"{self._lum_name(owner)} mode profiles", y, z, export)

    def _plot_mode_components(self, fig: Figure, owner):
        """|Ex| .. |Hz| of each stored mode (a row per mode), each normalised to the largest of E or H."""
        m = self.model
        modes = m.stored_modes(owner)[:3]
        axs = fig.subplots(len(modes), 6, squeeze=False)
        self._style_mpl_axes(axs)
        export, im, y, z = [], None, None, None
        for i, (num, neff) in enumerate(modes):
            y, z, M, _, _ = m.stored_mode(owner, i)
            for j, c in enumerate(("Ex", "Ey", "Ez", "Hx", "Hy", "Hz")):
                big = max(float(np.abs(M[c[0] + d]).max()) for d in "xyz") or 1.0
                A = np.abs(M[c]) / big
                im = self._colormesh(axs[i, j], y, z, A, shading="auto", cmap="magma", vmin=0.0, vmax=1.0)
                axs[i, j].set_title(f"|{c}|, mode {num}", fontsize=8.5)
                axs[i, j].set_aspect("equal")
                axs[i, j].tick_params(labelsize=7)
                export.append((f"abs_{c}_mode{num}", A))
            axs[i, 0].set_ylabel(f"n_eff={neff:.4f}\nz (μm)" if neff else "z (μm)", fontsize=8)
        self._zoom_to_field(axs.ravel(), y, z, sum(A for _, A in export))
        for ax in axs[-1]:
            ax.set_xlabel("y (μm)", fontsize=8)
        if im is not None:
            cb = fig.colorbar(im, ax=axs.ravel().tolist(), shrink=0.7)
            cb.set_label("/ max of E or H of the mode", color=THEME["text"])
            cb.ax.yaxis.set_tick_params(color=THEME["text_muted"], labelcolor=THEME["text_muted"])
        fig.suptitle(f"{self._lum_name(owner)} › mode profiles: field components", color=THEME["text"],
                     fontsize=10.5, fontweight="bold")
        fig.set_layout_engine("constrained")
        self._store_grid_export(f"{self._lum_name(owner)} mode field components", y, z, export)

    def _plot_monitor_2d(self, fig: Figure, mon, quantity, k):
        """|E|² or |H|² recorded by a monitor in a plane along the device (x-y or x-z): in dB and linear."""
        m = self.model
        g = m.h5[mon["group"]]
        comps = [c for c in (quantity + "x", quantity + "y", quantity + "z") if c in g]
        F2, lam = None, None
        for c in comps:
            a, lam = m.monitor_field(mon, c, k)
            F2 = np.abs(a) ** 2 if F2 is None else F2 + np.abs(a) ** 2
        u, v = m.monitor_grid(mon)
        step = max(1, len(u) // 3000)  # enough columns for the screen
        u_s, F2s = u[::step], F2[:, ::step]
        F2n = F2s / max(float(F2s.max()), 1e-300)
        dB = 10 * np.log10(F2n + 1e-12)
        axs = fig.subplots(2, 1)
        self._style_mpl_axes(list(axs))
        ims = [self._colormesh(axs[0], u_s, v, dB, shading="auto", cmap="inferno", vmin=-40, vmax=0, rasterized=True),
               self._colormesh(axs[1], u_s, v, F2n, shading="auto", cmap="inferno", vmin=0, vmax=1, rasterized=True)]
        if mon["u"] == "x":
            for ax in axs:
                for pt in m.ports:
                    ax.axvline(pt["x"], color=THEME["port_yellow"], ls="--", lw=1.0)
                for fm in m.field_monitors:
                    if fm["has_data"]:
                        ax.axvline(fm["x"], color=THEME["monitor_cyan"], ls=":", lw=0.8)
        for ax, im, label in zip(axs, ims, ("dB", "norm.")):
            ax.set_ylabel(f"{mon['v']} (μm)")
            cb = fig.colorbar(im, ax=ax, shrink=0.85)
            cb.set_label(label, color=THEME["text"])
            cb.ax.yaxis.set_tick_params(color=THEME["text_muted"], labelcolor=THEME["text_muted"])
        axs[1].set_xlabel(f"{mon['u']} (μm)")
        missing = [quantity + d for d in "xyz" if quantity + d not in g]
        note = f" (without {', '.join(missing)}: not recorded)" if missing else ""
        axs[0].set_title(f"|{quantity}|² in dB{note}", fontsize=9.5)
        axs[1].set_title(f"|{quantity}|² (linear)", fontsize=9.5)
        at = float(np.ravel(g[mon["normal"]][()])[0])
        at = at * 1e6 if abs(at) < 1e-2 and at else at
        title = f"{mon['name']} › {quantity}    λ = {lam:.1f} nm, {mon['normal']} = {at:.4g} μm" if lam else \
            f"{mon['name']} › {quantity}"
        f_mon = m.object_freqs(mon["obj"])
        lam_sel = m.get_frequencies_and_wavelengths()[1][k]
        other = lam is not None and abs(lam - lam_sel) > 0.05
        if other:  # the monitor did not record the chosen wavelength
            n = 1 if f_mon is None else f_mon.size
            title += (f"\nrecorded only at {lam:.1f} nm (the monitor has {n} frequency point{'s' if n > 1 else ''};"
                      f" chosen: {lam_sel:.1f} nm)")
        fig.suptitle(title, color=THEME["warning"] if other else THEME["text"], fontsize=10.5, fontweight="bold")
        fig.set_layout_engine("constrained")
        es = max(1, len(u) // 400)
        self._store_grid_export(f"{mon['name']} |{quantity}|^2 (dB)", u[::es], v,
                                [(f"{quantity}2_dB", 10 * np.log10(F2[:, ::es] / max(float(F2.max()), 1e-300) + 1e-12))],
                                u_name=f"{mon['u']}_um", v_name=f"{mon['v']}_um")

    def _plot_port_S(self, fig: Figure, k):
        """|S_ij|² of the port modes (j: the source port, its first mode): the power leaving through
        each port in each of its modes over the power the source injects, as Lumerical's S-matrix."""
        m = self.model
        src = m.source_port()
        got = m.port_outgoing(src["name"], 0) if src else None
        if got is None:
            raise RuntimeError("The source port has no stored mode or no recorded E and H fields.")
        incident = np.where(got[1] == 0, np.nan, got[1])
        j = m.ports.index(src) + 1
        lam = m.get_frequencies_and_wavelengths()[1]
        order = np.argsort(lam)
        lam_s = lam[order]
        a1, a2 = fig.subplots(1, 2)
        self._style_mpl_axes([a1, a2])
        header, cols, silent = ["wavelength_nm"], [], []
        colors = ["#ffd700", "#4c8dff", "#ff922b", "#7ee2b0", "#c7a8ff", "#ff6b6b"]
        n = 0
        for i, pt in enumerate(m.ports, start=1):
            for mode, (num, neff) in enumerate(m.stored_modes(pt["name"])):
                r = m.port_outgoing(pt["name"], mode)
                if r is None:
                    continue
                S2 = (r[0] / incident)[order]
                name = f"S{i}{j}" + (f" mode {num}" if len(m.stored_modes(pt["name"])) > 1 else "")
                header.append(f"|{name.replace(' ', '_')}|^2")
                cols.append(S2)
                if not np.nanmax(S2) > 1e-8:
                    silent.append(name)
                    continue
                c = colors[n % len(colors)]
                n += 1
                lbl = f"|{name}|²  ({pt['name']}, n_eff={neff:.4f})"
                a1.plot(lam_s, S2, color=c, lw=1.8, label=lbl)
                a2.plot(lam_s, 10 * np.log10(np.clip(S2, 1e-12, None)), color=c, lw=1.8, label=lbl)
        for ax in (a1, a2):
            ax.axvline(lam[k], color=THEME["text_dim"], ls=":", lw=1)
            ax.set_xlabel("Wavelength λ (nm)")
            ax.grid(alpha=0.25, color=THEME["grid_axis"])
            self._legend(ax)
        a1.set_ylabel("|S|²")
        a2.set_ylabel("|S|² (dB)")
        a1.set_title(f"source: {src['name']} (port {j}), mode {m.stored_modes(src['name'])[0][0]}", fontsize=9.5)
        a2.set_title("in dB" + (f"; below −80 dB, not drawn: {', '.join(silent)}" if silent else "")
                     + "\nS11 is found in the field at the source plane: below about −35 dB only approximate", fontsize=9)
        fig.suptitle(f"{self.PORTS_OBJECT} › S  (|S|² from the expansion of the port fields on the port modes)",
                     color=THEME["text"], fontsize=10.5, fontweight="bold")
        fig.set_layout_engine("constrained")
        self._store_columns(f"{self.PORTS_OBJECT} S", lam_s, header, cols)

    def _store_columns(self, title, lam_s, header, cols):
        """Columns against wavelength as a tab-separated table for the clipboard."""
        rows = [f"# {title}", "\t".join(["wavelength_nm"] + [h for h in header if h != "wavelength_nm"])]
        rows += ["\t".join([f"{lv:.3f}"] + [f"{c[i]:.6g}" for c in cols]) for i, lv in enumerate(lam_s)]
        self.last_exported_tsv = "\n".join(rows)
        self.last_exported_desc = f"{title} ({len(cols)} columns x {len(lam_s)} wavelengths)"

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
        self.report_failed_update()
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

    def report_failed_update(self):
        """Tell what the swap script wrote if it could not put a downloaded version in place."""
        path = os.path.expanduser(UPDATE_FAILED_FILE)
        try:
            with open(path, encoding="utf-8-sig") as f:
                text = f.read().strip()
            os.remove(path)
        except OSError:
            return
        if text:
            self.set_status("\u2717 The update was not put in place", "error")
            self.message(f"Update {APP_NAME}", text)

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
                result["latest"] = find_update()
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
        if FROZEN:
            remember_update_check()
            if is_newer(latest["version"], APP_VERSION):
                self.offer_package_update(latest, asked=True)
            else:
                self.message("Check for Updates", f"{APP_NAME} {APP_VERSION} is up to date.\n\n"
                                                  f"The newest release on GitHub is {latest['version']}.")
            return
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
        if FROZEN:
            self.offer_package_update(latest, asked)
            return
        config = load_config()
        installed = installed_copy(platform)
        if installed is None:  # a copy that is not installed
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

    def offer_download(self, latest, asked=False, why=""):
        """A newer version for a copy that cannot update itself (a Python copy that is not
        installed, or a packaged copy whose package is not in the release): offer the download
        page (of the release for a packaged copy, of the project for a Python copy)."""
        config = load_config()
        if not is_newer(latest["version"], APP_VERSION) or (
                not asked and config.get("skipped_version") == latest["version"]):
            return
        kind = packaged_kind()
        if FROZEN:
            how = ((why + "\n\n") if why else "") + (
                f"Download {release_asset_name(latest['version'], kind)} from the release page and install it."
                if kind else "Download the package for your system from the release page.")
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
            webbrowser.open(latest.get("page") or (RELEASES_PAGE if FROZEN else APP_WEBSITE))

    def offer_package_update(self, latest, asked=False):
        """A packaged copy (Windows .zip, macOS .dmg, Linux .deb / .rpm): offer the newer release,
        download the package made for this system and put it in place."""
        config = load_config()
        if not is_newer(latest["version"], APP_VERSION) or (
                not asked and config.get("skipped_version") == latest["version"]):
            return
        kind = packaged_kind()
        asset = release_asset_name(latest["version"], kind) if kind else None
        if asset is None or asset not in latest["assets"]:
            self.offer_download(latest, asked, why=(
                f"The release has no {asset} (yet)." if asset else
                "This copy was not installed from a release package, so it cannot update itself."))
            return
        url, size = latest["assets"][asset]
        how = {"zip": "The program closes, its folder is replaced by the new version, and it starts again.",
               "dmg": f"The program closes, {APP_NAME}.app is replaced by the new version, and it starts again.",
               }.get(kind, "The package is installed with the package manager, which asks for your password.")
        choice = AppDialog(
            self.root, f"Update {APP_NAME}?",
            f"A new version of {APP_NAME} is available.\n\n"
            f"    This version:  {APP_VERSION}\n"
            f"    New version:  {latest['version']}  ({asset}, {size / 1e6:.0f} MB)\n\n"
            f"Download and install it now? {how}",
            [("update", "Update now"), ("later", "Not now"), ("skip", "Skip this version")],
            note=f"From the release {latest['tag']} on github.com/{UPDATE_REPOSITORY}. "
                 "Help > Check for Updates looks for a new version at any time.",
            icon=self.dialog_icon(), checkbox=self._weekly_checkbox(config.get("check_updates", True))).show()
        config = load_config()
        if self.weekly is not None and choice is not None:
            config["check_updates"] = bool(self.weekly.get())
        if choice == "skip":
            config["skipped_version"] = latest["version"]
        save_config(config)
        if choice == "update":
            self._download_package(latest, kind, asset, url, size)

    def _download_package(self, latest, kind, asset, url, size):
        """Download (and for zip / dmg unpack) the package in a background thread, with the progress
        in the status bar; then finish the update in _finish_package_update."""
        if getattr(self, "_updating", False):
            return
        self._updating = True
        folder = download_folder() if kind in ("deb", "rpm") else tempfile.mkdtemp(prefix=f"{APP_SLUG}-")
        path = os.path.join(folder, asset)
        state = {"done": 0, "total": size}

        def progress(done, total):
            state["done"], state["total"] = done, total

        def work():  # background thread: no Tk calls here
            try:
                download_file(url, path, progress, size)
                if kind == "zip":
                    state["prepare"] = prepare_windows_swap
                elif kind == "dmg":
                    state["prepare"] = prepare_mac_swap
                state["ok"] = True
            except Exception as err:
                state["error"] = err

        gc.collect()  # see check_for_update: Tk objects must not be freed in the thread
        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        self.root.configure(cursor="watch")

        def wait():
            if thread.is_alive():
                done, total = state["done"], state["total"]
                share = f"{100 * done / total:.0f} %, " if total else ""
                self.set_status(f"\u23f3 Downloading {asset}: {share}{done / 1e6:.0f}"
                                + (f" of {total / 1e6:.0f}" if total else "") + " MB\u2026", "busy")
                self.root.after(250, wait)
                return
            self.root.configure(cursor="")
            self._updating = False
            if "error" in state:
                self.set_status(f"\u2717 The update to {latest['version']} failed", "error")
                if self.message(f"Update {APP_NAME}", f"The update was not installed.\n\n{state['error']}",
                                [("open", "Open Download Page"), ("ok", "OK")]) == "open":
                    webbrowser.open(latest.get("page") or RELEASES_PAGE)
                return
            self.set_status(f"\u2713 Downloaded {asset}", "ok")
            self._finish_package_update(latest, kind, path, state.get("prepare"))

        self.root.after(250, wait)

    def _open_files(self):
        """The files that are open now (to be opened again after a restart)."""
        if self.model is None:
            return []
        return [self.model.fsp_path] + ([self.model.h5_path] if self.model.h5_path else [])

    def _finish_package_update(self, latest, kind, path, prepare):
        version = latest["version"]
        if kind in ("deb", "rpm"):
            with self.busy(f"Installing {os.path.basename(path)}"):
                ok, message = install_linux_package(kind, path)
            if not ok:
                self.set_status(f"\u2717 {APP_NAME} {version} was not installed", "error")
                self.message(f"Update {APP_NAME}", f"{message}\n\nThe package is kept in {os.path.dirname(path)}.")
                return
            self.set_status(f"\u2713 {APP_NAME} {version} is installed", "ok")
            if self.message(f"Restart {APP_NAME}?",
                            f"{APP_NAME} {version} is installed.\n\nRestart now to use it? "
                            "The file that is open now is opened again.",
                            [("restart", "Restart now"), ("later", "Later")]) == "restart":
                self.restart()
            return
        choice = self.message(
            f"Restart {APP_NAME}?",
            f"{APP_NAME} {version} is downloaded.\n\n{APP_NAME} must close to put the new version in "
            "place. Restart now? The file that is open now is opened again.",
            [("restart", "Restart now"), ("exit", "When I quit"), ("cancel", "Cancel")],
            note="When I quit: the new version is put in place when you close the program, and runs at the next start.")
        if choice not in ("restart", "exit"):
            return
        try:
            with self.busy("Preparing the update"):
                cmd = prepare(path, self._open_files(), relaunch=choice == "restart")
                start_detached(cmd)
            shutil.rmtree(os.path.dirname(path), ignore_errors=True)  # the download (unpacked by now)
        except Exception as err:
            reveal = kind == "dmg"
            text = f"The update could not be prepared.\n\n{err}"
            if reveal:
                text += f"\n\nThe disk image is opened in Finder: drag {APP_NAME} to Applications to install it."
            self.message(f"Update {APP_NAME}", text)
            if reveal:
                subprocess.run(["open", path], check=False)
            else:
                webbrowser.open(latest.get("page") or RELEASES_PAGE)
            return
        if choice == "restart":
            self.root.destroy()  # the script waits for this process to end, then starts the new version
        else:
            self.set_status(f"\u2713 {APP_NAME} {version} is put in place when you quit", "ok")

    def restart(self, script=None):
        """Close the window and run `script` (the updated program; a packaged program runs itself
        again) in place of this process, with the files that are open now."""
        if FROZEN:
            args = _restart_args(self._open_files())
            self.root.destroy()
            os.execve(args[0], args, dict(os.environ, **RELAUNCH_ENV))
        args = [sys.executable, script] + self._open_files()
        self.root.destroy()
        os.execv(sys.executable, args)

    def show_about(self):
        kind = packaged_kind()
        if FROZEN:
            where = {"zip": "Windows application", "dmg": "macOS application",
                     "deb": "Linux package (.deb)", "rpm": "Linux package (.rpm)"}.get(kind, "Packaged application")
            where += "; updates from the GitHub releases"
        else:
            installed = installed_copy()
            where = (f"Installed version: {installed['version']}" if installed else "Not installed on this computer")
            where += "; updates from the main branch on GitHub"
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

    def copy_picture_to_clipboard(self, fig):
        """Copy the plot (as shown, at PICTURE_DPI) to the clipboard as a picture."""
        if self.model is None or fig is None or not fig.axes:
            messagebox.showinfo("No Plot to Copy", "Please open an .fsp file and plot a result first.")
            return
        try:
            with self.busy("Copying the picture"):
                png = figure_png(fig)
                copy_png_to_clipboard(png)
        except Exception as err:
            self.set_status(f"\u2717 The picture could not be copied: {err}", "error")
            return
        w, h = (int(round(v * PICTURE_DPI)) for v in fig.get_size_inches())
        self.set_status(f"\u2713 Picture copied to the clipboard ({w} \u00d7 {h} pixels): paste it into a document", "ok")


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
        if FROZEN:
            print(f"This is the packaged {APP_NAME}: it is installed from its package and updates itself "
                  "(Help > Check for Updates).")
            return 1
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
    enable_windows_dpi_awareness()  # sharp text on scaled displays (the program scales itself)
    root = tk.Tk(className=APP_CLASS)
    app = FSPGuiApp(root, initial_fsp=fsp_arg, initial_h5=h5_arg)
    root.after(100, lambda: bring_to_front(root))
    root.after(600, app.startup_check)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
