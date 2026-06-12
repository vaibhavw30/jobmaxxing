import difflib


def unified_diff(base: str, tailored: str, *, fromfile: str = "base.tex", tofile: str = "tailored.tex") -> str:
    """A unified diff base->tailored so the operator sees exactly what changed."""
    lines = difflib.unified_diff(
        base.splitlines(keepends=True),
        tailored.splitlines(keepends=True),
        fromfile=fromfile,
        tofile=tofile,
    )
    return "".join(lines)
