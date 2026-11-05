from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_coordination import rescore

PYTEST_PASS = """INSTALLING_DEPENDENCIES...
RUNNING_TESTS...
Tests/test_image_quantize.py::test_error_threshold_quantize
  /workspace/repo/Tests/test_image_quantize.py:109: DeprecationWarning: __array__ implementation doesn't accept a copy keyword, so passing copy=False failed.
\x1b[33m=================== \x1b[32m8 passed\x1b[0m, \x1b[33m1 skipped, 4 warnings\x1b[0m\x1b[33m in 0.44s\x1b[0m\x1b[33m ===================\x1b[0m
TEST_EXECUTION_COMPLETED
"""
PYTEST_FAIL = """RUNNING_TESTS...
FAILED tests/test_x.py::test_a - AssertionError
========================= 1 failed, 7 passed in 1.20s =========================
"""
PYTEST_ERROR = "==================== 3 passed, 1 error in 0.50s ====================\n"
NO_TESTS = "============================ no tests ran in 0.12s =============================\n"
GO_PASS = "Running Go tests...\nok  \tgithub.com/go-chi/chi/v5\t0.012s\n"
GO_FAIL = "--- FAIL: TestMux (0.00s)\nFAIL\nFAIL\tgithub.com/go-chi/chi/v5\t0.010s\n"
GO_BUILD = "# github.com/go-chi/chi/v5\n./mux.go:12:2: undefined: x\nFAIL\tgithub.com/go-chi/chi/v5 [build failed]\n"
CARGO_PASS = "running 3 tests\ntest result: ok. 3 passed; 0 failed; 0 ignored\n"
CARGO_FAIL = "test result: ok. 2 passed; 0 failed\ntest result: FAILED. 1 passed; 1 failed\n"
JEST_PASS = "PASS src/__tests__/useForm.test.tsx\nTests:       12 passed, 12 total\n"
JEST_FAIL = "FAIL src/__tests__/useForm.test.tsx\nTest Suites: 1 failed, 1 total\nTests:       1 failed, 11 passed, 12 total\n"
VITEST_FAIL = " Test Files  1 failed (1)\n      Tests  1 failed | 9 passed (10)\n"


class RescoreTests(unittest.TestCase):
    def test_paper_check_fails_a_passing_run_with_failed_in_a_warning(self):
        self.assertFalse(rescore.paper_check(PYTEST_PASS))
        self.assertIn("copy=False failed", rescore.trigger(PYTEST_PASS))
        self.assertEqual(rescore.corrected_check(PYTEST_PASS), (True, "pytest"))

    def test_paper_check_on_clean_logs(self):
        self.assertTrue(rescore.paper_check(GO_PASS))
        self.assertFalse(rescore.paper_check(PYTEST_FAIL))

    def test_corrected_check_reads_each_framework(self):
        cases = {PYTEST_FAIL: (False, "pytest"), PYTEST_ERROR: (False, "pytest"), NO_TESTS: (False, "pytest"),
                 GO_PASS: (True, "go"), GO_FAIL: (False, "go"), GO_BUILD: (False, "go"),
                 CARGO_PASS: (True, "cargo"), CARGO_FAIL: (False, "cargo"),
                 JEST_PASS: (True, "jest"), JEST_FAIL: (False, "jest"), VITEST_FAIL: (False, "jest"),
                 "Installing...\nsomething broke\n": (None, "unparsed")}
        for log, expected in cases.items():
            with self.subTest(log=log[:40]):
                self.assertEqual(rescore.corrected_check(log), expected)

    def test_corrected_entries_replace_scored_results(self):
        key = ("pillow_task", 290, 1, 4)
        pairs = {("gpt5", key): {"upstream": "coop/pillow_task/task290", "conflict": False, "passed": False,
                                 "stages": {"naive": [True, False], "union": [None, None], "llm": [None, None]}}}
        rows = [{"model": "gpt5", "repo": "pillow_task", "task_id": 290, "features": "1 4", "slot": 2,
                 "stage": "naive", "corrected": True}]
        entry = rescore.corrected_entries(pairs, rows)["gpt5", key]
        self.assertEqual(entry["stages"]["naive"], [True, True])
        self.assertTrue(entry["passed"])
        self.assertEqual(pairs["gpt5", key]["stages"]["naive"], [True, False])  # recorded entry untouched


if __name__ == "__main__":
    unittest.main()
