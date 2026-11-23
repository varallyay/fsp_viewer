#!/usr/bin/env python3
r"""
build_app.py - build FSP Viewer as an application that runs without Python, packed for a release.

PyInstaller packs the program for the system it runs on, so each package is built on its own
system (the GitHub Actions workflow .github/workflows/release.yml does all of them):

    Windows  FSP-Viewer-<version>-windows.zip       the folder "FSP Viewer" with "FSP Viewer.exe";
                                                    unpack anywhere and start, nothing is installed
    macOS    FSP-Viewer-<version>-macos-<cpu>.dmg   "FSP Viewer.app" to drag to Applications
                                                    (cpu: arm64 = Apple silicon, x86_64 = Intel)
    Linux    fsp-viewer_<version>_amd64.deb         packages that put the program in /opt/fsp-viewer,
             fsp-viewer-<version>-1.x86_64.rpm      with a menu entry and the command fsp-viewer

The names come from fsp_gui.release_asset_name: a packaged program finds its update in the
newest GitHub release by them. Usage:

    python -m pip install -r requirements-build.txt
    python build_app.py                       # the packages of this system, into dist/release
    python build_app.py --formats deb         # only some of them (zip, dmg, deb, rpm)
    python build_app.py --out /tmp/build      # dist/ and build/ elsewhere
    python build_app.py model.fsp             # the self-test also opens and plots model.fsp

Steps: icon and version information, PyInstaller, a self-test of the built program (it starts,
and every library it needs is there; see fsp_gui.self_test), then the packages.
Linux needs dpkg-deb for the .deb and rpmbuild for the .rpm (Debian / Ubuntu: apt install rpm),
and xvfb-run for the self-test where there is no display.
"""
import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fsp_gui  # noqa: E402  (APP_NAME, APP_VERSION, the icon drawing and the package names)

NAME = fsp_gui.APP_NAME
VERSION = fsp_gui.APP_VERSION
WINDOWS = sys.platform.startswith("win")
MAC = sys.platform == "darwin"
LINUX = sys.platform.startswith("linux")
ARCH = fsp_gui.machine_arch()
PKG = fsp_gui.LINUX_PACKAGE                 # Linux package, executable and command name
PKG_DIR = fsp_gui.LINUX_PACKAGE_DIR         # where the Linux packages put the program
MAINTAINER = f"{NAME} developers <{fsp_gui.APP_WEBSITE}>"
SUMMARY = fsp_gui.APP_DESCRIPTION
DESCRIPTION = ("Views Ansys Lumerical FDTD project files (.fsp) and their monitor data (.h5) without a "
               "Lumerical license, plots their results, and saves the geometry as GDS or as a gdsfactory script.")
MIME_TYPE = "application/x-lumerical-fsp"

# Large packages that are installed in many Python environments but not used by the viewer
EXCLUDE = ["PyQt5", "PyQt6", "PySide2", "PySide6", "IPython", "jupyter", "notebook", "pytest",
           "sphinx", "pandas", "sympy", "numba", "dask", "tensorflow", "torch"]


def run(cmd, **kw):
    print("Running:", " ".join(f'"{c}"' if " " in str(c) else str(c) for c in cmd), flush=True)
    return subprocess.run(cmd, check=True, **kw)


# ---- Windows ---------------------------------------------------------------------
def write_ico(path):
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


def make_zip(app_dir, dist, out):
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for folder, _, files in os.walk(app_dir):
            for name in files:
                full = os.path.join(folder, name)
                z.write(full, os.path.relpath(full, dist))  # the zip holds the folder "FSP Viewer"


# ---- macOS -----------------------------------------------------------------------
def mac_minimum_version(app):
    """The newest macOS that one of the libraries in the bundle was built for (e.g. NumPy's for
    Apple silicon need 14.0): the bundle cannot run on anything older."""
    newest = (11, 0)
    if not shutil.which("otool"):
        return "11.0"
    for folder, _, files in os.walk(os.path.join(app, "Contents")):
        for name in files:
            path = os.path.join(folder, name)
            if os.path.islink(path) or not (name.endswith((".so", ".dylib")) or os.access(path, os.X_OK)):
                continue
            out = subprocess.run(["otool", "-l", path], capture_output=True, text=True).stdout
            lines = out.splitlines()
            for i, line in enumerate(lines):
                words = line.split()
                if words[:1] == ["minos"] or (words[:1] == ["version"] and i >= 2
                                              and "LC_VERSION_MIN_MACOSX" in lines[i - 2]):
                    try:
                        newest = max(newest, tuple(int(p) for p in words[1].split(".")[:2]))
                    except ValueError:
                        pass
    return ".".join(map(str, newest))


def finish_mac_app(app):
    """Version, document types and the like in Info.plist (PyInstaller writes 0.0.0), then sign
    the bundle again (ad hoc: there is no Apple Developer certificate) after the change."""
    plist = os.path.join(app, "Contents", "Info.plist")
    with open(plist, "rb") as f:
        info = plistlib.load(f)
    info.update({
        "CFBundleName": NAME, "CFBundleDisplayName": NAME, "CFBundleIdentifier": fsp_gui.APP_ID,
        "CFBundleShortVersionString": VERSION, "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": mac_minimum_version(app), "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.education",
        "NSHumanReadableCopyright": f"GPL-3.0, {fsp_gui.APP_WEBSITE}",
        # Lumerical project files can be opened with the program (Finder: Open With)
        "CFBundleDocumentTypes": [{"CFBundleTypeName": "Lumerical FDTD project", "CFBundleTypeRole": "Viewer",
                                   "CFBundleTypeExtensions": ["fsp"], "LSHandlerRank": "Alternate"}],
    })
    with open(plist, "wb") as f:
        plistlib.dump(info, f)
    print(f"{NAME}.app needs macOS {info['LSMinimumSystemVersion']} or newer.", flush=True)
    run(["codesign", "--force", "--deep", "--sign", "-", app])


def make_dmg(app, work, out):
    """A disk image with the .app and a link to Applications to drag it onto."""
    stage = os.path.join(work, "dmg")
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage)
    run(["ditto", app, os.path.join(stage, os.path.basename(app))])
    os.symlink("/Applications", os.path.join(stage, "Applications"))
    if os.path.exists(out):
        os.remove(out)
    for attempt in range(4):  # hdiutil on build machines sometimes fails with "Resource busy"
        try:
            run(["hdiutil", "create", "-volname", f"{NAME} {VERSION}", "-srcfolder", stage,
                 "-fs", "HFS+", "-format", "UDZO", "-imagekey", "zlib-level=9", "-ov", out])
            return
        except subprocess.CalledProcessError:
            if attempt == 3:
                raise
            time.sleep(5)


# ---- Linux -----------------------------------------------------------------------
def linux_tree(app_dir, root):
    """The files of the Linux packages, under root as they are installed."""
    shutil.rmtree(root, ignore_errors=True)
    shutil.copytree(app_dir, root + PKG_DIR, symlinks=True)
    os.makedirs(os.path.join(root, "usr", "bin"))
    os.symlink(f"{PKG_DIR}/{PKG}", os.path.join(root, "usr", "bin", PKG))
    apps = os.path.join(root, "usr", "share", "applications")
    os.makedirs(apps)
    with open(os.path.join(apps, PKG + ".desktop"), "w", encoding="utf-8") as f:
        f.write("\n".join([
            "[Desktop Entry]", "Type=Application", f"Name={NAME}", "GenericName=Lumerical FSP Viewer",
            f"Comment={SUMMARY}", f"Exec={PKG_DIR}/{PKG} %f", f"Icon={PKG}", "Terminal=false",
            "Categories=Science;Education;", f"MimeType={MIME_TYPE};", f"StartupWMClass={fsp_gui.APP_CLASS}",
        ]) + "\n")
    for size in (48, 64, 128, 256, 512):
        folder = os.path.join(root, "usr", "share", "icons", "hicolor", f"{size}x{size}", "apps")
        os.makedirs(folder)
        fsp_gui.app_icon_image(size, margin=0.06).save(os.path.join(folder, PKG + ".png"))
    mime = os.path.join(root, "usr", "share", "mime", "packages")
    os.makedirs(mime)
    with open(os.path.join(mime, PKG + ".xml"), "w", encoding="utf-8") as f:
        f.write(f"""<?xml version="1.0" encoding="UTF-8"?>
<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">
  <mime-type type="{MIME_TYPE}">
    <comment>Lumerical FDTD project</comment>
    <glob pattern="*.fsp"/>
  </mime-type>
</mime-info>
""")
    for path, dirs, files in os.walk(root):  # readable by everyone, whatever the umask was
        os.chmod(path, 0o755)
        for name in files:
            full = os.path.join(path, name)
            if not os.path.islink(full):
                os.chmod(full, 0o755 if os.stat(full).st_mode & 0o111 else 0o644)


# run after installing and removing: the menus, file types and icons are read again
REFRESH_SCRIPT = """\
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q /usr/share/applications || true
command -v update-mime-database >/dev/null 2>&1 && update-mime-database /usr/share/mime || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t -f /usr/share/icons/hicolor || true
"""


def set_package_format(root, kind):
    """The program reads this file to know which package to update from (fsp_gui.packaged_kind)."""
    with open(root + f"{PKG_DIR}/{fsp_gui.PACKAGE_FORMAT_FILE}", "w", encoding="utf-8") as f:
        f.write(kind + "\n")
    os.chmod(root + f"{PKG_DIR}/{fsp_gui.PACKAGE_FORMAT_FILE}", 0o644)


def tree_size_kb(root):
    return sum(os.lstat(os.path.join(p, n)).st_size for p, _, ns in os.walk(root) for n in ns) // 1024 + 1


def make_deb(root, work, out):
    if not shutil.which("dpkg-deb"):
        sys.exit("dpkg-deb is needed for the .deb package (it is part of Debian and Ubuntu).")
    set_package_format(root, "deb")
    control_dir = os.path.join(root, "DEBIAN")
    shutil.rmtree(control_dir, ignore_errors=True)
    os.makedirs(control_dir)
    try:  # the program needs at least the C library of the build machine
        libc = (os.confstr("CS_GNU_LIBC_VERSION") or "glibc 2.35").split()[-1]
    except (ValueError, OSError):
        libc = "2.35"
    arch = {"x86_64": "amd64"}.get(ARCH, ARCH)
    with open(os.path.join(control_dir, "control"), "w", encoding="utf-8") as f:
        f.write(f"""Package: {PKG}
Version: {VERSION}
Section: science
Priority: optional
Architecture: {arch}
Installed-Size: {tree_size_kb(root)}
Depends: libc6 (>= {libc}), libx11-6, libxft2, libfontconfig1
Maintainer: {MAINTAINER}
Homepage: {fsp_gui.APP_WEBSITE}
Description: {SUMMARY}
 {DESCRIPTION}
""")
    for script in ("postinst", "postrm"):
        path = os.path.join(control_dir, script)
        with open(path, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\nset -e\n" + REFRESH_SCRIPT + "exit 0\n")
        os.chmod(path, 0o755)
    os.chmod(control_dir, 0o755)
    os.chmod(os.path.join(control_dir, "control"), 0o644)
    try:
        run(["dpkg-deb", "--root-owner-group", "-Zxz", "--build", root, out])
    finally:
        shutil.rmtree(control_dir, ignore_errors=True)


def make_rpm(root, work, out):
    if not shutil.which("rpmbuild"):
        sys.exit("rpmbuild is needed for the .rpm package (Fedora: dnf install rpm-build; "
                 "Debian / Ubuntu: apt install rpm).")
    set_package_format(root, "rpm")
    top = tempfile.mkdtemp(prefix="rpmbuild-")  # rpm macros cannot hold a path with spaces
    for sub in ("BUILD", "BUILDROOT", "RPMS", "SOURCES", "SPECS", "SRPMS"):
        os.makedirs(os.path.join(top, sub))
    arch = {"arm64": "aarch64"}.get(ARCH, ARCH)
    spec = os.path.join(top, "SPECS", PKG + ".spec")
    with open(spec, "w", encoding="utf-8") as f:
        f.write(f"""# The program is built by PyInstaller already: rpmbuild only packs the files
%define _topdir {top}
%define _rpmdir {top}/RPMS
%define _rpmfilename %%{{NAME}}-%%{{VERSION}}-%%{{RELEASE}}.%%{{ARCH}}.rpm
%define debug_package %{{nil}}
%define _build_id_links none
%define __strip /bin/true
%global __os_install_post %{{nil}}
%global __arch_install_post %{{nil}}
%define _binary_payload w9.xzdio

Name:       {PKG}
Version:    {VERSION}
Release:    1
Summary:    {SUMMARY}
License:    GPL-3.0-or-later
URL:        {fsp_gui.APP_WEBSITE}
AutoReqProv: no
Requires:   libX11.so.6()(64bit), libXft.so.2()(64bit), libfontconfig.so.1()(64bit)

%description
{DESCRIPTION}

%install
mkdir -p "%{{buildroot}}"
cp -a "{root}/." "%{{buildroot}}/"

%post
{REFRESH_SCRIPT}
%postun
{REFRESH_SCRIPT}
%files
%defattr(-,root,root,-)
{PKG_DIR}
/usr/bin/{PKG}
/usr/share/applications/{PKG}.desktop
/usr/share/icons/hicolor/*/apps/{PKG}.png
/usr/share/mime/packages/{PKG}.xml
""")
    run(["rpmbuild", "-bb", "--target", arch, spec])
    built = os.path.join(top, "RPMS", os.path.basename(out))
    if not os.path.isfile(built):
        sys.exit(f"rpmbuild did not make {built}.")
    shutil.move(built, out)
    shutil.rmtree(top, ignore_errors=True)


# ---- all -------------------------------------------------------------------------
def test_update_swap(kind, package, work):
    """Update a copy of the built program from the package the way a packaged program updates
    itself (fsp_gui.prepare_windows_swap / prepare_mac_swap and the script they write), while a
    stand-in process runs: the copy must be replaced only after that process has ended."""
    test = os.path.join(work, "update-test")
    shutil.rmtree(test, ignore_errors=True)
    os.makedirs(test)
    if kind == "zip":
        with zipfile.ZipFile(package) as z:
            z.extractall(test)
        installed = os.path.join(test, NAME)
        target = {"exe": os.path.join(installed, NAME + ".exe")}
        prepare = fsp_gui.prepare_windows_swap
    else:
        installed = os.path.join(test, NAME + ".app")
        run(["ditto", os.path.join(os.path.dirname(os.path.dirname(package)), NAME + ".app"), installed])
        target = {"app": installed}
        prepare = fsp_gui.prepare_mac_swap
    marker = os.path.join(installed, "OLD-VERSION-MARKER")
    open(marker, "w").close()
    stand_in = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    # Started from Explorer, the program runs in its own folder: the swap must work from there
    here = os.getcwd()
    os.chdir(installed)
    try:
        fsp_gui.start_detached(prepare(package, relaunch=False, pid=stand_in.pid, **target))
    finally:
        os.chdir(here)
    time.sleep(2)
    if not os.path.exists(marker):
        sys.exit("Update test: the program was replaced while it was still running.")
    stand_in.wait()
    deadline = time.time() + 120
    while time.time() < deadline and (os.path.exists(marker) or len(os.listdir(test)) > 1):
        time.sleep(1)
    left = sorted(os.listdir(test))
    if os.path.exists(marker) or left != [os.path.basename(installed)]:
        sys.exit(f"Update test: the program was not replaced (left in {test}: {left}).")
    print(f"Update test: {os.path.basename(installed)} was replaced by the new version after the program ended.")
    shutil.rmtree(test, ignore_errors=True)



def self_test(exe, work, fsp):
    """Start the built program in its self-test: does it start, with every library it needs?"""
    report = os.path.join(work, "self_test.txt")
    cmd = [exe, "--self-test", report] + ([os.path.abspath(fsp)] if fsp else [])
    if LINUX and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) and shutil.which("xvfb-run"):
        cmd = ["xvfb-run", "-a"] + cmd
    result = subprocess.run(cmd, timeout=600)
    text = open(report, encoding="utf-8").read() if os.path.isfile(report) else "(no report written)\n"
    print("Self-test of the built program:\n" + text, flush=True)
    if result.returncode != 0:
        sys.exit(f"The self-test of {exe} failed (exit code {result.returncode}).")


def main():
    default_formats = "zip" if WINDOWS else "dmg" if MAC else "deb,rpm" if LINUX else ""
    parser = argparse.ArgumentParser(description=f"Build {NAME} {VERSION} as an application without Python.")
    parser.add_argument("fsp", nargs="?", help="an .fsp file the self-test opens and plots (optional)")
    parser.add_argument("--out", default=HERE, help="folder that gets dist and build (default: next to this file)")
    parser.add_argument("--no-update-test", action="store_true",
                        help="skip the test that updates a copy of the program from the .zip / .dmg")
    parser.add_argument("--formats", default=default_formats,
                        help=f"packages to make, of zip (Windows), dmg (macOS), deb and rpm (Linux); "
                             f"default here: {default_formats or 'none'}")
    args = parser.parse_args()
    formats = [f.strip() for f in args.formats.split(",") if f.strip()]
    allowed = {"zip"} if WINDOWS else {"dmg"} if MAC else {"deb", "rpm"} if LINUX else set()
    if not formats or set(formats) - allowed:
        sys.exit(f"On {sys.platform} only these packages can be made: {', '.join(sorted(allowed)) or 'none'}.")

    out = os.path.abspath(args.out)
    dist, work = os.path.join(out, "dist"), os.path.join(out, "build")
    release = os.path.join(dist, "release")
    name = PKG if LINUX else NAME  # Linux: a command name without a space
    for folder in (os.path.join(dist, name), os.path.join(dist, name + ".app"), work):
        shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    os.makedirs(release, exist_ok=True)

    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
           "--name", name, "--distpath", dist, "--workpath", work, "--specpath", work,
           "--hidden-import", "PIL._tkinter_finder"]
    for module in EXCLUDE:
        cmd += ["--exclude-module", module]
    if WINDOWS:
        icon, version_file = os.path.join(work, "fsp_viewer.ico"), os.path.join(work, "version_info.txt")
        write_ico(icon)
        write_version_info(version_file)
        cmd += ["--icon", icon, "--version-file", version_file]
    elif MAC:
        icon = os.path.join(work, "AppIcon.icns")
        fsp_gui.app_icon_image(1024).save(icon, format="ICNS")
        cmd += ["--icon", icon, "--osx-bundle-identifier", fsp_gui.APP_ID]
    cmd.append(os.path.join(HERE, "fsp_gui.py"))
    run(cmd)

    app_dir = os.path.join(dist, name)
    if MAC:
        app = os.path.join(dist, NAME + ".app")
        finish_mac_app(app)
        exe = os.path.join(app, "Contents", "MacOS", NAME)
    else:
        exe = os.path.join(app_dir, name + (".exe" if WINDOWS else ""))
    if not os.path.isfile(exe):
        sys.exit(f"The build did not make {exe}.")
    self_test(exe, work, args.fsp)

    made = []
    for kind in formats:
        target = os.path.join(release, fsp_gui.release_asset_name(VERSION, kind, ARCH))
        if kind == "zip":
            make_zip(app_dir, dist, target)
        elif kind == "dmg":
            make_dmg(app, work, target)
        else:
            root = os.path.join(work, "linux-root")
            if not os.path.isdir(root + PKG_DIR):
                linux_tree(app_dir, root)
            (make_deb if kind == "deb" else make_rpm)(root, work, target)
        made.append(target)
        if kind in ("zip", "dmg") and not args.no_update_test:
            test_update_swap(kind, target, work)
    for path in made:
        print(f"Built {path} ({os.path.getsize(path) / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
