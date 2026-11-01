"""Predeclared identities for the three-repository handoff expansion.

This registry is separate from earlier experiments. It contains task metadata,
not answers, official test contents, or model outcomes.
"""
from task_specs import COMMIT, REPO, ROOT, TaskSpec


TASKS = {
    "llama_index17070_f1_f3": TaskSpec(
        "llama_index17070_f1_f3", "llama_index_task", 17070, (1, 3),
        "akhatua/cooperbench-llama-index:task17070",
        "0417380ada935e277d686aef941b84032e67c57a",
        ("llama-index-core/llama_index/core/evaluation/retrieval",)),
    "hf_datasets3997_f2_f3": TaskSpec(
        "hf_datasets3997_f2_f3", "huggingface_datasets_task", 3997, (2, 3),
        "akhatua/cooperbench-huggingface-datasets:task3997",
        "e229fade6d1a5b8eedb02370b5e6605b9b8b44aa", ("src/datasets/features",)),
    "pillow68_f1_f2": TaskSpec(
        "pillow68_f1_f2", "pillow_task", 68, (1, 2),
        "akhatua/cooperbench-pillow:task68",
        "5d52ede584c5137f3a0b00be97dea8764404306b", ("src/PIL",)),
}


def get_task(key):
    try:
        return TASKS[key]
    except KeyError:
        raise ValueError("Unknown cross-task handoff task: " + str(key)) from None
