"""Verify AI intent routing examples (run inside backend container)."""
from __future__ import annotations

from data_generation.intent_router import infer_business_template_key, route_llm_payload
from data_generation.llm_plan_generator import _expand_template_plan

CASES = [
    {
        'id': 1,
        'desc': '创建 TYPE_C 工单并转派给李亮',
        'expect_template': 'biz_create_and_transfer',
        'expect_steps': 6,
        'expect_step_names': ['创建工单', '分配给当前处理人', '当前处理人领取工单', '转派工单'],
        'expect_mode': 'template',
        'expect_params': {'assigneeName': '李亮', 'ticketType': 'TYPE_C'},
    },
    {
        'id': 2,
        'desc': '帮助我创建一个 TYPE_C 的自动化工单，并且工单状态是待处理',
        'expect_template': 'biz_create_and_assign',
        'expect_steps': 4,
        'expect_step_names': ['创建工单', '分配工单'],
        'expect_mode': 'template',
        'expect_params': {'ticketType': 'TYPE_C'},
    },
    {
        'id': 3,
        'desc': '仅创建 TYPE_A 工单，不要分配',
        'expect_template': 'biz_create_type_a',
        'expect_steps': 3,
        'expect_step_names': ['创建工单'],
        'expect_forbidden_steps': ['分配工单', '分配给当前处理人', '转派工单'],
        'expect_mode': 'template',
        'expect_params': {'ticketType': 'TYPE_A'},
    },
    {
        'id': 4,
        'desc': '先查工单详情 ticketId=99999，再把主题改成「回归测试」',
        'expect_template': None,
        'expect_mode': 'custom',
    },
]


def check(case: dict) -> bool:
    desc = case['desc']
    tpl = infer_business_template_key(desc)
    issues: list[str] = []

    if tpl != case.get('expect_template'):
        issues.append(f'template: got {tpl!r}, expect {case.get("expect_template")!r}')

    if case['expect_mode'] == 'template':
        llm_payload = {
            'generation_mode': 'custom',
            'steps': [{'type': 'api_call', 'body': {'ticketType': 'TYPE_C'}}],
        }
    else:
        llm_payload = {
            'generation_mode': 'custom',
            'name': '查详情改主题',
            'steps': [
                {
                    'type': 'api_call',
                    'name': '查工单详情',
                    'interface_id': 509,
                    'variables': {'ticketId': '99999'},
                },
                {
                    'type': 'api_call',
                    'name': '改主题',
                    'interface_id': 479,
                    'variables': {'subject': '回归测试'},
                },
            ],
        }

    routed = route_llm_payload(desc, llm_payload)
    mode = routed.get('generation_mode', 'custom')
    if mode != case['expect_mode']:
        issues.append(f'mode: got {mode!r}, expect {case["expect_mode"]!r}')

    step_names: list[str] = []
    summary_text = ''

    if mode == 'template' and routed.get('template_key'):
        plan = _expand_template_plan(routed, description=desc, default_environment_id=4)
        step_names = [str(s.get('name') or '') for s in plan.get('steps', [])]
        summary = plan.get('generation_summary') or {}
        summary_text = (
            f"mode={summary.get('mode')}, template={summary.get('template_name')}, "
            f"steps={summary.get('step_count')}, params={summary.get('input_params')}"
        )
        if len(step_names) != case.get('expect_steps'):
            issues.append(f'step_count: got {len(step_names)}, expect {case.get("expect_steps")}')
        for name in case.get('expect_step_names', []):
            if not any(name in sn for sn in step_names):
                issues.append(f'missing step containing: {name!r}')
        for forbidden in case.get('expect_forbidden_steps', []):
            if any(forbidden in sn for sn in step_names):
                issues.append(f'forbidden step present: {forbidden!r}')
        params = routed.get('input_params') or {}
        for key, value in case.get('expect_params', {}).items():
            if params.get(key) != value:
                issues.append(f'param {key}: got {params.get(key)!r}, expect {value!r}')
    else:
        steps = routed.get('steps') or []
        step_names = [str(s.get('name') or '') for s in steps]
        if tpl is not None:
            issues.append('should not infer template but did')
        if mode == 'template':
            issues.append('should stay custom but routed to template')

    ok = not issues
    print('=' * 60)
    print(f"例 {case['id']}: {desc}")
    print(f'  路由模板: {tpl}')
    print(f'  生成模式: {mode}')
    if step_names:
        print(f'  步骤({len(step_names)}): ' + ' -> '.join(step_names))
    if summary_text:
        print(f'  generation_summary: {summary_text}')
    print(f"  结果: {'PASS' if ok else 'FAIL'}")
    for issue in issues:
        print(f'    - {issue}')
    return ok


def main() -> None:
    results = [check(case) for case in CASES]
    print('=' * 60)
    print(f'总计: {sum(results)}/{len(results)} 通过')


if __name__ == '__main__':
    main()
