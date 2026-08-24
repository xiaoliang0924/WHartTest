"""
Skill 工具

提供渐进式加载的 Skill 系统：
- read_skill_content: 读取 Skill 的 SKILL.md 内容（按需加载）
- execute_skill_script: 执行 Skill 的 shell 命令（支持持久化浏览器会话）
"""

import logging
import subprocess
import os
import shutil
import threading
import json
import time
import mimetypes
import re
import signal
import shlex
from typing import Optional

from langchain_core.tools import tool as langchain_tool
from django.conf import settings

from .output_sanitizer import strip_terminal_control_sequences
from .persistent_playwright import PlaywrightSessionManager, extract_runjs_args

logger = logging.getLogger("orchestrator_integration")

_playwright_session_manager: Optional[PlaywrightSessionManager] = None
_playwright_session_manager_lock = threading.Lock()
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


_JS_START_RE = re.compile(
    r"^(?:const|let|var|await|async|function|import|export|class|if|for|while|try|#!|helpers\.|page\.|chromium\.|browser\.)\b",
    re.IGNORECASE,
)
_RUNJS_PREFIX_RE = re.compile(
    r"^(?:npx\s+)?(?:node\s+)?(?:\./)?run\.js(?:\s+|$)",
    re.IGNORECASE,
)


def _collapse_command_whitespace(command: str) -> str:
    return " ".join(
        line.strip() for line in (command or "").splitlines() if line.strip()
    )


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
        "page.goto(",
        "page.screenshot",
        "page.fill(",
        "page.click(",
    )
    return any(marker in stripped for marker in markers)


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
        if (rest[0] == rest[-1]) and rest[0] in {'"', "'"}:
            return f"node run.js {rest}"
        if rest.startswith("--"):
            return collapsed
        logger.warning(
            "[execute_skill_script] playwright run.js 参数未加引号，已自动转义"
        )
        return "node run.js " + shlex.quote(rest)

    if _looks_like_javascript(collapsed):
        logger.warning(
            "[execute_skill_script] playwright command 是裸 JS，已自动包裹 node run.js"
        )
        return "node run.js " + shlex.quote(collapsed)

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
                    settings, "PLAYWRIGHT_BROWSER_SESSION_IDLE_TIMEOUT_SECONDS", 15 * 60
                )
                max_sessions = getattr(settings, "PLAYWRIGHT_BROWSER_MAX_SESSIONS", 20)
                _playwright_session_manager = PlaywrightSessionManager(
                    idle_timeout_seconds=int(idle_timeout),
                    max_sessions=int(max_sessions),
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
) -> list[object]:
    """获取 Skill 工具列表（Skills 全局共享，不限制项目）"""
    current_user_id = user_id
    current_project_id = project_id if project_id is not None else 0
    current_test_case_id = test_case_id
    current_chat_session_id = chat_session_id

    @langchain_tool
    def read_skill_content(skill_name: str) -> str:
        """
        读取指定 Skill 的完整 SKILL.md 内容。

        当你需要使用某个 Skill 时，先调用此工具获取详细的使用说明。
        系统提示词中只包含 Skill 的名称和简短描述，完整的指令和示例需要通过此工具获取。

        Args:
            skill_name: Skill 名称

        Returns:
            SKILL.md 的完整内容，包含详细的使用说明和示例
        """
        from skills.models import Skill

        logger.info(f"[read_skill_content] skill_name={skill_name}")

        try:
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

            # 持久化 Playwright 会话路径
            # 仅当 session_id 存在 + skill_name == 'playwright-skill' + 命令是 run.js 调用时启用
            if session_id and skill_name == "playwright-skill":
                run_js_args = extract_runjs_args(exec_command)
                if run_js_args is not None:
                    # 调试日志
                    logger.debug(f"[execute_skill_script] run_js_args: {run_js_args}")
                    # session_key 包含 chat_session_id 以隔离不同对话的浏览器会话
                    chat_id_part = current_chat_session_id or "default"
                    session_key = f"{current_user_id}_{current_project_id}_{chat_id_part}_{session_id}"
                    try:
                        manager = _get_playwright_session_manager()
                        output = manager.execute_run_js(
                            session_key=session_key,
                            skill_dir=skill_dir,
                            run_js_args=run_js_args,
                            env=env,
                            timeout_seconds=120,
                        )
                        logger.info(
                            f"[execute_skill_script] 持久化会话执行完成, session_key={session_key}"
                        )
                        cleaned_output = strip_terminal_control_sequences(output)
                        cleaned_output = _truncate_skill_output(cleaned_output)
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
                        return "错误: 命令执行超时（120秒）"
                    except Exception as e:
                        logger.error(
                            f"[execute_skill_script] 持久化 Playwright 执行失败: {e}",
                            exc_info=True,
                        )
                        return f"错误: {str(e)}"

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
            return "错误: 命令执行超时（120秒）"
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

    return [read_skill_content, execute_skill_script]
