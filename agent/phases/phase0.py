"""Phase 0 – discovery, Hindi split, signature validation, index, distribution.

Outputs
-------
``state/index/<subject>.<n>.jsonl`` + ``state/index/manifest.json``
    one pointer record per in-scope, non-Hindi question, sharded by subject so
    that no generated file exceeds the GitHub per-file limit (100 MB)
``state/papers.json``
    one record per in-scope paper including its layout analysis
``state/distribution.json``
    full exam x year x shift x subject x chapter x topic x concept distribution
``database/_meta/distribution.md``, ``database/_meta/coverage.md``
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from .. import build_db, discover, distribution, indexer, keywords, paths, tracking
from ..util import Log, write_json

RAW_DEVANAGARI = re.compile(r"[\u0900-\u097F]")
NO_PUNCT_DEVANAGARI = re.compile(r"[\u0900-\u0963\u0966-\u097F]")


def hindi_variants(papers: List[Any]) -> Dict[str, int]:
    """Count Hindi questions under several detector definitions.

    Reported in ``_meta/coverage.md`` so the choice of detector is auditable.
    """

    counts = {
        "question+options+solution (raw range)": 0,
        "question+options (raw range)": 0,
        "question only (raw range)": 0,
        "question+options (range minus danda)": 0,
    }
    for paper in papers:
        for question in paper.questions:
            prompt = str(question.get("question") or "")
            options = " ".join(
                str(o.get("text") or "") for o in (question.get("options") or []) if isinstance(o, dict)
            )
            solution = str(question.get("solution") or "")
            if RAW_DEVANAGARI.search(f"{prompt} {options} {solution}"):
                counts["question+options+solution (raw range)"] += 1
            if RAW_DEVANAGARI.search(f"{prompt} {options}"):
                counts["question+options (raw range)"] += 1
            if RAW_DEVANAGARI.search(prompt):
                counts["question only (raw range)"] += 1
            if NO_PUNCT_DEVANAGARI.search(f"{prompt} {options}"):
                counts["question+options (range minus danda)"] += 1
    return counts


def run(ctx) -> Any:
    from . import PhaseResult

    log = ctx.log or Log("phase0")
    result = PhaseResult(phase="phase0")
    log.step("phase0: discovering papers")

    papers, out_of_scope = discover.discover(
        ctx.settings, ctx.exams, with_hashes=False
    )
    ctx.papers = papers
    ctx.out_of_scope = out_of_scope
    total_questions = sum(p.length for p in papers)
    log.info(
        f"{len(papers)} in-scope papers / {total_questions} questions "
        f"({len(out_of_scope)} papers dropped by the year filter)"
    )

    log.step("phase0: building the question index (Hindi split + signature validation)")
    records, paper_reports = indexer.build_index(
        papers, ctx.settings, ctx.exams, ctx.classifier, log=log
    )
    ctx.records = records
    ctx.paper_reports = paper_reports

    coverage = indexer.coverage(records, paper_reports)
    coverage["min_agreement"] = ctx.settings.signature.min_agreement
    coverage["min_section_agreement"] = ctx.settings.signature.min_section_agreement
    ctx.coverage = coverage
    log.info(
        "coverage: placed={placed} unclassified={unclassified} skipped_hindi={skipped_hindi} "
        "flagged={flagged_papers_questions} total={questions} balanced={balanced}".format(**coverage)
    )
    if not coverage["balanced"]:
        log.warn(
            "coverage identity is NOT balanced: accounted={accounted} vs in-scope={questions}".format(
                **coverage
            )
        )

    log.step("phase0: writing state/")
    index_target = indexer.write_index(records, log=log)
    write_json(paths.PAPERS_JSON, [r for r in paper_reports])
    write_json(
        paths.SUBJECT_KEYWORDS_JSON,
        {
            "generated_by": "agent.keywords.keyword_table_export",
            **keywords.keyword_table_export(),
        },
    )

    log.step("phase0: computing the distribution")
    dist = distribution.build_distribution(records, ctx.settings, paper_reports, coverage, log=log)
    ctx.distribution = dist
    distribution.write_distribution_json(dist)
    distribution.write_distribution_md(dist)

    log.step("phase0: writing database/_meta/")
    variants = hindi_variants(papers)
    build_db.write_meta(
        distribution=dist,
        coverage=coverage,
        paper_reports=paper_reports,
        out_of_scope=out_of_scope,
        hindi_variants=variants,
        log=log,
    )

    result.counters = {
        "papers_in_scope": len(papers),
        "papers_out_of_scope": len(out_of_scope),
        "questions_in_scope": total_questions,
        "placed": coverage["placed"],
        "unclassified": coverage["unclassified"],
        "skipped_hindi": coverage["skipped_hindi"],
        "flagged_papers_questions": coverage["flagged_papers_questions"],
        "papers_validated": coverage["papers_validated"],
        "papers_flagged": coverage["papers_flagged"],
        "papers_placeable": coverage["papers_placeable"],
    }
    result.notes.append(
        "coverage identity holds" if coverage["balanced"] else "coverage identity BROKEN"
    )
    result.files = [
        index_target,
        paths.INDEX_MANIFEST_JSON,
        *sorted(p for p in paths.INDEX_DIR.glob("*.jsonl") if p.is_file()),
        paths.PAPERS_JSON,
        paths.DISTRIBUTION_JSON,
        paths.SUBJECT_KEYWORDS_JSON,
        paths.META_DB_DIR / "distribution.md",
        paths.META_DB_DIR / "coverage.md",
        paths.META_DB_DIR / "schema.md",
        paths.META_DB_DIR / "papers.jsonl",
        paths.META_DB_DIR / "flagged_papers.jsonl",
    ]
    tracking.record_files("phase0", result.files)
    tracking.journal("phase0.complete", result.counters)
    return result
