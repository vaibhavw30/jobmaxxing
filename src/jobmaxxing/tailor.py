"""CLI entrypoint shim so `python -m jobmaxxing.tailor` works (parallel to jobmaxxing.run / .route).

The implementation lives in the tailoring package; this exposes its `main` at the top-level
module path the README and operator commands invoke.
"""

from .tailoring.tailor import main

if __name__ == "__main__":
    main()
