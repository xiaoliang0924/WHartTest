# -*- coding: utf-8 -*-
"""WHartTest 造数管理 Skill 工具。"""

import argparse
import io
import json
import os
import sys
from pathlib import Path
from urllib import error, parse, request

if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

_DEFAULT_BASE_URL = 'http://127.0.0.1:8000'
_DEFAULT_API_KEY = 'wharttest-default-mcp-key-2025'


def _base_url():
    return (os.environ.get('WHARTTEST_BACKEND_URL') or _DEFAULT_BASE_URL).rstrip('/')


def _api_key():
    return (os.environ.get('WHARTTEST_API_KEY') or _DEFAULT_API_KEY).strip()


def _headers():
    return {
        'accept': 'application/json',
        'Content-Type': 'application/json',
        'X-API-Key': _api_key(),
    }


def _error(message, data=None):
    result = {'status': 'error', 'message': message}
    if data not in (None, '', {}):
        result['data'] = data
    return result


def _success(data=None, message='ok'):
    result = {'status': 'success', 'message': message}
    if data is not None:
        result['data'] = data
    return result


def _load_json(raw_value, default):
    if raw_value is None or raw_value == '':
        return default
    if isinstance(raw_value, str) and raw_value.startswith('@'):
        return json.loads(Path(raw_value[1:]).read_text(encoding='utf-8'))
    return json.loads(raw_value)


def _request(method, url, payload=None, params=None):
    body = None
    headers = _headers()
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    if params:
        query = parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = f'{url}?{query}'
    req = request.Request(url, data=body, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=120) as resp:
            text = resp.read().decode('utf-8')
            return json.loads(text) if text else {}
    except error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='ignore')
        try:
            parsed = json.loads(detail)
        except json.JSONDecodeError:
            parsed = {'message': detail or str(exc)}
        message = parsed.get('message') or parsed.get('detail') or str(exc)
        return _error(message, data=parsed)


def _project_url(project_id, suffix):
    return f'{_base_url()}/api/projects/{project_id}/{suffix}'


def list_plans(args):
    resp = _request('GET', _project_url(args.project_id, 'data-generation-plans/'))
    return resp


def get_plan(args):
    plan_id = args.plan_id
    if not plan_id:
        return _error('plan_id 不能为空')
    return _request('GET', _project_url(args.project_id, f'data-generation-plans/{plan_id}/'))


def create_plan(args):
    payload = args.payload_obj or {}
    return _request('POST', _project_url(args.project_id, 'data-generation-plans/'), payload=payload)


def update_plan(args):
    plan_id = args.plan_id
    if not plan_id:
        return _error('plan_id 不能为空')
    payload = args.payload_obj or {}
    return _request('PATCH', _project_url(args.project_id, f'data-generation-plans/{plan_id}/'), payload=payload)


def run_plan(args):
    plan_id = args.plan_id
    if not plan_id:
        return _error('plan_id 不能为空')
    payload = args.payload_obj or {'input_params': {}}
    if 'input_params' not in payload:
        payload = {'input_params': payload}
    return _request('POST', _project_url(args.project_id, f'data-generation-plans/{plan_id}/run/'), payload=payload)


def list_runs(args):
    params = args.params_obj or {}
    return _request('GET', _project_url(args.project_id, 'data-generation-runs/'), params=params)


def get_run(args):
    run_id = args.run_id
    if not run_id:
        return _error('run_id 不能为空')
    return _request('GET', _project_url(args.project_id, f'data-generation-runs/{run_id}/'))


def rerun_run(args):
    run_id = args.run_id
    if not run_id:
        return _error('run_id 不能为空')
    return _request('POST', _project_url(args.project_id, f'data-generation-runs/{run_id}/rerun/'))


def cleanup_run(args):
    run_id = args.run_id
    if not run_id:
        return _error('run_id 不能为空')
    return _request('POST', _project_url(args.project_id, f'data-generation-runs/{run_id}/cleanup/'))


def list_templates(args):
    return _request('GET', _project_url(args.project_id, 'data-generation-plans/templates/'))


def run_template(args):
    payload = args.payload_obj or {}
    if 'template_key' not in payload:
        return _error('payload 需包含 template_key')
    return _request('POST', _project_url(args.project_id, 'data-generation-plans/run_template/'), payload=payload)


def generate_plan(args):
    payload = args.payload_obj or {}
    if not payload.get('description'):
        return _error('payload 需包含 description')
    return _request('POST', _project_url(args.project_id, 'data-generation-plans/generate/'), payload=payload)


def analyze_suite(args):
    payload = args.payload_obj or {}
    if not payload.get('suite_id'):
        return _error('payload 需包含 suite_id')
    return _request('POST', _project_url(args.project_id, 'data-generation-plans/analyze_suite/'), payload=payload)


ACTIONS = {
    'list_plans': list_plans,
    'get_plan': get_plan,
    'create_plan': create_plan,
    'update_plan': update_plan,
    'run_plan': run_plan,
    'list_runs': list_runs,
    'get_run': get_run,
    'rerun_run': rerun_run,
    'cleanup_run': cleanup_run,
    'list_templates': list_templates,
    'run_template': run_template,
    'generate_plan': generate_plan,
    'analyze_suite': analyze_suite,
}


def main():
    parser = argparse.ArgumentParser(description='WHartTest 造数管理 Skill 工具')
    parser.add_argument('--action', required=True, choices=sorted(ACTIONS))
    parser.add_argument('--project_id', type=int, required=True)
    parser.add_argument('--plan_id', type=int)
    parser.add_argument('--run_id', type=int)
    parser.add_argument('--payload', help='请求体 JSON，支持 @文件路径')
    parser.add_argument('--params', help='查询参数 JSON，支持 @文件路径')
    args = parser.parse_args()

    try:
        args.payload_obj = _load_json(args.payload, {})
        args.params_obj = _load_json(args.params, {})
        result = ACTIONS[args.action](args)
    except Exception as exc:
        result = _error(str(exc))

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if isinstance(result, dict) and result.get('status') == 'error':
        sys.exit(1)


if __name__ == '__main__':
    main()
