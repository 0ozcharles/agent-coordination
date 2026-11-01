"""Fixed fresh-task identities for the selector pilot.

Selected from public requirements before reference/environment validation.
Contains no solutions, hidden tests, or observed model outcomes.
"""
from task_specs import COMMIT, REPO, ROOT, TaskSpec


TASKS = {
    "dspy8563_f2_f3": TaskSpec(
        "dspy8563_f2_f3", "dspy_task", 8563, (2, 3),
        "akhatua/cooperbench-dspy:task8563",
        "80412ce96d70fdb64dcf2c63940f511d6f89ca44", ("dspy/adapters/types",)),
    "outlines1371_f1_f2": TaskSpec(
        "outlines1371_f1_f2", "dottxt_ai_outlines_task", 1371, (1, 2),
        "akhatua/cooperbench-dottxt-ai-outlines:task1371",
        "7149a5e613b9c2bb3c2ed3ee5b7c9228772504b1", ("outlines",)),
    "click2068_f4_f10": TaskSpec(
        "click2068_f4_f10", "pallets_click_task", 2068, (4, 10),
        "akhatua/cooperbench-pallets-click:task2068",
        "d791537317947448a3960662d00acf160eff2602", ("src/click",)),
    "jinja1465_f1_f2": TaskSpec(
        "jinja1465_f1_f2", "pallets_jinja_task", 1465, (1, 2),
        "akhatua/cooperbench-pallets-jinja:task1465",
        "20eb7f5e33120f1717b78bdf85540ab75631187a", ("src/jinja2",)),
}


def get_task(key):
    try:
        return TASKS[key]
    except KeyError:
        raise ValueError("Unknown selector pilot task: " + str(key)) from None
