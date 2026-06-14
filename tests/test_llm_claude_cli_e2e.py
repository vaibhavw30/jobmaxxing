"""Real `claude -p` round-trip. Skipped unless JOBMAXXING_E2E=1 and the claude CLI is present
(mirrors the Workday e2e + pdflatex skip pattern). Confirms subscription auth + the exact
invocation work on the operator's machine. Run: JOBMAXXING_E2E=1 uv run pytest tests/test_llm_claude_cli_e2e.py -v
"""

import os
import shutil

import pytest

from jobmaxxing.llm import providers

pytestmark = pytest.mark.skipif(
    os.environ.get("JOBMAXXING_E2E") != "1" or shutil.which("claude") is None,
    reason="set JOBMAXXING_E2E=1 and have the claude CLI logged in to run this",
)


def test_claude_cli_real_roundtrip():
    messages = [
        {"role": "system", "content": "You output only what is asked, nothing else."},
        {"role": "user", "content": "Reply with the single word PONG."},
    ]
    out = providers.call_provider("claude-cli", "sonnet", messages, max_tokens=20)
    assert out and "PONG" in out.upper()
