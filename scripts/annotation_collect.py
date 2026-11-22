"""Turn the documents of the annotation page's database into the files the agreement analysis reads.

The page (annotation/web, published as an artifact) keeps, for an annotator who is allowed to write to it, one document
per answered item at answers/<viewer id>/items/<item>. Save those documents with the artifact's data tool into a
directory (one JSON file per document, <dir>/answers/<viewer id>/items/<item>.json) and run
    python3 scripts/annotation_collect.py <dir>
It writes annotation/returned/web_<annotator>.json, one per annotator, and says when two viewers used the same number.
"""
import glob
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, "src")
from agent_coordination.annotation import WEB_FORMAT  # noqa: E402


def main(root, out="annotation/returned"):
    by_annotator = defaultdict(lambda: defaultdict(dict))   # annotator -> viewer -> item -> answers
    for f in sorted(glob.glob(f"{root}/answers/*/items/*.json")):
        doc = json.loads(Path(f).read_text())
        doc = doc.get("data", doc)
        if doc.get("annotator") and isinstance(doc.get("v"), dict):
            by_annotator[doc["annotator"]][Path(f).parts[-3]][doc.get("item") or Path(f).stem] = doc["v"]
    Path(out).mkdir(parents=True, exist_ok=True)
    for annotator, viewers in sorted(by_annotator.items()):
        if len(viewers) > 1:
            print(f"! {annotator} was used by {len(viewers)} viewers ({', '.join(f'{v}: {len(a)} items' for v, a in viewers.items())}); the one with the most answers is kept, look at the others")
        answers = max(viewers.values(), key=len)
        Path(out, f"web_{annotator}.json").write_text(json.dumps({"format": WEB_FORMAT, "annotator": annotator, "answers": answers}, ensure_ascii=False, indent=1) + "\n")
        print(f"{annotator}: {len(answers)} items -> {out}/web_{annotator}.json")
    if not by_annotator:
        print(f"no answer documents under {root}/answers")


if __name__ == "__main__":
    main(*sys.argv[1:3])
