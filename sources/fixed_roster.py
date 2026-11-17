"""Validated fixed roster for the known CSSCI WeChat profile links."""

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from urllib.parse import urlparse

from sources.journal_catalog import ProfileLinkTarget, parse_profile_links_file


@dataclass(frozen=True)
class FixedRoster:
    source_file: str
    source_sha256: str
    expected_count: int
    targets: tuple[ProfileLinkTarget, ...]

    @property
    def count(self) -> int:
        return len(self.targets)


def load_fixed_roster(path: str | Path, expected_count: int = 126) -> FixedRoster:
    source = Path(path)
    if expected_count < 1:
        raise ValueError("fixed_roster_count_must_be_positive")
    targets = tuple(parse_profile_links_file(str(source)))
    if len(targets) != expected_count:
        raise ValueError(f"fixed_roster_count:{len(targets)}")

    seen_biz: set[str] = set()
    seen_names: set[str] = set()
    for target in targets:
        if target.biz in seen_biz:
            raise ValueError(f"duplicate_roster_biz:{target.biz}")
        if target.journal_name in seen_names:
            raise ValueError(f"duplicate_roster_journal:{target.journal_name}")
        parsed = urlparse(target.profile_url)
        if parsed.scheme != "https" or parsed.hostname != "mp.weixin.qq.com" or parsed.path != "/mp/profile_ext":
            raise ValueError(f"invalid_roster_profile_url:{target.journal_name}")
        if not re.fullmatch(r"[A-Za-z0-9_=-]{1,200}", target.biz):
            raise ValueError(f"invalid_roster_biz:{target.journal_name}")
        seen_biz.add(target.biz)
        seen_names.add(target.journal_name)

    return FixedRoster(
        source_file=str(source),
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        expected_count=expected_count,
        targets=targets,
    )
