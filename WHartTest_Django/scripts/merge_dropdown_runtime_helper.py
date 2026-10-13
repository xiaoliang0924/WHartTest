"""Merge the evidence-producing dropdown helper into runtime hotfixes."""
from pathlib import Path
import sys


def merge_dropdown_helper(source, target):
    start_marker = "async function selectFormDropdownOption("
    end_marker = "async function selectFormDateRange("
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    block = source[start:end]

    target_start = target.find(start_marker)
    if target_start >= 0:
        target_end = target.index(end_marker, target_start)
        target = target[:target_start] + block + target[target_end:]
    else:
        target_end = target.index(end_marker)
        target = target[:target_end] + block + target[target_end:]
    return target


if __name__ == "__main__":
    source, target = map(Path, sys.argv[1:])
    original = target.read_text()
    merged = merge_dropdown_helper(source.read_text(), original)
    if merged != original:
        temporary = target.with_name(target.name + ".dropdown-merge.tmp")
        temporary.write_text(merged)
        temporary.replace(target)
    print("dropdown interaction evidence synchronized")
