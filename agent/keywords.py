"""Curated keyword rules used to guess a question's subject from ``concept``/``tags``.

These rules are deliberately kept in one place so the classifier can be audited
and regenerated (``state/subject_keywords.json`` records the compiled table).
Rules are evaluated **in order**; the first match wins.  ``None`` means
"ambiguous / no usable signal" — such questions are excluded from the signature
agreement denominator instead of biasing it.

The table was built by inspecting the observed vocabulary of the corpus
(1,402 distinct concepts, 935 distinct tags) against the four taxonomy files and
then tightened on the conflicts that showed up in real papers, e.g.::

    "Classification of Elements and Periodicity in Properties" -> GK (not REAS)
    "Indian Rivers and Water Resources"                        -> GK (not MATH)
    "Gupta Age"                                                -> GK (not MATH)
    "Biogeography"                                             -> GK (not MATH "graph")
    "Plane Figures" / "Solid Figures"                          -> MATH
    "Circular Arrangement"                                     -> REAS

The exact-match anchors in :data:`EXACT_SUBJECT` are checked first; they carry
the highest confidence because they were derived from a pure concept table.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# subject codes
# ---------------------------------------------------------------------------

ENG = "ENG"
REAS = "REAS"
MATH = "MATH"
GK = "GK"
COMPUTER = "COMPUTER"
HINDI = "HINDI"

ALL_SUBJECTS: Tuple[str, ...] = (REAS, GK, MATH, ENG, COMPUTER, HINDI)

# ---------------------------------------------------------------------------
# explicit exception anchors: label -> subject
#
# These are checked before the ordered regex rules so that a broad regex
# elsewhere cannot steal them.
# ---------------------------------------------------------------------------

EXACT_SUBJECT: Dict[str, str] = {
    # -- GK items that collide with reasoning/maths vocabulary -------------
    "classification of elements and periodicity in properties": GK,
    "periodic table": GK,
    "biogeography": GK,
    "indian rivers and water resources": GK,
    "water resource": GK,
    "gupta age": GK,
    "vedic age": GK,
    "sangam age": GK,
    "post guptas age": GK,
    "harappa civilization": GK,
    "delhi sultanate": GK,
    "mughal empire": GK,
    "later mughals": GK,
    "mauryan empire": GK,
    "maratha empire": GK,
    "southern dynasties": GK,
    "dynasties of magadh": GK,
    "chola empire cheras pandyas": GK,
    "triparty struggle prathiharas rashtrakutas imperial cholas": GK,
    "emergence of regional kingdoms": GK,
    "emergence of regional powers": GK,
    "regional kingdoms": GK,
    "rajput states": GK,
    "indian kingdoms in 18th century": GK,
    "religious movements": GK,
    "work power energy": GK,
    "work  power energy": GK,
    "motion in a straight line": GK,
    "moving charges and magnetism": GK,
    "electromagnetic waves": GK,
    "measures of skewness": MATH,
    "correlation and regression": MATH,
    "standard deviation": MATH,
    "mean proportional": MATH,
    "fourth proportional": MATH,
    "third proportional": MATH,
    "compound ratios": MATH,
    "direct or indirect proportion": MATH,
    "simple and compound both": MATH,
    "elementary statistics": MATH,
    "probability and statistics": MATH,
    # -- notable maths labels ---------------------------------------------
    "quantitative aptitude": MATH,
    "numerical ability": MATH,
    "mathematics": MATH,
    "solid figures": MATH,
    "plane figures": MATH,
    "data interpretation": MATH,
    "bar graph": MATH,
    "pie chart": MATH,
    "line graph": MATH,
    "tabulation": MATH,
    "single bar": MATH,
    "double bar": MATH,
    "single line": MATH,
    "single pie": MATH,
    "tabulation and pie chart": MATH,
    "pie chart, tabulation and pie chart": MATH,
    "interest": MATH,
    "quick math": MATH,
    "bodmas rule": MATH,
    "surds and indices": MATH,
    "surds & indices": MATH,
    "indices": MATH,
    # -- reasoning labels that collide ------------------------------------
    "circular arrangement": REAS,
    "linear arrangement": REAS,
    "polygon arrangement": REAS,
    "seating arrangement": REAS,
    "missing number in matrix": REAS,
    "missing number": REAS,
    "counting figures": REAS,
    "figure counting": REAS,
    "completion of incomplete pattern": REAS,
    "paper folding and cutting": REAS,
    "cube and dice": REAS,
    "clock and calendar": REAS,
    "situation reaction test": REAS,
    "quant based puzzle": REAS,
    "logical puzzle": REAS,
    "operations on place value": REAS,
    "mathematical inequalities": REAS,
    "arrangement and pattern": REAS,
    "analytical decision making": REAS,
    "letter based": REAS,
    "letter and number based": REAS,
    "number based": REAS,
    "order based": REAS,
    "rank based": REAS,
    "image based": REAS,
    "hidden image": REAS,
    "input output": REAS,
    "non verbal reasoning": REAS,
    "verbal reasoning": REAS,
    "general intelligence and reasoning": REAS,
    "problem solving": REAS,
    "decision making": REAS,
    # -- english labels ----------------------------------------------------
    "one word substitution": ENG,
    "phrase or idiom meaning": ENG,
    "phrase replacement": ENG,
    "error spotting": ENG,
    "parts of speech": ENG,
    "direct and indirect speech": ENG,
    "active and passive voice": ENG,
    # -- computer labels ---------------------------------------------------
    "computer fundamentals or terminologies": COMPUTER,
    "computer abbreviations": COMPUTER,
    "introduction to computers": COMPUTER,
    "keyboard shortcuts": COMPUTER,
    "operating systems": COMPUTER,
    "microsoft office": COMPUTER,
    "microsoft word": COMPUTER,
    "software": COMPUTER,
    "hardware": COMPUTER,
    "internet": COMPUTER,
    "networking": COMPUTER,
    "memory": COMPUTER,
}

# ---------------------------------------------------------------------------
# ordered regex rules
# ---------------------------------------------------------------------------

HINDI_PATTERN = (
    r"[\u0900-\u097F]"
    r"|अनेकार्थक|अविकारी|कारक|गद्यांश|पर्यायवाची|पाठ बोधन|मुहावरे|रिक्त स्थान|लिंग|लोकोक्ति"
    r"|वचन|वर्ण विचार|वाक्य|विकारी|विलोम|शब्द|समश्रुत|हिंदी|हिन्दी|व्याकरण|संधि|समास|अलंकार"
)

COMPUTER_PATTERN = (
    r"computer|computers|ms[- ]?office|ms[- ]?word|ms[- ]?excel|ms[- ]?powerpoint|microsoft"
    r"|internet|network(?:ing)?|software|hardware|operating system|memory|keyboard|shortcut"
    r"|binary|database|dbms|cyber|e[- ]?mail|protocol|byte|antivirus|virus|programming"
    r"|algorithm|flowchart|input[- ]output|abbreviation"
)

ENG_PATTERN = (
    r"grammar|vocabulary|synonym|antonym|idiom|phrase (?:replacement|or idiom)"
    r"|one word substitution|ows|spelling|homophone|homonym|cloze|para jumble|parajumble"
    r"|sentence improvement|sentence structure|error (?:detection|spotting)"
    r"|fill in the blank|reading comprehension|comprehension|active and passive|passive voice"
    r"|direct and indirect|narration|parts of speech|verbal ability|english"
    r"|sentence|voice|narration|shuffl|omission|word usage"
)

GK_EXCEPTION_PATTERN = (
    r"classification of elements|periodic table|biogeography|indian rivers|water resource"
    r"|gupta age|vedic age|sangam age|post guptas|harappa|mohenjo|indus valley"
    r"|human body|human physiology|anatomy|biomolecule|hydrocarbon|electromagnetic"
    r"|work power energy|moving charges|motion in a straight|genetics|evolution"
    r"|national park|wildlife sanctuary|biosphere|ramsar|climate change|conservation"
)

REAS_PATTERN = (
    r"reasoning|analogy|classification|coding|decoding|series|alphabet|dictionary"
    r"|word formation|mathematical operation|digit operation|ranking|order|direction"
    r"|blood relation|venn|syllogism|statement|assumption|argument|inference"
    r"|course of action|cause and effect|assertion|decision making|deduction"
    r"|data sufficiency|seating|arrangement|puzzle|scheduling|grouping|selection"
    r"|clock|calendar|missing number|matrix|mirror|water image|paper folding|paper cutting"
    r"|embedded|figures?|\bdice\b|cubes?|rotation|symmetry|spatial|pattern|place value"
    r"|input output|odd one|hidden image|image based|counting|similarity|rank based"
    r"|logical|analytical|critical reasoning|non verbal|situation reaction|quant based"
    r"|letter based|number based|mathematical inequalit|arrange"
)

MATH_PATTERN = (
    r"percentage|percent|profit|loss|discount|interest|ratio|proportion|average"
    r"|time and work|time & work|work efficiency|work and wages|pipe and cistern|pipes and cistern"
    r"|speed|distance|train|boat and river|number system|simplification|simplify|bodmas"
    r"|data interpretation|\bgraphs?\b|\bcharts?\b|tabulation|geometry|mensuration|trigonometric|trigonometry"
    r"|algebra|co-?ordinate|solid figure|plane figure|mixture|alligation|partnership"
    r"|problem on age|problem on ages|problem on train|statistics|probability|permutation"
    r"|combination|lcm|hcf|divisibility|remainder|fraction|decimal|integer|square root"
    r"|surds|indices|exponent|power of"
    r"|identity|identities|progression|heights and distances|installment|partial speed"
    r"|relative speed|quadratic|linear equation|rational or irrational|even and odd"
    r"|compound ratio|triangles|circles|quadrilateral|polygon|lines and angles|solid figures"
    r"|numerical ability|quantitative aptitude|quick math|basic calculation|mathematics"
    r"|magnitude|approximation|simplification|mensuration|arithmetic|number series"
)

GK_PATTERN = (
    r"polity|constitution|parliament|judiciary|president|prime minister|state government"
    r"|local government|constitutional bodies|election|history|ancient|medieval|modern india"
    r"|national movement|freedom|gandhi|congress|revolt|reform|british|european|viceroy"
    r"|governor|mughal|mauryan|gupta|sultanate|maratha|sikh|buddhism|jainism|vedic"
    r"|harappa|indus|dynasty|dynasties|empire|kingdom|geography|climate|monsoon|soil"
    r"|river|lake|mountain|ocean|continent|desert|island|forest|agriculture|crop"
    r"|mineral|industry|industrial|infrastructure|transport|railway|population|census"
    r"|tribe|economy|economic|bank|banking|money|budget|tax|taxation|inflation|gdp"
    r"|national income|finance|financial|market|rbi|rrb|nABARD|insurance"
    r"|biology|botany|zoology|chemistry|physics|science|scientific|cell|tissue|organ"
    r"|vitamin|disease|blood|nutrition|immunity|microbiology|biotechnology|plant|animal"
    r"|acid|base|salt|metal|non[- ]metal|carbon|organic|chemical|reaction|equilibrium"
    r"|motion|force|gravity|optics|light|sound|heat|thermodynamics|electricity|magnetism"
    r"|wave|nuclear|unit|measurement|instrument|invention|discovery|scientist"
    r"|environment|ecology|biodiversity|pollution|renewable|climate change"
    r"|art and culture|culture|architecture|monument|dance|music|festival|fair|painting"
    r"|theatre|handicraft|literature|book|author|award|honour|honor|nobel|padma"
    r"|sports|game|player|tournament|trophy|olympic|asia cup|world cup"
    r"|current affair|general knowledge|general science|static|person in news|obituar"
    r"|appointment|resignation|scheme|policy|policies|initiative|organisation|organization"
    r"|international|national affairs|state affairs|defence|space|nuclear|agreement|mou"
    r"|committee|index|report|survey|days and events|important|famous|places|location"
    r"|first in india|national symbols|miscellaneous gk|biograph|language and literature"
    r"|business and economy|economy|budget|planning|poverty|employment|external sector"
    r"|judiciary|polity|administration|governance|welfare|security|disaster|hazard"
)

#: (subject or None, compiled pattern) evaluated in order.
RULES: List[Tuple[Optional[str], "re.Pattern[str]"]] = [
    (HINDI, re.compile(HINDI_PATTERN)),
    (COMPUTER, re.compile(COMPUTER_PATTERN, re.I)),
    (ENG, re.compile(ENG_PATTERN, re.I)),
    (GK, re.compile(GK_EXCEPTION_PATTERN, re.I)),
    (REAS, re.compile(REAS_PATTERN, re.I)),
    (MATH, re.compile(MATH_PATTERN, re.I)),
    (GK, re.compile(GK_PATTERN, re.I)),
]

#: Labels that carry no usable subject signal.  Kept explicit so the signature
#: check can count them as "not comparable" rather than as disagreement.
AMBIGUOUS = frozenset(
    {
        "unidentified",
        "miscellaneous",
        "data",
        "distribution",
        "sequence",
        "consecutive",
        "mapping",
        "match the following",
        "meaning based",
        "election based",
        "basic operation",
        "other dimensions",
        "operations research",
        "research in education",
        "foundation of business",
        "date",
        "general",
        "image",
        "based",
        "",
    }
)

# ---------------------------------------------------------------------------
# compiled exact table (normalised keys)
# ---------------------------------------------------------------------------


def _norm(label: str) -> str:
    from .util import norm_key

    return norm_key(label)


EXACT_NORMALISED: Dict[str, str] = {_norm(k): v for k, v in EXACT_SUBJECT.items()}
AMBIGUOUS_NORMALISED = frozenset(_norm(a) for a in AMBIGUOUS if a)



def _split_labels(text: object) -> List[str]:
    """Split a concept/tag field into individual labels.

    Handles ``"Art and Culture, Famous People"`` and tag lists.  Commas inside
    parentheses are preserved.
    """

    if text is None:
        return []
    if isinstance(text, (list, tuple, set)):
        out: List[str] = []
        for item in text:
            out.extend(_split_labels(item))
        return out

    value = str(text).strip()
    if not value:
        return []

    parts: List[str] = []
    depth = 0
    current: List[str] = []
    for ch in value:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def subject_for_text(text: object) -> Optional[str]:
    """Return the subject implied by a raw concept/tag string, or ``None``."""

    labels = _split_labels(text)
    if not labels:
        return None

    for label in labels:
        key = _norm(label)
        if not key:
            continue
        if key in EXACT_NORMALISED:
            return EXACT_NORMALISED[key]
        if key in AMBIGUOUS_NORMALISED:
            continue
        for subject, pattern in RULES:
            if pattern.search(label):
                return subject
    return None


def keyword_table_export() -> Dict[str, object]:
    """Serialisable description of the compiled keyword table (for state/)."""

    return {
        "subjects": list(ALL_SUBJECTS),
        "exact_anchors": EXACT_SUBJECT,
        "ambiguous_labels": sorted(a for a in AMBIGUOUS if a),
        "ordered_rules": [
            {"subject": subject, "pattern": pattern.pattern} for subject, pattern in RULES
        ],
    }
