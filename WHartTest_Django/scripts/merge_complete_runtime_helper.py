"""Merge the completion dispatcher into a deployed Playwright helper copy.

Keep every other runtime hotfix intact.  The source is the tested bundled
helper, while the target may be a persistent media-directory override.
"""

import pathlib
import sys


source_path = pathlib.Path(sys.argv[1])
target_path = pathlib.Path(sys.argv[2])
source = source_path.read_text(encoding="utf-8")
target = target_path.read_text(encoding="utf-8")
source_start = source.index("function completeTicketActionForStep(")
source_end = source.index("/**\n * 按字段筛选", source_start)
target_start = target.index("async function runCompleteTicketCaseStep(")
existing_dispatcher = target.find("function completeTicketActionForStep(")
if existing_dispatcher != -1:
    if existing_dispatcher > target_start:
        raise SystemExit("Completion dispatcher appears after its helper")
    target_start = existing_dispatcher
target_end = target.index("/**\n * 按字段筛选", target_start)
if target.count("async function runCompleteTicketCaseStep(") != 1:
    raise SystemExit("Ambiguous completion helper target")
merged = target[:target_start] + source[source_start:source_end] + target[target_end:]
export_anchor = "  runCompleteTicketCaseStep,\n"
if merged.count(export_anchor) != 1:
    raise SystemExit("Ambiguous completion helper export")
for name in ("completeTicketActionForStep", "hasVisibleExactText", "waitForVisibleExactText"):
    exported = f"  {name},\n"
    if exported not in merged:
        merged = merged.replace(export_anchor, export_anchor + exported, 1)
if "不支持的工单完成步骤" in merged:
    raise SystemExit("Old fixed-step error remains")
if merged == target:
    print("already up to date")
else:
    target_path.write_text(merged, encoding="utf-8")
    print("completion dispatcher merged")
