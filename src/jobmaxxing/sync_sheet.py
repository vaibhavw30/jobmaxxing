"""CLI shim: `python -m jobmaxxing.sync_sheet` (run LOCALLY; needs the `sheets` extra)."""

from .sheets.sync import main

if __name__ == "__main__":
    main()
