#!/usr/bin/env python
"""Resolve a ``qid`` to its full question, options, solution and images.

The generated database stores compact pointer records, so this is the supported
way to get the question text back from the source paper JSON.

Usage
-----
::

    python tools/resolve.py <qid> [--json] [--paper-history]
    python tools/resolve.py --paper SSC-CGL/Previous_Year_Paper_Tier_I/2024/x.json
    python tools/resolve.py --find "spill the beans"
    python tools/resolve.py --no-index <qid>      # scan every paper JSON

Exit codes: ``0`` found, ``1`` not found, ``2`` usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import indexer, paths  # noqa: E402
from agent.config import exams  # noqa: E402
from agent.discover import load_paper  # noqa: E402
from agent.sections import iter_options  # noqa: E402
from agent.util import human_int  # noqa: E402

IMAGE_KEYS = ("solution_images", "images")
_SKIP_DIRS = {"state", "database", "config", "docs", "tools", "tests", "reports", ".git"}


def find_record(qid: str) -> Optional[Dict[str, Any]]:
    for record in indexer.read_index():
        if str(record.get("qid")) == qid:
            return record
    return None


def find_question(qid: str, paper_path: Optional[str]) -> Optional[Dict[str, Any]]:
    table = exams()
    if paper_path:
        paper = load_paper(paths.PROJECT_ROOT / paper_path, table)
        if paper is None:
            return None
        for question in paper.questions:
            if str(question.get("qid")) == qid:
                return question
        return None
    for root, dirs, files in os.walk(paths.PROJECT_ROOT):
        rel = Path(root).relative_to(paths.PROJECT_ROOT).as_posix()
        if rel == ".":
            # the repository root itself is not a paper directory: descend only
            # into the SSC-* trees (this used to prune every directory, which
            # made --no-index scan nothing at all)
            dirs[:] = [d for d in dirs if d.startswith("SSC-")]
            continue
        if rel.split("/")[0] in _SKIP_DIRS or not rel.startswith("SSC-"):
            dirs[:] = []
            continue
        for name in sorted(files):
            if not name.endswith(".json"):
                continue
            paper = load_paper(Path(root) / name, table)
            if paper is None:
                continue
            for question in paper.questions:
                if str(question.get("qid")) == qid:
                    return question
    return None


def render(question: Dict[str, Any], record: Optional[Dict[str, Any]], *, history: bool = False) -> str:
    lines: List[str] = []
    add = lines.append
    add("=" * 78)
    add(f"qid      : {question.get('qid')}")
    if record:
        add(
            f"source   : {record.get('exam')} {record.get('year')} "
            f"shift {record.get('shift') or 'NA'} · {record.get('paper_path')} "
            f"#{record.get('ordinal')} (n={record.get('n')})"
        )
        add(
            f"subject  : {record.get('subject')} ({record.get('subject_source')})   "
            f"concept: {record.get('concept')}   chapter: {record.get('chapter')}   "
            f"topic: {record.get('topic')}"
        )
    add(f"type     : {question.get('type')}   marks: +{question.get('marks_pos')} / {question.get('marks_neg')}")
    add(f"concept  : {question.get('concept')}   tags: {', '.join(question.get('tags') or []) or '-'}")
    add("-" * 78)
    add("QUESTION")
    add(str(question.get("question") or ""))
    add("-" * 78)
    add("OPTIONS")
    correct = str(question.get("correct") or "")
    for option in iter_options(question):
        marker = "*" if str(option.get("label")) == correct else " "
        add(f" {marker} {option.get('label')}) {option.get('text')}")
    add("-" * 78)
    add("SOLUTION")
    add(str(question.get("solution") or ""))
    images: List[str] = []
    for key in IMAGE_KEYS:
        block = question.get(key)
        if isinstance(block, list):
            for item in block:
                if isinstance(item, dict) and item.get("url"):
                    images.append(str(item["url"]))
    if images:
        add("-" * 78)
        add(f"IMAGES ({len(images)})")
        for url in dict.fromkeys(images):
            add(f"  {url}")
    if history and record:
        add("-" * 78)
        add(f"pointer  : {json.dumps(record, ensure_ascii=False, sort_keys=True)}")
    add("=" * 78)
    return "\n".join(lines)


def search_index(needle: str, limit: int = 20) -> List[Dict[str, Any]]:
    needle_lower = needle.lower()
    out: List[Dict[str, Any]] = []
    for record in indexer.read_index():
        blob = " ".join(
            str(record.get(field) or "")
            for field in ("concept_raw", "concept", "chapter", "topic", "paper_path")
        ).lower()
        if needle_lower in blob:
            out.append(record)
            if len(out) >= limit:
                break
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("qid", nargs="?", help="question id to resolve")
    parser.add_argument("--paper", help="paper path (skips the index lookup)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument("--paper-history", action="store_true", help="include the index pointer record")
    parser.add_argument("--no-index", action="store_true", help="ignore the index and scan papers")
    parser.add_argument("--find", help="list index records whose labels contain this text")
    args = parser.parse_args(argv)

    if args.find:
        rows = search_index(args.find)
        if not rows:
            print(f"no index records matching {args.find!r}", file=sys.stderr)
            return 1
        for record in rows:
            print(
                f"{record.get('qid')}  {record.get('exam'):14s} {record.get('year')}  "
                f"{record.get('subject'):9s} {str(record.get('concept'))[:34]:36s} "
                f"{record.get('paper_path')} #{record.get('ordinal')}"
            )
        print(f"\n{human_int(len(rows))} result(s)")
        return 0

    if not args.qid:
        parser.print_help()
        return 2

    record = None if args.no_index else find_record(args.qid)
    paper_path = args.paper or (record.get("paper_path") if record else None)
    question = find_question(args.qid, paper_path)
    if question is None:
        print(f"qid {args.qid!r} not found", file=sys.stderr)
        return 1

    if args.json:
        payload = {"question": question}
        if record:
            payload["pointer"] = record
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    print(render(question, record, history=args.paper_history))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
