"""Lexical fallback shared by prepared and interactive reference retrieval."""

from __future__ import annotations

import re


def reference_fragments(terms: tuple[str, ...]) -> tuple[str, ...]:
    """Language-level fragments, without engineering synonym substitutions."""
    fragments: set[str] = set()
    for term in terms:
        fragments.update(re.findall(r"[a-z][a-z0-9_-]+", term.casefold()))
        for phrase in re.findall(r"[\u3400-\u9fff]+", term):
            for size in range(2, min(4, len(phrase)) + 1):
                fragments.update(phrase[index:index + size] for index in range(len(phrase) - size + 1))
    return tuple(sorted(fragments))


def fallback_reference_scores(texts: list[str], terms: tuple[str, ...]) -> list[int]:
    """Score distinct overlaps; repeated boilerplate cannot inflate relevance."""
    bodies = [text.casefold() for text in texts]
    frequency = {term: sum(term in body for body in bodies) for term in reference_fragments(terms)}
    # Common fragments cannot discriminate. Favor longer, rarer overlaps.
    weights = {
        term: len(term) ** 2 * (len(texts) - count + 1)
        for term, count in frequency.items()
        if count and (len(texts) == 1 or count < len(texts))
    }
    return [sum(weight for term, weight in weights.items() if term in body) for body in bodies]
