"""CLI shim: `python -m jobmaxxing.enrich_workday` (run LOCALLY; needs the `headless` extra)."""

from .enrichment.workday import main

if __name__ == "__main__":
    main()
