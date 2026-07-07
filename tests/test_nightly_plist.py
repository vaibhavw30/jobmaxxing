import plistlib
from pathlib import Path


def test_plist_is_valid_and_scheduled_at_midnight():
    data = plistlib.loads(Path("scripts/com.jobmaxxing.nightly.plist").read_bytes())
    assert data["Label"] == "com.jobmaxxing.nightly"
    assert data["StartCalendarInterval"] == {"Hour": 0, "Minute": 0}   # 12am local
    assert data["ProgramArguments"][-2:] == ["-m", "jobmaxxing.nightly"]
    assert data["RunAtLoad"] is False
