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
import argparse, json, mmap, re, struct, sys
from collections import OrderedDict
import numpy as np

C0 = 299792458.0

# A file of many GB is not read into memory: it is memory-mapped, and a matrix larger than
# LAZY_BYTES stays on disk until it is used. Decoded complex matrices (which need a copy) are
# kept in a cache of at most CACHE_BYTES, the least recently used dropped first.
LAZY_BYTES = 1 << 20
CACHE_BYTES = 1 << 30


class FormatError(Exception):
    pass


class LazyMatrix:
    """A matrix of the file that is still on disk; FSPFile decodes it when it is used."""
    __slots__ = ("file", "offset", "total", "dims", "is_real", "dtype")

    def __init__(self, file, offset, total, dims, is_real, dtype):
        self.file, self.offset, self.total, self.dims = file, offset, total, dims
        self.is_real, self.dtype = is_real, dtype

    @property
    def shape(self):
        return tuple(self.dims) if self.total else (0,)

    @property
    def size(self):
        return self.total

    @property
    def ndim(self):
        return len(self.shape)

    @property
    def is_complex(self):
        return self.dtype == "<f8" and not self.is_real

    @property
    def nbytes(self):
        return self.total * (16 if self.is_complex else np.dtype(self.dtype).itemsize)

    def load(self):
        return self.file._load_matrix(self)

    def take_last(self, k):
        """The slice [..., k] along the last axis (e.g. one frequency of a field result), read
        alone: in column-major order it is one contiguous block of the file."""
        dims = tuple(self.dims)
        if not dims or not 0 <= k < dims[-1]:
            raise IndexError(f"index {k} out of range for shape {self.shape}")
        n = int(np.prod(dims[:-1]))
        size = np.dtype(self.dtype).itemsize
        d = self.file.data
        arr = np.frombuffer(d, self.dtype, n, self.offset + k * n * size)
        if self.is_complex:
            im = np.frombuffer(d, self.dtype, n, self.offset + (self.total + k * n) * size)
            arr = arr + 1j * im
        else:
            arr = arr.copy()
        return arr.reshape(dims[:-1], order="F")

    def __repr__(self):
        return f"<{'complex ' if self.is_complex else ''}array {self.shape} on disk, {self.nbytes / 1e6:.1f} MB>"


class LazyDict(dict):
    """Property / result dictionary whose large matrices are decoded from the file on access.
    raw_items() gives the stored values (LazyMatrix placeholders included) without reading them."""

    def __getitem__(self, key):
        v = dict.__getitem__(self, key)
        return v.load() if isinstance(v, LazyMatrix) else v

    def get(self, key, default=None):
        return self[key] if key in self else default

    def values(self):
        return (self[k] for k in self)

    def items(self):
        return ((k, self[k]) for k in self)

    def raw_items(self):
        return dict.items(self)

    def raw(self, key):
        """The stored value: a LazyMatrix placeholder for a large matrix, not read."""
        return dict.__getitem__(self, key)

    def take_last(self, key, k):
        """value[..., k], reading only that slice from the file when the matrix is large."""
        v = dict.__getitem__(self, key)
        return v.take_last(k) if isinstance(v, LazyMatrix) else np.asarray(v)[..., k]


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
            try:
                self.data = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
            except ValueError:  # empty file
                self.data = b""
        d = self.data
        if d[:22] != b"LUMERICAL file version":
            raise FormatError("not a Lumerical file (bad magic)")
        self.version = d[: d.find(b"\0", 0, 200)].decode()
        self._cache = OrderedDict()  # offset -> decoded large matrix
        self._cache_bytes = 0
        self.materials = self._read_materials()
        self.objects = self._read_objects()

    def close(self):
        """Releases the file (arrays that view it must not be used afterwards)."""
        self._cache.clear()
        if isinstance(self.data, mmap.mmap):
            try:
                self.data.close()
            except BufferError:  # arrays still look into the file: it closes when they are gone
                pass

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
        size = np.dtype(dtype).itemsize * n
        if p + size > len(d):
            raise FormatError(f"matrix at 0x{p:x} runs past the end of the file")
        lazy = LazyMatrix(self, p, total, dims, is_real, dtype)
        p += size
        if size > LAZY_BYTES:
            return label, lazy, p
        return label, self._decode(lazy, copy=True), p

    def _decode(self, m, copy):
        n = m.total * (2 if m.is_complex else 1)
        arr = np.frombuffer(self.data, dtype=m.dtype, count=n, offset=m.offset)
        if m.is_complex:
            c = np.empty(m.total, dtype=complex)
            c.real, c.imag = arr[:m.total], arr[m.total:]  # avoids NaN warnings from uninitialised values
            arr = c
        elif copy:
            arr = arr.copy()
        if m.total:
            arr = arr.reshape(m.dims, order="F")
        return arr

    def _load_matrix(self, m):
        """A large matrix, read now. A real one is a read-only view of the mapped file (no copy;
        the system pages it in and out); a complex one is assembled and cached."""
        if not m.is_complex:
            return self._decode(m, copy=False)
        arr = self._cache.get(m.offset)
        if arr is not None:
            self._cache.move_to_end(m.offset)
            return arr
        arr = self._decode(m, copy=False)
        if arr.nbytes <= CACHE_BYTES:
            self._cache[m.offset] = arr
            self._cache_bytes += arr.nbytes
            while self._cache_bytes > CACHE_BYTES and len(self._cache) > 1:
                _, old = self._cache.popitem(last=False)
                self._cache_bytes -= old.nbytes
        return arr

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
        props = LazyDict()
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
        """Finds the object nodes. The search continues after the property block and the results
        of each node, so the bulk data (field results of many GB) is jumped over, not scanned."""
        objs = []
        pos = getattr(self, "_materials_end", 0)
        while True:
            m = self.NODE_RE.search(self.data, pos)
            if m is None:
                break
            o = m.start()
            pos = m.end()
            tag = self._i32(o)
            uuid = m.group()[8:].decode()
            start = o + 8 + 38
            props = results = None
            if tag == 1001:
                try:
                    props, end = self._props(start)
                    pos = end
                    results = LazyDict()
                    nres = self._i32(end); end += 4
                    if not 0 <= nres < 10000:
                        raise FormatError(f"bad result count at 0x{end - 4:x}")
                    for _ in range(nres):
                        label, arr, end = self._matrix_body(end)
                        results[label] = arr
                        pos = end
                except FormatError:
                    if props is None:
                        continue
            else:  # tag 1000: skip the undecoded blob, find the property block
                for q in range(start, min(start + 200000, len(self.data) - 4)):
                    n = self._i32(q)
                    if 3 < n < 2000:
                        try:
                            props, end = self._props(q)
                            pos = end
                            break
                        except FormatError:
                            pass
                if props is None:
                    continue
                blob = self.data[start:q]
                objs.append(FSPObject(o, tag, uuid, props, LazyDict(), blob))
                continue
            objs.append(FSPObject(o, tag, uuid, props, results if results is not None else LazyDict()))
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
    if isinstance(v, LazyMatrix):
        return repr(v)
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


# A placement block follows the name of every shape: three rotation axes (-1 none, 0 x, 1 y, 2 z),
# stored last-rotation-first, then a 3 x 1 matrix of the rotation angles in degrees.
_ROTATION_RE = re.compile(rb"\x00(.{4})\x00(.{4})\x00(.{4})\x03\x00\x00\x00\x00\x03\x00\x00\x00\x02\x00\x00\x00"
                          rb"\x03\x00\x00\x00\x01\x00\x00\x00\x01\x00\x00\x00\x01\x00\x00\x00", re.S)
SHAPE_KINDS = {(6, 32): "rectangle", (11, 22): "polygon", (13, 18): "structure group"}


def _find_props(f, s, limit=200000):
    """(offset, props, end) of the property block that follows the undecoded blob starting at s."""
    for q in range(s, min(s + limit, len(f.data) - 4)):
        n = f._i32(q)
        if 3 < n < 2000:
            try:
                props, end = f._props(q)
                return q, props, end
            except FormatError:
                pass
    raise FormatError(f"no property block after 0x{s:x}")


def _parse_shape(f, s, depth=0):
    """One tag-1000 shape node at s (its blob, properties and, for a structure group, its
    children, which are stored inline without a node header). Returns (node, end)."""
    if depth > 64:
        raise FormatError("structure groups nested too deep")
    q, props, end = _find_props(f, s)
    blob = bytes(f.data[s:q])
    hdr = struct.unpack_from("<2i", blob, 0) if len(blob) >= 8 else (0, 0)
    node = {"offset": s, "blob": blob, "props": props, "kind": SHAPE_KINDS.get(hdr, "unknown"), "children": []}
    if hdr == (13, 18):
        n = f._i32(end); end += 4
        if not 0 <= n < 100000:
            raise FormatError(f"bad child count at 0x{end - 4:x}")
        for _ in range(n):
            child, end = _parse_shape(f, end, depth + 1)
            node["children"].append(child)
    return node, end


def shape_tree(f):
    """Top-level tag-1000 shapes (rectangles, polygons, structure groups with their children)."""
    out = []
    for ob in f.objects:
        if ob.tag != 1000:
            continue
        try:
            node, _ = _parse_shape(f, ob.offset + 8 + 38)
        except (FormatError, struct.error):
            continue
        node["object_offset"] = ob.offset
        out.append(node)
    return out


def _rotation_matrix(axes, angles_deg):
    """Rotation of a shape. The axes are stored last-rotation-first: the third entry is the first axis."""
    r = np.eye(3)
    for ax, ang in zip(reversed(axes), reversed(angles_deg)):
        if ax not in (0, 1, 2) or not ang:
            continue
        c, s = np.cos(np.radians(ang)), np.sin(np.radians(ang))
        i, j = [(1, 2), (2, 0), (0, 1)][ax]  # right-handed: positive angle is anticlockwise about the axis
        m = np.eye(3)
        m[i, i], m[i, j], m[j, i], m[j, j] = c, -s, s, c
        r = m @ r
    return r


def _string_before(b, end):
    """The length-prefixed NUL-terminated string that ends at `end`, or ''."""
    for n in range(2, min(300, end - 4) + 1):
        if struct.unpack_from("<i", b, end - n - 4)[0] == n and b[end - 1] == 0:
            s = b[end - n:end - 1]
            if all(32 <= c < 127 for c in s):
                return s.decode()
    return ""


def _shape_geometry(node):
    """Name, enabled flag, index text, own rotation and local geometry of a shape node."""
    b, kind = node["blob"], node["kind"]
    geo = {"name": "", "enabled": True, "index": "", "axes": (-1, -1, -1), "angles": (0.0, 0.0, 0.0),
           "xy": None, "z": None, "center": None, "position": np.zeros(3), "script": None}
    if len(b) >= 10:
        geo["enabled"] = bool(struct.unpack_from("<i", b, len(b) - 9)[0])
    m = _ROTATION_RE.search(b, 8)
    if m:
        geo["axes"] = tuple(struct.unpack("<i", g)[0] for g in m.groups())
        geo["angles"] = struct.unpack_from("<3d", b, m.end())
    strs = _blob_strings(b)
    if kind == "structure group":
        geo["name"] = strs[1] if len(strs) > 1 else ""
        scripts = [x for x in strs[2:] if ";" in x]
        geo["script"] = scripts[0] if scripts else None
        if geo["name"]:  # the position follows the name: three (flag, double) pairs
            key = struct.pack("<i", len(geo["name"]) + 1) + geo["name"].encode() + b"\0"
            p = b.find(key)
            if p >= 0:
                p += len(key)
                vals = []
                for k in range(3):
                    if p + 9 * (k + 1) > len(b) or b[p + 9 * k] not in (0, 1):
                        break
                    vals.append(struct.unpack_from("<d", b, p + 9 * k + 1)[0])
                if len(vals) == 3 and all(abs(v) < 1 for v in vals):
                    geo["position"] = np.array(vals)
        return geo
    if m:
        geo["name"] = _string_before(b, m.start())
    if kind == "rectangle":
        _, y0, x0, y1, x1, z0, z1 = struct.unpack_from("<7d", b, 12)
        x0, x1, y0, y1, z0, z1 = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1), min(z0, z1), max(z0, z1)
        geo["xy"] = np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]])
        geo["z"] = (z0, z1)
        geo["center"] = np.array([(x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2])
        geo["index"] = strs[0] if strs else ""
    elif kind == "polygon" and len(b) > 40 and b[22] == 3:
        # 00, int, 01 + double, 03 + vertex matrix, 01 + z min, 01 + z max, 02 + index text
        p = 23
        lbl = struct.unpack_from("<i", b, p)[0]; p += 4 + lbl
        total, ndim = struct.unpack_from("<2i", b, p)
        dims = struct.unpack_from(f"<{ndim}i", b, p + 8)
        p += 4 * (4 + ndim)
        if ndim == 2 and 2 in dims and 0 < total < 1000000:
            xy = np.frombuffer(b, "<f8", total, p).reshape(dims, order="F")
            xy = xy.T if dims[0] == 2 else xy
            p += 8 * total
            if b[p] in (0, 1) and b[p + 9] in (0, 1):
                z0, z1 = struct.unpack_from("<d", b, p + 1)[0], struct.unpack_from("<d", b, p + 10)[0]
                geo["z"] = (min(z0, z1), max(z0, z1))
                p += 18
                if b[p] == 2:
                    n = struct.unpack_from("<i", b, p + 1)[0]
                    geo["index"] = b[p + 5:p + 4 + n].decode("latin1")
            geo["xy"] = np.array(xy, dtype=float)
            # vertices are relative to the polygon position; the rotation turns about it
            geo["center"] = np.array([0.0, 0.0, 0.0 if geo["z"] is None else sum(geo["z"]) / 2])
    return geo


def structures(f):
    """Rectangles, polygons and structure groups (tag-1000 objects), placed in the model.

    The translation and rotation of the enclosing structure groups and the rotation of the
    shape itself are applied: xy (Nx2, metres) is the footprint of the placed shape and z its
    (zmin, zmax). Each dict also has kind, name, path (group names / name), material, index,
    enabled (False if the shape or a group above it is disabled), rotated (True if any rotation
    applies), script (structure groups) and offset.
    Objects after the FDTD solver in the tree (internal helper shapes) are skipped.
    """
    try:
        stop = f.get("::model::FDTD").offset
    except KeyError:
        stop = len(f.data)
    by_uuid = {m.get("materialuuid"): m.get("name") for m in f.materials}
    out = []

    def place(node, rot, shift, enabled, path):
        geo = _shape_geometry(node)
        name = geo["name"] or f"object_0x{node['offset']:x}"
        on = enabled and geo["enabled"]
        own = _rotation_matrix(geo["axes"], geo["angles"])
        uuid = node["props"].get("materialuuid", "")
        material = by_uuid.get(uuid) or (f"<object defined dielectric> n={geo['index']}" if geo["index"] else "?")
        item = dict(kind=node["kind"], name=name, path="/".join(path + [name]), material=material,
                    index=geo["index"], xy=None, z=None, script=geo["script"], offset=node["offset"],
                    enabled=on, rotated=bool(not np.allclose(rot, np.eye(3)) or not np.allclose(own, np.eye(3))))
        if node["kind"] == "structure group":
            item["material"] = "-"
            out.append(item)
            # children: point_parent = position + R_group @ point_child
            for child in node["children"]:
                place(child, rot @ own, shift + rot @ geo["position"], on, path + [name])
            return
        if geo["xy"] is not None and geo["z"] is not None:
            c = geo["center"]
            z0, z1 = geo["z"]
            prism = np.array([[x, y, z] for z in (z0, z1) for x, y in geo["xy"]])
            placed = (rot @ (c[:, None] + own @ (prism.T - c[:, None]))).T + shift
            n = len(geo["xy"])
            item["xy"] = placed[:n, :2]
            item["z"] = (float(placed[:, 2].min()), float(placed[:, 2].max()))
        out.append(item)

    for node in shape_tree(f):
        if node.get("object_offset", node["offset"]) > stop:
            continue
        place(node, np.eye(3), np.zeros(3), True, [])
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
    if isinstance(v, LazyMatrix):
        if v.size > max_elems:
            return f"<array shape={v.shape} dtype={'complex128' if v.is_complex else v.dtype}>"
        v = v.load()
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
            for k, v in ob.props.raw_items():
                print(f"   {k:45s} {_fmt(v)}")
            for k, v in ob.results.raw_items():
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
                            "props": {k: to_jsonable(v, a.max_elems) for k, v in ob.props.raw_items()},
                            "results": {k: to_jsonable(v, a.max_elems) for k, v in ob.results.raw_items()}}
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
            print("  note: objects inside structure groups are exported where the groups place them "
                  "(group translation and rotation applied).", file=sys.stderr)
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
