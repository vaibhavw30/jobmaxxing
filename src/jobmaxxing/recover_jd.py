"""CLI shim: `python -m jobmaxxing.recover_jd` (run LOCALLY on a residential IP)."""

from .recovery.recover import main

if __name__ == "__main__":
    main()
