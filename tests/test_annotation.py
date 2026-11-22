import random
import tempfile
import unittest
import zipfile
from itertools import combinations
from pathlib import Path

from agent_coordination import annotation as ann

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def write_xlsx(path, sheets, shared=()):
    """A minimal workbook: {sheet name: sheet XML body}; `shared` are raw <si> elements."""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook xmlns="{MAIN}" xmlns:r="{REL}"><sheets>'
                   + "".join(f'<sheet name="{n}" sheetId="{i}" r:id="rId{i}"/>' for i, n in enumerate(sheets, 1)) + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + "".join(f'<Relationship Id="rId{i}" Target="worksheets/sheet{i}.xml"/>' for i in range(1, len(sheets) + 1)) + "</Relationships>")
        z.writestr("xl/sharedStrings.xml", f'<sst xmlns="{MAIN}">' + "".join(shared) + "</sst>")
        for i, body in enumerate(sheets.values(), 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", f'<worksheet xmlns="{MAIN}"><sheetData>{body}</sheetData></worksheet>')


def inline(ref, text):
    return f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>'


class DesignTest(unittest.TestCase):
    def test_every_pair_and_every_rater_gets_the_same_load(self):
        raters = [f"R{i}" for i in range(1, 7)]
        pairs = list(combinations(raters, 2))
        given = ann.deal([f"A{i:03d}" for i in range(120)], pairs, random.Random(1))
        self.assertEqual({list(given.values()).count(p) for p in pairs}, {8})
        self.assertEqual({sum(r in p for p in given.values()) for r in raters}, {40})
        self.assertTrue(all(a != b for a, b in given.values()))

    def test_checked_clauses_are_clauses_of_the_rule(self):
        for rule, checks in ann.CHECKED.items():
            self.assertTrue(set(checks.values()) <= set(range(1, ann.N_CLAUSES[rule] + 1)))


class AgreementTest(unittest.TestCase):
    def test_alpha_on_krippendorffs_example(self):
        # Krippendorff (2011), Computing Krippendorff's Alpha-Reliability: four observers, twelve units
        rows = ["123321412...", "1233224125.3", ".3332342251.", "12332441251."]
        units = [[int(r[i]) if r[i] != "." else None for r in rows] for i in range(12)]
        self.assertEqual(ann.alpha(units, "nominal"), 0.743)
        self.assertEqual(ann.alpha(units, "interval"), 0.849)

    def test_alpha_limits(self):
        self.assertEqual(ann.alpha([[1, 1], [0, 0], [2, 2]], "interval"), 1.0)
        self.assertIsNone(ann.alpha([[1, 1], [1, 1]]))          # no variation, undefined
        self.assertIsNone(ann.alpha([[1, None], [0, None]]))    # nothing rated twice
        self.assertLess(ann.alpha([[0, 2], [2, 0], [0, 2], [2, 0]], "interval"), 0)

    def test_kappa(self):
        self.assertEqual(ann.kappa([(True, True), (False, False), (True, True), (False, False)]), 1.0)
        self.assertEqual(ann.kappa([(0, 0), (1, 1), (2, 2), (0, 2)]), 0.636)   # (3/4 - 5/16) / (1 - 5/16)
        self.assertGreater(ann.kappa([(0, 0), (1, 1), (2, 2), (0, 1)], True), ann.kappa([(0, 0), (1, 1), (2, 2), (0, 2)], True))

    def test_answers_as_typed(self):
        self.assertEqual([ann.score(v) for v in (0, "1", 2.0, " 2 ", "", None, 3, "x")], [0, 1, 2, 2, None, None, None, None])
        self.assertEqual([ann.yes_no(v) for v in ("Yes", "no ", "是", True, None, "maybe")], [True, False, True, True, None, None])
        self.assertEqual([ann.three_way(v) for v in ("yes", "Partly", "NO", None)], [2, 1, 0, None])
        self.assertEqual(ann.clause_numbers("2, 4"), [2, 4])
        self.assertEqual(ann.clause_numbers(3), [3])
        self.assertEqual(ann.clause_numbers(None), [])


class WorkbookTest(unittest.TestCase):
    def test_reads_what_excel_and_others_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.xlsx"
            shared = ["<si><t>plain</t></si>", '<si><r><t>rich </t></r><r><rPr/><t>text</t></r></si>', "<si><t>标注</t><rPh sb=\"0\" eb=\"2\"><t>ビョウチュウ</t></rPh></si>"]
            body = ('<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c><c r="C1" t="s"><v>2</v></c></row>'
                    '<row r="3"><c r="B3"><v>2</v></c><c r="C3"><v>1.5</v></c><c r="D3" t="b"><v>1</v></c><c r="AA3" t="str"><v>formula text</v></c><c r="E3"/></row>'
                    '<row><c t="inlineStr"><is><t>no refs</t></is></c><c><v>7</v></c></row>')
            write_xlsx(path, {"S": body}, shared)
            cells = ann.read_xlsx(path)["S"]
        self.assertEqual((cells[(1, 1)], cells[(1, 2)], cells[(1, 3)]), ("plain", "rich text", "标注"))
        self.assertEqual((cells[(3, 2)], cells[(3, 3)], cells[(3, 4)], cells[(3, 27)]), (2, 1.5, True, "formula text"))
        self.assertNotIn((3, 5), cells)
        self.assertEqual((cells[(4, 1)], cells[(4, 2)]), ("no refs", 7))

    def test_answers_of_one_annotator(self):
        a = (f'<row r="1">{inline("A1", "item")}{inline("B1", "move")}{inline("C1", "text")}{inline("D1", "assertiveness&#10;强硬度")}{inline("E1", "cooperativeness")}'
             f'{inline("F1", "asks_needs")}{inline("G1", "discloses_priorities")}{inline("H1", "note")}</row>'
             f'<row r="3">{inline("A3", "EXAMPLE")}{inline("B3", "agent1")}<c r="D3"><v>2</v></c></row>'
             f'<row r="5">{inline("A5", "AP1")}{inline("B5", "#1 Agent 1")}{inline("C5", "proposes")}</row>'
             f'<row r="6">{inline("A6", "AP1")}{inline("B6", "agent1")}<c r="D6"><v>2</v></c><c r="E6"><v>0</v></c>{inline("F6", "no")}{inline("G6", "yes")}</row>'
             f'<row r="7">{inline("A7", "AP1")}{inline("B7", "agent2")}<c r="D7"><v>1</v></c>{inline("H7", "unsure")}</row>'
             f'<row r="9">{inline("A9", "A017")}{inline("B9", "agent2")}{inline("E9", "1")}</row>')
        b = (f'<row r="1">{inline("A1", "item")}{inline("B1", "move")}{inline("C1", "text")}{inline("D1", "worth")}{inline("E1", "followed")}{inline("F1", "broken_clauses")}</row>'
             f'<row r="4">{inline("A4", "B003")}{inline("B4", "rule")}{inline("C4", "the rule")}</row>'
             f'<row r="5">{inline("A5", "B003")}{inline("B5", "answer")}{inline("E5", "no")}<c r="F5"><v>4</v></c></row>')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "returned.xlsx"
            write_xlsx(path, {"README": f'<row r="1">{inline("A1", "annotator: R4")}</row>', "A_style": a, "B_compliance": b})
            who, answers = ann.workbook_answers(path)
        self.assertEqual(who, "R4")
        self.assertEqual(answers["a"], {"AP1": {"agent1_assertiveness": 2, "agent1_cooperativeness": 0, "agent1_asks_needs": "no", "agent1_discloses_priorities": "yes",
                                                "agent2_assertiveness": 1, "agent2_note": "unsure"}, "A017": {"agent2_cooperativeness": "1"}})
        self.assertEqual(answers["b"], {"B003": {"followed": "no", "broken_clauses": 4}})
        self.assertEqual(answers["c"], {})


class WebExportTest(unittest.TestCase):
    def test_export_of_the_annotation_page(self):
        data = {"format": "bargain-annotation-web/1", "annotator": "R3", "started": True,
                "answers": {"AP1": {"agent1_assertiveness": 2, "agent1_asks_needs": "no", "agent1_note": "x"}, "B017": {"followed": "no", "broken_clauses": "1,4"},
                            "C005": {"q1_decisions": "partly"}, "EXAMPLE": {"followed": "yes"}}}
        who, answers = ann.web_answers(data)
        self.assertEqual(who, "R3")
        self.assertEqual(answers, {"a": {"AP1": {"agent1_assertiveness": 2, "agent1_asks_needs": "no", "agent1_note": "x"}},
                                   "b": {"B017": {"followed": "no", "broken_clauses": "1,4"}}, "c": {"C005": {"q1_decisions": "partly"}}})
        self.assertEqual(ann.clause_numbers(answers["b"]["B017"]["broken_clauses"]), [1, 4])
        self.assertEqual(ann.web_answers({"format": "something else", "annotator": "R3", "answers": {}}), (None, {}))
        self.assertEqual(ann.web_answers({"annotator": "nobody", "answers": {}}), (None, {}))


if __name__ == "__main__":
    unittest.main()
