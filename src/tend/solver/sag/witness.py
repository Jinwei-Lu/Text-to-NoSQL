"""Value-witness anchoring: locate the question's literals in the actual data.

In a schema-less world values double as schema (SDD dual): a literal from the NLQ
that is *witnessed* in the data — as a stored value or as a dynamic-map KEY — pins
the read path gold-free, including its exact stored form (case/spacing). Witnesses
feed the prompt as evidence lines and the A_value gate as enforceable anchors.

Literal classes: quoted strings / ALL-CAPS codes / dates are "hard" (enforceable);
TitleCase phrases are "soft" (evidence only). Short plain quoted words (e.g. 'Loan')
are demoted to soft — they are usually output labels, not stored values.
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only (avoids an import cycle)
    from .induction import GroundingIndex

_ENFORCE_MAX_LOCATIONS = 6
_DATE_PREFIX_MAX_PATHS = 6

STOP = set(
    """the this that these those show list find what which who whom how return give for from
in on at an a all per each every count top if of to and or not no yes display identify provide
calculate compute determine output get sort order group where when then else use using include
including among across between within their them are is was were do does has have had with by as
also only more less than least most highest lowest average total number amount""".split()
)


def vnorm(s: str) -> str:
    """Normalize a value for index lookup: lowercase, unify space/underscore runs."""
    return re.sub(r"[\s_]+", " ", s.strip().lower())


@dataclass(frozen=True)
class EnforcedLiteral:
    """A hard witnessed literal: the paths/forms its uses must align with."""

    paths: frozenset[tuple[str, str]]  # (collection, static-prefix path)
    exact: frozenset[str]  # exact stored forms
    kinds: frozenset[str]  # {"value", "dynamic KEY"}


def extract_literals(nlq: str) -> dict[str, str]:
    """Candidate value literals from the NLQ, with confidence class ('hard'/'soft')."""
    out: dict[str, str] = {}
    for m in re.finditer(r"'([^']{2,60})'", nlq):
        out.setdefault(m.group(1), "hard")
    for m in re.finditer(r'"([^"]{2,60})"', nlq):
        out.setdefault(m.group(1), "hard")
    for m in re.finditer(r"\b[A-Z][A-Z0-9_]{1,}(?:\s+[A-Z][A-Z0-9_]{1,}){0,4}\b", nlq):
        t = m.group(0)
        if len(t) >= 3 and t.lower() not in STOP:
            out.setdefault(t, "hard")
    for m in re.finditer(r"\b\d{4}[-/]\d{2}(?:[-/]\d{2})?\b", nlq):
        out.setdefault(m.group(0), "hard")
    for m in re.finditer(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3}\b", nlq):
        t = m.group(0)
        st = m.start()
        sentence_start = st == 0 or nlq[max(0, st - 2) : st].strip() in ("", ".", "?", "!", ":", ";")
        words = [w for w in t.split() if w.lower() not in STOP]
        if not words:
            continue
        if sentence_start and len(words) < 2:
            continue
        out.setdefault(t, "soft")
    return out


def witnesses(nlq: str, index: "GroundingIndex") -> tuple[list[str], dict[str, EnforcedLiteral]]:
    """-> (evidence_lines, enforce: {literal: EnforcedLiteral})"""
    lines: list[str] = []
    enforce: dict[str, EnforcedLiteral] = {}
    vidx = index.value_index
    vkeys = index.value_keys
    for lit, klass in extract_literals(nlq).items():
        n = vnorm(lit)
        hits = vidx.get(n)
        if not hits:
            # date-like prefix search
            if re.match(r"^\d{4}([-/]\d{2})?$", lit):
                i = bisect.bisect_left(vkeys, n)
                pre: set[tuple[str, str, str]] = set()
                while i < len(vkeys) and vkeys[i].startswith(n) and len(pre) < _DATE_PREFIX_MAX_PATHS:
                    for h in vidx[vkeys[i]]:
                        pre.add((h[0], h[1], h[2]))
                    i += 1
                if pre:
                    locs = "; ".join(f"{c}.{p} ({k})" for c, p, k in sorted(pre)[:5])
                    lines.append(f"- values starting '{lit}' exist at: {locs}")
            continue
        locs = sorted(hits)
        loc_s = "; ".join(f"{c}.{p} ({k}, stored as '{ex}')" for c, p, k, ex in locs[:5])
        lines.append(f"- '{lit}' found in data at: {loc_s}")
        # demote short plain English words (quoted labels like 'Loan'): evidence only
        if " " not in lit and len(lit) < 6 and not lit.isupper() and not re.search(r"[\d_:]", lit):
            klass = "soft"
        if klass == "hard" and len(locs) <= _ENFORCE_MAX_LOCATIONS:
            # truncate at the first <*>: the static prefix is the readable root
            enforce[lit] = EnforcedLiteral(
                paths=frozenset(
                    (c, p.split(".<*>")[0].replace("[]", "")) for c, p, k, ex in locs
                ),
                exact=frozenset(ex for c, p, k, ex in locs),
                kinds=frozenset(k for c, p, k, ex in locs),
            )
    return lines, enforce
