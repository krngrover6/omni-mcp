"""Test against installed Kit USD bindings without starting Kit or contacting a bridge.

Run with a Python matching Kit's major/minor (3.12 for Isaac Sim 6.1):
  python -I omniverse_scene_mcp/tools/check_kit_usd.py isaacsim-6.1.0
Only loads USD DLLs and runs tests on temporary stages. No app, service or reload.
"""
from __future__ import annotations

import os
import ctypes
from pathlib import Path
import sys


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit("Usage: check_kit_usd.py <installed Kit/Isaac directory> [test selectors]")
    root = Path(sys.argv[1]).resolve()
    candidates = [p for base in (root / "extscache", root / "exts", root / "kit" / "exts")
                  for p in base.glob("omni.usd.libs-*") if (p / "pxr").is_dir()]
    if len(candidates) != 1:
        raise SystemExit(f"Expected one installed omni.usd.libs, found {len(candidates)}; no runtime changed")
    usd = candidates[0]
    handles = [os.add_dll_directory(str(p)) for p in (usd / "bin", root / "kit", root / "kit" / "python") if p.is_dir()]
    sys.path.insert(0, str(usd))
    from pxr import Plug, Usd
    # Kit normally supplies this plugin directory during app startup. Register
    # the installed resources explicitly in this app-free compatibility process.
    Plug.Registry().RegisterPlugins(str(usd / "bin" / "usd"))
    version = Usd.GetVersion()
    print(f"Installed Kit USD: {version}; bindings: {usd}", flush=True)
    if version != (0, 25, 11):
        raise SystemExit(f"Expected USD 25.11 for this compatibility check, found {version}")
    # Plug's Windows loader does not use CPython's AddDllDirectory search flags.
    # Preload these installed validator DLLs with the same scoped DLL directories;
    # their transitive dependencies then resolve without changing PATH or Kit.
    handles.extend(ctypes.WinDLL(str(p)) for p in sorted((usd / "bin").glob("usd_*Validators.dll")))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import run_tests
    return run_tests.main(sys.argv[2:] or ['-q'])


if __name__ == "__main__":
    raise SystemExit(main())
