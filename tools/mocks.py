#!/usr/bin/env python
"""Work with the generated mock packs.

Usage
-----
::

    python tools/mocks.py list  [--kind concept] [--subject MATH] [--search profit] [--limit 20]
    python tools/mocks.py show   --kind concept --id profit-and-loss
    python tools/mocks.py build  --kind concept --id profit-and-loss [--resolve] [--limit 10]
    python tools/mocks.py kinds

``show`` prints the pack payload (``{kind,id,subject,count,exam_filter,year_range,questions}``).
``build`` prints a ready-to-serve mock: with ``--resolve`` every ``qid`` is
expanded to the full question, options, solution and images (via
``tools/resolve.py``), which is what a mock generator needs.

Exit codes: ``0`` ok, ``1`` not found, ``2`` usage error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import indexer, paths  # noqa: E402
from agent.util import human_int, read_json  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from resolve import find_question  # noqa: E402


def load_catalogue() -> Dict[str, Any]:
    data = read_json(paths.MOCKS_DIR / "index.json", default=None)
    if data is None:
        raise SystemExit(
            "database/mocks/index.json is missing – run `python -m agent.cli mocks` first"
        )
    return data


def select_packs(catalogue: Dict[str, Any], kind: Optional[str], subject: Optional[str], search: Optional[str]) -> List[Dict[str, Any]]:
    rows = catalogue.get("packs", [])
    if kind:
        rows = [r for r in rows if r.get("kind") == kind]
    if subject:
        rows = [r for r in rows if (r.get("subject") or "").upper() == subject.upper()]
    if search:
        needle = search.lower()
        rows = [r for r in rows if needle in str(r.get("id", "")).lower()]
    return rows


def cmd_list(args: argparse.Namespace) -> int:
    catalogue = load_catalogue()
    rows = select_packs(catalogue, args.kind, args.subject, args.search)
    rows.sort(key=lambda r: (-int(r.get("count") or 0), str(r.get("id"))))
    for row in rows[: args.limit or len(rows)]:
        print(
            f"{row.get('kind'):8s} {str(row.get('id'))[:52]:54s} "
            f"{(row.get('subject') or '-'):9s} {int(row.get('count') or 0):5d} q  "
            f"{row.get('year_range')}  {row.get('path')}"
        )
    print(f"\n{human_int(len(rows))} pack(s)")
    return 0


def cmd_kinds(args: argparse.Namespace) -> int:
    catalogue = load_catalogue()
    for kind, data in catalogue.get("by_kind", {}).items():
        print(f"{kind:9s} packs={data.get('packs'):5d} questions={data.get('questions'):7d}")
    print(f"total packs={catalogue['totals']['packs']}")
    return 0


def _load_pack(kind: str, pack_id: str) -> Dict[str, Any]:
    path = paths.MOCKS_DIR / "packs" / kind / f"{pack_id}.json"
    data = read_json(path, default=None)
    if data is None:
        raise SystemExit(f"pack not found: {paths.rel(path)}")
    return data


def cmd_show(args: argparse.Namespace) -> int:
    print(json.dumps(_load_pack(args.kind, args.id), indent=2, ensure_ascii=False))
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    pack = _load_pack(args.kind, args.id)
    qids: List[str] = list(pack.get("questions") or [])
    if args.limit:
        qids = qids[: args.limit]

    if not args.resolve:
        payload = {**pack, "questions": qids, "resolved": False}
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    pointers = {str(r.get("qid")): r for r in indexer.read_index()}
    questions: List[Dict[str, Any]] = []
    missing: List[str] = []
    for qid in qids:
        record = pointers.get(qid)
        question = find_question(qid, (record or {}).get("paper_path"))
        if question is None:
            missing.append(qid)
            continue
        questions.append(
            {
                "qid": qid,
                "exam": (record or {}).get("exam"),
                "year": (record or {}).get("year"),
                "shift": (record or {}).get("shift"),
                "subject": (record or {}).get("subject"),
                "concept": (record or {}).get("concept"),
                "chapter": (record or {}).get("chapter"),
                "topic": (record or {}).get("topic"),
                "paper_path": (record or {}).get("paper_path"),
                "ordinal": (record or {}).get("ordinal"),
                "type": question.get("type"),
                "question": question.get("question"),
                "options": question.get("options"),
                "correct": question.get("correct"),
                "solution": question.get("solution"),
                "solution_images": question.get("solution_images"),
                "images": question.get("images"),
                "marks_pos": question.get("marks_pos"),
                "marks_neg": question.get("marks_neg"),
            }
        )
    payload = {
        "kind": pack.get("kind"),
        "id": pack.get("id"),
        "subject": pack.get("subject"),
        "count": len(questions),
        "exam_filter": pack.get("exam_filter"),
        "year_range": pack.get("year_range"),
        "resolved": True,
        "missing": missing,
        "questions": questions,
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="list packs in the catalogue")
    p_list.add_argument("--kind", choices=["concept", "topic", "chapter", "subject", "exam", "year", "full"])
    p_list.add_argument("--subject")
    p_list.add_argument("--search")
    p_list.add_argument("--limit", type=int, default=40)

    sub.add_parser("kinds", help="summary per kind")

    p_show = sub.add_parser("show", help="print a pack payload")
    p_show.add_argument("--kind", required=True)
    p_show.add_argument("--id", required=True)

    p_build = sub.add_parser("build", help="print a mock built from a pack")
    p_build.add_argument("--kind", required=True)
    p_build.add_argument("--id", required=True)
    p_build.add_argument("--resolve", action="store_true", help="expand every qid to the full question")
    p_build.add_argument("--limit", type=int, default=0)

    args = parser.parse_args(argv)
    if args.command == "list":
        return cmd_list(args)
    if args.command == "kinds":
        return cmd_kinds(args)
    if args.command == "show":
        return cmd_show(args)
    if args.command == "build":
        return cmd_build(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
