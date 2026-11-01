"""Fixed task identities for the first two-task expansion; no provider calls."""
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT / "CooperBench"
COMMIT = "63b9d44d9f39a02fccf5bf0052db48a917a011fd"


@dataclass(frozen=True)
class TaskSpec:
    key: str
    repo: str
    task_id: int
    features: tuple[int, int]
    image: str
    base: str
    code_roots: tuple[str, ...]

    @property
    def agents(self):
        return dict(zip(("agent1", "agent2"), self.features))

    @property
    def task_dir(self):
        return REPO / "dataset" / self.repo / f"task{self.task_id}"

    @property
    def image_record_path(self):
        return ROOT / "artifacts/task_images" / f"{self.key}.json"

    def as_dict(self):
        value = asdict(self)
        value["features"] = list(self.features)
        value["code_roots"] = list(self.code_roots)
        return value


TASKS = {
    "click2800_f1_f2": TaskSpec(
        "click2800_f1_f2", "pallets_click_task", 2800, (1, 2),
        "akhatua/cooperbench-pallets-click:task2800",
        "d8763b93021c416549b5f8b4b5497234619410db", ("src/click",)),
    "dirty_equals43_f3_f7": TaskSpec(
        "dirty_equals43_f3_f7", "samuelcolvin_dirty_equals_task", 43, (3, 7),
        "akhatua/cooperbench-samuelcolvin-dirty-equals:task43",
        "593bcccf738ab8b724d7cb860881d74344171f5f", ("dirty_equals",)),
}


def get_task(key):
    try:
        return TASKS[key]
    except KeyError:
        raise ValueError("Unknown expansion task: " + str(key)) from None
