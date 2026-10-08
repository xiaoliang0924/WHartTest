"""
Skill 工具

提供渐进式加载的 Skill 系统：
- read_skill_content: 读取 Skill 的 SKILL.md 内容（按需加载）
- execute_skill_script: 执行 Skill 的 shell 命令（支持持久化浏览器会话）
"""

import asyncio
import logging
import subprocess
import os
import sys
import shutil
import threading
import json
import time
import mimetypes
import re
import signal
import shlex
from typing import Optional, Tuple, Dict, Union

from asgiref.sync import sync_to_async
from langchain_core.tools import tool as langchain_tool
from django.conf import settings

from .output_sanitizer import strip_terminal_control_sequences
from .persistent_playwright import PlaywrightSessionManager, extract_runjs_args

logger = logging.getLogger("orchestrator_integration")

_playwright_session_manager: Optional[PlaywrightSessionManager] = None
_playwright_session_manager_lock = threading.Lock()


def _set_complete_ticket_step_metadata(env: dict, testcase) -> None:
    """Pass only step semantics to the completion helper; never pass login steps."""
    steps = {
        str(step.step_number): {
            "description": step.description or "",
            "expected": step.expected_result or "",
        }
        for step in testcase.steps.order_by("step_number")
        if step.step_number > 1
    }
    env["WHARTTEST_COMPLETE_STEPS_JSON"] = json.dumps(steps, ensure_ascii=False)


def _ensure_testcase_playwright_session_id(
    session_id: Optional[str],
    *,
    skill_name: str,
    run_js_args: Optional[list],
    test_case_id: Optional[int],
) -> Optional[str]:
    """Keep testcase Playwright commands in the persistent browser session."""
    if (
        session_id
        or skill_name != "playwright-skill"
        or run_js_args is None
        or not test_case_id
    ):
        return session_id
    return f"case_{test_case_id}"


_ARTIFACT_EXTENSIONS = {
    ".drawio",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".pdf",
    ".html",
    ".htm",
    ".txt",
    ".json",
    ".csv",
    ".xml",
    ".zip",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
}
_MAX_ARTIFACT_SIZE_BYTES = 50 * 1024 * 1024
_DEFAULT_SKILL_COMMAND_TIMEOUT_SECONDS = 120
_DEFAULT_SKILL_CONTENT_TIMEOUT_SECONDS = 10.0
_DEFAULT_SKILL_OUTPUT_MAX_CHARS = 200_000
_DEFAULT_WHARTTEST_BACKEND_URL = "http://localhost:8000"
_DEFAULT_WHARTTEST_API_KEY = "wharttest-default-mcp-key-2025"
_ARTIFACT_TOKEN_RE = re.compile(
    r"(?P<path>(?:[A-Za-z]:)?[^\s<>\"'`|]+?\.(?:drawio|png|jpe?g|gif|svg|pdf|html?|txt|json|csv|xml|zip|docx?|xlsx?|pptx?))",
    re.IGNORECASE,
)


def _get_skill_runtime_env() -> dict[str, str]:
    """Build the environment passed to skills that call the internal API."""
    env = os.environ.copy()
    backend_url = getattr(settings, "WHARTTEST_BACKEND_URL", None) or env.get(
        "WHARTTEST_BACKEND_URL", _DEFAULT_WHARTTEST_BACKEND_URL
    )
    api_key = getattr(settings, "WHARTTEST_API_KEY", None) or env.get(
        "WHARTTEST_API_KEY", _DEFAULT_WHARTTEST_API_KEY
    )
    env["WHARTTEST_BACKEND_URL"] = backend_url
    env["WHARTTEST_API_KEY"] = api_key
    return env


def _get_skill_command_timeout_seconds() -> int:
    return int(
        getattr(
            settings,
            "SKILL_COMMAND_TIMEOUT_SECONDS",
            _DEFAULT_SKILL_COMMAND_TIMEOUT_SECONDS,
        )
    )


def _get_skill_output_max_chars() -> int:
    return int(
        getattr(
            settings,
            "SKILL_COMMAND_OUTPUT_MAX_CHARS",
            _DEFAULT_SKILL_OUTPUT_MAX_CHARS,
        )
    )


def _get_skill_content_timeout_seconds() -> float:
    """Bound Skill markdown reads so one tool call cannot stall an Agent stream."""
    configured = getattr(
        settings,
        "SKILL_CONTENT_TIMEOUT_SECONDS",
        os.environ.get(
            "SKILL_CONTENT_TIMEOUT_SECONDS",
            _DEFAULT_SKILL_CONTENT_TIMEOUT_SECONDS,
        ),
    )
    try:
        timeout = float(configured)
    except (TypeError, ValueError):
        timeout = _DEFAULT_SKILL_CONTENT_TIMEOUT_SECONDS
    return max(1.0, min(timeout, 60.0))


_JS_START_RE = re.compile(
    r"^(?:const|let|var|await|async|function|import|export|class|if|for|while|try|#!|helpers\.|page\.|chromium\.|browser\.)\b",
    re.IGNORECASE,
)
_RUNJS_PREFIX_RE = re.compile(
    r"^(?:npx\s+)?(?:node\s+)?(?:\./)?run\.js(?:\s+|$)",
    re.IGNORECASE,
)


_JS_START_RE = re.compile(
    r"^(?:const|let|var|await|async|function|import|export|class|if|for|while|try|#!|helpers\.|page\.|chromium\.|browser\.)\b",
    re.IGNORECASE,
)
_RUNJS_PREFIX_RE = re.compile(
    r"^(?:npx\s+)?(?:node\s+)?(?:\./)?run\.js(?:\s+|$)",
    re.IGNORECASE,
)
_PYTHON_PLAYWRIGHT_LINE_RE = re.compile(
    r"^(?:page\.)?(goto|fill|click|press|type|screenshot|wait_for_load_state|waitForLoadState)\(",
    re.IGNORECASE,
)


def _join_playwright_lines(command: str) -> str:
    lines = [line.strip() for line in (command or "").splitlines() if line.strip()]
    if not lines:
        return ""
    if len(lines) == 1:
        return lines[0]
    return ";\n".join(lines)


def _looks_like_playwright_snippet(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return False
    for line in stripped.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        if _PYTHON_PLAYWRIGHT_LINE_RE.match(candidate):
            return True
    return False


def normalize_playwright_snippet(text: str) -> str:
    """把 Python 风格或缺 await 的 Playwright 片段规范为可执行 JS。"""
    if not text or not text.strip():
        return text

    method_map = {
        "goto": "goto",
        "fill": "fill",
        "click": "click",
        "press": "press",
        "type": "type",
        "screenshot": "screenshot",
        "wait_for_load_state": "waitForLoadState",
        "waitforloadstate": "waitForLoadState",
    }
    lines_out: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.endswith(";"):
            line = line[:-1].strip()

        matched = False
        for py_name, js_name in method_map.items():
            bare = re.match(rf"^{re.escape(py_name)}\((.*)$", line, re.IGNORECASE)
            if bare:
                line = f"await page.{js_name}({bare.group(1)}"
                matched = True
                break
            dotted = re.match(
                rf"^page\.{re.escape(py_name)}\((.*)$", line, re.IGNORECASE
            )
            if dotted:
                line = f"await page.{js_name}({dotted.group(1)}"
                matched = True
                break

        if not matched:
            if line.startswith("page.") and not line.startswith("await "):
                line = "await " + line
            elif line.startswith("helpers.") and not line.startswith("await "):
                line = "await " + line

        lines_out.append(line)

    if not lines_out:
        return text.strip()
    return ";\n".join(lines_out)


def _collapse_command_whitespace(command: str) -> str:
    return _join_playwright_lines(command)


def _looks_like_javascript(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return False
    if _JS_START_RE.match(stripped):
        return True
    markers = (
        "await page.",
        "process.env.SCREENSHOT_DIR",
        "chromium.launch",
        "helpers.describePageForAI",
        "helpers.loginWorkOrderPortal",
        "helpers.loginStep1",
        "helpers.loginWorkOrderStep1",
        "helpers.screenshotCaseStep",
        "helpers.selectDialogMultiSelect",
        "helpers.navigateByMenu",
        "helpers.filterByFields",
        "page.goto(",
        "page.screenshot",
        "page.fill(",
        "page.click(",
    )
    return any(marker in stripped for marker in markers) or _looks_like_playwright_snippet(stripped)


def _extract_quoted_runjs_payload(rest: str) -> tuple[str | None, str | None]:
    rest = (rest or "").strip()
    if not rest:
        return None, rest
    if (rest[0] == rest[-1]) and rest[0] in {'"', "'"}:
        quote = rest[0]
        return rest[1:-1], ""
    return None, rest


def sanitize_inline_playwright_code(code: str) -> str:
    """修复 LLM 脚本中单引号路径在 shell 传递时被截断导致的 SyntaxError。"""
    if not code:
        return code
    result = code.strip()
    result = re.sub(r"(\+\s*)'([^'\\]*(?:\\.[^'\\]*)*)'", r'\1"\2"', result)
    result = re.sub(r"\bpath:\s*'([^']*)'", r'path: "\1"', result)
    result = re.sub(r"name:\s*'([^']*)'", r'name: "\1"', result)
    result = re.sub(
        r"getByPlaceholder\(\s*'([^']*)'\s*\)",
        r'getByPlaceholder("\1")',
        result,
    )
    result = re.sub(
        r"getByRole\(\s*'([^']*)'\s*,",
        r'getByRole("\1",',
        result,
    )
    result = re.sub(
        r"filterWorkOrdersByStatus\(\s*page\s*,\s*'([^']*)'\s*\)",
        r'filterWorkOrdersByStatus(page, "\1")',
        result,
    )
    result = re.sub(
        r"selectFormDropdownOption\(\s*page\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*\)",
        r'selectFormDropdownOption(page, "\1", "\2")',
        result,
    )
    result = re.sub(
        r"selectDialogMultiSelect\(\s*page\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*\)",
        r'selectDialogMultiSelect(page, "\1", "\2")',
        result,
    )
    result = re.sub(
        r"navigateByMenu\(\s*page\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*\)",
        r'navigateByMenu(page, "\1", "\2")',
        result,
    )
    return result


def extract_playwright_inline_code(
    raw_command: str,
    normalized_command: Optional[str] = None,
) -> Optional[str]:
    """从原始/规范化命令中提取 inline JS（优先 raw，避免 quote 被破坏）。"""
    for candidate in (raw_command, normalized_command):
        if not candidate:
            continue
        text = candidate.strip()
        args = extract_runjs_args(text)
        if args and args[0].strip():
            return sanitize_inline_playwright_code(args[0])
        if _looks_like_javascript(text):
            return sanitize_inline_playwright_code(normalize_playwright_snippet(text))
    return None


def normalize_playwright_skill_command(command: str) -> str:
    """
    LLM 常把 Playwright JS 直接当作 shell 命令传入，导致：
    - 退出码 127：/bin/sh 把 `const` 当成命令
    - 退出码 2：未加引号的 `page.goto(...)` 被 shell 解析成语法错误

    这里统一纠正为 `node run.js '<js>'`。
    """
    collapsed = _collapse_command_whitespace(command)
    if not collapsed:
        return collapsed

    prefix_match = _RUNJS_PREFIX_RE.match(collapsed)
    if prefix_match:
        rest = collapsed[prefix_match.end() :].strip()
        if not rest:
            return "node run.js"
        inner, trailing = _extract_quoted_runjs_payload(rest)
        if inner is not None:
            normalized = normalize_playwright_snippet(inner)
            rebuilt = "node run.js " + shlex.quote(normalized)
            return f"{rebuilt} {trailing}".strip() if trailing else rebuilt
        if rest.startswith("--"):
            return collapsed
        logger.warning(
            "[execute_skill_script] playwright run.js 参数未加引号，已自动转义"
        )
        normalized = normalize_playwright_snippet(rest)
        return "node run.js " + shlex.quote(normalized)

    if _looks_like_javascript(collapsed) or _looks_like_playwright_snippet(collapsed):
        logger.warning(
            "[execute_skill_script] playwright command 是裸 JS，已自动包裹 node run.js"
        )
        normalized = normalize_playwright_snippet(collapsed)
        return "node run.js " + shlex.quote(normalized)

    return collapsed


def _truncate_skill_output(output: str) -> str:
    if not output:
        return output

    max_chars = max(10_000, _get_skill_output_max_chars())
    if len(output) <= max_chars:
        return output

    head_len = int(max_chars * 0.7)
    tail_len = max_chars - head_len
    omitted = len(output) - head_len - tail_len
    return (
        output[:head_len]
        + f"\n\n[output truncated: omitted {omitted} characters; "
        + f"limit={max_chars}]\n\n"
        + output[-tail_len:]
    )


def _decode_command_output(data: bytes) -> str:
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            import locale

            encoding = locale.getpreferredencoding(False) or "utf-8"
            return data.decode(encoding, errors="replace")
        except Exception:
            return data.decode("gbk", errors="replace")


def _terminate_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return

    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            return
        except Exception:
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=3)
            return
        except Exception:
            pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except Exception:
            pass

    try:
        proc.kill()
    except Exception:
        pass


def _run_skill_command(
    exec_command: str,
    *,
    cwd: str,
    env: dict[str, str],
    timeout_seconds: int,
) -> tuple[int, str, str]:
    popen_kwargs: dict[str, object] = {
        "shell": True,
        "cwd": cwd,
        "env": env,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(exec_command, **popen_kwargs)
    try:
        stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _terminate_process_tree(proc)
        try:
            proc.communicate(timeout=3)
        except Exception:
            pass
        raise

    stdout = strip_terminal_control_sequences(_decode_command_output(stdout_bytes))
    stderr = strip_terminal_control_sequences(_decode_command_output(stderr_bytes))
    return proc.returncode or 0, stdout, stderr


_QUOTED_ARTIFACT_TOKEN_RE = re.compile(
    r"[`'\"](?P<path>[^`'\"]+?\.(?:drawio|png|jpe?g|gif|svg|pdf|html?|txt|json|csv|xml|zip|docx?|xlsx?|pptx?))[`'\"]",
    re.IGNORECASE,
)


def _sanitize_runtime_path_segment(value: Optional[str], default: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return default

    sanitized = raw.replace("..", "_")
    for separator in filter(None, {os.sep, os.altsep}):
        sanitized = sanitized.replace(separator, "_")

    sanitized = sanitized.strip(" .")
    return sanitized or default


def _build_skill_screenshots_dir(
    project_id: Optional[int] = None,
    case_dir_key: Optional[str] = None,
) -> str:
    project_segment = str(project_id if project_id is not None else 0)
    case_segment = _sanitize_runtime_path_segment(case_dir_key, "_default")
    return os.path.abspath(
        os.path.join(
            settings.MEDIA_ROOT,
            "skill_runtime",
            "screenshots",
            project_segment,
            case_segment,
        )
    )


# 截图/产物目录空闲超过该时长（秒）才允许清理，避免 chat_session_id 变化时误删正在使用的截图
_SKILL_DIR_STALE_SECONDS = 5 * 60 * 60


def _dir_latest_mtime(path: str) -> float:
    """目录树内最新文件 mtime；无文件返回 0（目录结构变更不算活跃写入）。"""
    latest = 0.0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                mtime = os.path.getmtime(os.path.join(root, name))
            except OSError:
                continue
            if mtime > latest:
                latest = mtime
    return latest


def _prepare_skill_screenshots_dir(
    project_id: Optional[int] = None,
    case_dir_key: Optional[str] = None,
) -> str:
    screenshots_dir = _build_skill_screenshots_dir(project_id, case_dir_key)
    if not case_dir_key:
        os.makedirs(screenshots_dir, exist_ok=True)
        return screenshots_dir

    should_clear = False
    if os.path.exists(screenshots_dir):
        latest = _dir_latest_mtime(screenshots_dir)
        if latest and time.time() - latest > _SKILL_DIR_STALE_SECONDS:
            should_clear = True

    if should_clear:
        shutil.rmtree(screenshots_dir, ignore_errors=True)
        logger.info(f"[execute_skill_script] 清空旧截图目录: {screenshots_dir}")

    os.makedirs(screenshots_dir, exist_ok=True)
    return screenshots_dir


def _build_skill_artifacts_dir(
    project_id: Optional[int] = None,
    case_dir_key: Optional[str] = None,
) -> str:
    project_segment = str(project_id if project_id is not None else 0)
    case_segment = _sanitize_runtime_path_segment(case_dir_key, "_default")
    return os.path.abspath(
        os.path.join(
            settings.MEDIA_ROOT,
            "skill_runtime",
            "artifacts",
            project_segment,
            case_segment,
        )
    )


def _prepare_skill_artifacts_dir(
    project_id: Optional[int] = None,
    case_dir_key: Optional[str] = None,
) -> str:
    artifacts_dir = _build_skill_artifacts_dir(project_id, case_dir_key)
    if not case_dir_key:
        os.makedirs(artifacts_dir, exist_ok=True)
        return artifacts_dir

    should_clear = False
    if os.path.exists(artifacts_dir):
        latest = _dir_latest_mtime(artifacts_dir)
        if latest and time.time() - latest > _SKILL_DIR_STALE_SECONDS:
            should_clear = True

    if should_clear:
        shutil.rmtree(artifacts_dir, ignore_errors=True)
        logger.info(f"[execute_skill_script] 清空旧产物目录: {artifacts_dir}")

    os.makedirs(artifacts_dir, exist_ok=True)
    return artifacts_dir


def _is_allowed_artifact_file(file_path: str) -> bool:
    if not file_path or not os.path.isfile(file_path) or os.path.islink(file_path):
        return False
    suffix = os.path.splitext(file_path)[1].lower()
    if suffix not in _ARTIFACT_EXTENSIONS:
        return False
    if os.path.basename(file_path).startswith("."):
        return False
    try:
        return os.path.getsize(file_path) <= _MAX_ARTIFACT_SIZE_BYTES
    except OSError:
        return False


def _snapshot_artifact_files(root_dir: str) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    if not root_dir or not os.path.isdir(root_dir):
        return snapshot

    for current_root, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "node_modules")]
        for name in files:
            full_path = os.path.join(current_root, name)
            if not _is_allowed_artifact_file(full_path):
                continue
            rel_path = os.path.relpath(full_path, root_dir).replace(os.sep, "/")
            snapshot[rel_path] = full_path
    return snapshot


def _path_to_media_url(file_path: str) -> Optional[str]:
    try:
        media_root = os.path.abspath(settings.MEDIA_ROOT)
        candidate = os.path.abspath(file_path)
        if os.path.commonpath([media_root, candidate]) != media_root:
            return None
        relative_path = os.path.relpath(candidate, media_root).replace(os.sep, "/")
        return f"{settings.MEDIA_URL.rstrip('/')}/{relative_path}"
    except Exception:
        return None


def _build_artifact_payload(file_path: str) -> Optional[dict[str, object]]:
    media_url = _path_to_media_url(file_path)
    if not media_url:
        return None

    mime_type, _ = mimetypes.guess_type(file_path)
    if file_path.lower().endswith(".drawio"):
        mime_type = mime_type or "application/vnd.jgraph.mxfile"

    payload: dict[str, object] = {
        "type": "file",
        "name": os.path.basename(file_path),
        "url": media_url,
        "path": os.path.relpath(file_path, settings.MEDIA_ROOT).replace(os.sep, "/"),
        "mime_type": mime_type or "application/octet-stream",
    }
    try:
        payload["size"] = os.path.getsize(file_path)
    except OSError:
        pass
    return payload


def _extract_artifact_candidates(text: str) -> list[str]:
    if not text:
        return []

    seen: set[str] = set()
    candidates: list[str] = []
    strip_chars = " \t\r\n()[]{}<>,;:：。，“”‘’"

    for pattern in (_QUOTED_ARTIFACT_TOKEN_RE, _ARTIFACT_TOKEN_RE):
        for match in pattern.finditer(text):
            candidate = (match.group("path") or "").strip(strip_chars)
            if "：" in candidate:
                candidate = candidate.split("：")[-1].strip(strip_chars)
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            candidates.append(candidate)

    return candidates


def _find_named_file(root_dir: str, candidate: str) -> Optional[str]:
    if not root_dir or not os.path.isdir(root_dir):
        return None

    normalized_candidate = candidate.replace("\\", os.sep)
    joined_path = os.path.join(root_dir, normalized_candidate.lstrip("./"))
    if _is_allowed_artifact_file(joined_path):
        return joined_path

    basename = os.path.basename(normalized_candidate)
    if not basename:
        return None

    for current_root, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "node_modules")]
        if basename in files:
            full_path = os.path.join(current_root, basename)
            if _is_allowed_artifact_file(full_path):
                return full_path
    return None


def _collect_skill_artifacts(
    output_text: str,
    *,
    skill_dir: str,
    artifacts_dir: str,
    artifacts_before: dict[str, str],
) -> list[dict[str, object]]:
    collected: list[dict[str, object]] = []
    seen_urls: set[str] = set()

    def add_file(file_path: Optional[str]) -> None:
        if not file_path or not _is_allowed_artifact_file(file_path):
            return
        payload = _build_artifact_payload(file_path)
        if not payload:
            return
        media_url = str(payload.get("url") or "")
        if not media_url or media_url in seen_urls:
            return
        seen_urls.add(media_url)
        collected.append(payload)

    artifacts_after = _snapshot_artifact_files(artifacts_dir)
    for rel_path, full_path in artifacts_after.items():
        if rel_path not in artifacts_before:
            add_file(full_path)

    for candidate in _extract_artifact_candidates(output_text):
        if candidate.startswith("/media/"):
            continue
        if os.path.isabs(candidate):
            add_file(candidate)
            continue
        add_file(_find_named_file(artifacts_dir, candidate))
        add_file(_find_named_file(skill_dir, candidate))

    return collected


_FAILURE_REMINDER = (
    "\n\n【必须输出完整测试报告】这是用例步骤未通过。"
    "请立即在对话中输出完整报告，不要等待用户追问，格式如下：\n"
    "## 测试执行结果: 不通过\n"
    "### 基本信息\n- 测试用例ID:\n- 名称:\n- 优先级:\n"
    "### 执行过程与结果\n| 步骤 | 操作 | 结果 |\n|------|------|------|\n"
    "| N | … | 通过/失败：原因/未执行 |\n"
    "### 问题分析\n- 失败步骤:\n- 失败原因:\n- 建议:\n"
    "### 结论\n未执行步骤标「未执行」。执行记录由系统自动保存，"
    "禁止调用 whart_tools 或其他工具更新用例执行结果。"
)

_RETRY_REMINDER = (
    "\n\n【脚本错误，请修正后重试】"
    "这是执行脚本问题，不是被测功能失败。请继续当前步骤："
    "持久化会话已有 page / chromium / helpers，禁止再次 "
    "`const { chromium } = require('playwright')`，禁止 chromium.launch()。"
    "登录步骤只调用 helpers.loginStep1(page)，它已包含步骤1截图；"
    "RESULT=FAIL 时禁止继续。其他步骤截图用 helpers.screenshotCaseStep(page, N)。"
    "点菜单用 helpers.navigateByMenu(page, 菜单名) 或 "
    "helpers.navigateByMenuPath(page, ['父菜单', '子菜单'])；"
    "只有明确知道唯一目标路由时才传 URL 片段，禁止传父级公共前缀。"
    "下拉用 helpers.selectFormDropdownOption(page, 字段标签, 选项)；"
    "不要再手写 `.el-form-item` / `innerText()` 重复校验下拉；"
    "该 helper 会返回是否选中并校验筛选框当前值。若 helper 后续步骤失败，"
    "只修复后续失败的定位，不要把已有异常当作步骤通过。"
    "弹窗多选用 helpers.selectDialogMultiSelect(page, 字段标签, 选项名)。"
    "禁止 getByText 点表格里的状态文字。"
    "断言区块/字段用 helpers.assertPageShows(page, ['基本信息', '工单类型'])，"
    "禁止 getByText('字段*')（必填星号是独立节点）。"
    "若已重试仍无法继续，必须立刻输出完整「## 测试执行结果: 不通过」报告"
    "（基本信息、步骤表、问题分析、结论），禁止沉默结束。"
)

_ASSERTION_FAIL_MARKERS = ("RESULT=FAIL",)

_SCRIPT_ERROR_MARKERS = (
    "SyntaxError",
    "already been declared",
    "ERR_BLOCKED_BY_CLIENT",
    "strict mode violation",
    "命令执行失败",
    "Process exited with code",
    "Automation error",
    "process.exit(",
    "文件不存在",
    "locator.waitFor: Timeout",
    "Timeout 15000ms exceeded",
    "Timeout 30000ms exceeded",
    "Playwright exec timed out",
    "浏览器已重置",
)


def _contains_any(output: str, markers: Tuple[str, ...]) -> bool:
    if not output:
        return False
    return any(marker in output for marker in markers)


def _with_failure_reminder(output: str, *, skill_name: str) -> str:
    if not output:
        return output
    if "【必须输出完整测试报告】" in output or "【脚本错误，请修正后重试】" in output:
        return output
    if re.search(r"RESULT=PASS:\s*步骤\d+", output or ""):
        if _contains_any(output, _ASSERTION_FAIL_MARKERS) or "locator.waitFor: Timeout" in output:
            return (
                f"{output.rstrip()}\n\n"
                "【步骤已通过】stdout 含 RESULT=PASS 与 CASE_SCREENSHOT，"
                "该步应判通过，忽略早前手写 waitFor/locator 超时。"
            )
    if _contains_any(output, _ASSERTION_FAIL_MARKERS):
        return f"{output.rstrip()}{_FAILURE_REMINDER}"
    if skill_name in ("playwright-skill", "whart-test") and _contains_any(
        output, _SCRIPT_ERROR_MARKERS
    ):
        return f"{output.rstrip()}{_RETRY_REMINDER}"
    return output


_CASE_SCREENSHOT_RE = re.compile(
    r"\[(?:CASE_SCREENSHOT|SCREENSHOT_SAVED)\]\s+(\S+)",
    re.IGNORECASE,
)


def _auto_upload_case_screenshots(
    output: str,
    *,
    project_id: Optional[int],
    case_id: Optional[int],
    env: dict,
) -> str:
    """Detect case step screenshots and upload to testcase management."""
    if not project_id or not case_id:
        return ""

    screenshot_dir = (env.get("SCREENSHOT_DIR") or "").strip()
    step_to_path: dict[str, str] = {}
    for match in _CASE_SCREENSHOT_RE.findall(output or ""):
        if not re.search(rf"case_{case_id}_step\d+\.png$", match, re.IGNORECASE):
            continue
        resolved = match
        if not os.path.isabs(resolved) and screenshot_dir:
            candidate = os.path.join(screenshot_dir, os.path.basename(match))
            if os.path.exists(candidate):
                resolved = candidate
        step_match = re.search(r"step(\d+)", os.path.basename(resolved), re.IGNORECASE)
        if step_match:
            step_to_path[step_match.group(1)] = resolved

    # 只接受当前命令 stdout 明确声明的截图。截图目录会在同一用例的多次
    # 命令间复用，扫描整个目录会把上一轮或其他步骤遗留的同名文件错误绑定到
    # 当前执行记录，导致“步骤”和截图内容不一致。缺少声明时宁可不上传，
    # 由调用方补充 CASE_SCREENSHOT，也不能上传无法归属的旧图。

    max_step = 0
    try:
        max_step = int(env.get("WHARTTEST_CASE_STEP_COUNT") or 0)
    except (TypeError, ValueError):
        max_step = 0
    if max_step > 0:
        step_to_path = {
            step: path
            for step, path in step_to_path.items()
            if step.isdigit() and int(step) <= max_step
        }

    paths = list(step_to_path.values())

    if not paths:
        return ""

    notes: list[str] = []
    uploaded_steps: set[str] = set()
    for filepath in paths:
        basename = os.path.basename(filepath)
        step_match = re.search(r"step(\d+)", basename, re.IGNORECASE)
        if not step_match:
            continue
        step_num = step_match.group(1)
        if step_num in uploaded_steps:
            continue

        resolved = filepath
        if not os.path.exists(resolved) and screenshot_dir:
            alt = os.path.join(screenshot_dir, basename)
            if os.path.exists(alt):
                resolved = alt
        if not os.path.exists(resolved) and screenshot_dir:
            last_png = os.path.join(screenshot_dir, "last.png")
            named = os.path.join(screenshot_dir, basename)
            if os.path.isfile(last_png):
                try:
                    shutil.copyfile(last_png, named)
                    resolved = named
                except OSError:
                    resolved = last_png
        if not os.path.exists(resolved):
            notes.append(f"[AUTO_UPLOAD] 跳过：文件不存在 {basename}")
            continue

        try:
            from testcases.run_record_service import attach_testcase_step_screenshot

            saved = attach_testcase_step_screenshot(
                testcase_id=int(case_id),
                step_number=int(step_num),
                filepath=resolved,
                title=f"步骤{step_num}",
            )
        except Exception as exc:
            notes.append(f"[AUTO_UPLOAD] 步骤{step_num} 异常: {exc}")
            continue
        if saved:
            uploaded_steps.add(step_num)
            notes.append(f"[AUTO_UPLOAD] 步骤{step_num} 上传成功 ({basename})")
        else:
            notes.append(f"[AUTO_UPLOAD] 步骤{step_num} 失败: 无法保存 {basename}")

    return "\n".join(notes)


_HELPER_STEP_CALL_RE = re.compile(
    r"helpers\.(run(?:ClaimableTicket|TransferTicket|ProcessingHandover|CompleteTicket|TicketDetail|Overview|Notification)CaseStep|"
    r"screenshotCaseStep)\s*\(\s*page\s*,\s*(\d+)",
    re.IGNORECASE,
)


def infer_fixed_helper_call_template(messages) -> str:
    """Infer the fixed helper call used by earlier steps, for auto-resume."""
    for msg in reversed(list(messages or [])):
        chunks: list[str] = []
        content = getattr(msg, "content", None)
        if isinstance(content, str):
            chunks.append(content)
        elif content is not None:
            chunks.append(json.dumps(content, ensure_ascii=False))
        for tool_call in getattr(msg, "tool_calls", None) or []:
            if isinstance(tool_call, dict):
                args = tool_call.get("args") or tool_call.get("arguments") or {}
            else:
                args = getattr(tool_call, "args", None) or {}
            if isinstance(args, str):
                chunks.append(args)
            elif isinstance(args, dict):
                chunks.append(str(args.get("command") or args.get("code") or ""))
        text = "\n".join(chunks)
        match = _HELPER_STEP_CALL_RE.search(text)
        if match:
            return f"await helpers.{match.group(1)}(page, {{step}});"
    # Never guess a claimable-ticket helper for an unrelated AI-only case.
    return ""


def _playwright_exec_timeout_for_helper_code(helper_code: str) -> tuple[int, int]:
    """Node PW_EXEC_TIMEOUT_MS and Python join timeout for a helper snippet.

    Steps 3–7 of the claimable-ticket helper navigate, filter, retry, and
    screenshot; the default 45s Node timeout kills them mid-run (no CASE_SCREENSHOT).
    Step 1 login must stay short so a wedged Chromium cannot freeze the chat.
    """
    text = helper_code or ""
    if re.search(
        r"runClaimableTicketCaseStep\s*\(\s*page\s*,\s*[34567]\s*\)",
        text,
        flags=re.IGNORECASE,
    ):
        return 120000, 150
    if re.search(
        r"run(?:TransferTicket|ProcessingHandover|CompleteTicket)CaseStep\s*\(\s*page\s*,\s*[2-9]\s*\)",
        text,
        flags=re.IGNORECASE,
    ):
        return 120000, 150
    if re.search(
        r"runTransferTicketCaseStep\s*\(\s*page\s*,\s*[3-9]\s*\)",
        text,
        flags=re.IGNORECASE,
    ):
        return 120000, 150
    return 45000, 55


def _format_persistent_helper_step_output(
    cleaned_output: str,
    *,
    browser_session_id: str,
    screenshots_dir: str,
    step: int,
    total_steps: int,
    upload_notes: str = "",
) -> str:
    """Match execute_skill_script stdout so the chat renders the same yellow cards."""
    body = (cleaned_output or "").strip() or "(无输出)"
    if "[执行进度]" not in body:
        body = f"[执行进度] 正在执行步骤 {step}/{total_steps}\n{body}"
    result = (
        f"[PERSISTENT_SESSION] session_id={browser_session_id}\n"
        f"[SCREENSHOT_DIR] {screenshots_dir}\n"
        f"{body}\n"
        f'[提示] 后续步骤请继续使用 session_id="{browser_session_id}"；'
        f"截图已保存在 {screenshots_dir}"
    )
    if upload_notes:
        result = f"{result}\n{upload_notes}"
    return result


def auto_run_missing_testcase_helper_steps(
    *,
    test_case_id: int,
    user_id: int,
    project_id: int,
    chat_session_id: str,
    browser_session_id: str,
    from_step: int,
    to_step: int,
    helper_call_template: Optional[str] = None,
    total_steps: Optional[int] = None,
) -> Union[Dict[str, str], str]:
    """Directly run remaining fixed helper steps on the live Playwright session.

    Used when the model stops early after a non-final PASS and skips the last
    screenshot step. Bypasses the LLM so step N always gets a real helper run.
    Returns execute_skill_script-style JSON (text + screenshot artifacts) so the
    chat can show the same step cards as a model-driven run.
    """
    from skills.models import Skill

    if int(from_step) > int(to_step):
        return ""

    skill = Skill.objects.filter(name="playwright-skill", is_active=True).first()
    if not skill:
        return "[AUTO_RESUME] 错误: 未找到 playwright-skill"
    skill_dir = skill.get_full_path()
    if not skill_dir or not os.path.isdir(skill_dir):
        return f"[AUTO_RESUME] 错误: Skill 目录不存在: {skill_dir}"

    env = os.environ.copy()
    backend_url = _resolve_skill_runtime_backend_url()
    api_key, _key_source = _resolve_skill_runtime_api_key(int(user_id))
    if backend_url:
        env["WHARTTEST_BACKEND_URL"] = backend_url
    if api_key:
        env["WHARTTEST_API_KEY"] = api_key

    screenshots_dir = _build_skill_screenshots_dir(
        int(project_id), str(test_case_id)
    )
    os.makedirs(screenshots_dir, exist_ok=True)
    env["SCREENSHOT_DIR"] = screenshots_dir
    if project_id:
        env["WHARTTEST_PROJECT_ID"] = str(project_id)
    env["WHARTTEST_CASE_ID"] = str(test_case_id)
    resolved_total = int(total_steps or to_step or from_step or 0) or int(to_step)
    env["WHARTTEST_CASE_STEP_COUNT"] = str(resolved_total)

    try:
        from testcases.models import TestCase
        from data_generation.testcase_pre_data import (
            collect_testcase_text,
            extract_login_credentials,
            get_latest_pre_data_ticket_id,
            get_latest_pre_data_ticket_no,
            is_claimable_ticket_detail_case,
            is_complete_ticket_ui_case,
            is_transfer_ready_ticket_case,
            is_transfer_ui_helper_case,
            is_processing_handover_case,
            is_processing_handover_submission_case,
            processing_handover_flow_kind,
            transfer_ui_helper_flow,
            get_pre_data_snapshot_for_execution,
            resolve_claim_login_credentials,
        )
        from data_generation.intent_router import extract_transfer_target_name

        tc = (
            TestCase.objects.prefetch_related("steps")
            .filter(id=int(test_case_id))
            .first()
        )
        if tc:
            blob = collect_testcase_text(tc)
            creds = extract_login_credentials(blob)
            if creds.get("username"):
                env["WHARTTEST_USERNAME"] = creds["username"]
            if creds.get("password"):
                env["WHARTTEST_PASSWORD"] = creds["password"]
            if creds.get("login_url"):
                env["WHARTTEST_LOGIN_URL"] = creds["login_url"]
            if is_transfer_ui_helper_case(tc):
                env["WHARTTEST_TRANSFER_CASE"] = "1"
                env["WHARTTEST_TRANSFER_FLOW"] = transfer_ui_helper_flow(tc)
                target_name = extract_transfer_target_name(blob)
                if target_name:
                    env["WHARTTEST_TRANSFER_TARGET_NAME"] = target_name
                env.setdefault("WHARTTEST_TRANSFER_REASON", "自动化测试转派")
            if is_processing_handover_case(tc):
                env["WHARTTEST_PROCESSING_HANDOVER_CASE"] = "1"
                handover_flow = processing_handover_flow_kind(tc)
                if handover_flow:
                    env["WHARTTEST_HANDOVER_FLOW"] = handover_flow
                if is_processing_handover_submission_case(tc):
                    env["WHARTTEST_PROCESSING_HANDOVER_SUBMISSION"] = "1"
                    for case_step in tc.steps.order_by("step_number"):
                        match = re.search(
                            r"(?:选择审批人(?!弹窗)|选中(?:审批人)?)\s*([\u4e00-\u9fff]{2,4})",
                            f"{case_step.description or ''} {case_step.expected_result or ''}",
                        )
                        if match:
                            env["WHARTTEST_HANDOVER_APPROVER_NAME"] = match.group(1)
                            break
            if is_complete_ticket_ui_case(tc):
                env["WHARTTEST_COMPLETE_TICKET_CASE"] = "1"
                _set_complete_ticket_step_metadata(env, tc)
            if (
                is_claimable_ticket_detail_case(tc)
                or is_transfer_ready_ticket_case(tc)
                or is_processing_handover_case(tc)
                or is_complete_ticket_ui_case(tc)
            ):
                if is_claimable_ticket_detail_case(tc) or is_transfer_ready_ticket_case(tc):
                    env["WHARTTEST_CLAIMABLE_PENDING"] = "1"
                ticket_no = get_latest_pre_data_ticket_no(
                    tc, chat_session_id=chat_session_id
                )
                ticket_id = get_latest_pre_data_ticket_id(
                    tc, chat_session_id=chat_session_id
                )
                if ticket_no:
                    env["WHARTTEST_TICKET_NO"] = ticket_no
                if ticket_id:
                    env["WHARTTEST_TICKET_ID"] = ticket_id
                snapshot = get_pre_data_snapshot_for_execution(
                    tc, chat_session_id=chat_session_id
                )
                if snapshot.get("approverName"):
                    env["WHARTTEST_HANDOVER_APPROVER_NAME"] = str(snapshot["approverName"])
                claim_credentials, _err = resolve_claim_login_credentials(tc, snapshot)
                if claim_credentials:
                    env["WHARTTEST_USERNAME"] = claim_credentials["username"]
                    env["WHARTTEST_PASSWORD"] = claim_credentials["password"]
                    if claim_credentials.get("login_url"):
                        env["WHARTTEST_LOGIN_URL"] = claim_credentials["login_url"]
                    env["WHARTTEST_CLAIM_ASSIGNEE_NAME"] = claim_credentials[
                        "assignee_name"
                    ]
                # 移交审批 / 工单完成用例步骤里写的是 UI SSO 账号，优先保留用例原文登录信息。
                if (
                    is_processing_handover_case(tc) or is_complete_ticket_ui_case(tc)
                ) and creds.get("username"):
                    env["WHARTTEST_USERNAME"] = creds["username"]
                    if creds.get("password"):
                        env["WHARTTEST_PASSWORD"] = creds["password"]
                    if creds.get("login_url"):
                        env["WHARTTEST_LOGIN_URL"] = creds["login_url"]
                    if snapshot.get("assigneeName"):
                        env["WHARTTEST_CLAIM_ASSIGNEE_NAME"] = str(
                            snapshot.get("assigneeName")
                        )
    except Exception as exc:
        logger.warning("[AUTO_RESUME] preload case env failed: %s", exc)

    session_key = (
        f"{int(user_id)}_{int(project_id)}_{chat_session_id or 'default'}"
        f"_{browser_session_id}"
    )
    manager = _get_playwright_session_manager()
    notes: list[str] = []
    template = helper_call_template
    if not template:
        return "RESULT=FAIL: 本用例没有匹配的自动补跑脚本"
    artifacts_before = _snapshot_artifact_files(screenshots_dir)

    for step in range(int(from_step), int(to_step) + 1):
        code = template.format(step=step)
        logger.info("[AUTO_RESUME] 强制执行步骤 %s/%s: %s", step, resolved_total, code)
        pw_ms, py_sec = _playwright_exec_timeout_for_helper_code(code)
        env["PW_EXEC_TIMEOUT_MS"] = str(pw_ms)
        try:
            output = manager.execute_run_js(
                session_key=session_key,
                skill_dir=skill_dir,
                run_js_args=[code],
                env=env,
                timeout_seconds=py_sec,
            )
        except Exception as exc:
            notes.append(f"[AUTO_RESUME] 步骤{step} 执行异常: {exc}")
            break
        cleaned = strip_terminal_control_sequences(output or "")
        upload_notes = _auto_upload_case_screenshots(
            cleaned,
            project_id=int(project_id),
            case_id=int(test_case_id),
            env=env,
        )
        notes.append(
            _format_persistent_helper_step_output(
                cleaned,
                browser_session_id=browser_session_id,
                screenshots_dir=screenshots_dir,
                step=step,
                total_steps=resolved_total,
                upload_notes=upload_notes,
            )
        )
        if re.search(r"RESULT\s*=\s*FAIL\b", cleaned, flags=re.IGNORECASE) or re.search(
            r"Playwright exec timed out|浏览器已重置",
            cleaned,
            flags=re.IGNORECASE,
        ):
            logger.info("[AUTO_RESUME] 步骤%s 返回 FAIL/超时，停止续跑", step)
            break

    combined = "\n".join(notes).strip()
    if not combined:
        return {"plain": "", "display": ""}
    display = _finalize_skill_result(
        combined,
        skill_dir=skill_dir,
        artifacts_dir=screenshots_dir,
        artifacts_before=artifacts_before,
    )
    return {"plain": combined, "display": display}


def _finalize_skill_result(
    result_output: str,
    *,
    skill_dir: str,
    artifacts_dir: str,
    artifacts_before: dict[str, str],
) -> str:
    artifacts = _collect_skill_artifacts(
        result_output,
        skill_dir=skill_dir,
        artifacts_dir=artifacts_dir,
        artifacts_before=artifacts_before,
    )
    if not artifacts:
        return result_output

    text_content = result_output.strip() if result_output and result_output.strip() else ""
    if not text_content:
        text_content = f"已生成 {len(artifacts)} 个文件，可直接下载。"

    payload = [{"type": "text", "text": text_content}, *artifacts]
    return json.dumps(payload, ensure_ascii=False)


def _get_playwright_session_manager() -> PlaywrightSessionManager:
    """延迟初始化，避免在 import 时启动后台清理线程（线程安全）"""
    global _playwright_session_manager
    if _playwright_session_manager is None:
        with _playwright_session_manager_lock:
            if _playwright_session_manager is None:
                idle_timeout = getattr(
                    settings, "PLAYWRIGHT_BROWSER_SESSION_IDLE_TIMEOUT_SECONDS", 180
                )
                max_sessions = getattr(settings, "PLAYWRIGHT_BROWSER_MAX_SESSIONS", 2)
                max_concurrent = getattr(
                    settings, "PLAYWRIGHT_BROWSER_MAX_CONCURRENT_EXECUTIONS", 1
                )
                # 600s queue wait made the chat look frozen when another UI run held
                # the single concurrency slot. Fail fast so the agent can report it.
                exec_queue_timeout = getattr(
                    settings, "PLAYWRIGHT_BROWSER_EXEC_QUEUE_TIMEOUT_SECONDS", 90
                )
                _playwright_session_manager = PlaywrightSessionManager(
                    idle_timeout_seconds=int(idle_timeout),
                    max_sessions=int(max_sessions),
                    max_concurrent_executions=int(max_concurrent),
                    exec_queue_timeout_seconds=int(exec_queue_timeout),
                )
                logger.info(
                    "[persistent_playwright] 会话管理: idle=%ss max_sessions=%s "
                    "max_concurrent=%s queue_timeout=%ss",
                    idle_timeout,
                    max_sessions,
                    max_concurrent,
                    exec_queue_timeout,
                )
    return _playwright_session_manager


def _resolve_skill_runtime_api_key(user_id: int) -> tuple[str, str]:
    """解析 Skill 运行时使用的 API Key。

    优先级：
    1. 非空的 settings/环境变量 WHARTTEST_API_KEY（运维覆盖）
    2. 当前对话用户名下最新一条有效 API Key
    3. 空字符串（不注入，保留 skill 脚本内默认值）

    Returns:
        (api_key, source_label) source_label 仅用于日志，不含密钥内容。
    """
    configured = (
        getattr(settings, "WHARTTEST_API_KEY", None)
        or os.environ.get("WHARTTEST_API_KEY")
        or ""
    ).strip()
    if configured:
        return configured, "env_or_settings"

    if not user_id:
        return "", "none"

    try:
        from api_keys.models import APIKey
    except ImportError:
        logger.debug(
            "[execute_skill_script] api_keys 应用不可用，跳过用户 Key 解析 user_id=%s",
            user_id,
        )
        return "", "none"

    try:
        for key_obj in APIKey.objects.filter(user_id=user_id, is_active=True).order_by(
            "-created_at"
        ):
            if key_obj.is_valid() and (key_obj.key or "").strip():
                return key_obj.key.strip(), f"user_key_id={key_obj.id}"
    except Exception as e:
        logger.error(
            "[execute_skill_script] 读取用户 API Key 失败 user_id=%s: %s",
            user_id,
            e,
            exc_info=True,
        )
        return "", "error"
    return "", "none"


def _resolve_skill_runtime_backend_url() -> str:
    """Skill 在 backend 进程内执行时，默认回环访问本服务。"""
    configured = (
        getattr(settings, "WHARTTEST_BACKEND_URL", None)
        or os.environ.get("WHARTTEST_BACKEND_URL")
        or ""
    ).strip()
    if configured:
        return configured.rstrip("/")
    # 容器/本机同进程：默认本机 8000，避免 skill 脚本默认 127.0.0.1 与部署端口不一致时无覆盖
    return "http://127.0.0.1:8000"


def get_skill_tools(
    user_id: int,
    project_id: Optional[int] = None,
    test_case_id: Optional[int] = None,
    chat_session_id: Optional[str] = None,
    generate_playwright_script: bool = False,
) -> list[object]:
    """获取 Skill 工具列表（Skills 全局共享，不限制项目）"""
    current_user_id = user_id
    current_project_id = project_id if project_id is not None else 0
    current_test_case_id = test_case_id
    current_chat_session_id = chat_session_id
    current_generate_ui_case = bool(generate_playwright_script and test_case_id)

    def _read_skill_content_sync(skill_name: str) -> str:
        """Read Skill content in a Django-safe synchronous worker."""
        from skills.models import Skill

        skill = Skill.objects.filter(name=skill_name, is_active=True).first()

        if not skill:
            available = Skill.objects.filter(is_active=True).values_list(
                "name", flat=True
            )
            available_list = list(available)
            return f"错误: 未找到名为 '{skill_name}' 的 Skill。可用的 Skills: {available_list}"

        if not skill.skill_content:
            return f"错误: Skill '{skill_name}' 没有 SKILL.md 内容"

        return skill.skill_content

    @langchain_tool
    async def read_skill_content(skill_name: str) -> str:
        """
        读取指定 Skill 的完整 SKILL.md 内容。

        当你需要使用某个 Skill 时，先调用此工具获取详细的使用说明。
        系统提示词中只包含 Skill 的名称和简短描述，完整的指令和示例需要通过此工具获取。

        Args:
            skill_name: Skill 名称

        Returns:
            SKILL.md 的完整内容，包含详细的使用说明和示例
        """
        logger.info(f"[read_skill_content] skill_name={skill_name}")

        try:
            timeout = _get_skill_content_timeout_seconds()
            return await asyncio.wait_for(
                sync_to_async(_read_skill_content_sync, thread_sensitive=True)(
                    skill_name
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            timeout = _get_skill_content_timeout_seconds()
            logger.error(
                "[read_skill_content] timed out after %ss: skill_name=%s",
                timeout,
                skill_name,
            )
            return (
                f"错误: 读取 Skill '{skill_name}' 超时（{timeout:g}秒）。"
                "请跳过该 Skill，继续使用已有信息完成任务。"
            )

        except Exception as e:
            logger.error(f"[read_skill_content] 读取失败: {e}", exc_info=True)
            return f"错误: {str(e)}"

    def _execute_single_skill_script(
        skill_name: str,
        command: str,
        session_id: Optional[str] = None,
    ) -> str:
        """内部函数：执行单条 Skill 命令"""
        from skills.models import Skill

        logger.info(
            f"[execute_skill_script] skill_name={skill_name}, command={command}"
        )

        _case_exec_blocked_skills = frozenset({"playwright-cli", "browser-use"})
        if current_test_case_id and skill_name in _case_exec_blocked_skills:
            case_session = f"case_{current_test_case_id}"
            return (
                f"错误: 用例管理执行（test_case_id={current_test_case_id}）禁止使用 {skill_name}。"
                f"请改用 playwright-skill，且全程 session_id=\"{case_session}\"。"
                "playwright-cli 的 snapshot + click eXX 引用易失效，会报 Element not found。"
            )

        try:
            skill = Skill.objects.filter(name=skill_name, is_active=True).first()

            if not skill:
                available = Skill.objects.filter(is_active=True).values_list(
                    "name", flat=True
                )
                available_list = list(available)
                return f"错误: 未找到名为 '{skill_name}' 的 Skill。可用的 Skills: {available_list}"

            skill_dir = skill.get_full_path()
            if not skill_dir or not os.path.isdir(skill_dir):
                return f"错误: Skill '{skill_name}' 目录不存在"

            logger.info(f"[execute_skill_script] 在目录 {skill_dir} 执行: {command}")

            env = os.environ.copy()
            # 1) 运维配置 / 2) 当前用户有效 API Key / 3) 不注入（保留脚本默认）
            backend_url = _resolve_skill_runtime_backend_url()
            api_key, key_source = _resolve_skill_runtime_api_key(current_user_id)
            if backend_url:
                env["WHARTTEST_BACKEND_URL"] = backend_url
            if api_key:
                env["WHARTTEST_API_KEY"] = api_key
                logger.info(
                    "[execute_skill_script] 已注入运行时 API Key (user_id=%s, source=%s)",
                    current_user_id,
                    key_source,
                )
            else:
                logger.warning(
                    "[execute_skill_script] 未找到可用 API Key (user_id=%s, source=%s)，"
                    "将依赖 skill 脚本内默认值（可能 401）",
                    current_user_id,
                    key_source,
                )

            case_dir_key = None
            if current_test_case_id:
                case_dir_key = str(current_test_case_id)
            elif session_id:
                case_dir_key = session_id

            screenshots_dir = _prepare_skill_screenshots_dir(
                project_id=current_project_id,
                case_dir_key=case_dir_key,
            )
            env["SCREENSHOT_DIR"] = screenshots_dir
            artifacts_dir = _prepare_skill_artifacts_dir(
                project_id=current_project_id,
                case_dir_key=case_dir_key,
            )
            env["SKILL_OUTPUT_DIR"] = artifacts_dir
            env["ARTIFACT_DIR"] = artifacts_dir
            if current_project_id:
                env["WHARTTEST_PROJECT_ID"] = str(current_project_id)
            if current_test_case_id:
                env["WHARTTEST_CASE_ID"] = str(current_test_case_id)
                try:
                    from testcases.models import TestCase

                    tc = (
                        TestCase.objects.prefetch_related("steps")
                        .filter(id=int(current_test_case_id))
                        .first()
                    )
                    if tc:
                        from data_generation.testcase_pre_data import (
                            collect_testcase_text,
                            extract_login_credentials,
                            get_latest_pre_data_ticket_id,
                            get_latest_pre_data_ticket_no,
                            is_claimable_ticket_detail_case,
                            is_complete_ticket_ui_case,
                            is_overview_dashboard_case,
                            is_overview_sla_detail_case,
                            is_processing_handover_case,
                            is_processing_handover_submission_case,
                            processing_handover_flow_kind,
                            is_ticket_detail_boundary_case,
                            is_transfer_ready_ticket_case,
                            is_transfer_ui_helper_case,
                            transfer_ui_helper_flow,
                            get_pre_data_snapshot_for_execution,
                            resolve_claim_login_credentials,
                        )
                        from data_generation.intent_router import (
                            extract_transfer_target_name,
                        )

                        blob = collect_testcase_text(tc)
                        creds = extract_login_credentials(blob)
                        step_count = tc.steps.count()
                        if step_count:
                            env["WHARTTEST_CASE_STEP_COUNT"] = str(step_count)
                        if creds.get("username"):
                            env["WHARTTEST_USERNAME"] = creds["username"]
                        if creds.get("password"):
                            env["WHARTTEST_PASSWORD"] = creds["password"]
                        if creds.get("login_url"):
                            env["WHARTTEST_LOGIN_URL"] = creds["login_url"]
                        if is_overview_sla_detail_case(tc):
                            env["WHARTTEST_OVERVIEW_SLA_DETAIL_CASE"] = "1"
                        elif is_overview_dashboard_case(tc):
                            env["WHARTTEST_OVERVIEW_CASE"] = "1"
                        if "通知记录" in blob or "通知中心" in blob:
                            env["WHARTTEST_NOTIFICATION_CASE"] = "1"
                        if is_transfer_ui_helper_case(tc):
                            env["WHARTTEST_TRANSFER_CASE"] = "1"
                            env["WHARTTEST_TRANSFER_FLOW"] = transfer_ui_helper_flow(tc)
                            target_name = extract_transfer_target_name(blob)
                            if target_name:
                                env["WHARTTEST_TRANSFER_TARGET_NAME"] = target_name
                            env.setdefault(
                                "WHARTTEST_TRANSFER_REASON", "自动化测试转派"
                            )
                        if is_processing_handover_case(tc):
                            env["WHARTTEST_PROCESSING_HANDOVER_CASE"] = "1"
                            handover_flow = processing_handover_flow_kind(tc)
                            if handover_flow:
                                env["WHARTTEST_HANDOVER_FLOW"] = handover_flow
                            if is_processing_handover_submission_case(tc):
                                env["WHARTTEST_PROCESSING_HANDOVER_SUBMISSION"] = "1"
                                for case_step in tc.steps.order_by("step_number"):
                                    match = re.search(
                                        r"(?:选择审批人(?!弹窗)|选中(?:审批人)?)\s*([\u4e00-\u9fff]{2,4})",
                                        f"{case_step.description or ''} {case_step.expected_result or ''}",
                                    )
                                    if match:
                                        env["WHARTTEST_HANDOVER_APPROVER_NAME"] = match.group(1)
                                        break
                        if is_complete_ticket_ui_case(tc):
                            env["WHARTTEST_COMPLETE_TICKET_CASE"] = "1"
                            _set_complete_ticket_step_metadata(env, tc)
                        # 详情类 / 领取类 / 转派按钮准备类 / 移交审批 / 工单完成都必须绑定本次造数工单；
                        # 领取与转派准备还要按自动分配结果切换浏览器登录人。
                        if (
                            is_ticket_detail_boundary_case(tc)
                            or is_claimable_ticket_detail_case(tc)
                            or is_transfer_ready_ticket_case(tc)
                            or is_processing_handover_case(tc)
                            or is_complete_ticket_ui_case(tc)
                        ):
                            if is_ticket_detail_boundary_case(tc):
                                env["WHARTTEST_TICKET_DETAIL_CASE"] = "1"
                            if is_claimable_ticket_detail_case(tc) or is_transfer_ready_ticket_case(tc):
                                env["WHARTTEST_CLAIMABLE_PENDING"] = "1"
                            ticket_no = get_latest_pre_data_ticket_no(
                                tc,
                                chat_session_id=current_chat_session_id,
                            )
                            ticket_id = get_latest_pre_data_ticket_id(
                                tc,
                                chat_session_id=current_chat_session_id,
                            )
                            if ticket_no:
                                env["WHARTTEST_TICKET_NO"] = ticket_no
                            if ticket_id:
                                env["WHARTTEST_TICKET_ID"] = ticket_id
                            if (
                                is_claimable_ticket_detail_case(tc)
                                or is_transfer_ready_ticket_case(tc)
                                or is_processing_handover_case(tc)
                                or is_complete_ticket_ui_case(tc)
                            ):
                                snapshot = get_pre_data_snapshot_for_execution(
                                    tc,
                                    chat_session_id=current_chat_session_id,
                                )
                                if snapshot.get("approverName"):
                                    env["WHARTTEST_HANDOVER_APPROVER_NAME"] = str(snapshot["approverName"])
                                claim_credentials, claim_credential_error = (
                                    resolve_claim_login_credentials(tc, snapshot)
                                )
                                keep_case_login = (
                                    is_processing_handover_case(tc)
                                    or is_complete_ticket_ui_case(tc)
                                )
                                if claim_credential_error and not keep_case_login:
                                    return f"错误: 领取工单前置账号不可用：{claim_credential_error}"
                                if claim_credentials and not keep_case_login:
                                    env["WHARTTEST_USERNAME"] = claim_credentials["username"]
                                    env["WHARTTEST_PASSWORD"] = claim_credentials["password"]
                                    if claim_credentials.get("login_url"):
                                        env["WHARTTEST_LOGIN_URL"] = claim_credentials["login_url"]
                                    env["WHARTTEST_CLAIM_ASSIGNEE_NAME"] = claim_credentials[
                                        "assignee_name"
                                    ]
                                if keep_case_login:
                                    if snapshot.get("assigneeName"):
                                        env["WHARTTEST_CLAIM_ASSIGNEE_NAME"] = str(
                                            snapshot.get("assigneeName")
                                        )
                                    # 保留用例步骤中的 UI SSO 账号（如李亮 802714）。
                                    if creds.get("username"):
                                        env["WHARTTEST_USERNAME"] = creds["username"]
                                    if creds.get("password"):
                                        env["WHARTTEST_PASSWORD"] = creds["password"]
                                    if creds.get("login_url"):
                                        env["WHARTTEST_LOGIN_URL"] = creds["login_url"]
                except Exception as exc:
                    logger.debug(
                        "[execute_skill_script] overview case hint skipped: %s", exc
                    )
            artifacts_before = _snapshot_artifact_files(artifacts_dir)

            # Windows 兼容：将单引号包裹的参数转换为双引号（用于 cmd.exe）
            # 同时处理多行字符串，将换行符转换为单行
            import platform
            import re

            exec_command = command
            if skill_name == "playwright-skill":
                exec_command = normalize_playwright_skill_command(command)
                if exec_command != command:
                    logger.info(
                        "[execute_skill_script] playwright 命令已规范化: %s",
                        exec_command[:300],
                    )

            if platform.system() == "Windows":
                # 处理多行字符串：将双引号内的换行符替换为空格或分号
                def collapse_multiline(m):
                    content = m.group(1)
                    # 将换行替换为空格，保持代码可执行
                    collapsed = " ".join(
                        line.strip() for line in content.split("\n") if line.strip()
                    )
                    return f'"{collapsed}"'

                # 匹配 "..." 形式的多行字符串
                exec_command = re.sub(
                    r'"([^"]*\n[^"]*)"', collapse_multiline, exec_command
                )

                # 单引号转双引号
                def convert_quotes(m):
                    param = m.group(1)
                    value = m.group(2)
                    escaped = value.replace('"', '\\"')
                    return f'{param}"{escaped}"'

                exec_command = re.sub(
                    r"(--\w+\s+)'([^']*)'", convert_quotes, exec_command
                )

                if exec_command != command:
                    logger.info(f"[execute_skill_script] Windows 命令转换完成")

            # 用例执行中的裸 JS/run.js 命令必须共享同一个持久化浏览器会话。
            # Agent 偶尔会漏传 session_id；在回退到独立 Node 进程前按用例补齐，
            # 避免 page 未定义并丢失登录态。非 JS 的 shell 命令仍走普通执行路径。
            inline_code = None
            run_js_args = None
            if skill_name == "playwright-skill":
                inline_code = extract_playwright_inline_code(command, exec_command)
                run_js_args = (
                    [inline_code]
                    if inline_code
                    else extract_runjs_args(exec_command)
                )
                resolved_session_id = _ensure_testcase_playwright_session_id(
                    session_id,
                    skill_name=skill_name,
                    run_js_args=run_js_args,
                    test_case_id=current_test_case_id,
                )
                if resolved_session_id != session_id:
                    logger.info(
                        "[execute_skill_script] 用例 Playwright 命令自动绑定持久化会话: %s",
                        resolved_session_id,
                    )
                    session_id = resolved_session_id

            # 持久化 Playwright 会话路径
            if session_id and skill_name == "playwright-skill":
                if run_js_args is not None:
                    # 调试日志
                    logger.debug(f"[execute_skill_script] run_js_args: {run_js_args}")
                    # session_key 包含 chat_session_id 以隔离不同对话的浏览器会话
                    chat_id_part = current_chat_session_id or "default"
                    session_key = f"{current_user_id}_{current_project_id}_{chat_id_part}_{session_id}"
                    helper_code = "\n".join(str(item) for item in run_js_args)
                    if current_test_case_id:
                        try:
                            from testcases.models import TestCase as _TC

                            _tc = _TC.objects.filter(id=int(current_test_case_id)).first()
                            if _tc:
                                from data_generation.testcase_pre_data import (
                                    build_testcase_navigation_hint_by_id,
                                    is_claimable_ticket_detail_case,
                                    is_complete_ticket_ui_case,
                                    is_processing_handover_case,
                                    is_processing_handover_helper_case,
                                    is_ticket_message_send_case,
                                    is_transfer_ui_helper_case,
                                )

                                if is_claimable_ticket_detail_case(_tc) and not re.search(
                                    r"runClaimableTicketCaseStep\s*\(\s*page\s*,\s*\d+\s*\)",
                                    helper_code,
                                    flags=re.IGNORECASE,
                                ):
                                    nav = build_testcase_navigation_hint_by_id(
                                        int(current_test_case_id)
                                    )
                                    return (
                                        "错误: 本用例为「待处理未分配→领取工单」专用流程，禁止手写筛选/点处理脚本。"
                                        "每一步必须且只能调用 "
                                        "`await helpers.runClaimableTicketCaseStep(page, <步骤号>);`。"
                                        f"{nav}"
                                    )
                                if is_transfer_ui_helper_case(_tc) and not re.search(
                                    r"runTransferTicketCaseStep\s*\(\s*page\s*,\s*\d+\s*\)",
                                    helper_code,
                                    flags=re.IGNORECASE,
                                ):
                                    nav = build_testcase_navigation_hint_by_id(
                                        int(current_test_case_id)
                                    )
                                    return (
                                        "错误: 本用例为「转派 UI」专用流程，禁止手写转派弹窗脚本或改用领取 helper。"
                                        "每一步必须且只能调用 "
                                        "`await helpers.runTransferTicketCaseStep(page, <步骤号>);`。"
                                        f"{nav}"
                                    )
                                if is_processing_handover_helper_case(_tc) and not re.search(
                                    r"runProcessingHandoverCaseStep\s*\(\s*page\s*,\s*\d+\s*\)",
                                    helper_code,
                                    flags=re.IGNORECASE,
                                ):
                                    nav = build_testcase_navigation_hint_by_id(
                                        int(current_test_case_id)
                                    )
                                    return (
                                        "错误: 本用例为「处理中→移交审批」专用流程，禁止改用领取 helper（会按待处理筛选）。"
                                        "每一步必须且只能调用 "
                                        "`await helpers.runProcessingHandoverCaseStep(page, <步骤号>);`。"
                                        f"{nav}"
                                    )
                                if is_complete_ticket_ui_case(_tc) and not re.search(
                                    r"runCompleteTicketCaseStep\s*\(\s*page\s*,\s*\d+\s*\)",
                                    helper_code,
                                    flags=re.IGNORECASE,
                                ):
                                    nav = build_testcase_navigation_hint_by_id(
                                        int(current_test_case_id)
                                    )
                                    return (
                                        "错误: 本用例为「工单完成确认弹窗」专用流程，禁止改用领取 helper（截图会停在列表页）。"
                                        "每一步必须且只能调用 "
                                        "`await helpers.runCompleteTicketCaseStep(page, <步骤号>);`。"
                                        f"{nav}"
                                    )
                                if is_ticket_message_send_case(_tc) and re.search(
                                    r"runTicketDetailCaseStep\s*\(",
                                    helper_code,
                                    flags=re.IGNORECASE,
                                ):
                                    return (
                                        "错误: 本用例为「沟通消息发送」流程，禁止调用 "
                                        "`runTicketDetailCaseStep`（那是沟通区只读边界用例）。"
                                        "请在可发消息的详情页输入步骤文本并发送，再用 "
                                        "`assertPageShows` 校验沟通记录出现该文本后截图。"
                                    )
                        except Exception as guard_exc:
                            logger.debug(
                                "[execute_skill_script] claimable guard skipped: %s",
                                guard_exc,
                            )
                    # Step1 login must fail fast; never inherit a sticky long timeout
                    # from a previous exec in the same Node process.
                    pw_ms, exec_timeout_seconds = _playwright_exec_timeout_for_helper_code(
                        helper_code
                    )
                    env["PW_EXEC_TIMEOUT_MS"] = str(pw_ms)
                    try:
                        manager = _get_playwright_session_manager()
                        output = manager.execute_run_js(
                            session_key=session_key,
                            skill_dir=skill_dir,
                            run_js_args=run_js_args,
                            env=env,
                            timeout_seconds=exec_timeout_seconds,
                        )
                        logger.info(
                            f"[execute_skill_script] 持久化会话执行完成, session_key={session_key}"
                        )
                        cleaned_output = strip_terminal_control_sequences(output)
                        cleaned_output = _truncate_skill_output(cleaned_output)
                        cleaned_output = _with_failure_reminder(
                            cleaned_output, skill_name=skill_name
                        )
                        # 固定用例 helper 的每一步都只执行一行
                        # runClaimableTicketCaseStep(page, N)。在此统一写入进度，
                        # 让页面无需依赖 helper 内部 console 或额外 SSE 事件也能显示。
                        progress_match = re.search(
                            r"run(?:ClaimableTicket|TransferTicket|ProcessingHandover|CompleteTicket)CaseStep\s*\(\s*page\s*,\s*(\d+)",
                            helper_code,
                            flags=re.IGNORECASE,
                        )
                        if progress_match and "[执行进度]" not in cleaned_output:
                            step_number = progress_match.group(1)
                            total_steps = env.get("WHARTTEST_CASE_STEP_COUNT") or "4"
                            cleaned_output = (
                                f"[执行进度] 正在执行步骤 {step_number}/{total_steps}\n"
                                f"{cleaned_output}"
                            )
                        pre_ticket = (env.get("WHARTTEST_TICKET_NO") or "").strip()
                        if pre_ticket and pre_ticket not in cleaned_output:
                            cleaned_output = (
                                f"[PRE_DATA] ticketNo={pre_ticket}\n{cleaned_output}"
                            )
                        auto_upload_notes = _auto_upload_case_screenshots(
                            cleaned_output,
                            project_id=current_project_id,
                            case_id=current_test_case_id,
                            env=env,
                        )
                        result_output = (
                            cleaned_output.strip()
                            if cleaned_output.strip()
                            else "(无输出)"
                        )
                        result_output = (
                            f'[PERSISTENT_SESSION] session_id={session_id}\n'
                            f'[SCREENSHOT_DIR] {screenshots_dir}\n'
                            f'{result_output}\n'
                            f'[提示] 后续步骤请继续使用 session_id="{session_id}"；截图已保存在 {screenshots_dir}'
                        )
                        if auto_upload_notes:
                            result_output = f"{result_output}\n{auto_upload_notes}"
                        return _finalize_skill_result(
                            result_output,
                            skill_dir=skill_dir,
                            artifacts_dir=artifacts_dir,
                            artifacts_before=artifacts_before,
                        )
                    except TimeoutError:
                        logger.error(
                            "[execute_skill_script] 持久化 Playwright 执行超时"
                        )
                        return _with_failure_reminder(
                            "错误: 命令执行超时（60秒）",
                            skill_name=skill_name,
                        )
                    except Exception as e:
                        logger.error(
                            f"[execute_skill_script] 持久化 Playwright 执行失败: {e}",
                            exc_info=True,
                        )
                        return _with_failure_reminder(
                            f"错误: {str(e)}",
                            skill_name=skill_name,
                        )

            # playwright-cli 在只读的 skill 目录下以相对文件名保存截图会 EACCES，
            # 改在可写的截图目录执行，使截图默认落入 SCREENSHOT_DIR
            exec_cwd = screenshots_dir if skill_name == "playwright-cli" else skill_dir
            returncode, stdout, stderr = _run_skill_command(
                exec_command,
                cwd=exec_cwd,
                env=env,
                timeout_seconds=_get_skill_command_timeout_seconds(),
            )

            output = ""
            if stdout:
                output += stdout
            if stderr:
                if output:
                    output += "\n--- stderr ---\n"
                output += stderr

            output = _truncate_skill_output(output)

            if returncode != 0:
                output = f"命令执行失败 (退出码: {returncode})\n{output}"

            logger.info(
                f"[execute_skill_script] 执行完成, returncode={returncode}, output_len={len(output)}"
            )
            if output:
                logger.debug(f"[execute_skill_script] output: {output[:500]}")
            result_output = output.strip() if output.strip() else "(无输出)"
            result_output = _with_failure_reminder(
                result_output, skill_name=skill_name
            )
            auto_upload_notes = ""
            if skill_name == "playwright-skill" and current_test_case_id:
                auto_upload_notes = _auto_upload_case_screenshots(
                    result_output,
                    project_id=current_project_id,
                    case_id=current_test_case_id,
                    env=env,
                )
            if auto_upload_notes:
                result_output = f"{result_output}\n{auto_upload_notes}"

            # playwright-skill 未带 session_id 时提醒：裸 JS 也会被规范化成 run.js，不能只检查原始 command
            if skill_name == "playwright-skill" and not session_id:
                case_hint = (
                    f'session_id="case_{current_test_case_id}"'
                    if current_test_case_id
                    else 'session_id="case_<用例ID>"'
                )
                result_output = (
                    f"[SCREENSHOT_DIR] {screenshots_dir}\n{result_output}\n\n"
                    f"[注意] 此次未使用 session_id，浏览器已关闭、登录态丢失。"
                    f"多步骤/用例执行必须全程使用 {case_hint}，直接操作 page，"
                    f"禁止 chromium.launch()；登录后调用 helpers.dismissBlockingDialogs(page)；"
                    f"禁止使用 #el-id-* 选择器。"
                )
            elif skill_name == "playwright-skill":
                result_output = f"[SCREENSHOT_DIR] {screenshots_dir}\n{result_output}"

            return _finalize_skill_result(
                result_output,
                skill_dir=skill_dir,
                artifacts_dir=artifacts_dir,
                artifacts_before=artifacts_before,
            )

        except subprocess.TimeoutExpired:
            logger.error("[execute_skill_script] 执行超时")
            return _with_failure_reminder(
                "错误: 命令执行超时（120秒）",
                skill_name=skill_name,
            )
        except Exception as e:
            logger.error(f"[execute_skill_script] 执行失败: {e}", exc_info=True)
            return f"错误: {str(e)}"

    @langchain_tool
    def execute_skill_script(
        skill_name: Optional[str] = None,
        command: Optional[str] = None,
        session_id: Optional[str] = None,
        commands: Optional[list[dict[str, str]]] = None,
        parallel: bool = True,
        max_workers: int = 5,
    ) -> str:
        """
        执行 Skill 命令，支持单个执行或批量并发执行。

        **单个执行模式**：传入 skill_name 和 command
        **批量执行模式**：传入 commands 列表（自动并发，大幅提升效率）

        Args:
            skill_name: Skill 名称（单个执行时必填）
            command: Playwright 必须使用 node run.js "一行 JS 代码"；不要把 const/await 直接当 shell 命令。其他 Skill 传对应脚本命令，如 "python whart_tools.py --action get_projects"
            session_id: playwright-skill 多步骤/用例执行时必填，全程使用相同值（建议 case_<用例ID>）以保持浏览器会话
            commands: 批量命令列表，每个元素包含 skill_name、command、session_id（可选）
                示例: [
                    {"skill_name": "whart-test", "command": "python whart_tools.py --action add_testcase ..."},
                    {"skill_name": "whart-test", "command": "python whart_tools.py --action add_testcase ..."}
                ]
            parallel: 批量模式下是否并发执行（默认 True）
            max_workers: 批量模式下最大并发数（默认 5）

        Returns:
            单个模式返回命令输出；如执行中生成了文件，会追加可下载附件信息。
            Skills 可将导出文件写入 `SKILL_OUTPUT_DIR`（或 `ARTIFACT_DIR`）以便 Web 端直接下载。
            批量模式返回 JSON 格式结果汇总
        """
        import json
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from django.db import close_old_connections

        # 批量执行模式
        if commands:
            logger.info(
                f"[execute_skill_script] 批量模式: {len(commands)} 条命令, parallel={parallel}, max_workers={max_workers}"
            )

            if not commands:
                return json.dumps({"error": "命令列表为空"}, ensure_ascii=False)

            def execute_single(idx: int, cmd: dict[str, str]) -> dict[str, object]:
                cmd_skill_name = cmd.get("skill_name")
                cmd_command = cmd.get("command")
                cmd_session_id = cmd.get("session_id")

                if not cmd_skill_name or not cmd_command:
                    return {
                        "index": idx,
                        "skill_name": cmd_skill_name,
                        "command": cmd_command,
                        "error": "缺少 skill_name 或 command",
                    }

                try:
                    result = _execute_single_skill_script(
                        cmd_skill_name, cmd_command, cmd_session_id
                    )
                    return {
                        "index": idx,
                        "skill_name": cmd_skill_name,
                        "command": cmd_command,
                        "result": result,
                    }
                except Exception as e:
                    return {
                        "index": idx,
                        "skill_name": cmd_skill_name,
                        "command": cmd_command,
                        "error": str(e),
                    }
                finally:
                    close_old_connections()

            results: list[Optional[dict[str, object]]] = [None] * len(commands)

            if parallel and len(commands) > 1:
                with ThreadPoolExecutor(
                    max_workers=min(max_workers, len(commands))
                ) as executor:
                    future_to_idx = {
                        executor.submit(execute_single, idx, cmd): idx
                        for idx, cmd in enumerate(commands)
                    }
                    for future in as_completed(future_to_idx):
                        idx = future_to_idx[future]
                        results[idx] = future.result()
            else:
                for idx, cmd in enumerate(commands):
                    results[idx] = execute_single(idx, cmd)

            success_count = sum(
                1
                for r in results
                if r is not None and "result" in r and "error" not in r
            )
            error_count = len(results) - success_count

            logger.info(
                f"[execute_skill_script] 批量完成: {success_count} 成功, {error_count} 失败"
            )

            return json.dumps(
                {
                    "summary": {
                        "total": len(commands),
                        "success": success_count,
                        "error": error_count,
                        "parallel": parallel,
                    },
                    "results": [r for r in results if r is not None],
                },
                ensure_ascii=False,
                indent=2,
            )

        # 单个执行模式
        if not skill_name or not command:
            return "错误: 单个执行模式需要提供 skill_name 和 command 参数"

        return _execute_single_skill_script(skill_name, command, session_id)

    tools = [read_skill_content, execute_skill_script]

    if current_generate_ui_case:
        @langchain_tool
        def save_generated_ui_testcase(payload_json: str) -> str:
            """保存本次浏览器执行中真实观察到的 UI 操作，并绑定到当前功能用例。

            仅在功能测试的所有步骤均通过后调用一次。payload_json 必须是 JSON 对象，格式：
            {"name":"用例名称","description":"简述","url":"观察到的页面 URL",
             "steps":[{"name":"步骤名称","description":"步骤说明","url":"页面 URL",
             "functional_step_numbers":[1],
             "actions":[{"type":"click|fill|check|uncheck|press","selector":
             {"name":"语义名称","locator_type":"role|text|label|placeholder|test_id|id|name|css|xpath",
             "locator_value":"实际观察到的定位表达式"}},
             {"type":"goto","url":"实际观察到的 URL"},
             {"type":"wait","seconds":1},
             {"type":"wait_for_ready","timeout":10},
             {"type":"wait_for_visible","timeout":10,"selector":
             {"name":"语义名称","locator_type":"role|text|label|placeholder|test_id|id|name|css|xpath",
             "locator_value":"实际观察到的可见元素定位表达式"}},
             {"type":"assert","mode":"visible|hidden|enabled|disabled|checked|text|contain_text|value|count|url|title",
             "selector":{"name":"语义名称","locator_type":"text","locator_value":"实际定位"},
             "value":"期望值","match":"exact|path|contains","timeout":10}]}]}。

             每个 UI steps 项必须提供 functional_step_numbers：功能用例步骤编号的数组。该映射按本次真实执行证据填写；一个 UI 组可以覆盖多个功能步骤，一个功能步骤也可以由多个 UI 组共同覆盖。所有功能步骤必须至少映射到一组，不能仅为满足数量而猜测或遗漏；证据不足时不要调用保存工具，并说明哪些功能步骤尚未被真实浏览器操作覆盖。

             页面 URL 断言默认使用 exact；只有观察到同一路由仅 query/hash 动态变化时才使用 match=path。
             URL 断言的 value 必须是执行该断言时浏览器实际观察到的当前 URL；禁止根据菜单名称、常见首页或猜测填写 dashboard 等目标路由。若实际落地路由与业务预期不同，应如实记录观察结果并说明差异，不得伪造 URL 断言。
             重复文案定位必须优先使用可见且唯一的语义定位器；不要用 .first() 或固定序号掩盖重复匹配。执行器会在重复匹配中优先选择可见项，但用例仍应尽量限定到正确的导航区域。
             页面加载完成后优先使用 wait_for_ready 或 wait_for_visible，不要用固定 wait 代替页面状态等待。
             对空列表结果，保留 assert mode=count、value=0；若观察到明确空态，先等待空态可见。禁止对数据行定位器添加 wait_for_visible，因为空列表中数据行应为零。

            所有动作和定位器必须来自本次执行观察，不得编造；不要包含密码、令牌或其他秘密。
            只有本工具明确返回“已保存并绑定”及 UI 用例 ID，才可以报告生成成功。
            """
            try:
                from ui_automation.generated_case_service import persist_generated_ui_testcase

                result = persist_generated_ui_testcase(
                    testcase_id=int(current_test_case_id),
                    project_id=int(current_project_id),
                    user_id=int(current_user_id),
                    payload=payload_json,
                )
                logger.info(
                    "[save_generated_ui_testcase] testcase=%s ui_case=%s created=%s",
                    current_test_case_id,
                    result.get("ui_test_case_id"),
                    result.get("created"),
                )
                return json.dumps(result, ensure_ascii=False)
            except Exception as exc:
                logger.error(
                    "[save_generated_ui_testcase] failed testcase=%s error=%s",
                    current_test_case_id,
                    exc,
                    exc_info=True,
                )
                return json.dumps(
                    {"status": "failed", "message": str(exc)[:1000]},
                    ensure_ascii=False,
                )

        tools.append(save_generated_ui_testcase)

    return tools
