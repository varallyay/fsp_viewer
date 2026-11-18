#!/usr/bin/env python3
r"""
build_windows.py - build FSP Viewer as a Windows application that runs without Python.

The result is a folder, "FSP Viewer", with "FSP Viewer.exe" and its libraries (_internal):
copy it anywhere (also to a server) and start the .exe; nothing is installed. The folder
is also packed as FSP-Viewer-<version>-windows.zip.

Run on Windows (PyInstaller builds only for the system it runs on):

    python -m pip install -r requirements-windows.txt
    python build_windows.py                      # into dist\ and build\ next to this file
    python build_windows.py --out C:\builds      # elsewhere
    python build_windows.py model.fsp            # the self-test also opens and plots model.fsp

The GitHub Actions workflow .github/workflows/windows.yml does the same for every release.
Steps: icon (.ico) and version information of the .exe, PyInstaller, a self-test of the
built program (it starts, and every library it needs is there; see fsp_gui.self_test),
then the .zip. On macOS / Linux the same steps make a test build without icon.
"""
import argparse
import os
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fsp_gui  # noqa: E402  (APP_NAME, APP_VERSION and the icon drawing)

NAME = fsp_gui.APP_NAME
VERSION = fsp_gui.APP_VERSION
WINDOWS = sys.platform.startswith("win")
ZIP_NAME = f"{NAME.replace(' ', '-')}-{VERSION}-windows.zip" if WINDOWS else \
    f"{NAME.replace(' ', '-')}-{VERSION}-{sys.platform}.zip"

# Large packages that are installed in many Python environments but not used by the viewer
EXCLUDE = ["PyQt5", "PyQt6", "PySide2", "PySide6", "IPython", "jupyter", "notebook", "pytest",
           "sphinx", "pandas", "sympy", "numba", "dask", "tensorflow", "torch"]


def write_icon(path):
    """Windows icon with the usual sizes, drawn by the program itself."""
    image = fsp_gui.app_icon_image(256, margin=0.03, rounding=0.18)
    image.save(path, sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)])


def write_version_info(path):
    """Version resource of the .exe (shown in its Properties > Details)."""
    nums = [int(p) for p in VERSION.split(".")[:4] if p.isdigit()]
    nums += [0] * (4 - len(nums))
    t = tuple(nums)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={t}, prodvers={t}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('FileDescription', '{fsp_gui.APP_DESCRIPTION}'),
      StringStruct('FileVersion', '{VERSION}'),
      StringStruct('InternalName', '{NAME}'),
      StringStruct('OriginalFilename', '{NAME}.exe'),
      StringStruct('ProductName', '{NAME}'),
      StringStruct('ProductVersion', '{VERSION}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""")


def main():
    parser = argparse.ArgumentParser(description="Build FSP Viewer as a Windows application.")
    parser.add_argument("fsp", nargs="?", help="an .fsp file the self-test opens and plots (optional)")
    parser.add_argument("--out", default=HERE, help="folder that gets dist and build (default: next to this file)")
    args = parser.parse_args()
    out = os.path.abspath(args.out)
    DIST, WORK = os.path.join(out, "dist"), os.path.join(out, "build")
    if not WINDOWS:
        print(f"Note: this is {sys.platform}, so this makes a test build for {sys.platform}, not a Windows program.")
    for folder in (os.path.join(DIST, NAME), WORK):
        shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)

    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
           "--name", NAME, "--distpath", DIST, "--workpath", WORK, "--specpath", WORK,
           "--hidden-import", "PIL._tkinter_finder"]
    for module in EXCLUDE:
        cmd += ["--exclude-module", module]
    if WINDOWS:
        icon, version_file = os.path.join(WORK, "fsp_viewer.ico"), os.path.join(WORK, "version_info.txt")
        write_icon(icon)
        write_version_info(version_file)
        cmd += ["--icon", icon, "--version-file", version_file]
    cmd.append(os.path.join(HERE, "fsp_gui.py"))
    print("Running:", " ".join(f'"{c}"' if " " in c else c for c in cmd), flush=True)
    subprocess.run(cmd, check=True)

    app_dir = os.path.join(DIST, NAME)
    exe = os.path.join(app_dir, NAME + (".exe" if WINDOWS else ""))
    if not os.path.isfile(exe):
        sys.exit(f"The build did not make {exe}.")

    # Self-test of the built program: does it start, with every library it needs?
    report = os.path.join(WORK, "self_test.txt")
    test_args = [exe, "--self-test", report] + ([os.path.abspath(args.fsp)] if args.fsp else [])
    result = subprocess.run(test_args, timeout=600)
    text = open(report, encoding="utf-8").read() if os.path.isfile(report) else "(no report written)\n"
    print("Self-test of the built program:\n" + text, flush=True)
    if result.returncode != 0:
        sys.exit(f"The self-test of {exe} failed (exit code {result.returncode}).")

    zip_path = os.path.join(DIST, ZIP_NAME)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for folder, _, files in os.walk(app_dir):
            for name in files:
                full = os.path.join(folder, name)
                z.write(full, os.path.relpath(full, DIST))  # the zip holds the folder "FSP Viewer"
    size = sum(os.path.getsize(os.path.join(f, n)) for f, _, ns in os.walk(app_dir) for n in ns)
    print(f"Built {app_dir} ({size / 1e6:.0f} MB) and {zip_path} ({os.path.getsize(zip_path) / 1e6:.0f} MB).")


if __name__ == "__main__":
    main()
