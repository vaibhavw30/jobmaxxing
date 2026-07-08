import re

# A single surrounding markdown fence (``` or ```lang) wrapping the WHOLE payload. LaTeX/JSON outputs
# never start with ``` so this only fires on a genuine wrapper (a broken .tex/JSON otherwise).
_CODE_FENCE = re.compile(r"\A```[^\n]*\n(.*)\n```\s*\Z", re.DOTALL)


def strip_code_fence(text: str) -> str:
    """Drop a single surrounding markdown code fence if a model wrapped the output; else unchanged."""
    m = _CODE_FENCE.match(text)
    return m.group(1).strip() if m else text
