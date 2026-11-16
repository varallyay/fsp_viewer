#!/usr/bin/env python3
"""
fsp_reader.py - read Ansys Lumerical FDTD project files (.fsp) without Lumerical.

Reverse-engineered from "LUMERICAL file version 1.1" files (2024-2026 releases).
No official specification exists, so treat everything here as best-effort.

File layout (all integers little-endian int32, floats float64):

  header      "LUMERICAL file version 1.1\\0", a few int32s, an (empty) table of contents
  materials   "Lumerical material data file version 2.0\\0", int32 n_materials,
              then for each material: a property block
  object tree one node per object (the root is "::model")

  property block  = int32 n_props, then n_props records
  record          = int32 name_len (incl. \\0), name, int32 type, value
     type 0  string          int32 len, bytes
     type 1  double          int32 count (=1), float64
     type 2  int             int32 count (=1), int32
     type 3  double matrix   matrix body, float64 data
     type 5  int32 matrix    matrix body, int32 data
  matrix body     = int32 label_len, label, int32 n_elements, int32 n_dims, dims...,
                    int32 is_real, int32 (unknown, usually 1), data in column-major order;
                    complex data is stored as all real parts followed by all imaginary parts
  node (tag 1001) = int32 1001, int32 38, "{uuid}", property block,
                    int32 n_results, n_results x (named matrix body, float64),
                    int32 n_children, children...
  node (tag 1000) = like 1001 but with an undecoded Qt-serialised blob before the
                    property block and a different results section (layer-builder
                    internals, primitive shapes). Properties are still recovered.

Usage:
  python fsp_reader.py model.fsp                 # object list
  python fsp_reader.py model.fsp --summary       # human-readable model settings
  python fsp_reader.py model.fsp --props FDTD    # every property of matching objects
  python fsp_reader.py model.fsp --json out.json # everything (large arrays summarised)
  python fsp_reader.py model.fsp --npz out.npz   # every array, full size
  python fsp_reader.py model.fsp --gds out.gds   # layer-builder polygons as GDSII
"""
import argparse, json, re, struct, sys
import numpy as np

C0 = 299792458.0


class FormatError(Exception):
    pass


class FSPObject:
    def __init__(self, offset, tag, uuid, props, results, blob=b""):
        self.offset, self.tag, self.uuid, self.blob = offset, tag, uuid, blob
        self.props, self.results = props, results

    @property
    def name(self):
        return self.props.get("name") or f"<unnamed @0x{self.offset:x}>"

    def __getitem__(self, key):
        return self.props[key]

    def get(self, key, default=None):
        return self.props.get(key, default)

    def __repr__(self):
        return f"FSPObject({self.name!r}, {len(self.props)} props, {len(self.results)} results)"


class FSPFile:
    NODE_RE = re.compile(rb"[\xe8\xe9]\x03\x00\x00\x26\x00\x00\x00\{[0-9a-f-]{36}\}")

    def __init__(self, path):
        with open(path, "rb") as fh:
            self.data = fh.read()
        d = self.data
        if not d.startswith(b"LUMERICAL file version"):
            raise FormatError("not a Lumerical file (bad magic)")
        self.version = d[: d.index(b"\0")].decode()
        self.materials = self._read_materials()
        self.objects = self._read_objects()

    # ---------- low level ----------
    def _i32(self, o):
        return struct.unpack_from("<i", self.data, o)[0]

    def _matrix_body(self, p, dtype="<f8"):
        d = self.data
        label_len = self._i32(p); p += 4
        label = d[p:p + label_len].rstrip(b"\0").decode("latin1"); p += label_len
        total, ndim = struct.unpack_from("<2i", d, p)
        if not (0 <= total < 5e8 and 0 <= ndim < 8):
            raise FormatError(f"bad matrix header at 0x{p:x}")
        dims = struct.unpack_from(f"<{ndim}i", d, p + 8)
        is_real, _ = struct.unpack_from("<2i", d, p + 8 + 4 * ndim)
        p += 4 * (4 + ndim)
        n = total * (1 if is_real or dtype == "<i4" else 2)
        arr = np.frombuffer(d, dtype=dtype, count=n, offset=p).copy()
        p += arr.itemsize * n
        if dtype == "<f8" and not is_real:
            c = np.empty(total, dtype=complex)
            c.real, c.imag = arr[:total], arr[total:]  # avoids NaN warnings from uninitialised values
            arr = c
        if total:
            arr = arr.reshape(dims, order="F")
        return label, arr, p

    def _record(self, o):
        d = self.data
        name_len = self._i32(o)
        if not 0 < name_len < 300:
            raise FormatError(f"bad name length at 0x{o:x}")
        raw = d[o + 4:o + 4 + name_len]
        if raw[-1] != 0 or not all(32 <= b < 127 for b in raw[:-1]):
            raise FormatError(f"bad name at 0x{o:x}")
        name = raw[:-1].decode()
        p = o + 4 + name_len
        typ = self._i32(p); p += 4
        if typ == 0:
            n = self._i32(p)
            val = d[p + 4:p + 4 + n].rstrip(b"\0").decode("latin1"); p += 4 + n
        elif typ == 1:
            val = struct.unpack_from("<d", d, p + 4)[0]; p += 12
        elif typ == 2:
            val = self._i32(p + 4); p += 8
        elif typ == 3:
            _, val, p = self._matrix_body(p, "<f8")
        elif typ == 5:
            _, val, p = self._matrix_body(p, "<i4")
        elif typ == 11:  # raw byte array (e.g. cached polygon decomposition)
            n = self._i32(p)
            val = d[p + 4:p + 4 + n]; p += 4 + n
        else:
            raise FormatError(f"unknown record type {typ} for {name!r} at 0x{o:x}")
        return name, val, p

    def _props(self, o):
        n = self._i32(o)
        if not 0 <= n < 5000:
            raise FormatError(f"bad property count at 0x{o:x}")
        o += 4
        props = {}
        for _ in range(n):
            k, v, o = self._record(o)
            props[k] = v
        return props, o

    # ---------- sections ----------
    def _read_materials(self):
        tag = b"Lumerical material data file version 2.0\0"
        o = self.data.find(tag)
        if o < 0:
            return []
        o += len(tag)
        n = self._i32(o); o += 4
        mats = []
        for _ in range(n):
            props, o = self._props(o)
            mats.append(props)
        self._materials_end = o
        return mats

    def _read_objects(self):
        objs = []
        for m in self.NODE_RE.finditer(self.data):
            o = m.start()
            tag = self._i32(o)
            uuid = m.group()[8:].decode()
            start = o + 8 + 38
            props = results = None
            if tag == 1001:
                try:
                    props, end = self._props(start)
                    results = {}
                    nres = self._i32(end); end += 4
                    for _ in range(nres):
                        label, arr, end = self._matrix_body(end)
                        results[label] = arr
                except FormatError:
                    if props is None:
                        continue
                    results = {}
            else:  # tag 1000: skip the undecoded blob, find the property block
                for q in range(start, min(start + 200000, len(self.data) - 4)):
                    n = self._i32(q)
                    if 3 < n < 2000:
                        try:
                            props, _ = self._props(q)
                            break
                        except FormatError:
                            pass
                if props is None:
                    continue
                results = {}
                blob = self.data[start:q]
                objs.append(FSPObject(o, tag, uuid, props, results, blob))
                continue
            objs.append(FSPObject(o, tag, uuid, props, results))
        return objs

    # ---------- convenience ----------
    def find(self, pattern):
        return [ob for ob in self.objects if pattern in ob.name]

    def get(self, name):
        for ob in self.objects:
            if ob.name == name or ob.name.endswith("::" + name):
                return ob
        raise KeyError(name)

    def material(self, name):
        for m in self.materials:
            if m.get("name") == name:
                return m
        raise KeyError(name)

    def material_index(self, name):
        """Refractive index (3 values: x, y, z) of a material, else None.

        Constant-permittivity materials (type 9) give their index. Sampled-data materials
        (a table of permittivity over frequency, such as "InP - Palik") give the table value
        interpolated at the centre frequency of the simulation; this is the measured index,
        not the fit the solver uses.
        """
        m = self.material(name)
        eps = m.get("permittivity")
        if isinstance(eps, np.ndarray) and eps.shape == (3, 3) and m.get("type") == 9:
            return np.sqrt(np.diag(eps))
        freq = m.get("frequency")
        if isinstance(eps, np.ndarray) and isinstance(freq, np.ndarray) and eps.size == freq.size >= 2:
            try:
                f0 = float(self.get("::model::FDTD").props["globalFrequency"])
            except (KeyError, TypeError, ValueError):
                return None
            freq, eps = freq.ravel().real, eps.ravel()
            order = np.argsort(freq)
            freq, eps = freq[order], eps[order]
            if not freq[0] <= f0 <= freq[-1]:
                return None
            n = np.sqrt(np.interp(f0, freq, eps.real) + 1j * np.interp(f0, freq, eps.imag))
            return np.full(3, n)
        return None


# ---------------------------------------------------------------- output helpers
def _fmt(v, n=6):
    if isinstance(v, np.ndarray):
        if v.size <= n:
            return np.array2string(v.ravel(), precision=6)
        return f"array{v.shape} {np.array2string(v.ravel()[:n], precision=4)} ..."
    if isinstance(v, str) and len(v) > 200:
        return repr(v[:200]) + "..."
    return repr(v) if isinstance(v, str) else str(v)


def _um(v):
    return f"{v * 1e6:.4g}"


def summary(f):
    out = []
    w = out.append
    w(f"File: {f.version}; {len(f.materials)} materials in database; {len(f.objects)} objects\n")

    for ob in f.objects:
        if "numProcessLayers" in ob.props:  # layer builder
            p = ob.props
            w(f"LAYER BUILDER {ob.name}  (process '{p.get('processName')}')")
            w(f"  centre x={_um(p['x'])} y={_um(p['y'])} z={_um(p['z'])} um, span x={_um(p['xSpan'])} y={_um(p['ySpan'])} um")
            w(f"  GDS file: {p.get('gdsFileName')}")
            for i in range(p["numProcessLayers"]):
                k = f"{i}_process_layer"
                z0 = p[k + "startPosition"]; t = p[k + "thickness"]
                mat = p[k + "material"] or "-"
                pat = p[k + "patternMaterial"]
                s = f"  layer {i}: {p[k + 'name']:<14} z {_um(z0):>6} .. {_um(z0 + t):<6} um  background={mat}"
                if pat:
                    s += f"  pattern GDS {p[k + 'patternLayerName']} -> {pat}"
                if mat != "-":
                    n = f.material_index(mat); s += f" (n={n[0].real:.4f})" if n is not None else ""
                if pat:
                    n = f.material_index(pat); s += f" (n={n[0].real:.4f})" if n is not None else ""
                s += f", sidewall {p[k + 'sidewallAngle']:g} deg"
                w(s)
            for i in range(p.get("GDSLayerCount", 0)):
                lay = p[f"GDSLayerNumber_{i}"]
                for j in range(p.get(f"GDSLayerObjectCount_{i}", 0)):
                    poly = p[f"GDSLayerObject_{i}_{j}"] * 1e6
                    pts = " ".join(f"({x:.4g},{y:.4g})" for x, y in poly)
                    w(f"  GDS {lay} polygon {j} [um]: {pts}")
            w("")

    sts = structures(f)
    if sts:
        w("STRUCTURES (x, y extents and z range in um)")
        for st in sts:
            line = f"  {st['kind']:<15} {st['name']:<16} {st['material']}"
            if st["xy"] is not None:
                xy = st["xy"] * 1e6
                line += f"  x {xy[:, 0].min():g}..{xy[:, 0].max():g}  y {xy[:, 1].min():g}..{xy[:, 1].max():g}"
            if st["z"]:
                line += f"  z {st['z'][0] * 1e6:g}..{st['z'][1] * 1e6:g}"
            w(line)
            if st["kind"] == "polygon":
                w("      vertices: " + " ".join(f"({x:.4g},{y:.4g})" for x, y in st["xy"] * 1e6))
            if st["script"]:
                w("      script: " + st["script"].strip().replace("\n", "\n              "))
        w("")

    try:
        p = f.get("::model::FDTD").props
        w("FDTD REGION")
        x0, x1 = p["GUIx"] - p["GUIwidth"] / 2, p["GUIx"] + p["GUIwidth"] / 2
        y0, y1 = p["GUIy"] - p["GUIheight"] / 2, p["GUIy"] + p["GUIheight"] / 2
        w(f"  x {_um(x0)} .. {_um(x1)} um, y {_um(y0)} .. {_um(y1)} um, z {_um(p['GUIz1'])} .. {_um(p['GUIz2'])} um")
        w(f"  simulation time {p['MaxSimTime'] * 1e12:g} ps, auto-shutoff min {p['autoShutoffMin']:g}, background index {p['mRefIndex']}")
        f1, f2 = p["fStart"], p["fEnd"]
        w(f"  band {f1 / 1e12:.2f} .. {f2 / 1e12:.2f} THz = {C0 / f2 * 1e9:.1f} .. {C0 / f1 * 1e9:.1f} nm, centre {C0 / p['globalFrequency'] * 1e9:.1f} nm")
        w(f"  PML layers (xmin xmax ymin ymax zmin zmax): {p['PMLLayersV7p0'].ravel().tolist()}, extend structure through PML: {p['extendStructure']}")
        w(f"  mesh accuracy {p['gridSlider']}, grid {len(p['xGrid']) - 1} x {len(p['yGrid']) - 1} x {len(p['zGrid']) - 1} cells")
        for ax in "xyz":
            g = np.diff(p[f"{ax}Grid"].ravel()) * 1e9
            w(f"    d{ax} {g.min():.1f} .. {g.max():.1f} nm")
        w(f"  dt {p['dt'] * 1e18:.2f} as, run time {p['simRunTime']:.0f} s, peak memory {p['peakMemory']:.2f} GB")
        sym = [o for o in f.objects if "extraState::simState::symmetry" in o.props]
        if sym:
            s = sym[0]["extraState::simState::symmetry"].ravel()
            names = ["xmin", "xmax", "ymin", "ymax", "zmin", "zmax"]
            kinds = {1: "symmetric", -1: "anti-symmetric"}
            sb = [f"{names[i]}={kinds[v]}" for i, v in enumerate(s) if v]
            w(f"  symmetry boundaries: {', '.join(sb) if sb else 'none'}")
        w("")
    except KeyError:
        pass

    ports = [o for o in f.objects if "portXcoord" in o.props]
    if ports:
        w("PORTS")
        for o in ports:
            p = o.props
            src = "SOURCE" if p.get("isSourcePort") else "      "
            w(f"  {o.name.split('::')[-1]:<13} {src} x={_um(p['portXcoord'])} y={_um(p['portYcoord'])} z={_um(p['portZcoord'])} um, "
              f"span y={_um(p['portYspan'])} z={_um(p['portZspan'])} um, direction={'+' if p['portInjectionDirection'] else '-'}, enabled={p['enabled']}")
        for o in f.objects:
            if o.name.endswith("Mode Source") and o.get("type") == 6 and o.get("mSelectedModeNumber") is not None:
                w(f"  mode source: mode #{int(o['mSelectedModeNumber'].ravel()[0]) + 1} of {o['numberOfModes']} searched near n={o['realShift']}, "
                  f"solved at {C0 / o['modeFrequency'] * 1e9:.1f} nm")
        w("")

    mons = [o for o in f.objects if "monitorShape" in o.props and "portXcoord" not in o.props and not o.name.startswith("::output") and not o.name.startswith("::input")]
    if mons:
        w("MONITORS (x/y/z extents after snapping to the mesh, um)")
        for o in mons:
            p = o.props
            ext = " ".join(f"{a}=[{', '.join(f'{v * 1e6:.3f}' for v in p[f'{a}GridBounds'].ravel())}]" for a in "xyz" if f"{a}GridBounds" in p)
            fr = p.get("frequency")
            nf = fr.size if isinstance(fr, np.ndarray) else 1
            w(f"  {o.name.split('::')[-1]:<20} enabled={p['enabled']} {ext} freq points={nf}")
        w("")
    return "\n".join(out)



# ---------------------------------------------------------------- structure objects
def _blob_strings(b):
    """Length-prefixed, NUL-terminated text strings inside an object blob, in order."""
    out, i = [], 0
    while i < len(b) - 4:
        n = struct.unpack_from("<i", b, i)[0]
        if 2 <= n <= 1_000_000 and i + 4 + n <= len(b) and b[i + 3 + n] == 0:
            s = b[i + 4:i + 3 + n]
            if all(32 <= c < 127 or c in (9, 10, 13) for c in s):
                out.append(s.decode()); i += 4 + n; continue
        i += 1
    return out


def structures(f):
    """Decode rectangles, polygons and structure groups stored as tag-1000 objects.

    Returns a list of dicts with keys kind, name, material, xy (Nx2, metres, or None),
    z (zmin, zmax) or None, script (structure groups) and offset.
    Objects after the FDTD solver in the tree (internal helper shapes) are skipped.
    """
    try:
        stop = f.get("::model::FDTD").offset
    except KeyError:
        stop = len(f.data)
    by_uuid = {m.get("materialuuid"): m.get("name") for m in f.materials}
    out = []
    for ob in f.objects:
        if ob.tag != 1000 or ob.offset > stop or len(ob.blob) < 64:
            continue
        b = ob.blob
        hdr = struct.unpack_from("<3i", b, 0)
        strs = _blob_strings(b)
        index = strs[0] if strs else ""
        name = strs[1] if len(strs) > 1 else f"object_0x{ob.offset:x}"
        uuid = ob.props.get("materialuuid", "")
        material = by_uuid.get(uuid) or (f"<object defined dielectric> n={index}" if index else "?")
        item = dict(kind="unknown", name=name, material=material, xy=None, z=None, script=None, offset=ob.offset)
        if hdr[:2] == (6, 32):  # rectangle: index, then ymin xmin ymax xmax zmin zmax
            y0, x0, y1, x1, z0, z1 = np.frombuffer(b, "<f8", 6, 20)
            item.update(kind="rectangle", xy=np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]]), z=(z0, z1))
        elif hdr[:2] == (11, 22):  # polygon: vertex matrix (2 x N), then z min, z max
            p = b.find(b"\x03\x00\x00\x00\x00", 8)
            if p > 0:
                p += 5
                tot, nd = struct.unpack_from("<2i", b, p)
                dims = struct.unpack_from(f"<{nd}i", b, p + 8)
                if nd == 2 and dims[0] == 2:
                    q = p + 16 + 4 * nd
                    xy = np.frombuffer(b, "<f8", tot, q).reshape(dims, order="F").T
                    e = q + 8 * tot
                    z = None
                    if b[e] == 1 and b[e + 9] == 1:
                        z = (struct.unpack_from("<d", b, e + 1)[0], struct.unpack_from("<d", b, e + 10)[0])
                    item.update(kind="polygon", xy=xy, z=z)
        elif hdr[:2] == (13, 18):
            scripts = [s for s in strs[2:] if ";" in s]
            item.update(kind="structure group", material="-", script=scripts[0] if scripts else None)
        out.append(item)
    return out


# ---------------------------------------------------------------- GDSII export
def _gds_rec(rtype, dtype, payload=b""):
    if len(payload) % 2:
        payload += b"\0"
    return struct.pack(">HBB", 4 + len(payload), rtype, dtype) + payload


def _gds_real8(v):
    """IBM excess-64 base-16 floating point, as required by GDSII."""
    if v == 0:
        return b"\0" * 8
    sign = 0x80 if v < 0 else 0
    v, e = abs(v), 64
    while v >= 1:
        v /= 16; e += 1
    while v < 1 / 16:
        v *= 16; e -= 1
    m = int(round(v * 2 ** 56))
    if m >= 2 ** 56:
        m //= 16; e += 1
    return bytes([sign | e]) + struct.pack(">Q", m)[1:]


def layer_builder_polygons(f):
    """{cell_name: [(layer, datatype, Nx2 array in metres), ...]} for every layer builder."""
    cells = {}
    for ob in f.objects:
        p = ob.props
        if "GDSLayerCount" not in p:
            continue
        cell = re.sub(r"[^A-Za-z0-9_$?]", "_", ob.name.split("::")[-1])[:32] or "TOP"
        polys = []
        for i in range(p["GDSLayerCount"]):
            layer, _, dtype = str(p[f"GDSLayerNumber_{i}"]).partition(":")
            for j in range(p.get(f"GDSLayerObjectCount_{i}", 0)):
                pts = np.asarray(p[f"GDSLayerObject_{i}_{j}"], dtype=float)
                if pts.ndim != 2 or len(np.unique(pts.round(12), axis=0)) < 3:
                    continue  # skip empty / degenerate placeholders such as layer 0:0
                polys.append((int(layer), int(dtype or 0), pts))
        cells[cell] = polys
    return cells


def structure_polygons(f):
    """{'STRUCTURES': [(layer, 0, Nx2 metres), ...]}, one GDS layer per rectangle/polygon object."""
    polys, layer = [], 0
    for s in structures(f):
        if s["xy"] is not None:
            layer += 1
            s["gds_layer"] = layer
            polys.append((layer, 0, s["xy"]))
    return {"STRUCTURES": polys} if polys else {}


def write_gds(f, path, libname="LUMERICAL_EXPORT", dbu=1e-9):
    """Write layer-builder polygons, or else individual structure objects, to GDSII
    (user unit 1 um, database unit `dbu` metres)."""
    cells = layer_builder_polygons(f) or structure_polygons(f)
    if not cells:
        shapes = [ob.name for ob in f.objects
                  if ob.tag == 1000 or ("materialuuid" in ob.props and "GDSLayerCount" not in ob.props)]
        msg = "no layer builder with GDS polygons found in this file."
        if shapes:
            msg += (" Its geometry is in individual structure objects, whose shape data is not decoded yet: "
                    + ", ".join(shapes[:20]) + (" ..." if len(shapes) > 20 else ""))
        raise ValueError(msg)
    date = struct.pack(">12h", *([2026, 1, 1, 0, 0, 0] * 2))
    out = [_gds_rec(0x00, 0x02, struct.pack(">h", 600)),
           _gds_rec(0x01, 0x02, date),
           _gds_rec(0x02, 0x06, libname.encode()),
           _gds_rec(0x03, 0x05, _gds_real8(dbu / 1e-6) + _gds_real8(dbu))]
    for cell, polys in cells.items():
        out += [_gds_rec(0x05, 0x02, date), _gds_rec(0x06, 0x06, cell.encode())]
        for layer, dtype, pts in polys:
            xy = np.rint(pts / dbu).astype(np.int64)
            if not np.array_equal(xy[0], xy[-1]):
                xy = np.vstack([xy, xy[:1]])  # GDSII boundaries must be closed
            out += [_gds_rec(0x08, 0x00),
                    _gds_rec(0x0D, 0x02, struct.pack(">h", layer)),
                    _gds_rec(0x0E, 0x02, struct.pack(">h", dtype)),
                    _gds_rec(0x10, 0x03, struct.pack(f">{xy.size}i", *xy.ravel())),
                    _gds_rec(0x11, 0x00)]
        out.append(_gds_rec(0x07, 0x00))
    out.append(_gds_rec(0x04, 0x00))
    with open(path, "wb") as fh:
        fh.write(b"".join(out))
    return cells


def gds_layer_map(f):
    """Lines describing which process layer / object and material each GDS layer holds."""
    lines = []
    if not layer_builder_polygons(f):
        layer = 0
        for s in structures(f):
            if s["xy"] is not None:
                layer += 1
                z = f", z {s['z'][0] * 1e6:g} .. {s['z'][1] * 1e6:g} um" if s["z"] else ""
                lines.append(f"{layer:>5}:0  -> {s['kind']} '{s['name']}': {s['material']}{z}")
            elif s["kind"] == "structure group" and s["script"]:
                lines.append(f"  (not exported) structure group '{s['name']}': geometry is generated by its script")
        return lines
    for ob in f.objects:
        p = ob.props
        if "numProcessLayers" not in p:
            continue
        for i in range(p["numProcessLayers"]):
            k = f"{i}_process_layer"
            if p.get(k + "patternLayerName"):
                z0, t = p[k + "startPosition"] * 1e6, p[k + "thickness"] * 1e6
                lines.append(f"{p[k + 'patternLayerName']:>8}  -> {p[k + 'name']}: {p[k + 'patternMaterial']}, "
                             f"z {z0:g} .. {z0 + t:g} um")
    return lines


def to_jsonable(v, max_elems):
    if isinstance(v, (bytes, bytearray)):
        return f"<{len(v)} bytes>"
    if isinstance(v, np.ndarray):
        if v.size <= max_elems:
            if np.iscomplexobj(v):
                return {"real": v.real.tolist(), "imag": v.imag.tolist()}
            return v.tolist()
        return f"<array shape={v.shape} dtype={v.dtype}>"
    return v


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("fsp")
    ap.add_argument("--summary", action="store_true", help="human-readable model settings")
    ap.add_argument("--props", metavar="NAME", help="print all properties of objects whose name contains NAME")
    ap.add_argument("--materials", action="store_true", help="list the material database")
    ap.add_argument("--json", metavar="OUT", help="dump everything to JSON (arrays > --max-elems summarised)")
    ap.add_argument("--max-elems", type=int, default=1000)
    ap.add_argument("--npz", metavar="OUT", help="save every array (full size) to a .npz file")
    ap.add_argument("--gds", metavar="OUT", help="export the layer-builder GDS polygons to a GDSII file")
    a = ap.parse_args()
    f = FSPFile(a.fsp)

    if a.summary:
        print(summary(f))
    elif a.props:
        for ob in f.find(a.props):
            print(f"== {ob.name}  (uuid {ob.uuid}, offset 0x{ob.offset:x})")
            for k, v in ob.props.items():
                print(f"   {k:45s} {_fmt(v)}")
            for k, v in ob.results.items():
                print(f"   [result] {k:36s} {_fmt(v)}")
    elif a.materials:
        for m in f.materials:
            print(f"{m.get('name'):45s} type {m.get('type')}")
    elif not (a.json or a.npz or a.gds):
        for ob in f.objects:
            res = f", results: {', '.join(ob.results)}" if ob.results else ""
            print(f"0x{ob.offset:08x}  {ob.name:50s} {len(ob.props):4d} props{res}")

    if a.json:
        doc = {"version": f.version,
               "materials": [{k: to_jsonable(v, a.max_elems) for k, v in m.items()} for m in f.materials],
               "objects": [{"name": ob.name, "uuid": ob.uuid, "offset": ob.offset,
                            "props": {k: to_jsonable(v, a.max_elems) for k, v in ob.props.items()},
                            "results": {k: to_jsonable(v, a.max_elems) for k, v in ob.results.items()}}
                           for ob in f.objects]}
        with open(a.json, "w") as fh:
            json.dump(doc, fh, indent=1)
        print(f"wrote {a.json}", file=sys.stderr)
    if a.gds:
        cells = write_gds(f, a.gds)
        for cell, polys in cells.items():
            layers = sorted({f"{l}:{d}" for l, d, _ in polys})
            print(f"wrote {a.gds}: cell {cell}, {len(polys)} polygons on layers {', '.join(layers)}", file=sys.stderr)
        for line in gds_layer_map(f):
            print("  " + line, file=sys.stderr)
        if not layer_builder_polygons(f):
            for st in structures(f):
                if st["script"]:
                    fn = re.sub(r"\.gds$", "", a.gds) + "_" + re.sub(r"\W+", "_", st["name"]) + ".lsf"
                    with open(fn, "w") as fh:
                        fh.write(st["script"])
                    print(f"  saved script of '{st['name']}' to {fn}", file=sys.stderr)
            print("  note: objects inside structure groups are exported in their stored coordinates; "
                  "group offsets/rotations are not applied.", file=sys.stderr)
    if a.npz:
        arrays = {}
        for ob in f.objects:
            for src, dct in (("prop", ob.props), ("result", ob.results)):
                for k, v in dct.items():
                    if isinstance(v, np.ndarray):
                        arrays[f"{ob.name}|{src}|{k}"] = v
        np.savez_compressed(a.npz, **arrays)
        print(f"wrote {a.npz} ({len(arrays)} arrays)", file=sys.stderr)


if __name__ == "__main__":
    main()
