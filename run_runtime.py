"""Freezer entrypoint; source users can run python -m harbor_runtime."""
from harbor_runtime.__main__ import main

if __name__ == "__main__":
    try:
        main()
    except Exception:
        import sys
        print("Harbor runtime failed. Check configuration and component status.", file=sys.stderr)
        raise SystemExit(1) from None
