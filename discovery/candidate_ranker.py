from difflib import SequenceMatcher

from sources.journal_catalog import normalize_journal_name


OFFICIAL_HINTS = ["官方", "编辑部", "期刊", "杂志", "学报", "本刊", "CSSCI"]
NEGATIVE_HINTS = ["代发", "发表助手", "快速发表", "论文发表", "投稿咨询"]


def rank_candidate(
    journal_name: str,
    candidate_name: str,
    description: str = "",
    evidence_titles: list[str] | None = None,
    is_verified: bool = False,
) -> float:
    normalized_journal = normalize_journal_name(journal_name)
    normalized_candidate = normalize_journal_name(candidate_name)
    text = f"{normalized_candidate} {description} {' '.join(evidence_titles or [])}"

    score = SequenceMatcher(None, normalized_journal, normalized_candidate).ratio() * 0.55
    if normalized_journal == normalized_candidate:
        score += 0.25
    elif normalized_journal in normalized_candidate or normalized_candidate in normalized_journal:
        score += 0.12
    if any(hint in text for hint in OFFICIAL_HINTS):
        score += 0.14
    if any(normalized_journal in title for title in evidence_titles or []):
        score += 0.08
    if is_verified:
        score += 0.05
    if any(hint in text for hint in NEGATIVE_HINTS):
        score -= 0.35
    return max(0.0, min(1.0, round(score, 4)))
