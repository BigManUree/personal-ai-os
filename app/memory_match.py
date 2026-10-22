"""Deterministic lexical matching for memory dedup / conflict detection.

Phase 2 iteration 03 deliberately keeps this dependency-free and offline:

* tokenization: Chinese runs -> character bigrams, latin/digit runs ->
  lower-cased words;
* duplicate signal -> Jaccard similarity over token sets;
* conflict signal -> overlap coefficient (robust for a short new sentence
  against a longer existing memory) PLUS an opposite-polarity cue for the
* same memory_type.

It is a *conservative* hint for human arbitration, not ground truth. Hidden
semantic contradictions can be missed; semantic/embedding judgements are
deferred to a later vector-retrieval phase.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

DUP_JACCARD = 0.80
CONFLICT_OVERLAP = 0.55

_CHINESE = r"\u4e00-\u9fff"
_TOKEN_RE = re.compile(rf"[{_CHINESE}]|[a-z0-9]+")

# Polarity lexicons (small, intentionally explicit). A candidate that flips
# polarity relative to a stored memory while sharing most of its topic tokens
# is treated as a potential contradiction.
# Standalone negative cues that do not embed a positive word.
_NEGATIVE_CUES = ("停止", "放弃", "不再", "戒了", "戒掉", "讨厌", "不要", "不想", "不喝", "不吃")
_POSITIVE_CUES = ("喜欢", "习惯", "坚持", "一直在", "总是", "偏爱", "热爱", "继续")
# Negation markers that can flip a positive cue appearing shortly after them
# (e.g. 不喜欢 / 没坚持 / 不再每天).
_NEGATION_MARKERS = ("不", "没", "别", "未", "戒", "无")


def tokenize(text: str) -> set[str]:
    """Return a token set: latin words/digits plus Chinese character bigrams."""
    text = text.lower()
    tokens: set[str] = set()
    chinese_runs: list[str] = []
    for match in _TOKEN_RE.finditer(text):
        piece = match.group(0)
        if re.fullmatch(f"[{_CHINESE}]", piece):
            # Collect contiguous Chinese characters via the scanner is awkward;
            # instead bigrams are built per Chinese run by the caller below.
            chinese_runs.append(piece)
        else:
            tokens.add(piece)
    # The regex emits single Chinese chars; rebuild bigrams per maximal run by
    # re-scanning (keeps tokenization readable).
    for run in re.findall(f"[{_CHINESE}]+", text):
        chars = list(run)
        if len(chars) == 1:
            tokens.add(chars[0])
        for i in range(len(chars) - 1):
            tokens.add(chars[i] + chars[i + 1])
    return tokens


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def overlap_coefficient(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _has_unnegated_positive(text: str) -> bool:
    for cue in _POSITIVE_CUES:
        start = 0
        while True:
            idx = text.find(cue, start)
            if idx == -1:
                break
            window = text[max(0, idx - 2):idx]
            if not any(marker in window for marker in _NEGATION_MARKERS):
                return True
            start = idx + len(cue)
    return False


def _polarity(text: str) -> int:
    """-1 negative, +1 positive, 0 neutral/unknown.

    "不喜欢" must count as negative even though it contains the positive cue
    "喜欢"; negation markers within two characters before a positive cue flip
    that cue.
    """
    neg = any(cue in text for cue in _NEGATIVE_CUES)
    negated_positive = False
    for cue in _POSITIVE_CUES:
        start = 0
        while True:
            idx = text.find(cue, start)
            if idx == -1:
                break
            window = text[max(0, idx - 2):idx]
            if any(marker in window for marker in _NEGATION_MARKERS):
                negated_positive = True
            start = idx + len(cue)
    is_negative = neg or negated_positive
    is_positive = _has_unnegated_positive(text)
    if is_negative and not is_positive:
        return -1
    if is_positive and not is_negative:
        return 1
    return 0


@dataclass(frozen=True)
class MatchResult:
    duplicate_of: int | None = None
    conflict_with: int | None = None


def _scores_against(candidate_tokens: set[str], target: dict) -> tuple[float, float]:
    target_tokens = tokenize(str(target.get("content", "")))
    return jaccard(candidate_tokens, target_tokens), overlap_coefficient(candidate_tokens, target_tokens)


def match_candidate(
    content: str,
    memory_type: str,
    existing_memories: Iterable[dict],
    pending_candidates: Iterable[dict] = (),
) -> MatchResult:
    """Return best duplicate/conflict target ids for one candidate.

    existing_memories: confirmed memories (dicts with id/content/memory_type).
    pending_candidates: other pending candidates (duplicates only; conflicts
    are always resolved against confirmed facts).
    Conflict takes precedence over duplicate.
    """
    candidate_text = content.strip()
    candidate_tokens = tokenize(candidate_text)
    candidate_polarity = _polarity(candidate_text)
    if not candidate_tokens:
        return MatchResult()

    best_conflict: tuple[float, int] | None = None
    best_duplicate: tuple[float, int] | None = None

    for target in existing_memories:
        target_id = target.get("id")
        target_content = str(target.get("content", "")).strip()
        if target_id is None or not target_content:
            continue
        jac, ov = _scores_against(candidate_tokens, target)

        # Exact / near-exact content is always a duplicate.
        if candidate_text == target_content or jac >= DUP_JACCARD:
            if best_duplicate is None or jac > best_duplicate[0]:
                best_duplicate = (jac, target_id)
            continue

        same_type = target.get("memory_type") == memory_type and memory_type in {"preference", "goal", "pattern", "fact"}
        flipped = (
            candidate_polarity != 0
            and candidate_polarity != _polarity(target_content)
            and _polarity(target_content) != 0
        )
        if ov >= CONFLICT_OVERLAP and same_type and flipped:
            if best_conflict is None or ov > best_conflict[0]:
                best_conflict = (ov, target_id)

    # Pending candidates can only mark duplicates (no confirmed contradiction yet).
    for target in pending_candidates:
        target_id = target.get("id")
        target_content = str(target.get("content", "")).strip()
        if target_id is None or not target_content:
            continue
        jac, _ = _scores_against(candidate_tokens, target)
        if jac >= DUP_JACCARD and (best_duplicate is None or jac > best_duplicate[0]):
            best_duplicate = (jac, target_id)

    if best_conflict is not None:
        return MatchResult(conflict_with=best_conflict[1])
    if best_duplicate is not None:
        return MatchResult(duplicate_of=best_duplicate[1])
    return MatchResult()


