"""CLI entrypoint shim so `python -m jobmaxxing.enrich` works (parallel to jobmaxxing.route).

The implementation lives in the enrichment package; this module exposes its `main` at the
top-level module path the workflow invokes.
"""

from .enrichment.enrich import main

if __name__ == "__main__":
    main()
