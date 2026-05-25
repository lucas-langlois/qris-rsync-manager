from __future__ import annotations

import re
from dataclasses import dataclass


RSYNC_PROGRESS_RE = re.compile(
    r"(?P<transferred>[\d,.]+(?:[KMGTPE]?i?B?|[KMGTPE])?)\s+"
    r"(?P<percent>\d{1,3})%\s+"
    r"(?P<speed>[\d,.]+[KMGTPE]?i?B?/s)\s+"
    r"(?P<eta>\d+:\d{2}(?::\d{2})?)",
    re.IGNORECASE,
)

TO_CHK_RE = re.compile(r"to-chk=(?P<remaining>\d+)/(?P<total>\d+)")


@dataclass
class RsyncProgress:
    percent: int
    transferred: str
    speed: str
    eta: str
    summary: str


def parse_rsync_progress(text: str) -> RsyncProgress | None:
    normalized = text.replace("\r", "\n")
    matches = list(RSYNC_PROGRESS_RE.finditer(normalized))
    if not matches:
        return None
    match = matches[-1]
    percent = max(0, min(100, int(match.group("percent"))))
    line_start = text.rfind("\n", 0, match.start()) + 1
    line_end = text.find("\n", match.end())
    if line_end == -1:
        line_end = len(text)
    summary = " ".join(text[line_start:line_end].replace("\r", " ").split())
    return RsyncProgress(
        percent=percent,
        transferred=match.group("transferred"),
        speed=match.group("speed"),
        eta=match.group("eta"),
        summary=summary,
    )
