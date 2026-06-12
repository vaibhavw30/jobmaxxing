"""CLI entrypoint shim so `python -m jobmaxxing.route` works (parallel to jobmaxxing.run).

The implementation lives in the routing package; this module just exposes its `main`
at the top-level module path that the workflow and README invoke.
"""

from .routing.route import main

if __name__ == "__main__":
    main()
