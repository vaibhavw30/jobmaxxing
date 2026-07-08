"""CLI shim: `python -m jobmaxxing.discover_gmail` (run LOCALLY; needs GMAIL_* env vars)."""

from .discovery.gmail_source import main

if __name__ == "__main__":
    main()
