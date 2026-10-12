"""Merge the tested table assertion into runtime hotfixes without replacing them."""
from pathlib import Path
import sys


def merge_table_assertion(source, target):
    start = source.index('async function assertTableColumnEquals(')
    end = source.index('/**\n * 断言页面', start)
    block = source[start:end]
    anchor = '/**\n * 断言页面'
    old_start = target.find('async function assertTableColumnEquals(')
    if old_start >= 0:
        old_end = target.index(anchor, old_start)
        target = target[:old_start] + block + target[old_end:]
    else:
        insertion = target.index(anchor)
        target = target[:insertion] + block + target[insertion:]
    export = '  assertTableColumnEquals,\n'
    if export not in target:
        export_anchor = '  assertPageShows,\n'
        if target.count(export_anchor) != 1:
            raise ValueError('Table helper export anchor is ambiguous')
        target = target.replace(export_anchor, export_anchor + export, 1)
    return target


if __name__ == '__main__':
    source, target = map(Path, sys.argv[1:])
    merged = merge_table_assertion(source.read_text(), target.read_text())
    if merged != target.read_text():
        temporary = target.with_name(target.name + '.table-merge.tmp')
        temporary.write_text(merged)
        temporary.replace(target)
    print('table assertion synchronized')
