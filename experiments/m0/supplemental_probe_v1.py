"""Fixed post-run suite for two feature-6 requirements absent from the primary tests.

Runs only inside an evaluator container against its delivered /workspace/repo.
Failures are observations, not an invitation to repair the accepted submission.
"""
import json
import sys
import tempfile

SUITE_ID = "jinja-task1621-supplemental-v1"


def alias(name):
    return "index.html" if name == "home" else name


def observe(call):
    try:
        return {"result": call()}
    except Exception as exc:
        return {"exception": type(exc).__name__, "name": getattr(exc, "name", None), "message": str(exc)}


def run_suite():
    sys.path.insert(0, "/workspace/repo/src")
    import jinja2
    from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader, FunctionLoader

    checks = []

    def record(identifier, issue_class, expected, call, expression):
        actual = observe(call)
        checks.append({"id": identifier, "issue_class": issue_class, "expected": expected,
                       "actual": actual, "passed": all(actual.get(k) == v for k, v in expected.items()),
                       "call": expression})

    def choice():
        return ChoiceLoader([DictLoader({"index.html": "INDEX"})], path_transform=alias)

    record("choice_loader_environment_lookup", "composite_path_transform", {"result": "INDEX"},
           lambda: Environment(loader=choice()).get_template("home").render(),
           "Environment(loader=ChoiceLoader([DictLoader({'index.html':'INDEX'})], path_transform=alias)).get_template('home').render()")
    record("choice_loader_get_source_control", "composite_path_transform", {"result": "INDEX"},
           lambda: choice().get_source(Environment(), "home")[0],
           "ChoiceLoader(..., path_transform=alias).get_source(Environment(), 'home')[0]")

    def missing(identifier, factory):
        record(identifier, "original_error_name", {"exception": "TemplateNotFound", "name": "home"},
               lambda: Environment(loader=factory()).get_template("home").render(),
               "Environment(loader=loader).get_template('home'); alias('home') == 'index.html', target absent")

    missing("dict_loader_original_error_name", lambda: DictLoader({}, path_transform=alias))
    missing("function_loader_original_error_name", lambda: FunctionLoader(lambda name: None, path_transform=alias))
    with tempfile.TemporaryDirectory() as directory:
        missing("filesystem_loader_original_error_name", lambda: FileSystemLoader(directory, path_transform=alias))
    return {"suite_id": SUITE_ID, "jinja2_source": jinja2.__file__, "checks": checks,
            "passed": all(c["passed"] for c in checks), "tests_passed": sum(c["passed"] for c in checks),
            "tests_total": len(checks)}


if __name__ == "__main__":
    try:
        result = run_suite()
    except Exception as exc:
        # Broken submitted code (for example a SyntaxError on import) is a
        # task failure, not a failure of Docker or the evaluation service.
        result = {"suite_id": SUITE_ID, "passed": False, "checks": [],
                  "tests_total": 0, "tests_passed": 0,
                  "artifact_load_error": {"type": type(exc).__name__, "message": str(exc)}}
    print(json.dumps(result, indent=2, ensure_ascii=False))
