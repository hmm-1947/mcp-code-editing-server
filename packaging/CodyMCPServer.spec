# -*- mode: python ; coding: utf-8 -*-
# Build with packaging\build.ps1 (it stages bin\ngrok.exe and bin\rg.exe first).
#
# One bundle, two executables sharing the same _internal folder:
#   CodyMCPServer.exe  windowed GUI + tray (what the user launches)
#   cody-server.exe    console MCP server, started hidden by the GUI (no Python needed)
import os
import sys

from PyInstaller.utils.hooks import (
    collect_all, collect_data_files, collect_submodules, copy_metadata,
)

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
EXE_EXT = ".exe" if IS_WIN else ""

ROOT = os.path.dirname(os.path.abspath(SPECPATH))
ICON = os.path.join(ROOT, "assets", "icon.icns" if IS_MAC else "icon.ico")
BIN_STAGE = os.path.join(SPECPATH, "bin")

# ---- server side: fastmcp is heavy on dynamic imports and package metadata --------------
srv_datas, srv_binaries, srv_hidden = [], [], []

# Native language grammars: small, need their compiled extension modules.
for package in (
    "tree_sitter", "tree_sitter_python", "tree_sitter_java",
    "tree_sitter_javascript", "tree_sitter_typescript", "tree_sitter_dart",
    "tree_sitter_go", "tree_sitter_rust", "tree_sitter_groovy", "tree_sitter_c", "tree_sitter_cpp",
    "tree_sitter_ruby", "tree_sitter_c_sharp", "tree_sitter_kotlin", "tree_sitter_scala",
    "tree_sitter_php", "tree_sitter_swift", "tree_sitter_lua", "tree_sitter_zig",
    "tree_sitter_powershell", "tree_sitter_elixir", "tree_sitter_objc", "tree_sitter_julia",
    "tree_sitter_verilog", "tree_sitter_fortran", "tree_sitter_bash", "tree_sitter_json",
    "numpy", "rapidfuzz",
):
    datas, binaries, hidden = collect_all(package)
    srv_datas += datas
    srv_binaries += binaries
    srv_hidden += hidden

# fastmcp / mcp ship CLI and experimental subpackages that need optional extras
# (typer, openai) and exit or fail on import. The server never uses them.
SKIP = ("mcp.cli", "fastmcp.cli", "fastmcp.experimental")


def _keep(name):
    return not any(name == prefix or name.startswith(prefix + ".") for prefix in SKIP)


for package in ("fastmcp", "mcp"):
    srv_hidden += collect_submodules(package, filter=_keep, on_error="ignore")
    srv_datas += collect_data_files(package)

# Loaded by name at runtime (event loops, protocol impls, backends, keyring backends).
for package in ("uvicorn", "anyio", "websockets", "keyring"):
    srv_hidden += collect_submodules(package, on_error="ignore")

for distribution in ("fastmcp", "mcp"):
    try:
        srv_datas += copy_metadata(distribution, recursive=True)
    except Exception as error:  # a missing dist must not silently ship a broken server
        raise SystemExit(f"cannot collect metadata for {distribution}: {error}")

for package in ("cody_cli", "core", "code_engine"):
    srv_hidden += collect_submodules(package)
srv_hidden += ["server", "config", "app_paths"]

VENDOR = os.path.join(ROOT, "third_party", "graphify")
GRAPHIFY_PKG = os.path.join(VENDOR, "graphify")
if not os.path.isdir(GRAPHIFY_PKG):
    raise SystemExit(f"missing bundled graphify at {GRAPHIFY_PKG}")
for folder, _dirs, files in os.walk(GRAPHIFY_PKG):
    if "__pycache__" in folder:
        continue
    relative = os.path.relpath(folder, VENDOR).replace(os.sep, ".")
    for file in files:
        if file.endswith(".py"):
            module = relative if file == "__init__.py" else relative + "." + file[:-3]
            srv_hidden.append(module)
srv_hidden += collect_submodules("networkx", on_error="ignore")

srv_datas += [
    (os.path.join(VENDOR, "assets"), os.path.join("third_party", "graphify", "assets")),
    (os.path.join(VENDOR, "LICENSE"), os.path.join("licenses", "graphify")),
    (os.path.join(VENDOR, "LICENSE-MIT"), os.path.join("licenses", "graphify")),
    (os.path.join(VENDOR, "NOTICE"), os.path.join("licenses", "graphify")),
]

shared_datas = [(os.path.join(ROOT, "assets"), "assets")]
for tool in ("ngrok" + EXE_EXT, "rg" + EXE_EXT):
    path = os.path.join(BIN_STAGE, tool)
    if not os.path.isfile(path):
        raise SystemExit(f"missing {path} - run packaging{os.sep}{'build_mac.sh' if IS_MAC else 'build.ps1'}, which stages it")
    shared_datas.append((path, "bin"))

# ---- GUI -------------------------------------------------------------------------------
gui = Analysis(
    [os.path.join(ROOT, "mcp_gui.py")],
    pathex=[ROOT],
    datas=shared_datas,
    hiddenimports=[] if IS_MAC else ["pystray._win32", "PIL._tkinter_finder"],
    excludes=["fastmcp", "mcp", "tree_sitter", "uvicorn", "starlette", "pydantic"] + (["pystray", "PIL"] if IS_MAC else []),
    noarchive=False,
)
gui_pyz = PYZ(gui.pure)
gui_exe = EXE(
    gui_pyz, gui.scripts, [],
    exclude_binaries=True,
    name="CodyMCPServer",
    icon=ICON,
    console=False,          # no console window: it is a desktop app
    upx=False,
)

# ---- server ----------------------------------------------------------------------------
srv = Analysis(
    [os.path.join(ROOT, "server.py")],
    pathex=[ROOT, VENDOR],
    datas=srv_datas + shared_datas,
    binaries=srv_binaries,
    hiddenimports=srv_hidden,
    excludes=["tkinter", "pystray", "PIL"],
    noarchive=False,
)
srv_pyz = PYZ(srv.pure)
srv_exe = EXE(
    srv_pyz, srv.scripts, [],
    exclude_binaries=True,
    name="cody-server",
    icon=ICON,
    console=True,           # real stdio for logging; the GUI hides the window when it spawns it
    upx=False,
)

coll = COLLECT(
    gui_exe, gui.binaries, gui.datas,
    srv_exe, srv.binaries, srv.datas,
    strip=False,
    upx=False,
    name="CodyMCPServer",
)

if IS_MAC:
    app = BUNDLE(
        coll,
        name="CodyMCPServer.app",
        icon=ICON,
        bundle_identifier="com.joshua.codymcpserver",
        info_plist={
            "CFBundleName": "Cody MCP Server",
            "CFBundleDisplayName": "Cody MCP Server",
            "CFBundleShortVersionString": os.environ.get("APP_VERSION", "1.0.0"),
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
        },
    )
