"""Keep a SWE-bench task's gold patch and evaluator tests away from the estimator and the worker."""

import json
import re

VISIBLE = ("instance_id", "repo", "base_commit", "problem_statement")
# hints_text holds issue comments that can postdate the fix, so it is withheld too.
HIDDEN = ("patch", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS", "hints_text")
_MIN_LINE = 30
_MIN_BLOCK = 80


class LeakError(ValueError):
    """Hidden SWE-bench material reached, or could have reached, the estimator or worker."""


def visible_task(instance: dict) -> dict:
    """The only fields the estimator and worker may see."""
    missing = [name for name in VISIBLE if not instance.get(name)]
    if missing:
        raise ValueError(f"SWE-bench instance lacks {', '.join(missing)}")
    return {name: instance[name] for name in VISIBLE}


def _test_ids(value) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = [value]
    return [item for item in value or [] if isinstance(item, str)]


def _added(diff: str) -> tuple[list[str], list[str], list[str]]:
    """Distinctive added lines, runs of consecutive added lines, and paths the diff creates."""
    lines, blocks, created, block, previous = [], [], [], [], ""
    for line in (diff or "").splitlines() + [""]:
        if line.startswith("+") and not line.startswith("+++"):
            block.append(line[1:])
            if len(line[1:].strip()) >= _MIN_LINE:
                lines.append(line[1:].strip())
        else:
            if len(block) >= 3 and len("\n".join(block)) >= _MIN_BLOCK:
                blocks.append("\n".join(block))
            block = []
            if line.startswith("+++ b/") and previous.startswith("--- /dev/null"):
                created.append(line[6:].strip())
        previous = line
    return lines, blocks, created


def leak_check(instance: dict, *, commit: str, files: dict[str, str], texts: dict[str, str]) -> dict:
    """Raise LeakError unless the estimator's inputs and outputs are free of the hidden fields.

    `files` are the selected repository files as read at `commit`; `texts` is everything else that is sent or
    saved (issue text, prompt framing, the estimate JSON). Repository files legitimately share single lines
    with the gold patch, so they are checked for whole added blocks and created paths, and the commit is pinned.
    """
    if commit != instance["base_commit"]:
        raise LeakError(f"Context was read at {commit}, not the task's base_commit {instance['base_commit']}")
    lines, blocks, created = [], [], []
    for name in ("patch", "test_patch"):
        found = _added(instance.get(name) or "")
        lines += found[0]
        blocks += found[1]
        created += found[2]
    problem = instance["problem_statement"]
    # A line the issue itself quotes is public; only material absent from the issue is a leak.
    lines = [line for line in dict.fromkeys(lines) if line not in problem]
    blocks = [block for block in blocks if block not in problem]
    test_ids = [test for test in _test_ids(instance.get("FAIL_TO_PASS")) + _test_ids(instance.get("PASS_TO_PASS"))
                if test not in problem]
    if leaked := sorted(set(created) & set(files)):
        raise LeakError(f"Selected files exist only in the gold or test patch: {', '.join(leaked)}")
    for path, content in files.items():
        if any(block in content for block in blocks):
            raise LeakError(f"{path} contains a block added by the gold or test patch; it was not read at the base commit")
    for name, text in texts.items():
        for label, markers in (("a gold or test patch line", lines), ("a gold or test patch block", blocks),
                               ("an evaluator test id", test_ids)):
            if any(marker in text for marker in markers):
                raise LeakError(f"{name} contains {label}")
        hint = (instance.get("hints_text") or "").strip()
        if len(hint) >= _MIN_BLOCK and hint not in problem and hint in text:
            raise LeakError(f"{name} contains the withheld hints_text")
        if re.search(r'"(?:test_patch|FAIL_TO_PASS|PASS_TO_PASS)"\s*:', text):
            raise LeakError(f"{name} carries a hidden SWE-bench field")
    return {"status": "passed", "instance_id": instance["instance_id"], "commit": commit,
            "hidden_fields_withheld": list(HIDDEN), "checked_texts": sorted(texts), "checked_files": sorted(files),
            "markers": {"patch_lines": len(lines), "patch_blocks": len(blocks), "evaluator_test_ids": len(test_ids),
                        "paths_created_by_patches": len(created)},
            "scope": "Checks the estimator's inputs and saved estimate. It says nothing about estimate accuracy."}
