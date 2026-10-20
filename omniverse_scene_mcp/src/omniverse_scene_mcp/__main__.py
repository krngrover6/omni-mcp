from __future__ import annotations

import json
import asyncio
import sys
from dataclasses import asdict

from .runtime import build_router
from .server import main as serve_main


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] in {"serve", "server"}:
        return serve_main(args[1:])

    router = build_router()
    if args:
        request = " ".join(args)
        result = asyncio.run(router.handle(request, dry_run=False))
        print(json.dumps({"decision": asdict(result.decision), "payload": result.payload}, indent=2, default=str))
    else:
        print(json.dumps(router.scene_summary(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
