"""Classification: raw ``concept``/``tags`` -> canonical concept -> chapter/topic.

Resolution ladder (first hit wins, recorded in ``source``):

1. ``alias``   – exact hit in ``state/alias_map.json`` (curated alias or exact
   taxonomy vocabulary, e.g. *Profit and Loss* -> *Profit & Loss*).
2. ``chapter`` – the raw label contains a chapter name of the question's subject.
3. ``topic``   – the raw label contains a topic name of the question's subject.
4. ``keyword`` – a curated per-subject keyword rule matched.
5. ``unmapped`` – nothing matched; the question is reported as ``unclassified``.

Two post-conditions are enforced on every result before it is returned
(``audit_db`` rules 6 and 7):

* **parent consistency** – when the resolved concept is vocabulary of exactly one
  chapter of the subject and the resolved chapter is a different one, the chapter
  (and topic) is re-pointed to the owner, so a leaf never contradicts its
  concept's taxonomy parent;
* **declared scope** – a concept the final chapter does not declare is flagged
  with ``leaf_bucket = "_other"`` so the writer parks it in the explicit
  ``_other`` bucket instead of pretending the chapter declares it.

Label spelling is canonicalised case-insensitively (first canonical spelling of
the taxonomy/alias map wins), so ``"World Geography"`` and ``"WORLD GEOGRAPHY"``
can never produce two leaves for the same concept.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .taxonomy import ConceptResolver, ConceptScope, ResolvedConcept, build_label_index
from .util import contains_phrase, fold_key, norm_key, norm_space, slugify

#: explicit bucket for a placed question whose concept the chapter does not declare
OTHER_BUCKET = "_other"

# ---------------------------------------------------------------------------
# curated keyword fallbacks (raw label fragment -> chapter name)
# ---------------------------------------------------------------------------

MATH_FALLBACK: Tuple[Tuple[str, str], ...] = (
    (r"percent", "Percentage"),
    (r"profit|loss|selling|marked price|markup|cost price|sp\b|cp\b|dishonest", "Profit & Loss"),
    (r"discount", "Discount"),
    (r"simple interest|\bsi\b|installment", "Simple Interest"),
    (r"compound interest|\bci\b", "Compound Interest"),
    (r"ratio|proportion", "Ratio & Proportion"),
    (r"age", "Age"),
    (r"partnership|investment", "Partnership"),
    (r"mixture|alligation", "Mixture & Alligation"),
    (r"average|mean", "Average"),
    (r"time and work|work|wages|efficiency|pipes?|cistern", "Time & Work"),
    (r"speed|distance|train|boat|stream|relative|partial", "Time & Distance"),
    (r"race|circular motion", "Race & Circular Motion"),
    (r"number system|divisibility|remainder|factor|multiple|unit digit|digit|lcm|hcf|fraction|decimal|surds|indices|simplif|bodmas", "Number System"),
    (r"algebra|equation|identit|polynomial|progression|quadratic|inequalit", "Algebra"),
    (r"trigonometr|height|distance", "Trigonometry"),
    (r"geometry|triangle|circle|quadrilateral|polygon|angle|chord|tangent|centr", "Geometry"),
    (r"co-?ordinate", "Coordinate Geometry"),
    (r"mensuration|area|volume|perimeter|solid|plane figure|cone|cylinder|sphere|cube|prism|pyramid", "Mensuration 2D & 3D"),
    (r"statistic|deviation|skewness|correlation|regression|distribution", "Statistics"),
    (r"permutation|combination|arrangement|selection", "Permutation & Combination"),
    (r"probability", "Probability"),
    (r"data interpretation|graph|chart|tabulation|table", "Data Interpretation"),
)

REAS_FALLBACK: Tuple[Tuple[str, str], ...] = (
    (r"analogy", "ANALOGY"),
    (r"classification|odd one", "CLASSIFICATION / ODD ONE OUT"),
    (r"series", "SERIES"),
    (r"coding|decoding", "CODING-DECODING"),
    (r"alphabet|word test", "ALPHABET TEST"),
    (r"dictionary|word formation", "WORD / DICTIONARY ORDER"),
    (r"mathematical operation|digit operation|place value|inequalit|simplif", "MATHEMATICAL OPERATIONS"),
    (r"number system|number based|numeral", "NUMBER / DIGIT OPERATIONS"),
    (r"percentage|percent|profit|loss|interest|average|ratio|proportion|time and work|time & work|speed|distance|discount|partnership|mixture|pipe", "MATHEMATICAL OPERATIONS"),
    (r"ranking|order|rank", "RANKING / ORDER / POSITION"),
    (r"direction", "DIRECTION SENSE"),
    (r"blood relation", "BLOOD RELATIONS"),
    (r"venn", "VENN DIAGRAM"),
    (r"syllogism", "SYLLOGISM"),
    (
        r"statement|assumption|argument|conclusion|inference|course of action|cause|assertion|decision|deduction|data sufficiency|situation reaction",
        "LOGICAL STATEMENT QUESTIONS",
    ),
    (r"seating|arrangement", "SEATING ARRANGEMENT"),
    (r"floor|building", "FLOOR / BUILDING ARRANGEMENT"),
    (r"box|stack", "BOX / STACK ARRANGEMENT"),
    (r"schedul", "SCHEDULING / DAY / MONTH ARRANGEMENT"),
    (r"grouping|distribution", "GROUPING / DISTRIBUTION"),
    (r"selection", "SELECTION PROBLEMS"),
    (r"puzzle", "PUZZLES"),
    (r"clock", "CLOCK"),
    (r"calendar", "CALENDAR"),
    (r"age", "AGE-BASED REASONING"),
    (r"missing number", "MISSING NUMBER"),
    (r"matrix", "NUMBER MATRIX / TABLE REASONING"),
    (r"mirror|water image", "MIRROR IMAGE"),
    (r"image based", "NON-VERBAL SERIES"),
    (r"paper folding", "PAPER FOLDING"),
    (r"paper cutting", "PAPER CUTTING"),
    (r"embedded", "EMBEDDED FIGURE"),
    (r"figure completion|pattern completion", "FIGURE COMPLETION"),
    (r"figure counting|counting", "FIGURE COUNTING"),
    (r"dice", "DICE"),
    (r"cube", "CUBE"),
    (r"rotation", "FIGURE ROTATION / IMAGE ROTATION"),
    (r"symmetry", "SYMMETRY"),
    (r"spatial", "SPATIAL / VISUAL REASONING"),
    (r"input.?output", "INPUT-OUTPUT / MACHINE TYPE REASONING"),
    (r"non verbal|figure|image|hidden", "NON-VERBAL SERIES"),
    (r"mixed", "MIXED REASONING"),
    (r"logical|cricital|critical|similarity|intelligence", "Logical Reasoning"),
)

GK_FALLBACK: Tuple[Tuple[str, str], ...] = (
    (
        r"current affair|appointment|resignation|person in news|obituar|days and events|national affairs|states affairs|international affairs|agreement|mou|places in news",
        "CURRENT AFFAIRS",
    ),
    (r"ancient|harappa|indus|vedic|mauryan|buddhism|jainism|sangam|post mauryan|gupta age|prehistoric", "ANCIENT INDIAN HISTORY"),
    (r"medieval|sultanate|mughal|lodhi|khilji|tughlaq|sayyid|bahmani|vijayanagar|maratha|sikh history|bhakti|sufi|rajput", "MEDIEVAL INDIAN HISTORY"),
    (r"modern india|national movement|freedom|gandhi|congress|revolt|british|east india|viceroy|governor|reform|nationalism|partition|post independence", "MODERN INDIAN HISTORY"),
    (r"art|culture|dance|music|festival|fair|architect|monument|paint|theatre|handicraft|gharana|literature|unesco", "ART & CULTURE"),
    (r"world geography|country|capital|currency|international organisation|world organisation", "WORLD GEOGRAPHY"),
    (r"geography|biogeograph|river|mountain|lake|island|climate|monsoon|soil|ocean|continent|desert|strait|canal|national park|forest|deforestation", "INDIAN GEOGRAPHY"),
    (r"polity|constitution|article|parliament|president|judiciary|supreme court|high court|election|amendment|fundamental right|directive|panchayat|body|schedule", "INDIAN POLITY & CONSTITUTION"),
    (r"economy|economic|national income|inflation|bank|rbi|money|budget|tax|gdp|finance|market|insurance|poverty|employment|planning|sector|industry|agriculture", "INDIAN ECONOMY"),
    (r"physics|motion|force|gravity|energy|heat|thermo|sound|light|optic|electric|magnet|electromagnet|unit|measurement|instrument", "GENERAL SCIENCE — PHYSICS"),
    (r"chemistry|biochem|acid|base|salt|metal|non-metal|carbon|organic|periodic|bond|reaction|equilibrium|hydrocarbon|biomolecule", "GENERAL SCIENCE — CHEMISTRY"),
    (r"biology|botany|zoology|cell|tissue|organ|vitamin|disease|blood|nutrition|immun|genetic|plant|animal|human body|physiolog|microorganism|microbe", "GENERAL SCIENCE — BIOLOGY"),
    (r"environment|ecology|biodiversity|pollution|conservation|renewable|climate change|ecosystem", "ENVIRONMENT & ECOLOGY"),
    (r"space|nuclear|satellite|missile", "INDIAN SPACE & NUCLEAR PROGRAMME"),
    (r"defence|security|army|navy|air force", "DEFENCE & SECURITY — STATIC GK"),
    (r"science and technology|invention|discovery|scientist", "SCIENTISTS, INVENTIONS & DISCOVERIES"),
    (r"sports|game|player|tournament|trophy|olympic", "SPORTS — STATIC GK"),
    (r"award|honour|nobel|padma|bharat ratna", "AWARDS — STATIC"),
    (r"book|author", "BOOKS & AUTHORS — STATIC"),
    (r"scheme|policy|policies|initiative|welfare|social security|education", "GOVERNMENT SCHEMES & WELFARE — STATIC/SEMI-STATIC"),
    (r"index|report|survey", "IMPORTANT REPORTS & INDICES"),
    (r"census|demograph|tribe|human development", "CENSUS & DEMOGRAPHY"),
    (r"national symbol|first in india|famous|place|location|important person|personality|biograph|days", "STATIC GK"),
    (r"computer|software|hardware|internet|network|memory|keyboard|shortcut|ms office|microsoft", "COMPUTER / TECHNOLOGY OVERLAP"),
    (r"general knowledge|general science|miscellaneous", "Static GK"),
)

ENG_FALLBACK: Tuple[Tuple[str, str], ...] = (
    (r"synonym", "Synonym"),
    (r"antonym|opposite", "Antonym"),
    (r"idiom|phrase", "Idioms"),
    (r"one word|ows", "OWS"),
    (r"spell", "Spelling"),
    (r"homophone|homonym", "Homophones"),
    (r"cloze", "Cloze Test"),
    (r"para jumble|parajumble", "Para Jumbles"),
    (r"fill in the blank", "Fill in the Blanks"),
    (r"reading comprehension|comprehension|passage", "Reading Comprehension"),
    (r"error|spotting", "Error Detection"),
    (r"sentence improvement|improvement|phrase replacement|omission", "Sentence Improvement"),
    (r"shuffl", "Shuffling of Sentence parts"),
    (r"voice|passive", "Active & Passive Voice"),
    (r"narration|direct and indirect", "Direct & Indirect Speech"),
    (r"grammar|tense|article|preposition|verb|noun|pronoun|adjective|adverb|conjunction|degree|parts of speech|sentence", "Grammar"),
)

COMPUTER_FALLBACK: Tuple[Tuple[str, str], ...] = (
    (r"excel|word(?!s? )|powerpoint|office|spreadsheet|slides?", "MS Office"),
    (r"internet|network|browser|http|www|protocol|e-?mail|cyber|security|firewall|virus|antivirus", "Internet & Networking"),
    (r"operating system|windows|linux|unix|android|dos|boot", "Operating Systems"),
    (r"software|program|database|dbms|sql|algorithm|flowchart|compiler|interpreter", "Computer Software"),
    (r"hardware|device|printer|scanner|monitor|mouse|keyboard|processor|cpu|motherboard", "Computer Hardware"),
    (r"memory|ram|rom|byte|bit|binary|abbreviation|fundamental|terminolog|introduction|shortcut|input|output|generation", "Computer Fundamentals"),
)

FALLBACKS: Dict[str, Tuple[Tuple[str, str], ...]] = {
    "MATH": MATH_FALLBACK,
    "REAS": REAS_FALLBACK,
    "GK": GK_FALLBACK,
    "ENG": ENG_FALLBACK,
    "COMPUTER": COMPUTER_FALLBACK,
}

# ---------------------------------------------------------------------------
# classification result
# ---------------------------------------------------------------------------

@dataclass
class Classification:
    concept: Optional[str] = None
    chapter: Optional[str] = None
    topic: Optional[str] = None
    subject: Optional[str] = None
    source: str = "unmapped"
    confidence: str = "low"
    raw: str = ""
    #: ``None`` for a declared concept, :data:`OTHER_BUCKET` otherwise
    leaf_bucket: Optional[str] = None

    @property
    def classified(self) -> bool:
        return self.concept is not None

    @property
    def placed(self) -> bool:
        """A question is *placed* when it has a chapter to live in."""

        return self.chapter is not None

    @property
    def key(self) -> str:
        if self.concept is None:
            return "unclassified"
        return norm_key(self.concept)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "concept": self.concept,
            "chapter": self.chapter,
            "topic": self.topic,
            "subject": self.subject,
            "source": self.source,
            "confidence": self.confidence,
            "leaf_bucket": self.leaf_bucket,
        }

# ---------------------------------------------------------------------------
# chapter/topic index per subject
# ---------------------------------------------------------------------------

def _subject_names(body: Dict[str, Any]) -> List[str]:
    """Every chapter/topic/subtopic/concept name one subject's taxonomy owns."""

    names: List[str] = []
    for chapter in body.get("chapters", []):
        if chapter.get("name"):
            names.append(chapter["name"])
        for topic in chapter.get("topics", []):
            if topic.get("name"):
                names.append(topic["name"])
            for sub in topic.get("subtopics", []):
                if sub.get("name"):
                    names.append(sub["name"])
        for concept in chapter.get("concepts", []):
            if concept.get("name"):
                names.append(concept["name"])
    return names


class SubjectIndex:
    """Chapter/topic name lookup for one subject, with compiled regexes."""

    def __init__(self, subject: str, body: Dict[str, Any]) -> None:
        self.subject = subject
        self.chapters: List[str] = []
        self.chapter_keys: Dict[str, str] = {}
        self.topics: List[Tuple[str, str]] = []  # (topic, chapter)
        self.topic_keys: Dict[str, Tuple[str, str]] = {}
        for chapter in body.get("chapters", []):
            name = chapter.get("name", "")
            if not name:
                continue
            self.chapters.append(name)
            self.chapter_keys[norm_key(name)] = name
            for topic in chapter.get("topics", []):
                topic_name = topic.get("name", "")
                if topic_name:
                    self.topics.append((topic_name, name))
                    self.topic_keys.setdefault(norm_key(topic_name), (topic_name, name))
                for sub in topic.get("subtopics", []):
                    sub_name = sub.get("name", "")
                    if sub_name:
                        self.topics.append((sub_name, name))
                        self.topic_keys.setdefault(norm_key(sub_name), (sub_name, name))

    def has_chapter(self, name: str) -> bool:
        return norm_key(name) in self.chapter_keys

    def match_chapter(self, text: str, *, allow_fuzzy: bool = True) -> Optional[str]:
        """Resolve *text* to a chapter of this subject.

        Resolution order: exact chapter name, chapter name contained in the text
        (word-boundary aware — ``ratio`` is **not** contained in ``mensuration``),
        exact topic name (returns the topic's parent chapter), contained topic
        name, then a majority token-overlap match.  Returning ``None`` is
        preferred over inventing a chapter that the taxonomy does not have.

        ``allow_fuzzy=False`` skips the token-overlap step.  It is used when the
        label is another subject's vocabulary: the overlap rule would otherwise
        file e.g. the maths label *Time & Work* under an English grammar rule
        (``Time``/``and`` overlap "At, On, and In as Prepositions of Time").
        """

        key = norm_key(text)
        if not key:
            return None
        if key in self.chapter_keys:
            return self.chapter_keys[key]
        best: Optional[str] = None
        best_len = 0
        for chapter_key, chapter in self.chapter_keys.items():
            if len(chapter_key) < 4:
                continue
            if contains_phrase(key, chapter_key) and len(chapter_key) > best_len:
                best, best_len = chapter, len(chapter_key)
        if best is not None:
            return best

        topic = self.match_topic(text)
        if topic is not None:
            return topic[1]

        if not allow_fuzzy:
            return None
        return self.match_chapter_fuzzy(text)

    def match_chapter_fuzzy(self, text: str) -> Optional[str]:
        """Token-overlap fallback (>=60% of the chapter's tokens present)."""

        tokens = {t for t in re.split(r"[^a-z0-9]+", norm_key(text)) if len(t) > 2}
        if not tokens:
            return None
        best: Optional[str] = None
        best_score = 0.0
        for chapter in self.chapters:
            chapter_tokens = {t for t in re.split(r"[^a-z0-9]+", norm_key(chapter)) if len(t) > 2}
            if not chapter_tokens:
                continue
            overlap = len(tokens & chapter_tokens) / len(chapter_tokens)
            if overlap >= 0.6 and overlap > best_score:
                best, best_score = chapter, overlap
        return best

    def match_topic(self, text: str) -> Optional[Tuple[str, str]]:
        key = norm_key(text)
        if not key:
            return None
        if key in self.topic_keys:
            return self.topic_keys[key]
        best: Optional[Tuple[str, str]] = None
        best_len = 0
        for topic_key, value in self.topic_keys.items():
            if len(topic_key) < 5:
                continue
            if contains_phrase(key, topic_key) and len(topic_key) > best_len:
                best, best_len = value, len(topic_key)
        return best

    def chapter_of_topic(self, topic: str) -> Optional[str]:
        key = norm_key(topic)
        value = self.topic_keys.get(key)
        return value[1] if value else None

#: Subjects whose ``database/`` leaves are audited for cross-subject vocabulary
#: (:mod:`tools.audit_db`, rule 3).  For these subjects the classifier never
#: writes a concept that only *another* subject's taxonomy owns, because that
#: name would become a foreign slug in the leaf path (the English grammar rule
#: *At, On, and In as Prepositions of Time* used to contain a ``time-and-work``
#: leaf taken from the maths chapter *Time & Work*).  Other subjects keep shared
#: names on purpose (``reasoning`` legitimately discusses *Number System*).
LEAF_HYGIENE_SUBJECTS: Tuple[str, ...] = ("ENG",)

#: A concept that only another subject's taxonomy owns must never *name a leaf*
#: of this subject.  For a record without a chapter the concept is the leaf name,
#: so the label is dropped (it stays in ``concept_raw``) and the question is filed
#: under ``<subject>/_unclassified`` — the paper's layout decided the subject and
#: the source label is simply wrong (a reasoning word-pair question tagged
#: ``General Knowledge`` used to create ``reasoning/_unclassified/static-gk``).
#: With a chapter present the chapter names the leaf, so the shared label is kept
#: (``maths/.../speed-time-and-distance`` with the concept *Speed Time & Distance*
#: is filed correctly even though *Reasoning* also declares that chapter).
FOREIGN_CONCEPT_HYGIENE_WITHOUT_CHAPTER = True


class Classifier:
    """Full classification pipeline for one question."""

    def __init__(
        self,
        taxonomy: Dict[str, Any],
        alias_map: Dict[str, Any],
        *,
        leaf_hygiene_subjects: Sequence[str] = LEAF_HYGIENE_SUBJECTS,
    ) -> None:
        self.taxonomy = taxonomy
        self.resolver = ConceptResolver(alias_map)
        self.indexes: Dict[str, SubjectIndex] = {
            subject: SubjectIndex(subject, body)
            for subject, body in taxonomy.get("subjects", {}).items()
        }
        self.fallbacks = {
            # the leading word boundary matters: the bare pattern ``ratio`` used to
            # match inside ``mensuration`` (mensu·ration) and filed the geometry
            # concept under the maths chapter *Ratio*.
            subject: tuple(
                (re.compile(rf"\b(?:{pattern})", re.I), chapter) for pattern, chapter in rules
            )
            for subject, rules in FALLBACKS.items()
        }
        self.leaf_hygiene_subjects = tuple(leaf_hygiene_subjects)
        #: which concepts a chapter may hold / who owns a concept name
        self.scope = ConceptScope(taxonomy)
        #: case-insensitive label -> canonical spelling (taxonomy spelling wins)
        self.labels = build_label_index(taxonomy, alias_map)
        #: normalised name -> subjects that own it in their taxonomy
        self.name_owners: Dict[str, Set[str]] = {}
        for subject, body in taxonomy.get("subjects", {}).items():
            for name in _subject_names(body):
                key = norm_key(name)
                if key:
                    self.name_owners.setdefault(key, set()).add(subject)

    # -- label spelling ---------------------------------------------------
    def canonical_label(self, name: Optional[str]) -> Optional[str]:
        """First canonical spelling for *name* (case/diacritic-insensitive)."""

        if not name:
            return name
        return self.labels.get(fold_key(name), norm_space(name))

    # -- helpers ----------------------------------------------------------
    def foreign_vocabulary(self, subject: str, name: Optional[str]) -> bool:
        """True when *name* belongs to another subject and not to *subject*.

        Only names the taxonomy actually owns are considered; observed corpus
        labels (which no subject claims) are never foreign.
        """

        if not name:
            return False
        owners = self.name_owners.get(norm_key(name))
        return bool(owners) and subject not in owners

    def _label_text(self, concept: Optional[str], tags: Optional[Sequence[str]]) -> str:
        parts = [norm_space(concept)]
        for tag in tags or []:
            parts.append(norm_space(tag))
        return " | ".join(p for p in parts if p)

    def _from_alias(self, resolved: ResolvedConcept, subject: Optional[str]) -> Classification:
        chapter = resolved.chapter
        topic = resolved.topic
        return Classification(
            concept=resolved.canonical,
            chapter=chapter,
            topic=topic,
            subject=resolved.subject or subject,
            source="alias",
            confidence="high",
            raw=resolved.raw,
        )

    def _leaf_hygiene(self, result: Classification, subject: Optional[str]) -> Classification:
        """Keep unusable labels out of the subject tree.

        The invariants enforced here (and checked by ``tools/audit_db.py`` and
        ``tools/cross_subject_audit.py``):

        * a concept that cannot form a directory name (no ASCII slug at all,
          e.g. a Devanagari label on a question the Hindi detector did not
          catch) is dropped — the raw label stays in ``concept_raw``;
        * for the subjects listed in :data:`LEAF_HYGIENE_SUBJECTS` the concept
          is dropped when only another subject's taxonomy owns that name, so no
          leaf of the subject is ever named after foreign vocabulary;
        * for **every** subject the same applies when the record has no chapter:
          the concept is then the leaf's own name, so another subject's name must
          not become that leaf (see
          :data:`FOREIGN_CONCEPT_HYGIENE_WITHOUT_CHAPTER`).
        """

        if result.concept and not slugify(result.concept, fallback=""):
            result.concept = None
        if result.concept and self.foreign_vocabulary(subject, result.concept):
            if subject in self.leaf_hygiene_subjects:
                result.concept = None
            elif FOREIGN_CONCEPT_HYGIENE_WITHOUT_CHAPTER and not result.chapter:
                # the concept would become the leaf name of a subject that does
                # not own it: drop it, keep it in ``concept_raw``
                result.concept = None
        return result

    def _fallback_chapter(self, subject: Optional[str], text: str) -> Optional[str]:
        """Curated keyword fallback; the result must exist in the taxonomy."""

        rules = self.fallbacks.get(subject or "", ())
        index = self.indexes.get(subject or "")
        for pattern, chapter in rules:
            if not pattern.search(text):
                continue
            if index is None:
                return chapter
            matched = index.match_chapter(chapter)
            if matched:
                return matched
            # curated target that the taxonomy does not know: keep looking
            continue
        return None

    def _align_parent(self, result: Classification, subject: Optional[str]) -> None:
        """Re-point a leaf whose chapter contradicts the concept's parent.

        When the concept is vocabulary of exactly one chapter of the subject and
        the leaf sits in a different one (``Mensuration`` under the maths chapter
        *Ratio*, a world-geography concept under a maths chapter, ...) the leaf is
        moved to the owning chapter — including its topic when the owner declares
        the concept as a topic.  Ambiguous ownership is left alone: the
        classifier moves a leaf only when the taxonomy is unambiguous.
        """

        if not (subject and result.concept and result.chapter):
            return
        if self.scope.declares(subject, result.chapter, result.concept):
            return
        owners = self.scope.owners_of(subject, result.concept)
        if len(owners) != 1:
            return
        owner = next(iter(owners))
        if owner == norm_key(result.chapter):
            return
        result.chapter = self.scope.owner_name(subject, owner)
        result.topic = self.scope.topic_of(subject, owner, result.concept)

    def _finalize(self, result: Classification, subject: Optional[str]) -> Classification:
        """Canonical spelling + parent consistency + declared-scope bucket."""

        result = self._leaf_hygiene(result, subject)
        result.concept = self.canonical_label(result.concept)
        result.chapter = self.canonical_label(result.chapter)
        result.topic = self.canonical_label(result.topic)
        self._align_parent(result, subject)
        result.chapter = self.canonical_label(result.chapter)
        result.topic = self.canonical_label(result.topic)
        result.leaf_bucket = None
        if (
            result.concept
            and result.chapter
            and not self.scope.declares(subject, result.chapter, result.concept)
        ):
            # the chapter does not declare this concept: park it in `_other`
            result.leaf_bucket = OTHER_BUCKET
        return result

    # -- public API -------------------------------------------------------
    def classify(
        self,
        concept: Optional[str],
        tags: Optional[Sequence[str]] = None,
        subject: Optional[str] = None,
    ) -> Classification:
        resolved = self.resolver.resolve(concept, tags)
        index = self.indexes.get(subject or "")
        if resolved.canonical is not None:
            result = self._from_alias(resolved, subject)
            # an alias may resolve to another subject's chapter (the corpus
            # contains questions whose source label names a different subject);
            # in that case ignore the foreign chapter and resolve locally.
            cross_subject = bool(
                result.subject is not None
                and subject is not None
                and result.subject != subject
            )
            if cross_subject:
                result.subject = subject
                result.chapter = None
                result.topic = None
                result.source = "alias"

            text = self._label_text(concept, tags)
            if index is not None:
                if not result.chapter:
                    # A cross-subject label may still name a local chapter
                    # through exact/containment matching (*Circles* -> the maths
                    # topic *Circle*), but never through the token-overlap rule.
                    chapter = index.match_chapter(
                        result.concept or text, allow_fuzzy=not cross_subject
                    )
                    if chapter:
                        result.chapter = chapter
                        result.source = "chapter"
                        result.confidence = "medium"
                if result.chapter and not result.topic:
                    topic = index.match_topic(result.concept or text)
                    if topic:
                        result.topic = topic[0]
            if result.chapter is not None:
                return self._finalize(result, subject)
            # alias resolved but no chapter: let the keyword ladder try
            text_all = self._label_text(concept, tags)
            chapter = self._fallback_chapter(subject, text_all)
            if chapter:
                result.chapter = chapter
                result.source = "keyword"
                result.confidence = "medium"
                return self._finalize(result, subject)
            return self._finalize(result, subject)

        # unresolved label -> keyword fallbacks
        text = self._label_text(concept, tags)
        if index is not None:
            chapter = index.match_chapter(text)
            if chapter:
                topic = index.match_topic(text)
                return self._finalize(
                    Classification(
                        concept=chapter,
                        chapter=chapter,
                        topic=topic[0] if topic else None,
                        subject=subject,
                        source="chapter",
                        confidence="medium",
                        raw=norm_space(concept),
                    ),
                    subject,
                )
            topic = index.match_topic(text)
            if topic:
                return self._finalize(
                    Classification(
                        concept=topic[0],
                        chapter=topic[1],
                        topic=topic[0],
                        subject=subject,
                        source="topic",
                        confidence="medium",
                        raw=norm_space(concept),
                    ),
                    subject,
                )
        chapter = self._fallback_chapter(subject, text)
        if chapter:
            return self._finalize(
                Classification(
                    concept=chapter,
                    chapter=chapter,
                    topic=None,
                    subject=subject,
                    source="keyword",
                    confidence="low",
                    raw=norm_space(concept),
                ),
                subject,
            )

        return self._finalize(
            Classification(
                concept=None,
                chapter=None,
                topic=None,
                subject=subject,
                source="unmapped",
                confidence="low",
                raw=norm_space(concept),
            ),
            subject,
        )

# ---------------------------------------------------------------------------
# raw label inventory
# ---------------------------------------------------------------------------

def collect_raw_labels(papers: Iterable[Any]) -> List[str]:
    """Every distinct raw concept and tag string in scope (Hindi excluded).

    Excluding Hindi labels keeps the alias map focused on the English corpus;
    the Hindi labels are skipped anyway (``skipped_hindi`` bucket).
    """

    seen: Dict[str, None] = {}
    for paper in papers:
        for question in paper.questions:
            concept = norm_space(question.get("concept"))
            if concept:
                seen.setdefault(concept, None)
            for tag in question.get("tags") or []:
                value = norm_space(tag)
                if value:
                    seen.setdefault(value, None)
    return sorted(seen)
