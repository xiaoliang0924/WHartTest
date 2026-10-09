"""Normalize actual browser stdout into the shared testcase receipt protocol.

Call only for tool output, never assistant prose or executable command strings.
A receipt must identify its step; screenshots and successful actions are not assertions.
"""
import re


def normalize_testcase_receipts(output: str, testcase_id=None) -> str:
    text = str(output or '')
    sessions = set(re.findall(r'\[PERSISTENT_SESSION\]\s*session_id=case_(\d+)\b', text))
    boundaries = set(re.findall(r'\[CASE_STEP_EXECUTION\]\s*case=(\d+)\s+step=\d+', text))
    cases = sessions | boundaries
    if testcase_id is None:
        if len(cases) != 1:
            return text
        testcase_id = int(next(iter(cases)))
    case_id = int(testcase_id)
    if cases and cases != {str(case_id)}:
        return text

    # Only line-oriented stdout receipts count. Ignore quoted examples, commands,
    # generic RESULT=PASS, INFO and prose that merely claims a step succeeded.
    pattern = re.compile(
        r'(?m)^[ \t]*RESULT[ \t]*=[ \t]*(PASS|FAIL)\b[ \t]*[:：][ \t]*'
        r'(?:步骤[ \t]*(\d+)|step[ \t]+(\d+))(?!\d)[^\n]*', re.IGNORECASE,
    )
    def tag(match):
        step = int(match.group(2) or match.group(3))
        if step < 1:
            return match.group(0)
        prior = list(re.finditer(r'\[CASE_STEP_EXECUTION\]\s*case=(\d+)\s+step=(\d+)', text[:match.start()]))
        if prior and (int(prior[-1].group(1)), int(prior[-1].group(2))) == (case_id, step):
            return match.group(0)
        return f'[CASE_STEP_EXECUTION] case={case_id} step={step}\n' + match.group(0).lstrip()
    return pattern.sub(tag, text)
