"""Cross-subject placement audit.

For every question in ``state/index/*.jsonl`` this checks whether the canonical
concept (or its raw tag) belongs to a **different** subject's taxonomy than the
subject the question is currently filed under.  Those are the "wrong folder"
additions: e.g. a question tagged ``Verbal Ability`` (an English chapter) that
ended up in ``gk/_unclassified/verbal-ability`` because the GK taxonomy has no
such chapter.

Only concepts that are *unique* to one subject are reported, so shared labels
(``Analogy`` lives in both REAS and ENG) never produce noise.

Usage::

    python3 tools/cross_subject_audit.py            # report only
    python3 tools/cross_subject_audit.py --verbose  # per-question lines
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def load_taxonomy() -> dict:
    with open(os.path.join(ROOT, "state", "taxonomy.json"), encoding="utf-8") as fh:
        return json.load(fh)


def subject_names(tax: dict) -> dict:
    """``subject -> {canonical label -> (chapter, topic)}`` (chapters+topics+concepts)."""

    out: dict = {}
    for subject, body in tax.get("subjects", {}).items():
        labels: dict = {}

        def add(name: str, chapter: str, topic: str) -> None:
            key = norm(name)
            if key and key not in labels:
                labels[key] = (chapter, topic)

        for chapter in body.get("chapters", []) or []:
            cname = chapter.get("name") or ""
            for concept in chapter.get("concepts", []) or []:
                add(concept if isinstance(concept, str) else concept.get("name", ""), cname, "")
            for topic in chapter.get("topics", []) or []:
                tname = topic.get("name") or ""
                add(tname, cname, tname)
                for concept in topic.get("concepts", []) or []:
                    add(concept if isinstance(concept, str) else concept.get("name", ""), cname, tname)
            add(cname, cname, "")
        out[subject] = labels
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    tax = load_taxonomy()
    labels = subject_names(tax)

    # labels that belong to exactly one subject
    owner: dict = {}
    owners: dict = defaultdict(set)
    for subject, mapping in labels.items():
        for key in mapping:
            owners[key].add(subject)
    for key, subs in owners.items():
        if len(subs) == 1:
            owner[key] = next(iter(subs))

    alias = {"MATH": "math", "REAS": "reasoning", "GK": "gk", "ENG": "english", "COMPUTER": "computer"}
    findings: Counter = Counter()
    detail: dict = defaultdict(list)
    total = 0

    for path in sorted(glob.glob(os.path.join(ROOT, "state", "index", "*.jsonl"))):
        subject = os.path.basename(path).split(".")[0].upper()
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                total += 1
                rec = json.loads(line)
                candidates = [rec.get("concept"), rec.get("concept_raw")]
                candidates += [t for t in (rec.get("tags") or []) if isinstance(t, str)]
                for raw in candidates:
                    key = norm(raw)
                    if not key:
                        continue
                    other = owner.get(key)
                    if other and other != subject:
                        findings[(subject, other, rec.get("concept_raw") or rec.get("concept"))] += 1
                        if len(detail[(subject, other, rec.get("concept_raw") or rec.get("concept"))]) < 5:
                            detail[(subject, other, rec.get("concept_raw") or rec.get("concept"))].append(
                                {
                                    "qid": rec.get("qid"),
                                    "exam": rec.get("exam"),
                                    "year": rec.get("year"),
                                    "ordinal": rec.get("ordinal"),
                                    "status": rec.get("status"),
                                    "subject_source": rec.get("subject_source"),
                                    "chapter": rec.get("chapter"),
                                    "topic": rec.get("topic"),
                                }
                            )
                        break

    print(f"scanned {total} index records")
    print(f"cross-subject concept hits: {sum(findings.values())}")
    print()
    for (src, dst, concept), count in sorted(findings.items(), key=lambda kv: -kv[1]):
        print(f"  {count:6d}  {alias.get(src, src):10s} -> {alias.get(dst, dst):10s}  {concept!r}")
        if args.verbose:
            for row in detail[(src, dst, concept)]:
                print(f"           {row}")
    if not findings:
        print("  (none)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
