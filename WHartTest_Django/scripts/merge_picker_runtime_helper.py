"""Merge verified picker selection while preserving unrelated runtime hotfixes."""
from pathlib import Path
import sys


def merge_picker(source, target):
    anchor = '/**\n * 确认弹窗后'
    start = source.index('async function queryFilterResults(')
    end = source.index(anchor, start)
    target_start = target.find('async function queryFilterResults(')
    if target_start < 0:
        target_start = target.find('async function readFilterDialogSelection(')
    if target_start < 0:
        target_start = target.index('async function selectFilterDialogOption(')
    target_end = target.index(anchor, target_start)
    merged = target[:target_start] + source[start:end] + target[target_end:]
    if '  queryFilterResults,\n' not in merged:
        merged = merged.replace('  selectFilterDialogOption,\n', '  selectFilterDialogOption,\n  queryFilterResults,\n', 1)
    return merged


if __name__ == '__main__':
    source, target = map(Path, sys.argv[1:])
    merged = merge_picker(source.read_text(), target.read_text())
    if merged != target.read_text():
        temporary = target.with_name(target.name + '.picker-merge.tmp')
        temporary.write_text(merged)
        temporary.replace(target)
    print('picker state verification synchronized')
