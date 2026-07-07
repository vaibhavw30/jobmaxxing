"""CLI shim: `python -m jobmaxxing.nightly` (local macOS nightly scheduler entrypoint)."""

from .scheduling.nightly import main

if __name__ == "__main__":
    main()
