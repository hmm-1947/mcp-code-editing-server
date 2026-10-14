import os
import sys
from pathlib import Path


def _vendor_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "third_party" / "graphify"


def main(argv: list[str]) -> int:
    if not getattr(sys, "frozen", False):
        vendor = str(_vendor_dir())
        if vendor not in sys.path:
            sys.path.insert(0, vendor)
    os.environ.setdefault("GRAPHIFY_MAX_WORKERS", os.environ.get("CODY_GRAPH_WORKERS", "1"))
    os.environ.setdefault("GRAPHIFY_NO_TIPS", "1")
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    if not argv:
        print("error: no graphify command given", file=sys.stderr)
        return 2
    sys.argv = ["graphify", *argv]
    from graphify.cli import dispatch_command
    try:
        dispatch_command(argv[0])
    except SystemExit as exit_request:
        code = exit_request.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 1
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    if sys.path and Path(sys.path[0]).resolve() == Path(__file__).resolve().parent:
        sys.path.pop(0)
    sys.exit(main(sys.argv[1:]))
