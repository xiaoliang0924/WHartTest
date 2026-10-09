"""Coordinate authentication without dropping business or role-switch actions."""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit


def same_route(left: str, right: str) -> bool:
    try:
        a, b = urlsplit(left), urlsplit(right)
        return bool(a.netloc and b.netloc and
                    (a.scheme.lower(), a.netloc.lower(), a.path.rstrip('/') or '/') ==
                    (b.scheme.lower(), b.netloc.lower(), b.path.rstrip('/') or '/'))
    except ValueError:
        return False


def is_password_fill(step: dict) -> bool:
    if step.get('ope_key') != 'fill':
        return False
    selector = step.get('element') or {}
    label = ' '.join(str(selector.get(key) or '') for key in ('locator_value', 'locator_value_2', 'locator_value_3'))
    label += ' ' + str(step.get('element_name') or '')
    return bool(re.search(r'password|passwd|密码|密碼|口令', label, re.I)) or (
        (step.get('ope_value') or {}).get('value') == '${{password}}'
    )


def prepare_authenticated_group(steps: list[dict], *, mode: str, login_profile: dict | None,
                                first_group: bool, bound_authentication: bool = False) -> list[dict]:
    """Retain assertions after a proven same-account environment login.

    Compatibility with existing generated login groups is deliberately narrow:
    two credential fills, one submit click, an observed login navigation, and a
    post-login URL assertion. Mixed groups and other accounts require explicit
    script authentication; no business actions are silently omitted.
    """
    if mode == 'script':
        return steps
    if mode == 'environment':
        if not login_profile and not bound_authentication:
            raise ValueError('环境认证步骤缺少可用的环境自动登录配置或绑定登录态。')
        if not steps or any(not str(s.get('ope_key') or '').startswith('assert_') for s in steps):
            raise ValueError('环境认证步骤只能包含登录成功断言，不能重复填写或提交登录表单。')
        return steps
    if not login_profile:
        return steps

    login_url = str(login_profile.get('url') or '')
    login_gotos = [s for s in steps if s.get('ope_key') == 'goto'
                   and same_route(str((s.get('ope_value') or {}).get('url') or ''), login_url)]
    passwords = [s for s in steps if is_password_fill(s)]
    # Password fields also occur in ordinary account-management workflows.
    # Only an observed navigation to the configured login route is evidence
    # that this auto-mode group is an authentication sequence.
    if not login_gotos:
        return steps
    fills = [s for s in steps if s.get('ope_key') == 'fill']
    clicks = [s for s in steps if s.get('ope_key') == 'click']
    accounts = [s for s in fills if not is_password_fill(s)]
    assertions = [s for s in steps if str(s.get('ope_key') or '').startswith('assert_')]
    post_login_assertions = [s for s in assertions if s.get('ope_key') == 'assert_url'
                            and (s.get('ope_value') or {}).get('expected', (s.get('ope_value') or {}).get('value'))
                            and not same_route(urljoin(login_url, str((s.get('ope_value') or {}).get('expected') or
                                                   (s.get('ope_value') or {}).get('value') or '')), login_url)]
    allowed = {'goto', 'fill', 'click', 'wait', 'wait_for_ready'}
    pure_login = (
        first_group and len(login_gotos) == 1 and len(passwords) == 1
        and len(fills) == 2 and len(accounts) == 1 and len(clicks) == 1
        and len([s for s in steps if s.get('ope_key') == 'goto']) == 1
        and bool(post_login_assertions)
        and steps[0] is login_gotos[0]
        and steps.index(clicks[0]) > max(steps.index(s) for s in fills)
        and all(steps.index(s) > steps.index(clicks[0]) for s in assertions)
        and all(s.get('ope_key') in allowed or str(s.get('ope_key') or '').startswith('assert_') for s in steps)
        and str((accounts[0].get('ope_value') or {}).get('value') or '') == str(login_profile.get('username') or '')
    )
    if not pure_login:
        raise ValueError('脚本登录流程与环境自动登录重复或账号不一致；请明确选择脚本认证，或将独立登录组改为环境认证后的成功断言。')
    # Preserve the observed expectations and their database references. The
    # environment login's own result records the credential submission.
    return [s for s in steps if str(s.get('ope_key') or '').startswith('assert_')
            or s.get('ope_key') in {'wait', 'wait_for_ready'}]
