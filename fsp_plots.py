#!/usr/bin/env python3
"""
fsp_plots.py - visualise the edge-coupler simulation from its .fsp (settings) and .h5 (fields).

HDF5 layout (as found in my_simulation.h5):
  Monitor0      -> input_port DFT        Monitor1..6 -> output_port2..7 DFT
  Monitor7      -> dft_xz_propagation (y = 0 plane)
  each group: x, y, z (um) and Ex..Hz float32 arrays shaped (z, y, x, 2*n_freq),
  last axis = [Re f1, Im f1, Re f2, Im f2, ...]. Frequencies come from the .fsp.

Usage:  python fsp_plots.py my_simulation.fsp my_simulation.h5 [output_dir]
"""
import os, sys
import numpy as np
import h5py
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon, Rectangle
from scipy.interpolate import RegularGridInterpolator
from fsp_reader import FSPFile, C0

fsp_path, h5_path = sys.argv[1], sys.argv[2]
out = sys.argv[3] if len(sys.argv) > 3 else "."
os.makedirs(out, exist_ok=True)

F = FSPFile(fsp_path)
H = h5py.File(h5_path, "r")
T = np.trapezoid if hasattr(np, "trapezoid") else np.trapz

PORTS = ["input_port"] + [f"output_port{k}" for k in range(2, 8)]
MON = {name: f"Monitor{i}" for i, name in enumerate(PORTS)}
MON["dft_xz_propagation"] = "Monitor7"

freqs = F.get("::input_port DFT").results["f"].ravel()
lam = C0 / freqs * 1e9
KC = int(np.argmin(abs(lam - 1310)))  # index of the centre wavelength

lb = F.get("::model::Ridge_SSC").props
fd = F.get("::model::FDTD").props


def field(mon, comp, k):
    a = H[mon][comp]
    return a[..., 2 * k] + 1j * a[..., 2 * k + 1]


def layer_z(name):
    for i in range(lb["numProcessLayers"]):
        if lb[f"{i}_process_layername"] == name:
            z0 = lb[f"{i}_process_layerstartPosition"] * 1e6
            return z0, z0 + lb[f"{i}_process_layerthickness"] * 1e6


def polys(layer):
    for i in range(lb["GDSLayerCount"]):
        if lb[f"GDSLayerNumber_{i}"] == layer:
            return [lb[f"GDSLayerObject_{i}_{j}"] * 1e6 for j in range(lb[f"GDSLayerObjectCount_{i}"])]
    return []


port_x = {p: F.get(f"::model::FDTD::ports::{p}")["portXcoord"] * 1e6 for p in PORTS}
fx0 = (fd["GUIx"] - fd["GUIwidth"] / 2) * 1e6
fx1 = (fd["GUIx"] + fd["GUIwidth"] / 2) * 1e6
fy = fd["GUIheight"] / 2 * 1e6

# ---------------------------------------------------------------- 1. layout
fig, (a1, a2) = plt.subplots(2, 1, figsize=(13, 7.5), gridspec_kw={"height_ratios": [1.4, 1]})
for ax, ylim in ((a1, (-25, 25)), (a2, (-0.6, 0.6))):
    for p in polys("510:0"):
        ax.add_patch(Polygon(p, closed=True, fc="#cfe8ff", ec="#5b9bd5", lw=0.8, label="air trench (510:0)"))
    for p in polys("30:0"):
        ax.add_patch(Polygon(p, closed=True, fc="#c0392b", ec="#7b241c", lw=0.8, label="SiN core (30:0)"))
    ax.add_patch(Rectangle((fx0, -fy), fx1 - fx0, 2 * fy, fill=False, ec="orange", lw=1.5, ls="-", label="FDTD region"))
    for p, x in port_x.items():
        ax.axvline(x, color="green" if p == "input_port" else "purple", lw=0.9, ls="--")
    ax.set_xlim(-35, 228); ax.set_ylim(*ylim); ax.set_xlabel("x (µm)"); ax.set_ylabel("y (µm)")
h, l = a1.get_legend_handles_labels(); u = dict(zip(l, h))
a1.legend(u.values(), u.keys(), loc="lower left", fontsize=8)
a1.set_title("Top view: layer-builder GDS polygons, FDTD region, ports (green = source, purple = output ports)")
a2.set_title("Same, y exaggerated: SiN inverse taper 400 nm → 250 nm over 200 µm, then 20 µm of oxide ridge")
for p, x in port_x.items():
    a2.text(x, 0.52, p.replace("output_", "").replace("_port", ""), rotation=90, fontsize=7, ha="right", va="top")
plt.tight_layout(); plt.savefig(f"{out}/fig1_layout_top_view.png", dpi=130); plt.close()

# ---------------------------------------------------------------- 2. cross sections (stored index + mode)
def stored_mode(port):
    p = F.get(f"::{port} Mode Expansion Monitor").props
    y, z = p["yMatrix"].ravel() * 1e6, p["zMatrix"].ravel() * 1e6
    g = lambda c, i: p[f"freqEHn{c}{i}"][0, :, :, 0].T  # (z, y)
    M = {f"E{c}": g(c, 0) for c in "xyz"} | {f"H{c}": g(c, 1) for c in "xyz"}
    M["n"] = g("x", 2).real
    return y, z, M

show = ["input_port", "output_port2", "output_port4", "output_port7"]
fig, axs = plt.subplots(2, len(show), figsize=(4.2 * len(show), 7))
for j, port in enumerate(show):
    y, z, M = stored_mode(port)
    im = axs[0, j].pcolormesh(y, z, M["n"], shading="auto", cmap="viridis", vmin=1, vmax=2)
    axs[0, j].set_title(f"{port} (x = {port_x[port]:.1f} µm)\nrefractive index", fontsize=9)
    E2 = sum(abs(M[f"E{c}"]) ** 2 for c in "xyz")
    axs[1, j].pcolormesh(y, z, E2 / E2.max(), shading="auto", cmap="inferno")
    axs[1, j].contour(y, z, M["n"], levels=[1.2, 1.445, 1.7], colors="w", linewidths=0.5)
    axs[1, j].set_title("port mode |E|² (normalised)", fontsize=9)
    for ax in axs[:, j]:
        ax.set_aspect("equal"); ax.set_xlabel("y (µm)")
    axs[0, j].set_ylabel("z (µm)"); axs[1, j].set_ylabel("z (µm)")
fig.colorbar(im, ax=axs[0, :].tolist(), shrink=0.8, label="n")
fig.suptitle(f"Cross-sections and port modes stored in the .fsp (mode solved at {C0 / F.get('::input_port Mode Source')['modeFrequency'] * 1e9:.0f} nm)")
plt.savefig(f"{out}/fig2_cross_sections_and_port_modes.png", dpi=120, bbox_inches="tight"); plt.close()

# ---------------------------------------------------------------- 3. xz propagation
g = H[MON["dft_xz_propagation"]]
x, z = g["x"][()], g["z"][()]
fig, axs = plt.subplots(3, 1, figsize=(14, 10), constrained_layout=True)
for ax, k, xr in ((axs[0], KC, None), (axs[1], KC, (185, 220.002)), (axs[2], 0, (185, 220.002))):
    E2 = sum(abs(field(MON["dft_xz_propagation"], c, k)[:, 0, :]) ** 2 for c in ("Ex", "Ey", "Ez"))
    dB = 10 * np.log10(E2 / E2.max() + 1e-12)
    sel = slice(None) if xr is None else (x >= xr[0]) & (x <= xr[1])
    im = ax.pcolormesh(x[sel], z, dB[:, sel], shading="auto", cmap="inferno", vmin=-35, vmax=0, rasterized=True)
    zc0, zc1 = layer_z("SiN_Core"); zt = layer_z("CLAD")[1]
    ax.plot([-30, 200.002], [zc0, zc0], "c-", lw=0.5); ax.plot([-30, 200.002], [zc1, zc1], "c-", lw=0.5)
    ax.axhline(0, color="w", lw=0.5, ls=":"); ax.axhline(zt, color="w", lw=0.5, ls=":")
    ax.set_ylabel("z (µm)"); ax.set_title(f"|E|² at y = 0 (dB), λ = {lam[k]:.1f} nm" + ("" if xr is None else f", zoom x = {xr[0]}–{xr[1]} µm"), fontsize=10)
    ax.set_xlim(*(xr or (x[0], x[-1])))
axs[-1].set_xlabel("x (µm)")
fig.colorbar(im, ax=axs.tolist(), shrink=0.6, label="dB")
plt.savefig(f"{out}/fig3_xz_propagation.png", dpi=130); plt.close()

# ---------------------------------------------------------------- 4. port field profiles
fig, axs = plt.subplots(1, len(PORTS), figsize=(3.3 * len(PORTS), 4.2))
for ax, port in zip(axs, PORTS):
    m = MON[port]; y, z = H[m]["y"][()], H[m]["z"][()]
    E2 = sum(abs(field(m, c, KC)[:, :, 0]) ** 2 for c in ("Ex", "Ey", "Ez"))
    yy = np.concatenate([-y[:0:-1], y]); E2f = np.concatenate([E2[:, :0:-1], E2], axis=1)  # unfold symmetry
    ax.pcolormesh(yy, z, E2f / E2f.max(), shading="auto", cmap="inferno")
    ys, zs, M = stored_mode(port)
    ax.contour(ys, zs, M["n"], levels=[1.2, 1.445, 1.7], colors="w", linewidths=0.5)
    ax.set_xlim(yy[0], yy[-1]); ax.set_ylim(z[0], z[-1]); ax.set_aspect("equal")
    ax.set_title(f"{port.replace('output_', '')}\nx = {H[m]['x'][0]:.2f} µm", fontsize=9); ax.set_xlabel("y (µm)")
axs[0].set_ylabel("z (µm)")
fig.suptitle(f"Simulated |E|² in each port plane (normalised), λ = {lam[KC]:.1f} nm; white = index contours")
plt.savefig(f"{out}/fig4_port_field_profiles.png", dpi=120, bbox_inches="tight"); plt.close()

# ---------------------------------------------------------------- 5. power and mode coupling
def plane(port, k):
    m = MON[port]; y, z = H[m]["y"][()], H[m]["z"][()]
    E = {c: field(m, c, k)[:, :, 0] for c in ("Ey", "Ez", "Hy", "Hz")}
    return y, z, E

def integ(F2, y, z):
    return T(T(F2, y, axis=1), z, axis=0)

P = np.zeros((len(PORTS), len(freqs))); eta = np.zeros_like(P)
for i, port in enumerate(PORTS):
    ys, zs, M = stored_mode(port)
    keep = ys >= -1e-9; ys = ys[keep]
    M = {k: v[:, keep] for k, v in M.items()}
    for k in range(len(freqs)):
        y, z, E = plane(port, k)
        P[i, k] = integ(np.real(E["Ey"] * np.conj(E["Hz"]) - E["Ez"] * np.conj(E["Hy"])), y, z)
        # interpolate the simulated fields onto the stored mode grid, then overlap
        Z, Y = np.meshgrid(np.clip(zs, z[0], z[-1]), np.clip(ys, y[0], y[-1]), indexing="ij")
        pts = np.stack([Z, Y], -1)
        Ei = {c: RegularGridInterpolator((z, y), v.real)(pts) + 1j * RegularGridInterpolator((z, y), v.imag)(pts) for c, v in E.items()}
        a = integ(Ei["Ey"] * np.conj(M["Hz"]) - Ei["Ez"] * np.conj(M["Hy"]), ys, zs)
        b = integ(M["Ey"] * np.conj(Ei["Hz"]) - M["Ez"] * np.conj(Ei["Hy"]), ys, zs)
        c = integ(np.real(M["Ey"] * np.conj(M["Hz"]) - M["Ez"] * np.conj(M["Hy"])), ys, zs)
        d = integ(np.real(Ei["Ey"] * np.conj(Ei["Hz"]) - Ei["Ez"] * np.conj(Ei["Hy"])), ys, zs)
        eta[i, k] = np.real(a * b / c) / d
Prel = P / P[0]
Tmode = Prel * eta
xs = np.array([H[MON[p]]["x"][0] for p in PORTS])

fig, (a1, a2) = plt.subplots(1, 2, figsize=(13, 4.6))
for k in range(len(freqs)):
    a1.plot(xs[1:], Prel[1:, k], "o-", label=f"{lam[k]:.0f} nm")
    a2.plot(xs[1:], Tmode[1:, k], "s-", label=f"{lam[k]:.0f} nm")
a1.set_title("Total forward power through port plane / input power"); a1.set_xlabel("x (µm)"); a1.legend(fontsize=8)
a2.set_title("Power in each port's stored mode / input power"); a2.set_xlabel("x (µm)"); a2.legend(fontsize=8)
for ax in (a1, a2):
    ax.axvline(200.002, color="gray", ls=":", lw=1); ax.text(200.3, ax.get_ylim()[0], " SiN tip", color="gray", fontsize=8, va="bottom")
    ax.grid(alpha=0.3)
plt.tight_layout(); plt.savefig(f"{out}/fig5_power_and_mode_coupling.png", dpi=130); plt.close()

with open(f"{out}/port_power_table.txt", "w") as fh:
    fh.write("x_um  port          " + "  ".join(f"P/Pin@{l:.0f}nm" for l in lam) + "   " + "  ".join(f"mode/Pin@{l:.0f}nm" for l in lam) + "\n")
    for i, p in enumerate(PORTS):
        fh.write(f"{xs[i]:7.3f} {p:13s} " + "  ".join(f"{v:12.4f}" for v in Prel[i]) + "   " + "  ".join(f"{v:15.4f}" for v in Tmode[i]) + "\n")
print(open(f"{out}/port_power_table.txt").read())
print("figures written to", os.path.abspath(out))
