from __future__ import annotations

import json
import asyncio
import sys
from dataclasses import asdict

APPLY_FLAG = "--apply"


def build_router():
    """Imported lazily so `doctor` runs without the router's heavy dependencies; tests may replace this."""
    from .runtime import build_router as build

    return build()


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "doctor":
        # Read-only connection check. Imported first and alone: it must work even when the router (or its MCP
        # library) cannot start, which is exactly when you need it.
        from .doctor import main as doctor_main

        return doctor_main(args[1:])
    if args and args[0] in {"serve", "server"}:
        from .server import main as serve_main

        return serve_main(args[1:])

    # Preview by default: scene edits run as dry runs unless --apply is passed.
    apply = APPLY_FLAG in args
    args = [arg for arg in args if arg != APPLY_FLAG]
    router = build_router()
    if args:
        request = " ".join(args)
        result = asyncio.run(router.handle(request, dry_run=not apply))
        output = {"decision": asdict(result.decision), "payload": result.payload, "dry_run": not apply}
        if not apply:
            output["note"] = f"Preview only: scene edits that support dry run were not applied. Add {APPLY_FLAG} to apply."
        print(json.dumps(output, indent=2, default=str))
    else:
        print(json.dumps(router.scene_summary(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
