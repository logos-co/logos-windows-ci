#!/usr/bin/env python3
"""Assert every non-system DLL a staged PE imports is shipped beside it.

This is the gate that catches "plugin shipped without its DLLs" -- a class of
bug that is invisible on the build host (nothing links at load time on Linux)
and shows up on Windows only as a module that silently fails to load.

Reads: STAGE, MIN_PES, ASSEMBLE (JSON), OBJDUMP, FLAT_TREE.

FLAT_TREE=1 means STAGE is a single payload root rather than a stage holding
one directory per target -- used for the tree extracted out of a .lgx, where
"beside it" is the payload's own bin/ and lib/.
"""
import glob
import json
import os
import subprocess
import sys

OBJDUMP = os.environ["OBJDUMP"]
STAGE = os.environ.get("STAGE", "stage")
MIN_PES = int(os.environ.get("MIN_PES", "1"))
ASSEMBLE = json.loads(os.environ.get("ASSEMBLE") or "{}")
FLAT_TREE = os.environ.get("FLAT_TREE") == "1"

# Everything Windows itself provides. Anything else must ship with us.
SYSTEM = (
    "kernel32 ntdll user32 advapi32 ws2_32 mswsock shell32 ole32 "
    "oleaut32 version winmm netapi32 userenv authz mpr crypt32 "
    "bcrypt secur32 dbghelp psapi imm32 gdi32 comdlg32 shlwapi "
    "iphlpapi dnsapi wtsapi32 setupapi winspool rpcrt4 msvcrt "
    "uxtheme dwmapi d3d9 d3d11 d3d12 dxgi dwrite opengl32 wldap32 "
    "normaliz winhttp wininet ncrypt cfgmgr32 powrprof propsys "
    "oleacc avrt ucrtbase"
).split()


def search_dirs(pe_path):
    """Directories this PE's imports may legitimately resolve from.

    Default: the PE's own directory, plus bin/ and lib/ of the staged target it
    belongs to -- that is how every single-derivation target here is laid out.
    `assemble` widens it for apps built from several derivations.
    """
    if FLAT_TREE:
        root = STAGE
        target = ""
    else:
        rel = os.path.relpath(pe_path, STAGE)
        target = rel.split(os.sep)[0]
        root = os.path.join(STAGE, target)
    dirs = [os.path.dirname(pe_path), os.path.join(root, "bin"), os.path.join(root, "lib")]
    for extra in ASSEMBLE.get(target, []):
        dirs.append(os.path.join(STAGE, extra))
    seen, out = set(), []
    for d in dirs:
        if d not in seen and os.path.isdir(d):
            seen.add(d); out.append(d)
    return out


def is_system(dll):
    # Names are compared case-insensitively with the extension stripped, because
    # import tables mix spellings freely: real trees contain both "KERNEL32.dll"
    # and "KERNEL32.DLL", and "IPHLPAPI.DLL".
    b = os.path.splitext(dll)[0].lower()
    return b in SYSTEM or b.startswith("api-ms-win") or b.startswith("ext-ms-win")


pes = [p for p in glob.glob(os.path.join(STAGE, "**", "*"), recursive=True)
       if p.lower().endswith((".exe", ".dll")) and os.path.isfile(p)]

if len(pes) < MIN_PES:
    sys.exit(f"::error::only {len(pes)} PEs discovered (need >= {MIN_PES}) "
             "— refusing to pass vacuously")

bad = 0
for p in sorted(pes):
    r = subprocess.run([OBJDUMP, "-p", p], capture_output=True, text=True)
    # An objdump that errors would otherwise yield an empty import list,
    # indistinguishable from "no imports" -- i.e. a silent pass.
    if r.returncode != 0:
        print(f"::error::objdump failed on {p}: {r.stderr.strip()[:200]}")
        bad = 1
        continue
    # Second line of defence behind the action's `objdump --info` check.
    # This gate's verdict is worth exactly what its reader is worth, so make
    # the reader say out loud that it parsed a 64-bit PE. Measured on one real
    # tree: GNU mingw objdump says "file format pei-x86-64"; llvm-objdump says
    # "coff-x86-64"; the build host's own native binutils cannot parse the file
    # at all. Only the first is the reader this action pins, and accepting
    # anything else means accepting an import list nobody has calibrated.
    if "file format pei-x86-64" not in r.stdout:
        fmt = next((l for l in r.stdout.splitlines() if "file format" in l), "<none>")
        print(f"::error::{p}: objdump did not report a pei-x86-64 file format ({fmt.strip()})")
        print("::error::Either this is not a 64-bit PE or the objdump cannot read PEs;")
        print("::error::in both cases its import list means nothing.")
        bad = 1
        continue
    imports = [l.split("DLL Name:")[1].strip()
               for l in r.stdout.splitlines() if "DLL Name:" in l]
    if not imports:
        print(f"::error::{p} has no import table at all")
        bad = 1
        continue
    here = search_dirs(p)
    for dll in imports:
        if is_system(dll):
            continue
        if not any(os.path.exists(os.path.join(d, dll)) for d in here):
            print(f"::error::{p} imports {dll}, not found in {here}")
            bad = 1

print(f"checked {len(pes)} PEs")
sys.exit(bad)
