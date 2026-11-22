"""The human annotation study: who rates what, reading the returned workbooks, agreement statistics.

Standard library only, so that the analysis runs wherever the project runs; only building the workbooks needs openpyxl
(scripts/make_annotation_pack.py)."""
import itertools
import re
import zipfile
from collections import Counter
from xml.etree import ElementTree

# the clauses of each rule that the compliance checker verifies, as the number of the clause in the rule text shown to
# the annotators (make_annotation_pack.CLAUSES); the remaining clauses of a rule are not checked by the program
CHECKED = {"competing": {"opens_high": 1, "one_decision_per_step": 2, "accepts_60": 4},
           "collaborating": {"asks_first": 2, "proposes_trade": 3, "accepts_60": 4},
           "compromising": {"steps_of_two_decisions": 1, "monotone": 2, "accepts_50": 3},
           "accommodating": {"asks_first": 1, "accepts_first_above_floor": 2},
           "avoiding": {"one_proposal": 1, "ends_by_second_move": 3}}
N_CLAUSES = {"competing": 4, "collaborating": 4, "compromising": 3, "accommodating": 3, "avoiding": 3}
A_FIELDS = ("assertiveness", "cooperativeness", "asks_needs", "discloses_priorities")
C_FIELDS = ("q1_decisions", "q2_needed_options", "q3_middle_option", "q4_importance")


# ---------------------------------------------------------------- design

def deal(items, pairs, rng):
    """Give every item to one pair of raters. The items are dealt in the order given to the pairs taken in a random
    order, so the pairs get equal numbers (when the count divides) and a run of similar items is spread over them."""
    order = list(pairs)
    rng.shuffle(order)
    return {item: order[i % len(order)] for i, item in enumerate(items)}


# ---------------------------------------------------------------- agreement

def kappa(pairs, weighted=False):
    """Cohen's kappa over (a, b) pairs; linearly weighted for ordinal scores when weighted is True."""
    pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
    if not pairs:
        return None
    cats = sorted({x for p in pairs for x in p})
    span = (max(cats) - min(cats)) if weighted and len(cats) > 1 else 1
    w = (lambda a, b: 1 - abs(a - b) / span) if weighted else (lambda a, b: 1.0 if a == b else 0.0)
    n = len(pairs)
    po = sum(w(a, b) for a, b in pairs) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum(ca[x] * cb[y] * w(x, y) for x in cats for y in cats) / n ** 2
    return None if pe == 1 else round((po - pe) / (1 - pe), 3)


def alpha(units, metric="nominal"):
    """Krippendorff's alpha. `units` is a list of the values the raters gave to each unit (None is a missing rating);
    units with fewer than two ratings are dropped. The raters may differ from unit to unit, which is the design here
    (each item goes to two of the six annotators). metric: nominal, or interval for the 0-2 scores."""
    units = [[v for v in u if v is not None] for u in units]
    units = [u for u in units if len(u) >= 2]
    n = sum(len(u) for u in units)
    if n <= 1:
        return None
    delta = (lambda a, b: float(a != b)) if metric == "nominal" else (lambda a, b: float(a - b) ** 2)
    observed = sum(sum(delta(a, b) for a, b in itertools.permutations(u, 2)) / (len(u) - 1) for u in units) / n
    counts = Counter(v for u in units for v in u)
    expected = sum(counts[a] * counts[b] * delta(a, b) for a in counts for b in counts) / (n * (n - 1))
    return None if expected == 0 else round(1 - observed / expected, 3)


def agree(pairs, ordinal=False):
    pairs = [(a, b) for a, b in pairs if a is not None and b is not None]
    out = {"n": len(pairs), "exact": round(sum(a == b for a, b in pairs) / len(pairs), 3) if pairs else None}
    if ordinal:
        out["within_one"] = round(sum(abs(a - b) <= 1 for a, b in pairs) / len(pairs), 3) if pairs else None
    return out


# ---------------------------------------------------------------- answers as people type them

def score(v):
    """A 0-2 score, or None when the cell is empty or holds something else."""
    try:
        x = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    return int(x) if x in (0, 1, 2) else None


def yes_no(v):
    return {"yes": True, "y": True, "true": True, "是": True, "no": False, "n": False, "false": False, "否": False}.get(str(v).strip().lower()) if v is not None else None


def three_way(v):
    """yes / partly / no as 2 / 1 / 0."""
    return {"yes": 2, "y": 2, "partly": 1, "partial": 1, "p": 1, "no": 0, "n": 0}.get(str(v).strip().lower()) if v is not None else None


def clause_numbers(v):
    return sorted({int(x) for x in re.findall(r"\d", str(v))}) if v is not None else []


# ---------------------------------------------------------------- reading a workbook

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _text(node):
    """The text of a shared or inline string: its own <t> and those of its runs; phonetic runs (<rPh>) are not text."""
    parts = []
    for child in node:
        if _local(child.tag) == "t":
            parts.append(child.text or "")
        elif _local(child.tag) == "r":
            parts.extend(t.text or "" for t in child if _local(t.tag) == "t")
    return "".join(parts)


def _cells(sheet, shared):
    cells, row_n = {}, 0
    for row in sheet.iter():
        if _local(row.tag) != "row":
            continue
        row_n = int(row.get("r", row_n + 1))
        col_n = 0
        for c in row:
            if _local(c.tag) != "c":
                continue
            ref = re.fullmatch(r"([A-Z]+)\d+", c.get("r", ""))
            col_n = sum((ord(ch) - 64) * 26 ** i for i, ch in enumerate(reversed(ref.group(1)))) if ref else col_n + 1
            kind = c.get("t", "n")
            v = next((x for x in c if _local(x.tag) == "v"), None)
            if kind == "inlineStr":
                node = next((x for x in c if _local(x.tag) == "is"), None)
                value = _text(node) if node is not None else None
            elif v is None or v.text is None:
                continue
            elif kind == "s":
                value = shared[int(v.text)]
            elif kind == "b":
                value = v.text == "1"
            else:
                try:
                    number = float(v.text)
                    value = int(number) if number == int(number) else number
                except ValueError:
                    value = v.text
            if value not in (None, ""):
                cells[(row_n, col_n)] = value
    return cells


def read_xlsx(path):
    """The cell values of every sheet of an .xlsx file: {sheet name: {(row, column): value}}, both counted from 1."""
    with zipfile.ZipFile(path) as z:
        def xml(name):
            return ElementTree.fromstring(z.read(name))
        shared = [_text(si) for si in xml("xl/sharedStrings.xml") if _local(si.tag) == "si"] if "xl/sharedStrings.xml" in z.namelist() else []
        rels = {r.get("Id"): r.get("Target") for r in xml("xl/_rels/workbook.xml.rels")}
        out = {}
        for el in xml("xl/workbook.xml").iter():
            if _local(el.tag) == "sheet":
                target = rels[next(v for k, v in el.attrib.items() if _local(k) == "id")]
                out[el.get("name")] = _cells(xml(target.lstrip("/") if target.startswith("/") else "xl/" + target), shared)
        return out


def workbook_answers(path):
    """What one annotator filled in: (annotator id, {"a" | "b" | "c": {item: {field: value as typed}}}).

    In every answer sheet the first row names the columns (the first line of each header cell), column A holds the
    item and column B says what the row is; answers are read from the rows marked agent1, agent2 or answer."""
    book = read_xlsx(path)
    found = re.search(r"R\d+", str(book.get("README", {}).get((1, 1), ""))) or re.search(r"R\d+", str(path).rsplit("/", 1)[-1])
    out = {}
    for task, name in (("a", "A_style"), ("b", "B_compliance"), ("c", "C_extraction")):
        cells = book.get(name, {})
        header = {col: str(v).split("\n")[0].strip() for (row, col), v in cells.items() if row == 1}
        answers = {}
        for (row, col), kind in cells.items():
            item = cells.get((row, 1))
            if col != 2 or kind not in ("agent1", "agent2", "answer") or not re.fullmatch(r"[ABC]P?\d+", str(item)):
                continue
            prefix = f"{kind}_" if task == "a" else ""
            for c, key in header.items():
                if c > 3 and (row, c) in cells:
                    answers.setdefault(item, {})[prefix + key] = cells[(row, c)]
        out[task] = answers
    return (found.group(0) if found else None), out
