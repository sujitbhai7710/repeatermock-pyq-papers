"""Taxonomy parsing.

Turns the four canonical markdown files in ``chapter-and-topic/`` into

* ``state/taxonomy.json`` – ``subject -> chapter/family -> topic -> concept``
* ``state/alias_map.json``  – raw concept/tag string -> canonical concept

The parser is line based and deliberately strict about the heading shapes the
files actually use, so re-running it is deterministic and diffable:

===================  =========================================================
file                 shape
===================  =========================================================
maths                ``# N. Chapter`` / ``## Topic`` / ``- [TAG] concept``
reasoning            ``# N. SECTION`` (1-59 real, 60-77 meta) / ``## N.M Topic``
                     plus ``## FAMILY K — NAME`` blocks used for the 5 families
gk                   ``# N. DOMAIN`` (0-106 real, 107-115 rules) / ``## N.M`` /
                     ``### N.M.K``
english              ``## Rule N: Title`` + ``**Topic:**``
===================  =========================================================
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from . import paths
from .util import fold_key, norm_key, norm_space, tokens, write_json

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

H1_RE = re.compile(r"^#\s+(.*\S)\s*$")
H2_RE = re.compile(r"^##\s+(.*\S)\s*$")
H3_RE = re.compile(r"^###\s+(.*\S)\s*$")
BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*\S)\s*$")
NUMBERED_RE = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?\.?\s+(.*\S)$")
RULE_RE = re.compile(r"^Rule\s+(\d+)\s*:\s*(.*\S)$")
TOPIC_RE = re.compile(r"^\*\*Topic:\*\*\s*(.*\S)$")
TAG_PREFIX_RE = re.compile(r"^\[([A-Z0-9\-]+)\]\s*")
FAMILY_RE = re.compile(r"^FAMILY\s+(\d+)\s*[—\-–]\s*(.*\S)$")


def clean_text(text: str) -> str:
    """Strip markdown emphasis/backticks and collapse whitespace."""

    value = text.strip()
    value = value.replace("`", "")
    value = re.sub(r"\*\*(.+?)\*\*", r"\1", value)
    value = re.sub(r"\*(.+?)\*", r"\1", value)
    value = re.sub(r"^[—\-–]+\s*", "", value)
    return norm_space(value)


#: list labels the source files use as prose headings ("For each:") that must
#: never be treated as a concept *or* as the target of a cross chapter move
NOT_A_CONCEPT = frozenset(
    {
        "for each",
        "note",
        "notes",
        "example",
        "examples",
        "important",
        "basics",
        "introduction",
        "types",
        "other",
        "others",
        "miscellaneous",
    }
)


def is_prose_bullet(text: str) -> bool:
    """Heuristic: bullets that are sentences/examples rather than concept names."""

    value = text.strip()
    if not value:
        return True
    if value.startswith("```") or value.startswith("|"):
        return True
    if value.endswith(".") and len(value.split()) > 6:
        return True
    if value.startswith(("✓", "✗", "→", "->", "Example", "Note:")):
        return True
    if norm_key(value) in NOT_A_CONCEPT:
        return True
    return False


# ---------------------------------------------------------------------------
# maths
# ---------------------------------------------------------------------------

MATHS_MIN_CHAPTER = 1
MATHS_MAX_CHAPTER = 30


def parse_maths(text: str) -> Dict[str, Any]:
    chapters: List[Dict[str, Any]] = []
    current_chapter: Optional[Dict[str, Any]] = None
    current_topic: Optional[Dict[str, Any]] = None
    chapter_count = 0

    for raw_line in text.splitlines():
        h1 = H1_RE.match(raw_line)
        if h1:
            match = NUMBERED_RE.match(clean_text(h1.group(1)))
            if match and not match.group(2):
                number = int(match.group(1))
                if MATHS_MIN_CHAPTER <= number <= MATHS_MAX_CHAPTER:
                    chapter_count += 1
                    current_chapter = {
                        "id": str(number),
                        "name": clean_text(match.group(4)),
                        "key": norm_key(match.group(4)),
                        "topics": [],
                        "concepts": [],
                    }
                    chapters.append(current_chapter)
                    current_topic = None
                    continue
            # any other H1 ends the taxonomy body
            current_chapter = None
            current_topic = None
            continue

        h2 = H2_RE.match(raw_line)
        if h2 and current_chapter is not None:
            name = clean_text(h2.group(1))
            current_topic = {"name": name, "key": norm_key(name), "concepts": []}
            current_chapter["topics"].append(current_topic)
            continue

        bullet = BULLET_RE.match(raw_line)
        if bullet and current_chapter is not None:
            body = bullet.group(1).strip()
            tag = ""
            tag_match = TAG_PREFIX_RE.match(body)
            if tag_match:
                tag = tag_match.group(1)
                body = body[tag_match.end() :]
            name = clean_text(body)
            if not name or is_prose_bullet(name):
                continue
            concept = {"name": name, "key": norm_key(name), "tag": tag}
            target = current_topic if current_topic is not None else current_chapter
            target["concepts"].append(concept)
            continue

    return {
        "subject": "MATH",
        "kind": "maths",
        "chapter_label": "chapter",
        "topic_label": "topic",
        "chapters": chapters,
        "declared_chapter_count": chapter_count,
    }


# ---------------------------------------------------------------------------
# reasoning
# ---------------------------------------------------------------------------

REAS_MIN_SECTION = 1
REAS_MAX_SECTION = 59
REAS_MAX_NUMBERED = 77


def parse_reasoning(text: str) -> Dict[str, Any]:
    chapters: List[Dict[str, Any]] = []
    meta_sections: List[Dict[str, Any]] = []
    families: Dict[str, List[str]] = {}
    by_number: Dict[int, Dict[str, Any]] = {}
    current_chapter: Optional[Dict[str, Any]] = None
    current_topic: Optional[Dict[str, Any]] = None
    current_family: Optional[str] = None
    in_family_block = False

    for raw_line in text.splitlines():
        h1 = H1_RE.match(raw_line)
        if h1:
            heading = clean_text(h1.group(1))
            if heading.upper().startswith("FAMILY "):
                continue
            match = NUMBERED_RE.match(heading)
            if match and not match.group(2):
                number = int(match.group(1))
                name = clean_text(match.group(4))
                if number < REAS_MIN_SECTION:
                    meta_sections.append({"id": str(number), "name": name, "kind": "meta"})
                    current_chapter = None
                    in_family_block = False
                elif number <= REAS_MAX_SECTION:
                    current_chapter = {
                        "id": str(number),
                        "name": name,
                        "key": norm_key(name),
                        "topics": [],
                        "concepts": [],
                        "family": None,
                        "kind": "section",
                    }
                    chapters.append(current_chapter)
                    by_number[number] = current_chapter
                    in_family_block = False
                elif number <= REAS_MAX_NUMBERED:
                    meta_sections.append({"id": str(number), "name": name, "kind": "meta"})
                    current_chapter = None
                    in_family_block = False
                else:
                    current_chapter = None
                current_topic = None
                continue
            if heading.upper() == "END":
                current_chapter = None
                current_topic = None
                continue
            current_chapter = None
            current_topic = None
            continue

        h2 = H2_RE.match(raw_line)
        if h2:
            heading = clean_text(h2.group(1))
            family_match = FAMILY_RE.match(heading)
            if family_match:
                current_family = f"FAMILY {family_match.group(1)} — {family_match.group(2)}"
                families.setdefault(current_family, [])
                in_family_block = True
                continue
            match = NUMBERED_RE.match(heading)
            if match and match.group(2) and current_chapter is not None:
                name = clean_text(match.group(4))
                current_topic = {"name": name, "key": norm_key(name), "concepts": []}
                current_chapter["topics"].append(current_topic)
                continue
            if current_chapter is not None:
                name = clean_text(heading)
                current_topic = {"name": name, "key": norm_key(name), "concepts": []}
                current_chapter["topics"].append(current_topic)
            continue

        # family member bullets: ``## FAMILY k`` followed by ``- Analogy``
        if in_family_block and current_family is not None:
            bullet = BULLET_RE.match(raw_line)
            if bullet:
                name = clean_text(bullet.group(1))
                if name and not is_prose_bullet(name):
                    families[current_family].append(name)
                continue
            if raw_line.strip() and not raw_line.startswith("#"):
                in_family_block = False

        bullet = BULLET_RE.match(raw_line)
        if bullet and current_chapter is not None:
            name = clean_text(bullet.group(1))
            if not name or is_prose_bullet(name):
                continue
            target = current_topic if current_topic is not None else current_chapter
            target["concepts"].append({"name": name, "key": norm_key(name), "tag": ""})
            continue

    # attach families to sections by name overlap
    family_lookup: Dict[str, str] = {}
    for family, members in families.items():
        for member in members:
            family_lookup.setdefault(norm_key(member), family)
    for chapter in chapters:
        key = chapter["key"]
        chapter["family"] = family_lookup.get(key)
        if chapter["family"] is None:
            for member_key, family in family_lookup.items():
                if member_key and (member_key in key or key in member_key):
                    chapter["family"] = family
                    break

    return {
        "subject": "REAS",
        "kind": "reasoning",
        "chapter_label": "section",
        "topic_label": "subtopic",
        "chapters": chapters,
        "meta_sections": meta_sections,
        "families": families,
    }


# ---------------------------------------------------------------------------
# GK / GS
# ---------------------------------------------------------------------------

GK_MIN_DOMAIN = 1
GK_MAX_DOMAIN = 106
GK_MAX_NUMBERED = 115


def parse_gk(text: str) -> Dict[str, Any]:
    chapters: List[Dict[str, Any]] = []
    meta_sections: List[Dict[str, Any]] = []
    current_chapter: Optional[Dict[str, Any]] = None
    current_topic: Optional[Dict[str, Any]] = None
    current_subtopic: Optional[Dict[str, Any]] = None

    for raw_line in text.splitlines():
        h1 = H1_RE.match(raw_line)
        if h1:
            heading = clean_text(h1.group(1))
            match = NUMBERED_RE.match(heading)
            current_topic = None
            current_subtopic = None
            if match and not match.group(2):
                number = int(match.group(1))
                name = clean_text(match.group(4))
                if GK_MIN_DOMAIN <= number <= GK_MAX_DOMAIN:
                    current_chapter = {
                        "id": str(number),
                        "name": name,
                        "key": norm_key(name),
                        "topics": [],
                        "concepts": [],
                        "kind": "domain",
                    }
                    chapters.append(current_chapter)
                elif number <= GK_MAX_NUMBERED:
                    meta_sections.append({"id": str(number), "name": name, "kind": "meta"})
                    current_chapter = None
                else:
                    current_chapter = None
            else:
                current_chapter = None
            continue

        h2 = H2_RE.match(raw_line)
        if h2 and current_chapter is not None:
            heading = clean_text(h2.group(1))
            match = NUMBERED_RE.match(heading)
            name = clean_text(match.group(4)) if match else heading
            current_topic = {"name": name, "key": norm_key(name), "concepts": [], "subtopics": []}
            current_chapter["topics"].append(current_topic)
            current_subtopic = None
            continue

        h3 = H3_RE.match(raw_line)
        if h3 and current_chapter is not None:
            heading = clean_text(h3.group(1))
            match = NUMBERED_RE.match(heading)
            name = clean_text(match.group(4)) if match else heading
            current_subtopic = {"name": name, "key": norm_key(name), "concepts": []}
            if current_topic is not None:
                current_topic["subtopics"].append(current_subtopic)
            else:
                current_chapter["concepts"].append(
                    {"name": name, "key": norm_key(name), "tag": "", "subtopic": True}
                )
            continue

        bullet = BULLET_RE.match(raw_line)
        if bullet and current_chapter is not None:
            name = clean_text(bullet.group(1))
            if not name or is_prose_bullet(name):
                continue
            entry = {"name": name, "key": norm_key(name), "tag": ""}
            if current_subtopic is not None:
                current_subtopic["concepts"].append(entry)
            elif current_topic is not None:
                current_topic["concepts"].append(entry)
            else:
                current_chapter["concepts"].append(entry)
            continue

    return {
        "subject": "GK",
        "kind": "gk",
        "chapter_label": "domain",
        "topic_label": "topic",
        "chapters": chapters,
        "meta_sections": meta_sections,
    }


# ---------------------------------------------------------------------------
# english grammar
# ---------------------------------------------------------------------------


def parse_english(text: str) -> Dict[str, Any]:
    chapters: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None

    for raw_line in text.splitlines():
        h2 = H2_RE.match(raw_line)
        if h2:
            heading = clean_text(h2.group(1))
            match = RULE_RE.match(heading)
            if match:
                number = int(match.group(1))
                title = clean_text(match.group(2))
                current = {
                    "id": str(number),
                    "name": title,
                    "key": norm_key(title),
                    "topics": [],
                    "concepts": [],
                    "kind": "rule",
                    "topic": "",
                }
                chapters.append(current)
            else:
                current = None
            continue

        if current is None:
            continue

        topic_match = TOPIC_RE.match(raw_line.strip())
        if topic_match:
            current["topic"] = clean_text(topic_match.group(1))
            current["topics"].append(
                {"name": current["topic"], "key": norm_key(current["topic"]), "concepts": []}
            )
            continue

        bullet = BULLET_RE.match(raw_line)
        if bullet:
            name = clean_text(bullet.group(1))
            if not name or name.startswith(("✓", "✗")):
                continue
            current["concepts"].append(
                {"name": name, "key": norm_key(name), "tag": "example"}
            )

    return {
        "subject": "ENG",
        "kind": "english_grammar",
        "chapter_label": "rule",
        "topic_label": "topic",
        "chapters": chapters,
        "declared_rule_count": len(chapters),
    }


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

#: chapter names such as ``GENERAL SCIENCE — PHYSICS`` carry their real domain
#: after the em/en dash; that trailing segment is the chapter's *short name* and
#: is what a concept bullet like ``Physics`` refers to.
DASH_SPLIT_RE = re.compile(r"\s*[—–]\s*")


def chapter_short_name(name: str) -> str:
    """``GENERAL SCIENCE — PHYSICS`` -> ``PHYSICS`` (``""`` when there is no dash)."""

    parts = DASH_SPLIT_RE.split(norm_space(name))
    if len(parts) < 2:
        return ""
    return clean_text(parts[-1])


def iter_concepts(body: Dict[str, Any]) -> Iterator[Tuple[List[Dict[str, Any]], Dict[str, Any]]]:
    """Every concept entry of one subject with the list that owns it."""

    for chapter in body.get("chapters", []):
        for concept in chapter.get("concepts", []):
            yield chapter["concepts"], concept
        for topic in chapter.get("topics", []):
            for concept in topic.get("concepts", []):
                yield topic["concepts"], concept
            for sub in topic.get("subtopics", []):
                for concept in sub.get("concepts", []):
                    yield sub["concepts"], concept


def relocate_cross_chapter_concepts(body: Dict[str, Any]) -> Dict[str, Any]:
    """Move concept bullets that *name* another chapter of the same subject.

    A syllabus file sometimes lists a foreign chapter as a bullet: §39
    *Scientists, Inventions & Discoveries* enumerates the research **fields**
    (``Physics``, ``Chemistry``, ``Biology``, ...), and those bullets used to
    become concepts of §39 — which made the alias map file every general-science
    question under *Scientists, Inventions & Discoveries* instead of under the
    PHYSICS / CHEMISTRY / BIOLOGY chapters that the same file declares.

    Rule: a concept whose normalised name uniquely matches another chapter's
    full name, its short name (the segment after the em/en dash) or one of its
    topic/subtopic names is a *cross reference*; it is attached to the owning
    chapter (the matching topic when there is one) and removed from the chapter
    where the bullet appeared.  Ambiguous names are left untouched — the
    taxonomy never guesses.  Only the chapter a concept is filed under changes;
    no name is invented or dropped.
    """

    chapters = body.get("chapters", [])
    by_chapter_name: Dict[str, List[Dict[str, Any]]] = {}
    by_topic_name: Dict[str, List[Tuple[Dict[str, Any], str]]] = {}
    for chapter in chapters:
        by_chapter_name.setdefault(norm_key(chapter.get("name", "")), []).append(chapter)
        short = chapter_short_name(chapter.get("name", ""))
        if short:
            by_chapter_name.setdefault(norm_key(short), []).append(chapter)
        for topic in chapter.get("topics", []):
            by_topic_name.setdefault(norm_key(topic.get("name", "")), []).append((chapter, topic["name"]))
            for sub in topic.get("subtopics", []):
                by_topic_name.setdefault(norm_key(sub.get("name", "")), []).append((chapter, sub["name"]))

    def topic_entry(chapter: Dict[str, Any], topic_name: str) -> Optional[Dict[str, Any]]:
        for topic in chapter.get("topics", []):
            if norm_key(topic.get("name", "")) == norm_key(topic_name):
                return topic
            for sub in topic.get("subtopics", []):
                if norm_key(sub.get("name", "")) == norm_key(topic_name):
                    return sub
        return None

    moves: List[Dict[str, Any]] = []
    for chapter in chapters:
        for owner_list, concept in list(iter_concepts({"chapters": [chapter]})):
            key = concept.get("key") or norm_key(concept.get("name", ""))
            if not key or key in NOT_A_CONCEPT or is_prose_bullet(concept.get("name", "")):
                continue
            target_chapter: Optional[Dict[str, Any]] = None
            target_topic: Optional[Dict[str, Any]] = None
            via = ""
            chapters_for_name = by_chapter_name.get(key) or []
            if len(chapters_for_name) == 1:
                target_chapter, via = chapters_for_name[0], "chapter"
            elif not chapters_for_name:
                topics_for_name = by_topic_name.get(key) or []
                if len(topics_for_name) == 1:
                    target_chapter, topic_name = topics_for_name[0]
                    target_topic = topic_entry(target_chapter, topic_name)
                    via = "topic"
            if target_chapter is None or target_chapter is chapter:
                continue
            target_list = target_topic["concepts"] if target_topic is not None else target_chapter.setdefault("concepts", [])
            if any(existing.get("key") == key for existing in target_list):
                owner_list.remove(concept)
            else:
                owner_list.remove(concept)
                target_list.append(concept)
            moves.append(
                {
                    "concept": concept.get("name"),
                    "from": chapter.get("name"),
                    "to": target_chapter.get("name"),
                    "via": via,
                    "target_topic": target_topic.get("name") if target_topic else "",
                }
            )

    return {"moved": len(moves), "moves": moves}


def _dedupe_supplementary(body: Dict[str, Any], chapter: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Merge a supplementary chapter that duplicates an existing name by case.

    ``config/supplementary_taxonomy.json`` declares a ``Static GK`` chapter while
    the markdown declares ``STATIC GK``.  Both are the same chapter; adding both
    produced two taxonomy nodes whose names differ only in letter case — and two
    leaf directories that slugified to the same name.  The markdown name is
    authoritative (it comes first), so the supplementary topics are merged into
    it instead of creating a second chapter.
    """

    existing = {norm_key(c.get("name", "")): c for c in body["chapters"]}
    target = existing.get(norm_key(chapter["name"]))
    if target is None or target is chapter:
        return None
    known = {norm_key(t.get("name", "")) for t in target.get("topics", [])}
    merged: List[str] = []
    for topic in chapter.get("topics", []):
        if norm_key(topic) in known:
            continue
        known.add(norm_key(topic))
        target.setdefault("topics", []).append({"name": topic, "key": norm_key(topic), "concepts": []})
        merged.append(topic)
    return {
        "subject": None,
        "chapter": chapter["name"],
        "into": target.get("name"),
        "topics_merged": merged,
        "topics_skipped": [t for t in chapter.get("topics", []) if t not in merged],
    }


PARSERS = {
    "MATH": parse_maths,
    "REAS": parse_reasoning,
    "GK": parse_gk,
    "ENG": parse_english,
}


def merge_supplementary(taxonomy: Dict[str, Any], source: Optional[paths.Path] = None) -> Dict[str, Any]:
    """Append the supplementary chapters from ``config/supplementary_taxonomy.json``.

    The four canonical markdown files do not cover every label the corpus uses
    (English vocabulary, DI-style "Quantitative Aptitude", ...).  Those labels get
    an explicit home here, marked ``source: "supplementary"`` so the origin of
    every chapter in ``taxonomy.json`` is traceable.
    """

    import json

    path = source or paths.SUPPLEMENTARY_TAXONOMY_FILE
    if not path.is_file():
        return taxonomy
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)

    added: Dict[str, int] = {}
    merged: List[Dict[str, Any]] = []
    for subject, chapters in (data.get("subjects") or {}).items():
        body = taxonomy["subjects"].get(subject)
        if body is None:
            # subjects with no source markdown file (COMPUTER) are created here
            body = {
                "subject": subject,
                "kind": "supplementary",
                "chapter_label": "chapter",
                "topic_label": "topic",
                "chapters": [],
                "concepts": [],
            }
            taxonomy["subjects"][subject] = body
            taxonomy.setdefault("sources", {})[subject] = paths.rel(path)
            taxonomy.setdefault("counts", {})[subject] = {"chapters": 0, "topics": 0, "concepts": 0}
        counts = taxonomy.setdefault("counts", {}).setdefault(subject, {})
        for chapter in chapters:
            duplicate = _dedupe_supplementary(body, chapter)
            if duplicate is not None:
                duplicate["subject"] = subject
                merged.append(duplicate)
                counts["topics"] = counts.get("topics", 0) + len(duplicate["topics_merged"])
                continue
            entry = {
                "id": chapter["id"],
                "name": chapter["name"],
                "key": norm_key(chapter["name"]),
                "topics": [
                    {"name": topic, "key": norm_key(topic), "concepts": []}
                    for topic in chapter.get("topics", [])
                ],
                "concepts": [],
                "kind": "supplementary",
                "source": "supplementary",
            }
            body["chapters"].append(entry)
            added[subject] = added.get(subject, 0) + 1
            counts["chapters"] = counts.get("chapters", 0) + 1
            counts["topics"] = counts.get("topics", 0) + len(entry["topics"])
    taxonomy["supplementary"] = {
        "source": paths.rel(path),
        "chapters_added": added,
        "duplicates_merged": merged,
    }
    return taxonomy


def _count_concepts(chapter: Dict[str, Any]) -> int:
    total = len(chapter.get("concepts", []))
    for topic in chapter.get("topics", []):
        total += len(topic.get("concepts", []))
        for sub in topic.get("subtopics", []):
            total += len(sub.get("concepts", []))
    return total


def build_taxonomy(taxonomy_dir: Optional[paths.Path] = None) -> Dict[str, Any]:
    base = taxonomy_dir or paths.TAXONOMY_DIR
    subjects: Dict[str, Any] = {}
    sources: Dict[str, str] = {}
    counts: Dict[str, Any] = {}
    relocations: Dict[str, Any] = {}

    for subject, filename in paths.TAXONOMY_FILES.items():
        path = base / filename
        if not path.is_file():
            raise FileNotFoundError(f"taxonomy file missing: {path}")
        text = path.read_text(encoding="utf-8")
        parsed = PARSERS[subject](text)
        relocated = relocate_cross_chapter_concepts(parsed)
        if relocated["moved"]:
            relocations[subject] = relocated
        subjects[subject] = parsed
        sources[subject] = paths.rel(path)

        chapters = parsed["chapters"]
        topic_count = sum(len(c.get("topics", [])) for c in chapters)
        counts[subject] = {
            "chapters": len(chapters),
            "topics": topic_count,
            "concepts": sum(_count_concepts(c) for c in chapters),
        }

    taxonomy = {
        "version": 1,
        "sources": sources,
        "counts": counts,
        "relocations": relocations,
        "subjects": subjects,
    }
    return merge_supplementary(taxonomy)


# ---------------------------------------------------------------------------
# chapter scope: which concepts a chapter may hold (A4) and canonical labels (A2)
# ---------------------------------------------------------------------------

def build_label_index(
    taxonomy: Dict[str, Any], alias_map: Optional[Dict[str, Any]] = None
) -> Dict[str, str]:
    """``fold_key(label) -> canonical spelling`` (first occurrence wins).

    Two records whose labels differ only in letter case (``"World Geography"``
    vs ``"WORLD GEOGRAPHY"``) must resolve to the *same* label, otherwise they
    become two leaves that slugify to the same directory — and the second one
    silently overwrites the first.  The taxonomy spelling wins (it is the
    authority the database is built from), then the alias-map canonicals, then
    the raw corpus spellings in sorted order, so the choice is deterministic.
    """

    labels: Dict[str, str] = {}

    def add(name: Any) -> None:
        value = norm_space(name)
        if not value or not _indexable(value):
            return
        key = fold_key(value)
        if key and key not in labels:
            labels[key] = value

    for _subject, body in sorted((taxonomy.get("subjects") or {}).items()):
        for chapter in body.get("chapters", []):
            add(chapter.get("name"))
            for concept in chapter.get("concepts", []):
                add(concept.get("name"))
            for topic in chapter.get("topics", []):
                add(topic.get("name"))
                for concept in topic.get("concepts", []):
                    add(concept.get("name"))
                for sub in topic.get("subtopics", []):
                    add(sub.get("name"))
                    for concept in sub.get("concepts", []):
                        add(concept.get("name"))
    if alias_map:
        for key in sorted((alias_map.get("map") or {})):
            entry = alias_map["map"][key]
            add(entry.get("canonical"))
            add(entry.get("raw"))
    return labels


class ConceptScope:
    """Chapter concept sets for the generated database.

    ``declares()`` answers the audit question *"may this chapter hold this
    concept?"*.  A concept is declared when its normalised name is a chapter,
    short-name, topic, subtopic or concept name of that chapter, or when all of
    the concept's own tokens occur in the chapter's name (``Physics`` is
    declared by ``GENERAL SCIENCE — PHYSICS``, ``Mensuration`` by
    ``Mensuration 2D & 3D``).  Everything else belongs in the explicit
    ``_other`` bucket rather than pretending the taxonomy declares it.

    ``owners_of()`` answers the parent-consistency question *"which chapters own
    this concept name?"* so the classifier can re-point a leaf whose chapter
    contradicts the concept's taxonomy parent.
    """

    def __init__(self, taxonomy: Dict[str, Any]) -> None:
        self.keys: Dict[Tuple[str, str], Set[str]] = {}
        self.chapter_tokens: Dict[Tuple[str, str], Set[str]] = {}
        self.chapter_names: Dict[Tuple[str, str], str] = {}
        self.owners: Dict[Tuple[str, str], Set[str]] = {}
        #: (subject, chapter key, concept key) -> the topic that spells the concept
        self.topic_lookup: Dict[Tuple[str, str, str], str] = {}

        def own(subject: str, name: Any, chapter_key: str) -> None:
            key = norm_key(name)
            if not key or not _indexable(norm_space(name)):
                return
            self.owners.setdefault((subject, key), set()).add(chapter_key)

        def declare(keys: Set[str], name: Any) -> None:
            value = norm_space(name)
            if value and _indexable(value):
                keys.add(norm_key(value))

        for subject, body in (taxonomy.get("subjects") or {}).items():
            for chapter in body.get("chapters", []):
                chapter_key = norm_key(chapter.get("name", ""))
                if not chapter_key:
                    continue
                scope = (subject, chapter_key)
                self.chapter_names[scope] = norm_space(chapter.get("name"))
                keys = self.keys.setdefault(scope, set())
                tokens_: Set[str] = set(tokens(chapter.get("name", ""), min_len=3))
                keys.add(chapter_key)
                own(subject, chapter.get("name"), chapter_key)
                short = chapter_short_name(chapter.get("name", ""))
                if short:
                    keys.add(norm_key(short))
                    tokens_ |= set(tokens(short, min_len=3))

                def declare(name: Any, topic_name: str = "") -> None:
                    value = norm_space(name)
                    if not value or not _indexable(value):
                        return
                    key = norm_key(value)
                    keys.add(key)
                    own(subject, value, chapter_key)
                    if topic_name:
                        self.topic_lookup.setdefault((subject, chapter_key, key), topic_name)

                for concept in chapter.get("concepts", []):
                    declare(concept.get("name"), concept.get("name", ""))
                for topic in chapter.get("topics", []):
                    declare(topic.get("name"), topic.get("name", ""))
                    for sub in topic.get("subtopics", []):
                        declare(sub.get("name"), sub.get("name", ""))
                    for concept in topic.get("concepts", []):
                        declare(concept.get("name"), topic.get("name", ""))
                self.chapter_tokens[scope] = tokens_

    def declares(self, subject: Optional[str], chapter: Optional[str], concept: Optional[str]) -> bool:
        if not subject or not chapter or not concept:
            return False
        scope = (subject, norm_key(chapter))
        keys = self.keys.get(scope)
        if keys is None:
            return False
        if norm_key(concept) in keys:
            return True
        concept_tokens = set(tokens(concept, min_len=3))
        return bool(concept_tokens) and concept_tokens <= self.chapter_tokens.get(scope, set())

    def owners_of(self, subject: Optional[str], concept: Optional[str]) -> Set[str]:
        """Chapter keys (of *subject*) whose vocabulary owns *concept*."""

        if not subject or not concept:
            return set()
        return set(self.owners.get((subject, norm_key(concept)), set()))

    def owner_name(self, subject: Optional[str], chapter_key: str) -> str:
        return self.chapter_names.get((subject or "", chapter_key), chapter_key)

    def topic_of(self, subject: str, chapter_key: str, concept: str) -> Optional[str]:
        """Topic of *chapter_key* that names *concept*, when there is one."""

        return self.topic_lookup.get((subject, chapter_key, norm_key(concept)))


# ---------------------------------------------------------------------------
# alias map
# ---------------------------------------------------------------------------


#: Hand-curated aliases (normalised raw -> canonical).  Mirrors the examples in
#: the specification plus the variants that actually occur in the corpus.
CURATED_ALIASES: Dict[str, str] = {
    "profit and loss": "Profit & Loss",
    "profit loss": "Profit & Loss",
    "profit & loss": "Profit & Loss",
    "simple profit and loss": "Profit & Loss",
    "absolute profit and loss": "Profit & Loss",
    "successive selling": "Profit & Loss",
    "marked price and discount": "Discount",
    "discount and mp": "Discount",
    "discount and marked price": "Discount",
    "coding decoding": "Coding-Decoding",
    "coding and decoding": "Coding-Decoding",
    "coding-decoding": "Coding-Decoding",
    "coded blood relation problems": "Blood Relations",
    "general blood relation problems": "Blood Relations",
    "family tree problems": "Blood Relations",
    "time and work": "Time & Work",
    "time & work": "Time & Work",
    "work and wages": "Time & Work",
    "work efficiency": "Time & Work",
    "ratio and proportion": "Ratio & Proportion",
    "ratio & proportion": "Ratio & Proportion",
    "simple ratios": "Ratio & Proportion",
    "compound ratios": "Ratio & Proportion",
    "direct or indirect proportion": "Ratio & Proportion",
    "fourth proportional": "Proportion",
    "third proportional": "Proportion",
    "mean proportional": "Proportion",
    "simple interest": "Simple Interest",
    "compound interest": "Compound Interest",
    "interest": "Simple Interest",
    "surds and indices": "Surds & Indices",
    "surds & indices": "Surds & Indices",
    "indices": "Surds & Indices",
    "exponents": "Surds & Indices",
    "simple and compound both": "Interest",
    "installments": "Installment",
    "problem on trains": "Problem on Trains",
    "average speed": "Speed Time & Distance",
    "speed time and distance": "Speed Time & Distance",
    "speed time & distance": "Speed Time & Distance",
    "partial speed": "Speed Time & Distance",
    "relative speed": "Speed Time & Distance",
    "boat and river": "Boat & Stream",
    "problem on age": "Problem on Age",
    "one word substitution": "OWS",
    "ows": "OWS",
    "one word substitution (ows)": "OWS",
    "idioms": "Idioms",
    "idioms & phrases": "Idioms",
    "idiom": "Idioms",
    "phrase or idiom meaning": "Idioms",
    "synonym": "Synonym",
    "synonyms": "Synonym",
    "synonyms or antonyms": "Synonym",
    "antonym": "Antonym",
    "antonyms": "Antonym",
    "spelling": "Spelling",
    "homophones": "Homophones",
    "homonyms": "Homophones",
    "error detection": "Error Detection",
    "error spotting": "Error Detection",
    "sentence improvement": "Sentence Improvement",
    "phrase replacement": "Sentence Improvement",
    "para jumbles": "Para Jumbles",
    "cloze test": "Cloze Test",
    "fill in the blanks": "Fill in the Blanks",
    "reading comprehension": "Reading Comprehension",
    "active and passive voice": "Active & Passive Voice",
    "direct and indirect speech": "Direct & Indirect Speech",
    "narration": "Direct & Indirect Speech",
    "voice": "Active & Passive Voice",
    "non verbal reasoning": "Non-Verbal Reasoning",
    "alphabet/word test": "Alphabet & Word Test",
    "alphabet or word test": "Alphabet & Word Test",
    "alphabet test": "Alphabet Test",
    "dictionary order": "Dictionary Order",
    "dictionary or alphabet based": "Dictionary Order",
    "word formation": "Word Formation",
    "seating arrangement": "Seating Arrangement",
    "linear arrangement": "Seating Arrangement",
    "circular arrangement": "Seating Arrangement",
    "polygon arrangement": "Seating Arrangement",
    "puzzle": "Puzzle",
    "logical puzzle": "Puzzle",
    "quant based puzzle": "Puzzle",
    "blood relations": "Blood Relations",
    "venn diagram": "Venn Diagram",
    "venn diagram problems": "Venn Diagram",
    "syllogism": "Syllogism",
    "conventional syllogism": "Syllogism",
    "coding and decoding in fictitious language": "Coding-Decoding (Fictitious Language)",
    "coding decoding based on numbers": "Coding-Decoding (Numbers)",
    "coding letters of a word": "Coding-Decoding (Letters)",
    "coding and decoding by letter shifting": "Coding-Decoding (Letter Shifting)",
    "classification": "Classification",
    "analogy": "Analogy",
    "series": "Series",
    "number series": "Number Series",
    "alphabet series": "Alphabet Series",
    "mixed series": "Mixed Series",
    "arrangement and pattern": "Arrangement & Pattern",
    "order based": "Ranking & Order",
    "rank based": "Ranking & Order",
    "ordering and ranking": "Ranking & Order",
    "similarity and differences": "Similarity & Differences",
    "mirror/water image": "Mirror & Water Image",
    "mirror image": "Mirror & Water Image",
    "image based": "Image Based",
    "hidden image": "Hidden Image",
    "figure counting": "Figure Counting",
    "counting figures": "Figure Counting",
    "completion of incomplete pattern": "Pattern Completion",
    "paper folding/cutting": "Paper Folding & Cutting",
    "paper folding and cutting": "Paper Folding & Cutting",
    "clock and calendar": "Clock & Calendar",
    "cube and dice": "Cube & Dice",
    "missing number": "Missing Number",
    "missing number in matrix": "Missing Number in Matrix",
    "input output": "Input-Output",
    "mathematical operations": "Mathematical Operations",
    "logical reasoning": "Logical Reasoning",
    "critical reasoning": "Critical Reasoning",
    "analytical decision making": "Decision Making",
    "decision making": "Decision Making",
    "statements and conclusions": "Statement & Conclusion",
    "statements and assumptions": "Statement & Assumption",
    "statements and inferences": "Statement & Inference",
    "course of action": "Course of Action",
    "cause and effect": "Cause & Effect",
    "situation reaction test": "Situation Reaction Test",
    "data sufficiency": "Data Sufficiency",
    "number system": "Number System",
    "simplification": "Simplification",
    "bodmas rule": "Simplification",
    "basic calculation": "Simplification",
    "quick math": "Simplification",
    "algebra": "Algebra",
    "geometry": "Geometry",
    "mensuration": "Mensuration",
    "plane figures": "Mensuration 2D",
    "solid figures": "Mensuration 3D",
    "trigonometry": "Trigonometry",
    "trigonometric ratios and identities": "Trigonometry",
    "heights and distances": "Height & Distance",
    "co-ordinate geometry": "Coordinate Geometry",
    "percentage": "Percentage",
    "average": "Average",
    "partnership": "Partnership",
    "mixture problems": "Mixture & Alligation",
    "to make a mixture from two mixtures": "Mixture & Alligation",
    "to make a mixture from two or more things": "Mixture & Alligation",
    "to make a mixture from two quantity": "Mixture & Alligation",
    "pipe and cistern": "Pipe & Cistern",
    "lcm and hcf": "LCM & HCF",
    "divisibility and remainder": "Number System",
    "fractions": "Number System",
    "decimals": "Number System",
    "integers": "Number System",
    "square and square root": "Number System",
    "rational or irrational numbers": "Number System",
    "even and odd number": "Number System",
    "operations on place value": "Number System",
    "identities": "Algebra",
    "linear equation in 1 variable": "Algebra",
    "linear equation in 2 variable": "Algebra",
    "quadratic equation": "Algebra",
    "progression": "Algebra",
    "data interpretation": "Data Interpretation",
    "bar graph": "Data Interpretation",
    "pie chart": "Data Interpretation",
    "line graph": "Data Interpretation",
    "tabulation": "Data Interpretation",
    "single bar": "Data Interpretation",
    "double bar": "Data Interpretation",
    "single line": "Data Interpretation",
    "single pie": "Data Interpretation",
    "tabulation and pie chart": "Data Interpretation",
    "elementary statistics": "Statistics",
    "probability": "Probability",
    "probability and statistics": "Statistics",
    "standard deviation": "Statistics",
    "measures of skewness": "Statistics",
    "correlation and regression": "Statistics",
    "indian geography": "Indian Geography",
    "geography (world geography)": "World Geography",
    "indian economic and human geography": "Indian Geography",
    "world economic and human geography": "World Geography",
    "ancient history": "Ancient Indian History",
    "medieval history": "Medieval Indian History",
    "modern india (national movement )": "Modern Indian History",
    "modern india (pre-congress phase)": "Modern Indian History",
    "modern indian history": "Modern Indian History",
    "art and culture": "Art & Culture",
    "art & culture": "Art & Culture",
    "polity": "Indian Polity & Constitution",
    "indian polity": "Indian Polity & Constitution",
    "basics of constitution": "Indian Polity & Constitution",
    "economy": "Indian Economy",
    "business and economy": "Indian Economy",
    "general knowledge": "Static GK",
    "general science": "General Science",
    "science and technology": "Science & Technology",
    "science technology and inventions": "Science & Technology",
    "awards and honours": "Awards & Honours",
    "books and authors": "Books & Authors",
    "government policies and schemes": "Government Schemes",
    "initiatives by government": "Government Schemes",
    "indexes and reports": "Reports & Indices",
    "ecology and environment": "Environment & Ecology",
    "environment": "Environment & Ecology",
    "conservation efforts: india and world": "Environment & Ecology",
    "computer fundamentals or terminologies": "Computer Fundamentals",
    "introduction to computers": "Computer Fundamentals",
    "computer abbreviations": "Computer Fundamentals",
    "microsoft office": "MS Office",
    "microsoft word": "MS Office",
    "keyboard shortcuts": "MS Office",
    "operating systems": "Operating Systems",
    "internet": "Internet & Networking",
    "networking": "Internet & Networking",
    "memory": "Computer Fundamentals",
    "hardware": "Computer Hardware",
    "software": "Computer Software",
}

#: raw labels that are never mapped to a concept
UNMAPPED_LABELS = {
    "unidentified",
    "unknown",
    "na",
    "n a",
    "none",
    "null",
    "general",
    "miscellaneous",
    "other",
}


def _indexable(name: str) -> bool:
    """Concept bullets in the taxonomy files are sometimes whole sentences.

    Only short, name-like strings are useful as canonical vocabulary, so longer
    prose bullets stay in ``taxonomy.json`` but are left out of the name index.
    """

    value = norm_space(name)
    if not value or len(value) > 60:
        return False
    if len(value.split()) > 9:
        return False
    if value.endswith(".") and len(value.split()) > 5:
        return False
    return True


def _taxonomy_name_index(taxonomy: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """normalised name -> list of taxonomy nodes carrying that name."""

    index: Dict[str, List[Dict[str, Any]]] = {}

    def add(name: str, node: Dict[str, Any]) -> None:
        if not _indexable(name):
            return
        key = norm_key(name)
        if key:
            index.setdefault(key, []).append(node)

    for subject, body in taxonomy["subjects"].items():
        for chapter in body["chapters"]:
            base = {
                "subject": subject,
                "chapter_id": chapter.get("id", ""),
                "chapter_name": chapter.get("name", ""),
                "topic_name": "",
                "level": "chapter",
            }
            add(chapter.get("name", ""), base)
            add(f"{subject} {chapter.get('name','')}", base)
            for topic in chapter.get("topics", []):
                node = {
                    "subject": subject,
                    "chapter_id": chapter.get("id", ""),
                    "chapter_name": chapter.get("name", ""),
                    "topic_name": topic.get("name", ""),
                    "level": "topic",
                }
                add(topic.get("name", ""), node)
                for concept in topic.get("concepts", []):
                    cnode = dict(node)
                    cnode["level"] = "concept"
                    cnode["concept_name"] = concept.get("name", "")
                    add(concept.get("name", ""), cnode)
                for sub in topic.get("subtopics", []):
                    snode = {
                        "subject": subject,
                        "chapter_id": chapter.get("id", ""),
                        "chapter_name": chapter.get("name", ""),
                        "topic_name": topic.get("name", ""),
                        "subtopic_name": sub.get("name", ""),
                        "level": "subtopic",
                    }
                    add(sub.get("name", ""), snode)
                    for concept in sub.get("concepts", []):
                        cnode = dict(snode)
                        cnode["level"] = "concept"
                        cnode["concept_name"] = concept.get("name", "")
                        add(concept.get("name", ""), cnode)
            for concept in chapter.get("concepts", []):
                cnode = dict(base)
                cnode["level"] = "concept"
                cnode["concept_name"] = concept.get("name", "")
                add(concept.get("name", ""), cnode)
    return index


def build_alias_map(
    taxonomy: Dict[str, Any],
    raw_labels: Iterable[str],
) -> Dict[str, Any]:
    """Map every raw concept/tag string to a canonical concept.

    Resolution order for each raw label:

    1. curated alias table (``CURATED_ALIASES``);
    2. exact taxonomy vocabulary match (concept, then topic, then chapter);
    3. **multi-label strings only** (``"Grammar, Vocabulary"``): the whole string
       is tried first (many comma-bearing strings are genuine taxonomy names such
       as *Acids, Bases & Salts*); only when the whole string fails are the
       comma-separated components tried left to right and the first component
       that maps to a canonical taxonomy name is used.  A concept is **never**
       built by concatenating the raw multi-label string, because that would put
       a synthetic slug (``circles-chords-and-tangents``) into the database;
    4. the label itself, marked ``observed``.

    Labels in ``UNMAPPED_LABELS`` resolve to ``None`` so that ``classify.py``
    can report them as unclassified instead of inventing a concept.

    Entries for comma-bearing labels carry ``multi`` = ``whole`` | ``split`` |
    ``unresolved`` (plus ``multi_component`` for a split) and the counters are
    reported under ``stats.multi_*`` so the classification of the multi-label
    corpus stays auditable.
    """

    index = _taxonomy_name_index(taxonomy)
    curated = {norm_key(k): v for k, v in CURATED_ALIASES.items()}

    # canonical concepts that are themselves taxonomy names
    canonical_nodes: Dict[str, Dict[str, Any]] = {}
    for key, nodes in index.items():
        canonical_nodes[key] = nodes[0]

    mapping: Dict[str, Dict[str, Any]] = {}
    stats = {
        "curated": 0,
        "taxonomy": 0,
        "observed": 0,
        "unmapped": 0,
        "multi_whole": 0,
        "multi_split": 0,
        "multi_unresolved": 0,
    }

    def lookup(key: str, raw: str) -> Optional[Dict[str, Any]]:
        """Canonical entry for one normalised key, or ``None``."""

        if key in curated:
            canonical = curated[key]
            node = canonical_nodes.get(norm_key(canonical), {})
            return {
                "canonical": canonical,
                "kind": "concept",
                "subject": node.get("subject"),
                "chapter": node.get("chapter_name"),
                "topic": node.get("topic_name") or node.get("subtopic_name"),
                "source": "curated",
            }

        nodes = index.get(key)
        if not nodes:
            return None
        node = nodes[0]
        concept_name = node.get("concept_name") or node.get("name") or raw
        if node["level"] == "chapter":
            concept_name = node.get("chapter_name") or raw
        elif node["level"] == "topic":
            concept_name = node.get("topic_name") or raw
        elif node["level"] == "subtopic":
            concept_name = node.get("subtopic_name") or raw
        return {
            "canonical": concept_name,
            "kind": node["level"],
            "subject": node.get("subject"),
            "chapter": node.get("chapter_name"),
            "topic": node.get("topic_name") or node.get("subtopic_name"),
            "source": "taxonomy",
        }

    def resolve(raw: str) -> Dict[str, Any]:
        key = norm_key(raw)
        if not key or key in UNMAPPED_LABELS:
            stats["unmapped"] += 1
            return {
                "canonical": None,
                "kind": "unmapped",
                "subject": None,
                "chapter": None,
                "topic": None,
                "source": "unmapped",
            }

        is_multi = "," in raw
        entry = lookup(key, raw)
        if entry is not None:
            stats[entry["source"]] += 1
            if is_multi:
                stats["multi_whole"] += 1
                entry = {**entry, "multi": "whole"}
            return entry

        if is_multi:
            for component in raw.split(","):
                component = component.strip()
                if not component:
                    continue
                sub = lookup(norm_key(component), component)
                if sub is not None:
                    stats[sub["source"]] += 1
                    stats["multi_split"] += 1
                    return {**sub, "multi": "split", "multi_component": component}
            stats["multi_unresolved"] += 1
            return {
                "canonical": None,
                "kind": "unmapped",
                "subject": None,
                "chapter": None,
                "topic": None,
                "source": "unmapped",
                "multi": "unresolved",
            }

        stats["observed"] += 1
        return {
            "canonical": norm_space(raw),
            "kind": "observed",
            "subject": None,
            "chapter": None,
            "topic": None,
            "source": "observed",
        }

    seen: Set[str] = set()
    for raw in raw_labels:
        if raw is None:
            continue
        value = norm_space(raw)
        if not value:
            continue
        key = norm_key(value)
        if key in seen:
            continue
        seen.add(key)
        mapping[key] = {"raw": value, **resolve(value)}

    # make sure the curated aliases are present even if the corpus lacks them
    for key, canonical in curated.items():
        if key in mapping:
            continue
        node = canonical_nodes.get(norm_key(canonical), {})
        mapping[key] = {
            "raw": canonical,
            "canonical": canonical,
            "kind": "concept",
            "subject": node.get("subject"),
            "chapter": node.get("chapter_name"),
            "topic": node.get("topic_name") or node.get("subtopic_name"),
            "source": "curated",
        }
        stats["curated"] += 1

    return {
        "version": 1,
        "stats": stats,
        "map": dict(sorted(mapping.items())),
    }


# ---------------------------------------------------------------------------
# resolver (used by classify.py)
# ---------------------------------------------------------------------------


@dataclass
class ResolvedConcept:
    canonical: Optional[str]
    subject: Optional[str] = None
    chapter: Optional[str] = None
    topic: Optional[str] = None
    source: str = "unmapped"
    raw: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "canonical": self.canonical,
            "subject": self.subject,
            "chapter": self.chapter,
            "topic": self.topic,
            "source": self.source,
            "raw": self.raw,
        }


class ConceptResolver:
    """Resolve raw concept/tag strings using ``state/alias_map.json``."""

    def __init__(self, alias_map: Dict[str, Any]) -> None:
        self.map: Dict[str, Dict[str, Any]] = alias_map.get("map", {})

    def resolve_label(self, raw: Optional[str]) -> ResolvedConcept:
        if raw is None:
            return ResolvedConcept(canonical=None, raw="")
        value = norm_space(raw)
        if not value:
            return ResolvedConcept(canonical=None, raw="")
        entry = self.map.get(norm_key(value))
        if entry is None:
            return ResolvedConcept(canonical=value, subject=None, source="observed", raw=value)
        return ResolvedConcept(
            canonical=entry.get("canonical"),
            subject=entry.get("subject"),
            chapter=entry.get("chapter"),
            topic=entry.get("topic"),
            source=entry.get("source", "observed"),
            raw=value,
        )

    def resolve(self, concept: Optional[str], tags: Optional[Sequence[str]] = None) -> ResolvedConcept:
        """Resolve concept first; fall back to the first tag that resolves."""

        primary = self.resolve_label(concept)
        if primary.canonical is not None:
            return primary
        for tag in tags or []:
            candidate = self.resolve_label(tag)
            if candidate.canonical is not None:
                return candidate
        return primary


def load_taxonomy(path: Optional[paths.Path] = None) -> Dict[str, Any]:
    import json

    target = path or paths.TAXONOMY_JSON
    with target.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_alias_map(path: Optional[paths.Path] = None) -> Dict[str, Any]:
    import json

    target = path or paths.ALIAS_MAP_JSON
    with target.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def write_taxonomy(taxonomy: Dict[str, Any], path: Optional[paths.Path] = None) -> paths.Path:
    return write_json(path or paths.TAXONOMY_JSON, taxonomy)


def write_alias_map(alias_map: Dict[str, Any], path: Optional[paths.Path] = None) -> paths.Path:
    return write_json(path or paths.ALIAS_MAP_JSON, alias_map)
