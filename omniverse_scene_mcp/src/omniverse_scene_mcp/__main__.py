from __future__ import annotations

import json
import asyncio
import sys
from dataclasses import asdict

from .runtime import build_router
from .server import main as serve_main

APPLY_FLAG = "--apply"


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] in {"serve", "server"}:
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
