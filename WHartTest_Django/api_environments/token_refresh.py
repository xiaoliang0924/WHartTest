"""Refresh API auth tokens once per credential group before test execution."""

from __future__ import annotations

import base64
import json
import logging
import time
from copy import deepcopy
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from .models import ApiEnvironment, ApiEnvironmentVariable
from .services import infer_environment_variable_type, serialize_environment_variable_value

logger = logging.getLogger(__name__)

LOGIN_PATH = '/api/auth/login'
TOKEN_REFRESH_BUFFER_SECONDS = 120

# Each group shares one login call; all listed token vars receive the same access token.
TOKEN_CREDENTIAL_GROUPS: Tuple[Dict[str, Any], ...] = (
    {
        'token_vars': ('accessToken', 'adminToken'),
        'username_vars': ('adminUsername',),
        'password_vars': ('adminPassword',),
    },
    {
        'token_vars': ('assigneeToken',),
        'username_vars': ('assigneeUsername', 'wecomUserId'),
        'password_vars': ('assigneePassword', 'noCommonPassword'),
        'default_password': '000000',
    },
    {
        'token_vars': ('noCommonToken', 'noPermissionToken'),
        'username_vars': ('noCommonUsername',),
        'password_vars': ('noCommonPassword',),
    },
)


def _decode_jwt_exp(token: str) -> Optional[int]:
    try:
        parts = token.split('.')
        if len(parts) < 2:
            return None
        payload = parts[1]
        payload += '=' * ((4 - len(payload) % 4) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get('exp')
        return int(exp) if exp is not None else None
    except Exception:
        return None


def _token_still_valid(token: Optional[str], *, now: Optional[int] = None) -> bool:
    if not token or not isinstance(token, str):
        return False
    exp = _decode_jwt_exp(token)
    if exp is None:
        return False
    current = now if now is not None else int(time.time())
    return exp > current + TOKEN_REFRESH_BUFFER_SECONDS


def _group_token_is_valid(variables: Dict[str, Any], group: Dict[str, Any]) -> bool:
    for token_var in group.get('token_vars', ()):
        token = variables.get(token_var)
        if _token_still_valid(str(token) if token is not None else None):
            return True
    return False

def _first_present(variables: Dict[str, Any], keys: Iterable[str]) -> Optional[str]:
    for key in keys:
        value = variables.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _resolve_credentials(
    variables: Dict[str, Any],
    group: Dict[str, Any],
) -> Optional[Tuple[str, str]]:
    username = _first_present(variables, group.get('username_vars', ()))
    if not username:
        return None

    password = _first_present(variables, group.get('password_vars', ()))
    if not password:
        password = group.get('default_password')
    if not password:
        return None

    return username, str(password)


def _login_for_token(
    base_url: str,
    username: str,
    password: str,
    *,
    verify_ssl: bool = False,
    timeout: int = 30,
) -> str:
    url = base_url.rstrip('/') + LOGIN_PATH
    response = requests.post(
        url,
        json={'username': username, 'password': password},
        verify=verify_ssl,
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    token = payload.get('accessToken') or payload.get('token')
    if not token:
        raise ValueError('Login response missing accessToken')
    return str(token)


def _persist_token_variables(
    environment_id: int,
    updates: Dict[str, str],
) -> None:
    if not updates:
        return

    environment = ApiEnvironment.objects.filter(id=environment_id).first()
    if environment is None:
        return

    for name, value in updates.items():
        serialized_value = serialize_environment_variable_value(value)
        variable, created = ApiEnvironmentVariable.objects.get_or_create(
            environment=environment,
            name=name,
            defaults={
                'value': serialized_value,
                'type': infer_environment_variable_type(value),
                'is_sensitive': True,
            },
        )
        if created:
            continue
        if variable.value != serialized_value:
            variable.value = serialized_value
            variable.is_sensitive = True
            variable.save(update_fields=['value', 'is_sensitive', 'updated_at'])


def refresh_environment_tokens(
    *,
    base_url: str,
    variables: Optional[Dict[str, Any]] = None,
    verify_ssl: bool = False,
    environment_id: Optional[int] = None,
    persist: bool = True,
    force_token_vars: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Login once per credential group and refresh token environment variables."""
    merged = dict(variables or {})
    updates: Dict[str, str] = {}
    login_cache: Dict[Tuple[str, str], str] = {}
    forced = set(force_token_vars or ())

    for group in TOKEN_CREDENTIAL_GROUPS:
        group_token_vars = group.get('token_vars', ())
        should_force = any(token_var in forced for token_var in group_token_vars)
        if not should_force and _group_token_is_valid(merged, group):
            logger.info(
                'Skipping token refresh; existing token still valid (group=%s)',
                ','.join(group_token_vars),
            )
            continue

        credentials = _resolve_credentials(merged, group)
        if credentials is None:
            continue

        username, password = credentials
        cache_key = (username, password)
        if cache_key not in login_cache:
            try:
                login_cache[cache_key] = _login_for_token(
                    base_url,
                    username,
                    password,
                    verify_ssl=verify_ssl,
                )
                logger.info(
                    'Refreshed auth token for username=%s (group=%s)',
                    username,
                    ','.join(group.get('token_vars', ())),
                )
            except Exception as exc:
                logger.warning(
                    'Failed to refresh auth token for username=%s: %s',
                    username,
                    exc,
                )
                continue

        token = login_cache[cache_key]
        for token_var in group.get('token_vars', ()):
            if token_var in merged or token_var.endswith('Token'):
                merged[token_var] = token
                updates[token_var] = token

    if persist and environment_id and updates:
        _persist_token_variables(environment_id, updates)

    return merged


def refresh_environment_tokens_for_execution(
    environment: Optional[Dict[str, Any]],
    *,
    persist: bool = True,
) -> Optional[Dict[str, Any]]:
    """Refresh tokens for a runner/task execution environment payload."""
    if not isinstance(environment, dict):
        return environment

    base_url = environment.get('base_url')
    if not base_url:
        return environment

    variables = environment.get('variables')
    if not isinstance(variables, dict):
        variables = {}

    updated_variables = refresh_environment_tokens(
        base_url=base_url,
        variables=variables,
        verify_ssl=bool(environment.get('verify_ssl', False)),
        environment_id=environment.get('id'),
        persist=persist,
    )

    refreshed = deepcopy(environment)
    refreshed['variables'] = updated_variables
    return refreshed
