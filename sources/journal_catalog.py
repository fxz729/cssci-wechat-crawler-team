from dataclasses import dataclass
import re
from urllib.parse import parse_qs, urlparse


@dataclass(frozen=True)
class JournalSeedData:
    catalog_version: str
    discipline: str
    tier: str
    journal_name: str
    normalized_name: str
    aliases: list[str]
    source_file: str
    source_line: int


@dataclass(frozen=True)
class ProfileLinkTarget:
    discipline: str
    tier: str
    journal_name: str
    account_name: str
    profile_url: str
    biz: str
    source_file: str
    source_line: int


def normalize_journal_name(name: str) -> str:
    return (
        name.strip()
        .replace("（", "(")
        .replace("）", ")")
        .replace("　", " ")
    )


def build_aliases(name: str) -> list[str]:
    normalized = normalize_journal_name(name)
    aliases = [normalized]
    no_parentheses = re.sub(r"\([^)]*\)", "", normalized).strip()
    if no_parentheses and no_parentheses not in aliases:
        aliases.append(no_parentheses)
    return aliases


def parse_catalog_text(
    markdown: str,
    source_file: str,
    catalog_version: str = "2025-2026",
) -> list[JournalSeedData]:
    seeds: list[JournalSeedData] = []
    discipline = ""

    for line_number, raw_line in enumerate(markdown.splitlines(), start=1):
        line = raw_line.strip()
        if line.startswith("## "):
            discipline = line.removeprefix("## ").strip()
            continue
        if not discipline or not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 4 or cells[0] in {"序号", "---"} or cells[1] == "期刊":
            continue
        left_index, left_name, right_index, right_name = cells[:4]
        if left_index.isdigit() and left_name:
            seeds.append(_seed(catalog_version, discipline, "cssci", left_name, source_file, line_number))
        if right_index.isdigit() and right_name:
            seeds.append(_seed(catalog_version, discipline, "cssci_extended", right_name, source_file, line_number))

    return seeds


def parse_catalog_file(path: str, catalog_version: str = "2025-2026") -> list[JournalSeedData]:
    with open(path, "r", encoding="utf-8") as file:
        return parse_catalog_text(file.read(), source_file=path, catalog_version=catalog_version)


def parse_profile_links_text(markdown: str, source_file: str) -> list[ProfileLinkTarget]:
    targets: list[ProfileLinkTarget] = []
    for line_number, raw_line in enumerate(markdown.splitlines(), start=1):
        line = raw_line.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 7 or not cells[0].isdigit():
            continue
        _, discipline, tier, journal_name, account_name, profile_url, _ = cells[:7]
        biz = parse_qs(urlparse(profile_url).query).get("__biz", [""])[0]
        if journal_name != account_name or not biz:
            continue
        targets.append(ProfileLinkTarget(
            discipline=discipline,
            tier=tier,
            journal_name=journal_name,
            account_name=account_name,
            profile_url=profile_url,
            biz=biz,
            source_file=source_file,
            source_line=line_number,
        ))
    return targets


def parse_profile_links_file(path: str) -> list[ProfileLinkTarget]:
    with open(path, "r", encoding="utf-8") as file:
        return parse_profile_links_text(file.read(), source_file=path)


def _seed(
    catalog_version: str,
    discipline: str,
    tier: str,
    journal_name: str,
    source_file: str,
    source_line: int,
) -> JournalSeedData:
    normalized = normalize_journal_name(journal_name)
    return JournalSeedData(
        catalog_version=catalog_version,
        discipline=discipline,
        tier=tier,
        journal_name=journal_name.strip(),
        normalized_name=normalized,
        aliases=build_aliases(normalized),
        source_file=source_file,
        source_line=source_line,
    )
