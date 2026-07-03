"""CLI shim: `python -m jobmaxxing.discover_jobspy` (run LOCALLY; needs the `discovery` extra)."""

from .discovery.jobspy_source import main

if __name__ == "__main__":
    main()
