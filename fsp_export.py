#!/usr/bin/env python3
"""
fsp_export.py - save the geometry of a Lumerical model as a GDSII file or as a Python
script that builds the same layout with gdsfactory.

It works on the structure items of fsp_gui.FSPModelData (the shapes the viewer draws):

    from fsp_gui import FSPModelData
    import fsp_export
    model = FSPModelData("model.fsp")
    shapes = fsp_export.export_shapes(model.structures)
    fsp_export.write_gds("model.gds", shapes, cell="model")
    open("model_geometry.py", "w").write(fsp_export.gdsfactory_script(shapes, "model", "model.fsp"))

or from the command line:

    python fsp_export.py model.fsp model.gds            # GDSII
    python fsp_export.py model.fsp model_geometry.py    # gdsfactory script

Layers. Shapes of a layer builder keep their GDS layer numbers (e.g. 30:0), and its
unpatterned background slabs are left out, as they are not part of the layout. Primitive
shapes (rectangles, polygons) get one GDS layer per material and z range, numbered from 1
in the order of the model tree, so e.g. two silicon tapers at the same height share a layer.

Script. Every outline is cut into gdsfactory rectangles and tapers where that reproduces it
exactly: a waveguide with tapered ends becomes rectangle + taper pieces. Only an outline
that cannot be resolved this way (or whose taper widths are off gdsfactory's 2 nm grid) is
written as a polygon.

All lengths are in micrometres. The GDS files use a 1 um user unit and a 1 nm grid.
"""

import os
import re
import struct
import sys
import time

import numpy as np

from fsp_reader import _gds_real8, _gds_rec

TOL = 1e-6      # um: coordinates closer than this are the same (1 pm)
GRID = 1e-3     # um: database unit of the GDS file and of gdsfactory (1 nm)


# ---------------------------------------------------------------- shapes and layers
def _identifier(text, fallback="X"):
    name = re.sub(r"[^A-Za-z0-9]+", "_", str(text)).strip("_")
    if not name:
        name = fallback
    return ("_" + name) if name[0].isdigit() else name


def clean_polygon(points):
    """Nx2 array in um without the closing point, repeated points and points on a straight edge."""
    pts = [(round(float(x), 6), round(float(y), 6)) for x, y in points]
    out = []
    for p in pts:
        if not out or abs(p[0] - out[-1][0]) > TOL or abs(p[1] - out[-1][1]) > TOL:
            out.append(p)
    if len(out) > 1 and abs(out[0][0] - out[-1][0]) <= TOL and abs(out[0][1] - out[-1][1]) <= TOL:
        out.pop()
    changed = True
    while changed and len(out) > 3:
        changed = False
        for i in range(len(out)):
            (ax, ay), (bx, by), (cx, cy) = out[i - 1], out[i], out[(i + 1) % len(out)]
            cross = (bx - ax) * (cy - by) - (by - ay) * (cx - bx)
            scale = max(abs(bx - ax) + abs(by - ay), abs(cx - bx) + abs(cy - by), 1e-12)
            if abs(cross) / scale <= TOL:
                del out[i]
                changed = True
                break
    return np.array(out, dtype=float).reshape(-1, 2)


def export_shapes(structures):
    """The shapes to save, from the structure items of fsp_gui.FSPModelData.

    Returns a list of dicts: name, material, z (zmin, zmax), layer (number, datatype),
    layer_name (a Python identifier such as 'SI') and points (Nx2 array, um).
    """
    from_layer_builder = any(st.get("gds_layer") for st in structures)
    shapes, layers, used_names = [], {}, set()

    def layer_name(text):
        base = _identifier(text, "LAYER").upper()
        name, k = base, 2
        while name in used_names:
            name, k = f"{base}_{k}", k + 1
        used_names.add(name)
        return name

    for st in structures:
        points = clean_polygon(st["xy_poly"])
        if len(points) < 3:
            continue
        gds_layer = st.get("gds_layer") or ""
        if from_layer_builder:
            if not gds_layer:
                continue  # a background slab of the process stack: not in the layout
            number, _, datatype = gds_layer.partition(":")
            key = (int(number), int(datatype or 0))
            label = st.get("layer_name") or f"L{key[0]}_{key[1]}"
        else:
            key = (st.get("material"), round(st["zmin"], 6), round(st["zmax"], 6))
            material = str(st.get("material") or "")
            # an object-defined index ("n=3.35") is no name: use the name of the object
            label = st["name"] if (not material or material.startswith("n=")) else material
        if key not in layers:
            number = key if from_layer_builder else (len(layers) + 1, 0)
            layers[key] = (number, layer_name(label))
        number, name = layers[key]
        shapes.append(dict(name=st["name"], material=st.get("material") or "?",
                           z=(float(st["zmin"]), float(st["zmax"])), layer=number, layer_name=name,
                           points=points))
    return shapes


def layer_table(shapes):
    """[(layer, layer_name, description)] in the order of first use."""
    table, seen = [], set()
    for sh in shapes:
        if sh["layer"] in seen:
            continue
        seen.add(sh["layer"])
        names = [s["name"] for s in shapes if s["layer"] == sh["layer"]]
        text = f"{sh['material']}, z {sh['z'][0]:g} .. {sh['z'][1]:g} um: {', '.join(names)}"
        table.append((sh["layer"], sh["layer_name"], text))
    return table


# ---------------------------------------------------------------- GDSII
def write_gds(path, shapes, cell="TOP", libname="LUMERICAL_EXPORT"):
    """Write the outlines as GDSII boundaries (user unit 1 um, database unit 1 nm)."""
    if not shapes:
        raise ValueError("there is no geometry to save")
    now = time.localtime()
    date = struct.pack(">12h", *([now.tm_year, now.tm_mon, now.tm_mday, now.tm_hour, now.tm_min, now.tm_sec] * 2))
    cell = (re.sub(r"[^A-Za-z0-9_$?]", "_", cell) or "TOP")[:32]
    out = [_gds_rec(0x00, 0x02, struct.pack(">h", 600)),
           _gds_rec(0x01, 0x02, date),
           _gds_rec(0x02, 0x06, libname.encode()),
           _gds_rec(0x03, 0x05, _gds_real8(GRID) + _gds_real8(GRID * 1e-6)),
           _gds_rec(0x05, 0x02, date),
           _gds_rec(0x06, 0x06, cell.encode())]
    for sh in shapes:
        xy = np.rint(sh["points"] / GRID).astype(np.int64)
        xy = np.vstack([xy, xy[:1]])  # GDSII boundaries are closed
        out += [_gds_rec(0x08, 0x00),
                _gds_rec(0x0D, 0x02, struct.pack(">h", sh["layer"][0])),
                _gds_rec(0x0E, 0x02, struct.pack(">h", sh["layer"][1])),
                _gds_rec(0x10, 0x03, struct.pack(f">{xy.size}i", *xy.ravel())),
                _gds_rec(0x11, 0x00)]
    out += [_gds_rec(0x07, 0x00), _gds_rec(0x04, 0x00)]
    with open(path, "wb") as fh:
        fh.write(b"".join(out))


# ---------------------------------------------------------------- rectangles and tapers
def _cut(points, axis):
    """Cut a polygon into trapezoids with two sides parallel to the other axis.

    Returns [(a0, a1, lo0, hi0, lo1, hi1)]: the piece between a0 and a1 along `axis` spans
    lo0..hi0 across at a0 and lo1..hi1 at a1. None if the outline cannot be cut (it crosses itself).
    """
    P = points if axis == 0 else points[:, ::-1]
    cuts = []
    for v in sorted(P[:, 0]):
        if not cuts or v - cuts[-1] > TOL:
            cuts.append(v)
    edges = [(P[i], P[(i + 1) % len(P)]) for i in range(len(P))]
    pieces = []
    for a0, a1 in zip(cuts[:-1], cuts[1:]):
        spanning = []
        for p, q in edges:
            if p[0] > q[0]:
                p, q = q, p
            if q[0] - p[0] > TOL and p[0] <= a0 + TOL and q[0] >= a1 - TOL:
                slope = (q[1] - p[1]) / (q[0] - p[0])
                b0, b1 = p[1] + slope * (a0 - p[0]), p[1] + slope * (a1 - p[0])
                spanning.append((b0 + b1, b0, b1))
        spanning.sort()
        if len(spanning) % 2:
            return None
        for lo, hi in zip(spanning[::2], spanning[1::2]):
            if hi[1] - lo[1] > TOL or hi[2] - lo[2] > TOL:
                pieces.append((a0, a1, lo[1], hi[1], lo[2], hi[2]))
    return pieces


def _classify(pieces, axis):
    """Turn trapezoids into ('rect', xmin, ymin, xmax, ymax), ('taper', x, y, length, width1,
    width2, rotation) or ('other', points); adjacent rectangles of the same width are joined."""
    out = []
    for a0, a1, lo0, hi0, lo1, hi1 in pieces:
        w0, w1, c0, c1 = hi0 - lo0, hi1 - lo1, (hi0 + lo0) / 2, (hi1 + lo1) / 2
        if abs(c0 - c1) > TOL:
            quad = [(a0, lo0), (a1, lo1), (a1, hi1), (a0, hi0)]
            out.append(("other", [(u, v) if axis == 0 else (v, u) for u, v in quad]))
        elif abs(w0 - w1) <= TOL:
            last = out[-1] if out else None
            if last and last[0] == "rect" and abs(last[2] - a0) <= TOL and abs(last[3] - lo0) <= TOL \
                    and abs(last[4] - hi0) <= TOL:
                out[-1] = ("rect", last[1], a1, lo0, hi0)
            else:
                out.append(("rect", a0, a1, lo0, hi0))
        else:
            out.append(("taper", a0, c0, a1 - a0, w0, w1))
    result = []
    for item in out:
        if item[0] == "rect":  # stored as (a0, a1, lo, hi) while joining
            _, a0, a1, lo, hi = item
            result.append(("rect", a0, lo, a1, hi) if axis == 0 else ("rect", lo, a0, hi, a1))
        elif item[0] == "taper":
            _, a0, c, length, w0, w1 = item
            result.append(("taper", a0, c, length, w0, w1, 0) if axis == 0 else ("taper", c, a0, length, w0, w1, 90))
        else:
            result.append(item)
    return result


def _on_taper_grid(width):
    """gdsfactory tapers need widths that are a positive, even number of nanometres."""
    n = width / GRID
    return n > 0.5 and abs(n - round(n)) < 1e-3 and round(n) % 2 == 0


def decompose(points):
    """Resolve an outline into rectangles and tapers.

    Returns a list of ('rect', xmin, ymin, xmax, ymax) and ('taper', x, y, length, width1,
    width2, rotation) items - a taper starts at (x, y) with width1 and runs `length` along +x
    (rotation 0) or +y (rotation 90) to width2 - or None if that cannot reproduce the outline
    exactly; it is then kept as a polygon.
    """
    points = clean_polygon(points)
    if len(points) < 3:
        return None
    best = None
    for axis in (0, 1):
        pieces = _cut(points, axis)
        if not pieces:
            continue
        items = _classify(pieces, axis)
        ok = all(it[0] == "rect" or (it[0] == "taper" and _on_taper_grid(it[4]) and _on_taper_grid(it[5]))
                 for it in items)
        if ok and (best is None or len(items) < len(best)):
            best = items
    return best


# ---------------------------------------------------------------- gdsfactory script
def _num(v):
    text = f"{round(float(v), 6):.6f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


SCRIPT_HEAD = '''#!/usr/bin/env python3
"""
Geometry of {source}, written by fsp_export.py ({date}).

Run this file to build the layout with gdsfactory and save it as {gds_name}:

    pip install gdsfactory
    python {script_name}

All lengths are in micrometres.
"""
from pathlib import Path

import gdsfactory as gf

# GDS layers ({layer_note})
{layers}


def rectangle(c, xmin, ymin, xmax, ymax, layer):
    """Add a gdsfactory rectangle with the corners (xmin, ymin) and (xmax, ymax)."""
    ref = c << gf.components.rectangle(size=(xmax - xmin, ymax - ymin), layer=layer, centered=False, port_type=None)
    ref.move((xmin, ymin))
    return ref


def taper(c, x, y, length, width1, width2, layer, rotation=0):
    """Add a gdsfactory taper: it starts at (x, y) with width1 and ends `length` further
    with width2, along +x (rotation=0) or +y (rotation=90)."""
    ref = c << gf.components.taper(length=length, width1=width1, width2=width2, layer=layer)
    if rotation:
        ref.rotate(rotation)
    ref.move((x, y))
    return ref


@gf.cell
def {function}():
    c = gf.Component()
'''

SCRIPT_TAIL = '''    return c


if __name__ == "__main__":
    component = {function}()
    gds_file = Path(__file__).with_suffix(".gds")
    component.write_gds(gds_file)
    print(f"wrote {{gds_file}}")
    # component.show()  # open the layout in KLayout (needs the klive plugin)
'''


def gdsfactory_script(shapes, cell="model", source="model.fsp", script_name="model_geometry.py"):
    """Python source that rebuilds the shapes with gdsfactory, from rectangles and tapers
    wherever they reproduce an outline exactly and from polygons otherwise."""
    if not shapes:
        raise ValueError("there is no geometry to save")
    function = _identifier(cell, "model").lower()
    if function in ("rectangle", "taper", "gf", "c"):
        function += "_geometry"
    table = layer_table(shapes)
    width = max(len(name) for _, name, _ in table) + len("LAYER_")
    layer_lines = [f"{'LAYER_' + name:<{width}} = ({layer[0]}, {layer[1]})  # {text}" for layer, name, text in table]
    numbered = all(layer == (i + 1, 0) for i, (layer, _, _) in enumerate(table))
    out = [SCRIPT_HEAD.format(
        source=source, date=time.strftime("%Y-%m-%d %H:%M"), script_name=script_name,
        gds_name=os.path.splitext(script_name)[0] + ".gds", function=function, layers="\n".join(layer_lines),
        layer_note="one per material and z range of the model" if numbered else "the layer numbers of the model")]
    for sh in shapes:
        layer = "LAYER_" + sh["layer_name"]
        items = decompose(sh["points"])
        n = len(sh["points"])
        out.append(f"\n    # {sh['name']}: {sh['material']}, z {sh['z'][0]:g} .. {sh['z'][1]:g} um\n")
        if items is None:
            out.append(f"    # outline with {n} vertices that rectangles and tapers cannot reproduce: a polygon\n")
            pts = ", ".join(f"({_num(x)}, {_num(y)})" for x, y in sh["points"])
            out.append(f"    c.add_polygon([{pts}], layer={layer})\n")
            continue
        for it in items:
            if it[0] == "rect":
                out.append(f"    rectangle(c, {_num(it[1])}, {_num(it[2])}, {_num(it[3])}, {_num(it[4])}, {layer})\n")
            else:
                _, x, y, length, w1, w2, rotation = it
                rot = f", rotation={rotation}" if rotation else ""
                out.append(f"    taper(c, {_num(x)}, {_num(y)}, length={_num(length)}, width1={_num(w1)}, "
                           f"width2={_num(w2)}, layer={layer}{rot})\n")
    out.append("\n" + SCRIPT_TAIL.format(function=function))
    return "".join(out)


def summary(shapes):
    """One line per shape: how the script builds it."""
    lines = []
    for sh in shapes:
        items = decompose(sh["points"])
        if items is None:
            how = f"polygon ({len(sh['points'])} vertices)"
        else:
            r, t = sum(it[0] == "rect" for it in items), sum(it[0] == "taper" for it in items)
            how = " + ".join(f"{k} {word}{'s' if k > 1 else ''}" for k, word in ((r, "rectangle"), (t, "taper")) if k)
        lines.append(f"{sh['layer'][0]}:{sh['layer'][1]:<3} {sh['name']:<24} {how}")
    return lines


def save(structures, path, source="model.fsp"):
    """Save the geometry to `path`: a gdsfactory script if it ends in .py, else a GDSII file.
    Returns the saved shapes."""
    shapes = export_shapes(structures)
    cell = os.path.splitext(os.path.basename(source))[0] or "model"
    if path.lower().endswith(".py"):
        text = gdsfactory_script(shapes, cell, os.path.basename(source), os.path.basename(path))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        write_gds(path, shapes, cell)
    return shapes


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    from fsp_gui import FSPModelData
    model = FSPModelData(sys.argv[1])
    shapes = save(model.structures, sys.argv[2], sys.argv[1])
    print(f"wrote {sys.argv[2]}: {len(shapes)} shapes on {len(layer_table(shapes))} layers")
    for layer, name, text in layer_table(shapes):
        print(f"  {layer[0]}:{layer[1]}  {name}: {text}")
    if sys.argv[2].lower().endswith(".py"):
        for line in summary(shapes):
            print("  " + line)


if __name__ == "__main__":
    main()
