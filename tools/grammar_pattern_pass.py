#!/usr/bin/env python3
"""Deterministic grammar-rule classifier — the fast path.

The AI debate is accurate but slow (~9 questions/min on the free tier).  This
pass does part of the work in pure Python instead:

1. Reads the **labelled** questions already filed under a rule leaf in
   ``database/english/grammar/<rule>/questions.jsonl``.
2. Learns a distinctive-term signature (TF-IDF style) for each of the 129 rules.
3. Holdout-validates and **tunes the score threshold for precision**, not volume —
   it only keeps assignments it can justify (default target: 85 % precision).
4. Writes the survivors into ``state/grammar_ai_state.json`` in exactly the shape
   the AI pass uses, so ``python -m agent.cli grammar`` applies them unchanged.

Everything below the precision target is deliberately left for the AI.

Run from the repository root:  ``python3 tools/grammar_pattern_pass.py --apply``
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import indexer  # noqa: E402
from agent.grammar import gather_grammar_questions  # noqa: E402
from agent.phases.subject_phase import _questions_for  # noqa: E402

GRAMMAR_DIR = ROOT / "database" / "english" / "grammar"
STATE_PATH = ROOT / "state" / "grammar_ai_state.json"

TOKEN_RE = re.compile(r"[a-z][a-z'\-]+")
STOP = set(
    """a an the and or but if then than that this these those there here of in on at to for from
by with without into onto over under about above below between among is am are was were be been
being do does did done have has had having will would shall should can could may might must not no
nor so as it its he she they them him her his their you your we our us i me my which who whom whose
what when where why how all any both each few more most other some such only own same too very
sentence word words option options following given choose correct incorrect fill blank part parts
one two three four english grammar question suitable appropriate best""".split()
)


def tokenize(text: str):
    return [t for t in TOKEN_RE.findall((text or "").lower()) if t not in STOP]


def load_rules():
    src = ROOT / "chapter-and-topic" / "english-grammar-rules.md"
    rules, current, buf = {}, None, []
    for line in src.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^##\s+Rule\s+(\d+)\s*:\s*(.+)$", line)
        if m:
            if current:
                rules[current["number"]] = {**current, "body": " ".join(buf)}
            current = {"number": int(m.group(1)), "title": m.group(2).strip(), "topic": ""}
            buf = []
            continue
        if current is None:
            continue
        t = re.match(r"^\*\*Topic:\*\*\s*(.+)$", line)
        if t:
            current["topic"] = t.group(1).strip()
            continue
        buf.append(line)
    if current:
        rules[current["number"]] = {**current, "body": " ".join(buf)}
    return rules


def load_labelled():
    labels = {}
    if not GRAMMAR_DIR.is_dir():
        return labels
    for child in GRAMMAR_DIR.iterdir():
        if not child.is_dir():
            continue
        m = re.match(r"^(\d+)-", child.name)
        if not m:
            continue
        rule_no = int(m.group(1))
        for jf in child.glob("*.jsonl"):
            for line in jf.open(encoding="utf-8"):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("qid"):
                    labels[str(row["qid"])] = rule_no
    return labels


def item_text(item) -> str:
    parts = [getattr(item, "prompt", "") or ""]
    for o in getattr(item, "options", None) or []:
        if isinstance(o, dict):
            parts.append(o.get("text") or "")
        elif isinstance(o, str):
            parts.append(o)
    return " ".join(parts)


def build_signatures(train_docs, rules, top_k=50, min_df=2):
    n_rules = max(1, len(train_docs))
    doc_freq = Counter()
    for rule_docs in train_docs.values():
        for toks in rule_docs:
            doc_freq.update(set(toks))

    sigs = {}
    for rule_no, rule_docs in train_docs.items():
        tf = Counter()
        for toks in rule_docs:
            tf.update(toks)
        n_docs = max(1, len(rule_docs))
        scored = {}
        for term, count in tf.items():
            if count < min_df:
                continue
            idf = math.log((n_rules + 1) / (doc_freq[term] + 0.5))
            scored[term] = count * idf * (0.4 + count / n_docs)
        sigs[rule_no] = dict(sorted(scored.items(), key=lambda kv: kv[1], reverse=True)[:top_k])
        # seed with the rule's own title/topic so thin rules still have a hook
        meta = rules.get(rule_no, {})
        for term in tokenize(f"{meta.get('title','')} {meta.get('topic','')}"):
            sigs[rule_no][term] = sigs[rule_no].get(term, 0) + 1.0
    return sigs


def classify(tokens, sigs):
    tset = set(tokens)
    best_rule, best, second = None, 0.0, 0.0
    for rule_no, sig in sigs.items():
        score = sum(w for t, w in sig.items() if t in tset)
        if score > best:
            best_rule, second, best = rule_no, best, score
        elif score > second:
            second = score
    return best_rule, best, second


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--target-precision", type=float, default=0.85)
    args = ap.parse_args()

    rules = load_rules()
    labels = load_labelled()

    records = [r for r in indexer.read_index() if r.get("subject") == "ENG"]
    questions = _questions_for(records)
    pool, _types = gather_grammar_questions(records, questions)
    print(f"rules parsed       : {len(rules)}")
    print(f"grammar questions  : {len(pool)}")
    print(f"labelled (in leaves): {len(labels)}")

    by_qid = {str(getattr(i, "qid", "")): i for i in pool}

    docs = defaultdict(list)
    for qid, rule_no in labels.items():
        item = by_qid.get(qid)
        if item is not None:
            docs[rule_no].append(tokenize(item_text(item)))
    print(f"rules with training : {len(docs)}")

    # ---- holdout + threshold tuning ------------------------------------
    train_docs, test = defaultdict(list), []
    for rule_no, qdocs in docs.items():
        split = max(1, int(len(qdocs) * 0.8))
        train_docs[rule_no].extend(qdocs[:split])
        test.extend((rule_no, d) for d in qdocs[split:])
    sigs = build_signatures(train_docs, rules)

    scored_test = []
    for true_rule, d in test:
        pred, score, second = classify(d, sigs)
        scored_test.append((score, second, pred == true_rule))

    scored_test.sort(key=lambda x: -x[0])
    # walk thresholds, keep the lowest score that still hits the precision target
    chosen, cov_at = None, 0
    for idx in range(20, len(scored_test)):
        thr = scored_test[idx][0]
        kept = [s for s in scored_test if s[0] >= thr]
        prec = sum(1 for s in kept if s[2]) / len(kept)
        if prec >= args.target_precision:
            chosen, cov_at = thr, len(kept) / len(scored_test)
            break
    overall = sum(1 for s in scored_test if s[2]) / max(1, len(scored_test))
    print(f"holdout size       : {len(scored_test)}")
    print(f"unthresholded acc  : {overall*100:.1f}%")
    if chosen is None:
        print("could not reach the precision target - NOTHING will be written")
        return 1
    print(f"chosen score floor : {chosen:.2f}  (precision >= {args.target_precision:.0%})")
    print(f"holdout coverage   : {cov_at*100:.1f}% of questions clear that bar")

    # ---- full model ------------------------------------------------------
    sigs = build_signatures(docs, rules)
    decisions, skipped = {}, 0
    for qid, item in by_qid.items():
        if qid in labels:
            continue
        toks = tokenize(item_text(item))
        if not toks:
            continue
        pred, score, second = classify(toks, sigs)
        if pred is None or score < chosen:
            skipped += 1
            continue
        confidence = max(0.0, min(1.0, 0.5 + 0.5 * (1 - (second / score if score else 1))))
        decisions[qid] = {
            "rule": pred,
            "confidence": round(confidence, 3),
            "reason": f"pattern signature (score {score:.1f}, runner-up {second:.1f}, floor {chosen:.1f})",
            "reason_code": "",
            "proposer_model": "pattern-pass",
            "proposer_provider": "local",
            "judge_model": "pattern-pass",
            "judge_provider": "local",
            "same_model_fallback": False,
            "verdict": "accept",
            "rounds": 1,
        }
    print(f"\nnewly assigned     : {len(decisions)}")
    print(f"left for the AI    : {skipped}")

    if not args.apply:
        print("\n(--apply not given: nothing written)")
        return 0

    state = {"version": 1, "score_version": 1, "questions": {}, "batches": {}}
    if STATE_PATH.is_file():
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    state.setdefault("questions", {}).update(decisions)
    STATE_PATH.write_text(json.dumps(state, indent=1), encoding="utf-8")
    print(f"wrote {len(decisions)} decisions -> {STATE_PATH}")
    print("now run:  python3 -m agent.cli grammar")
    return 0


if __name__ == "__main__":
    sys.exit(main())
