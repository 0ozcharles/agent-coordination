"""Frozen, model-free choice from a validated checkpoint diff packet.

Only source edit coordinates determine the action. Context lines in a diff do
not count as edits. Comments/docstrings do count when their lines are edited;
this is a textual integration-risk rule, not a semantic dependency detector.
The caller must validate the packet against its checkpoint before calling this
module, and freeze the returned decision before either continuation is run.

No runner, SDK, evaluator, history, model outcome, or hidden test is imported or
opened. Packet-wide hashing is provenance only; the feature projection is
explicitly allowlisted below. This module cannot prove when its caller ran it;
the supervisor must enforce decision-before-outcome ordering in its receipt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

AGENTS = ("agent1", "agent2")
SCHEMA = "checkpoint_edit_overlap_selector_v1"
RULE_ID = "shared_base_edit_v1"
RULE = {
    "id": RULE_ID,
    "positive_action": "integrator",
    "negative_action": "strongshare",
    "threshold": "one_or_more_shared_edit_locations",
    "shared_location": [
        "the same deleted/replaced one-based original source line",
        "the same pure-insertion gap (number of original lines before it)",
        "both agents create the same path, or a deletion meets any edit to that path",
    ],
    "diff_context_counts_as_change": False,
    "comment_and_docstring_edits_count": True,
    "identical_edits_still_count_as_overlap": True,
    "adjacent_edits_do_not_count": True,
    "task_specific_exceptions": False,
}
PACKET_FIELDS = {
    "schema_version", "representation", "context_lines", "checkpoint_sha256",
    "task_key", "base_commit", "checkpoint_only", "limits_bytes", "agents",
}
CHANGE_FIELDS = {
    "path", "change_type", "before_sha256", "after_sha256",
    "before_bytes", "after_bytes", "unified_diff",
}
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)$")
_SHA = re.compile(r"^[0-9a-f]{64}$")


def canonical_sha256(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _git_path(path):
    if re.fullmatch(r"[A-Za-z0-9_./-]+", path):
        return path
    parts = []
    for byte in path.encode("utf-8"):
        if byte in (34, 92):
            parts.append("\\" + chr(byte))
        elif 32 <= byte < 127:
            parts.append(chr(byte))
        else:
            parts.append("\\%03o" % byte)
    return '"' + "".join(parts) + '"'


def _path(value):
    if not isinstance(value, str) or not value or "\\" in value or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid source path")
    p = PurePosixPath(value)
    if (p.is_absolute() or any(x in (".", "..", "") for x in value.split("/"))
            or p.suffix != ".py" or any(x.lower() in ("tests", "__pycache__") for x in p.parts)):
        raise ValueError("Only safe repository-relative Python source paths are allowed")
    return value


def _spans(numbers):
    result = []
    for number in sorted(numbers):
        if result and number == result[-1][1] + 1:
            result[-1][1] = number
        else:
            result.append([number, number])
    return result


def parse_source_change(row):
    """Read actual edits from one complete Git-style unified diff; fail closed."""
    if not isinstance(row, dict) or set(row) != CHANGE_FIELDS:
        raise ValueError("Incomplete source change record")
    path = _path(row["path"])
    kind = row["change_type"]
    if kind not in {"added", "modified", "deleted"}:
        raise ValueError("Invalid source change type")
    for side in ("before", "after"):
        absent = (kind == "added" and side == "before") or (kind == "deleted" and side == "after")
        checksum, size = row[side + "_sha256"], row[side + "_bytes"]
        if absent:
            if checksum is not None or size is not None:
                raise ValueError("Absent source side must use null hash and size")
        elif (not isinstance(checksum, str) or not _SHA.fullmatch(checksum)
              or type(size) is not int or not 0 <= size <= 512 * 1024):
            raise ValueError("Invalid source hash or size")
    if kind == "modified" and row["before_sha256"] == row["after_sha256"]:
        raise ValueError("Unchanged source cannot be a change record")
    diff = row["unified_diff"]
    if not isinstance(diff, str) or not diff.endswith("\n"):
        raise ValueError("Complete newline-terminated unified diff required")
    lines = diff.split("\n")[:-1]
    old_path, new_path = _git_path("a/" + path), _git_path("b/" + path)
    if not lines or lines[0] != f"diff --git {old_path} {new_path}":
        raise ValueError("Diff path does not match source record")
    i = 1
    if kind != "modified":
        mode = "new file mode 100644" if kind == "added" else "deleted file mode 100644"
        if i >= len(lines) or lines[i] != mode:
            raise ValueError("Missing file-presence marker")
        i += 1
    if i >= len(lines) or not re.fullmatch(r"index [0-9a-f]{7}\.\.[0-9a-f]{7}", lines[i]):
        raise ValueError("Missing source index marker")
    i += 1
    if i == len(lines):
        side = "after" if kind == "added" else "before"
        if kind == "modified" or row[side + "_bytes"] != 0:
            raise ValueError("Missing source diff hunks")
        removed, gaps, added_count = set(), set(), 0
    else:
        expected_old = "/dev/null" if kind == "added" else old_path
        expected_new = "/dev/null" if kind == "deleted" else new_path
        if lines[i:i + 2] != ["--- " + expected_old, "+++ " + expected_new]:
            raise ValueError("Source side headers mismatch")
        i += 2
        removed, gaps, added_count = set(), set(), 0
        previous_old_end = previous_new_end = 0
        while i < len(lines):
            match = _HUNK.fullmatch(lines[i])
            if not match:
                raise ValueError("Malformed or trailing unified-diff content")
            old_start, old_count, new_start, new_count = [int(x) if x is not None else 1 for x in match.groups()]
            old_cursor = old_start if old_count else old_start + 1
            new_cursor = new_start if new_count else new_start + 1
            if (old_cursor < previous_old_end or new_cursor < previous_new_end
                    or (old_count and old_start == 0) or (new_count and new_start == 0)):
                raise ValueError("Overlapping or invalid diff hunk positions")
            i += 1
            old_used = new_used = 0
            block_removed, block_added, block_gap = [], 0, None

            def flush():
                nonlocal block_removed, block_added, block_gap
                if block_added and not block_removed:
                    gaps.add(block_gap)
                block_removed, block_added, block_gap = [], 0, None

            previous_was_content = False
            while i < len(lines) and not lines[i].startswith("@@ "):
                line = lines[i]
                if line == "\\ No newline at end of file":
                    if not previous_was_content:
                        raise ValueError("Orphan no-newline marker")
                    previous_was_content = False
                    i += 1
                    continue
                if not line or line[0] not in " +-":
                    raise ValueError("Invalid diff body line")
                sign = line[0]
                if sign == " ":
                    flush()
                    old_cursor += 1
                    new_cursor += 1
                    old_used += 1
                    new_used += 1
                else:
                    if block_gap is None:
                        block_gap = old_cursor - 1
                    if sign == "-":
                        removed.add(old_cursor)
                        block_removed.append(old_cursor)
                        old_cursor += 1
                        old_used += 1
                    else:
                        block_added += 1
                        added_count += 1
                        new_cursor += 1
                        new_used += 1
                if old_used > old_count or new_used > new_count:
                    raise ValueError("Diff hunk count overflow")
                previous_was_content = True
                i += 1
            flush()
            if old_used != old_count or new_used != new_count:
                raise ValueError("Truncated diff hunk")
            previous_old_end, previous_new_end = old_cursor, new_cursor
        if not removed and not gaps:
            raise ValueError("Source diff has no actual changes")
        if kind == "added" and removed or kind == "deleted" and added_count:
            raise ValueError("File-presence marker contradicts diff")
    return {
        "path": path, "change_type": kind,
        "before_sha256": row["before_sha256"], "after_sha256": row["after_sha256"],
        "changed_base_spans": _spans(removed), "pure_insertion_gaps": sorted(gaps),
        "removed_base_lines": len(removed), "added_lines": added_count,
    }


def decide(packet):
    """Return a deterministic auditable decision; no external state is consulted."""
    if (not isinstance(packet, dict) or set(packet) != PACKET_FIELDS
            or packet["schema_version"] != 2 or packet["representation"] != "unified_diff"
            or packet["checkpoint_only"] is not True or packet["context_lines"] != 3
            or not isinstance(packet["checkpoint_sha256"], str)
            or not _SHA.fullmatch(packet["checkpoint_sha256"])
            or not isinstance(packet["base_commit"], str)
            or not re.fullmatch(r"[0-9a-f]{40}", packet["base_commit"])
            or not isinstance(packet["task_key"], str) or not packet["task_key"]
            or set(packet.get("agents", {})) != set(AGENTS)):
        raise ValueError("Only a complete checkpoint-only schema-2 packet is accepted")
    projected, features = {}, {}
    for agent in AGENTS:
        evidence = packet["agents"][agent]
        if not isinstance(evidence, dict) or set(evidence) != {"original_task", "source_changes", "scratch_files"}:
            raise ValueError("Unexpected agent evidence fields")
        changes = evidence["source_changes"]
        if not isinstance(changes, list) or not changes:
            raise ValueError("Both agents require source changes")
        parsed = [parse_source_change(row) for row in changes]
        if len({row["path"] for row in parsed}) != len(parsed):
            raise ValueError("Duplicate source paths")
        features[agent] = {row["path"]: row for row in sorted(parsed, key=lambda r: r["path"])}
        projected[agent] = sorted(changes, key=lambda r: r["path"])
    common = sorted(set(features["agent1"]) & set(features["agent2"]))
    overlaps = []
    for path in common:
        left, right = features["agent1"][path], features["agent2"][path]
        if left["before_sha256"] != right["before_sha256"]:
            raise ValueError("Shared path does not have the same base source hash")
        old_lines = lambda x: {n for lo, hi in x["changed_base_spans"] for n in range(lo, hi + 1)}
        shared_lines = old_lines(left) & old_lines(right)
        shared_gaps = set(left["pure_insertion_gaps"]) & set(right["pure_insertion_gaps"])
        presence = (left["change_type"] == right["change_type"] == "added"
                    or "deleted" in {left["change_type"], right["change_type"]})
        if shared_lines or shared_gaps or presence:
            overlaps.append({"path": path, "shared_base_spans": _spans(shared_lines),
                             "shared_pure_insertion_gaps": sorted(shared_gaps),
                             "file_presence_interaction": presence})
    result = {
        "schema": SCHEMA, "rule_id": RULE_ID, "rule_sha256": canonical_sha256(RULE),
        "selector_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "checkpoint_sha256": packet["checkpoint_sha256"],
        "packet_sha256": canonical_sha256(packet),
        "feature_input_sha256": canonical_sha256(projected),
        "selected_action": "integrator" if overlaps else "strongshare",
        "features": {"agents": features, "common_source_files": common, "overlaps": overlaps},
        "input_contract": {
            "feature_fields": sorted(CHANGE_FIELDS),
            "feature_scope": "agents.agent{1,2}.source_changes only",
            "history_task_scratch_test_or_outcome_features": False,
            "packet_hash_is_provenance_only": True,
            "caller_must_validate_packet_against_checkpoint": True,
            "caller_must_freeze_before_continuation_outcomes": True,
            "model_calls": 0,
        },
    }
    result["decision_sha256"] = canonical_sha256(result)
    return result


def verify_decision(packet, decision):
    """Pure read-only recomputation; changed rule/source/input/choice fails."""
    expected = decide(packet)
    if decision != expected:
        raise ValueError("Frozen selector decision does not match source and packet")
    return decision


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    packet = json.loads(args.packet.read_text())
    decision = decide(packet)
    if args.output.exists():
        verify_decision(packet, json.loads(args.output.read_text()))
    else:
        with args.output.open("x") as stream:
            json.dump(decision, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
    print(json.dumps({"action": decision["selected_action"], "decision_sha256": decision["decision_sha256"]}))


if __name__ == "__main__":
    main()
