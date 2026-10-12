"""UI generation runs outside an HTTP/WebSocket connection's lifetime."""
import asyncio
import logging
from datetime import timedelta

from asgiref.sync import async_to_sync, sync_to_async
from celery import shared_task
from django.conf import settings
from redis import Redis
from django.utils import timezone

from .ui_generation_jobs import ACTIVE_STATUSES, job_timeout_seconds, reconcile_expired_job, update_job

logger = logging.getLogger(__name__)


async def _generate_from_run(record_id, task_id):
    from testcases.models import TestCaseRunRecord
    from langgraph_integration.models import LLMConfig
    from langgraph_integration.views import create_llm_instance, check_project_permission
    from wharttest_django.checkpointer import get_async_checkpointer
    from .builtin_tools import get_builtin_tools
    from .agent_loop_view import _finalize_generated_ui_case

    record = await sync_to_async(
        lambda: TestCaseRunRecord.objects.select_related("testcase", "executor").get(pk=record_id)
    )()
    if not record.executor or not record.executor.is_active or record.status != "pass":
        raise ValueError("原执行用户已失效或功能测试未通过。")
    if not await sync_to_async(check_project_permission)(record.executor, record.testcase.project_id):
        raise ValueError("原执行用户已无此项目的访问权限。")
    config_id = record.ui_generation.get("llm_config_id")
    config = await sync_to_async(
        lambda: LLMConfig.objects.filter(pk=config_id).first() if config_id
        else LLMConfig.objects.filter(is_active=True).first()
    )()
    if config is None:
        raise ValueError("生成使用的模型配置不存在。")
    thread_id = f"{record.executor_id}_{record.testcase.project_id}_{record.session_id}"
    async with get_async_checkpointer() as checkpointer:
        checkpoint = await checkpointer.aget_tuple({"configurable": {"thread_id": thread_id}})
    messages = (checkpoint.checkpoint.get("channel_values", {}).get("messages", [])
                if checkpoint else [])
    # Fast-finish branches can exit immediately after a tool completes. Its
    # evidence is then in pending writes until the graph's next checkpoint.
    # Match the messages reducer used by agent.aget_state, including those writes.
    from langgraph.graph.message import add_messages
    for _, channel, value in (getattr(checkpoint, "pending_writes", None) or []):
        if channel == "messages":
            messages = add_messages(messages, value)
    if not messages:
        raise ValueError("没有找到本次执行的浏览器操作记录，不能安全生成 UI 用例。")
    llm = await sync_to_async(create_llm_instance)(config, temperature=0.7)
    tools = get_builtin_tools(
        user_id=record.executor_id, project_id=record.testcase.project_id,
        test_case_id=record.testcase_id, chat_session_id=record.session_id,
        generate_playwright_script=True,
    )

    async def on_stage(stage):
        accepted = await sync_to_async(update_job)(
            record_id, task_id, status=stage,
            message="功能测试已通过，正在回放验证生成的 UI 用例。",
        )
        if not accepted:
            raise asyncio.CancelledError()

    return await _finalize_generated_ui_case(
        llm=llm, tools=tools, messages=messages, test_case_id=record.testcase_id,
        project_id=record.testcase.project_id, record=record, user=record.executor,
        background=True, on_stage=on_stage,
        # The generation pass must use the same provider-specific forced-tool
        # mode as the original request (Anthropic uses tool_choice="any").
        provider=getattr(config, "provider", "openai_compatible"),
    )


async def _supervise_generation(record_id, task_id):
    from testcases.models import TestCaseRunRecord
    task = asyncio.create_task(_generate_from_run(record_id, task_id))
    deadline = asyncio.get_running_loop().time() + job_timeout_seconds()
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=1)
            state = await sync_to_async(
                lambda: TestCaseRunRecord.objects.values_list("ui_generation", flat=True).get(pk=record_id)
            )()
            if state.get("task_id") != task_id or state.get("status") not in ACTIVE_STATUSES:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                return
            if state.get("status") == "stopping":
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await sync_to_async(update_job)(record_id, task_id, status="stopped",
                    message="功能测试已通过，UI 用例生成已由用户停止。", completed_at=timezone.now().isoformat())
                return
            if asyncio.get_running_loop().time() >= deadline:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise TimeoutError("UI 用例生成及验证超过任务总时限。")
            if done:
                result = task.result()
                await sync_to_async(update_job)(record_id, task_id,
                    **{k: v for k, v in result.items() if k != "enabled"},
                    completed_at=timezone.now().isoformat())
                return
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@shared_task(name="orchestrator_integration.generate_ui_case_from_run", soft_time_limit=1560, time_limit=1620)
def generate_ui_case_from_run(record_id, task_id):
    from testcases.models import TestCaseRunRecord
    # Claim once: duplicate queue delivery must not replay business actions.
    record = TestCaseRunRecord.objects.filter(pk=record_id).first()
    if record is None:
        return
    reconcile_expired_job(record)
    claimed = TestCaseRunRecord.objects.filter(
        pk=record_id, ui_generation__task_id=task_id, ui_generation__status="queued",
    ).update(ui_generation={
        **(record.ui_generation or {}),
        "status": "generating", "started_at": timezone.now().isoformat(),
        "deadline_at": (timezone.now() + timedelta(seconds=job_timeout_seconds())).isoformat(),
        "message": "功能测试已通过，正在后台生成 UI 用例。切页或刷新不会中断。",
    })
    if not claimed:
        return
    record.refresh_from_db()
    lock_key = f"ui-generation-case:{record.testcase_id}"
    lock = None
    acquired = False
    try:
        lock = Redis.from_url(settings.CELERY_BROKER_URL).lock(lock_key, timeout=1700, blocking=False)
        acquired = lock.acquire()
        if not acquired:
            raise ValueError("该用例的上一次后台任务正在清理，请稍后重试。")
        update_job(record_id, task_id, status="generating", message=record.ui_generation["message"])
        logger.info("UI generation started; run=%s task=%s", record_id, task_id)
        async_to_sync(_supervise_generation)(record_id, task_id)
    except Exception as exc:
        logger.exception("UI generation failed; run=%s task=%s", record_id, task_id)
        update_job(record_id, task_id, status="failed",
                   message="功能测试已通过，但 UI 用例生成或验证未完成。可使用已有执行记录重试。",
                   reason=str(exc)[:700], completed_at=timezone.now().isoformat())
    finally:
        if acquired:
            try:
                lock.release()
            except Exception:
                logger.exception("UI generation lock cleanup failed; run=%s task=%s", record_id, task_id)


@shared_task(name="orchestrator_integration.reconcile_ui_generation_jobs")
def reconcile_ui_generation_jobs():
    from testcases.models import TestCaseRunRecord
    for record in TestCaseRunRecord.objects.filter(ui_generation__status__in=list(ACTIVE_STATUSES)).iterator():
        reconcile_expired_job(record)
