"""同步智慧AI工单系统业务造数模板所需接口与 DB 模板计划。"""
from __future__ import annotations

import json
import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "wharttest_django.settings")
django.setup()

from api_interfaces.models import ApiInterface
from data_generation.models import DataGenerationPlan
from data_generation.templates import (
    BUILTIN_BUSINESS_TEMPLATES,
    BUILTIN_STEP_TEST_TEMPLATES,
    LEGACY_TEMPLATE_KEYS,
)
from data_generation.template_resolver import resolve_template_steps

PROJECT_ID = 1
ENV_TEST = 4

TEMPLATE_BINDINGS = {
    'default_environment_id': ENV_TEST,
    'interfaces': {
        'create_ticket': 445,
        'assign_ticket': 484,
        'transfer_ticket': 491,
        'claim_ticket': 490,
        'resolve_ticket': 520,
        'update_subject': 479,
        'ticket_detail': 509,
    },
    'database_configs': {
        'default': 1,
    },
    'functions': {
        'default': 12,
    },
}

# 旧版自动创建的模板计划，与内置 biz_* 重复，停用以免快速造数列表混乱
LEGACY_TEMPLATE_PLAN_KEYS = set(LEGACY_TEMPLATE_KEYS.keys()) | {
    "create_ticket_type_a",
    "create_ticket_with_delay",
    "create_score_test_ticket_type_a",
}


def _update_create_ticket_interface() -> None:
    iface = ApiInterface.objects.get(pk=445, project_id=PROJECT_ID)
    iface.body = {
        "type": "raw",
        "content": json.dumps(
            {
                "ticketType": "$ticketType",
                "summary": "$summary",
                "customerName": "张三",
                "salesName": "李四",
                "inquirySnapshot": {
                    "destination": "美国",
                    "weight": 10,
                    "dimensions": "30x20x10",
                    "channel": "air_freight",
                    "productName": "电子产品",
                    "manualQuote": 1500,
                },
                "initialMessage": "客户需要紧急报价",
            },
            ensure_ascii=False,
        ),
    }
    iface.save(update_fields=["body"])
    print("Updated interface 445: ticketType=$ticketType, summary=$summary")


def _update_update_subject_interface() -> None:
    iface = ApiInterface.objects.get(pk=479, project_id=PROJECT_ID)
    iface.body = {
        "type": "raw",
        "content": json.dumps({"subject": "$subject"}, ensure_ascii=False),
    }
    iface.save(update_fields=["body"])
    print("Updated interface 479: subject=$subject")


def _update_assign_interface() -> None:
    iface = ApiInterface.objects.get(pk=484, project_id=PROJECT_ID)
    iface.body = {
        "type": "raw",
        "content": json.dumps(
            {
                "assigneeUserId": "$assigneeUserId",
                "assigneeName": "$assigneeName",
                "assigneeDepartment": "$assigneeDepartment",
                "assigneeRole": "$assigneeRole",
            },
            ensure_ascii=False,
        ),
    }
    iface.save(update_fields=["body"])
    print("Updated interface 484: assigneeUserId/Name/Department/Role")


def _update_transfer_interface() -> None:
    iface = ApiInterface.objects.get(pk=491, project_id=PROJECT_ID)
    iface.body = {
        "type": "raw",
        "content": json.dumps(
            {
                "targetUserId": "$targetUserId",
                "targetRole": "$targetRole",
                "reason": "$reason",
            },
            ensure_ascii=False,
        ),
    }
    headers = iface.headers if isinstance(iface.headers, list) else []
    for item in headers:
        if isinstance(item, dict) and item.get("key") == "Authorization":
            item["value"] = "Bearer ${assigneeToken}"
    iface.headers = headers
    iface.validators = [
        {"eq": ["status_code", 200]},
        {"eq": ["body.message", "转派成功"]},
        {"type_match": ["body.assigneeUserId", "int"]},
        {"type_match": ["body.assigneeName", "str"]},
    ]
    iface.save(update_fields=["body", "headers", "validators"])
    print("Updated interface 491: JSON body, Authorization=assigneeToken")


def _update_claim_and_resolve_interfaces() -> None:
    empty_body = {"type": "raw", "content": "{}"}
    assignee_auth = "Bearer ${assigneeToken}"

    claim = ApiInterface.objects.get(pk=490, project_id=PROJECT_ID)
    claim.body = empty_body
    headers = claim.headers if isinstance(claim.headers, list) else []
    for item in headers:
        if isinstance(item, dict) and item.get("key") == "Authorization":
            item["value"] = assignee_auth
    claim.headers = headers
    claim.save(update_fields=["body", "headers"])
    print("Updated interface 490: body={}, Authorization=assigneeToken")

    resolve = ApiInterface.objects.get(pk=520, project_id=PROJECT_ID)
    resolve.body = empty_body
    headers = resolve.headers if isinstance(resolve.headers, list) else []
    for item in headers:
        if isinstance(item, dict) and item.get("key") == "Authorization":
            item["value"] = assignee_auth
    resolve.headers = headers
    resolve.save(update_fields=["body", "headers"])
    print("Updated interface 520: body={}, Authorization=assigneeToken")


def _update_ticket_detail_interface() -> None:
    iface = ApiInterface.objects.get(pk=509, project_id=PROJECT_ID)
    headers = iface.headers if isinstance(iface.headers, list) else []
    for item in headers:
        if isinstance(item, dict) and item.get("key") == "Authorization":
            item["value"] = "Bearer $adminToken"
    iface.headers = headers
    iface.save(update_fields=["headers"])
    print("Updated interface 509: Authorization=adminToken")


def _deactivate_legacy_template_plans() -> None:
    qs = DataGenerationPlan.objects.filter(
        project_id=PROJECT_ID,
        is_template=True,
        template_key__in=LEGACY_TEMPLATE_PLAN_KEYS,
        is_active=True,
    )
    count = qs.update(is_active=False)
    print(f"Deactivated {count} legacy template plan(s): {sorted(LEGACY_TEMPLATE_PLAN_KEYS)}")


def _sync_template_plans(templates, label: str) -> None:
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user = User.objects.filter(is_superuser=True).first() or User.objects.first()
    legacy_by_target = {v: k for k, v in LEGACY_TEMPLATE_KEYS.items()}

    for template in templates:
        key = template["template_key"]
        plan = DataGenerationPlan.objects.filter(
            project_id=PROJECT_ID,
            template_key=key,
            is_template=True,
        ).order_by('id').first()

        if plan is None:
            legacy_key = legacy_by_target.get(key)
            if legacy_key:
                plan = DataGenerationPlan.objects.filter(
                    project_id=PROJECT_ID,
                    template_key=legacy_key,
                    is_template=True,
                ).first()

        if plan is None:
            plan = DataGenerationPlan(
                project_id=PROJECT_ID,
                template_key=key,
                is_template=True,
                created_by=user,
            )

        plan.name = template["name"]
        plan.description = template.get("description", "")
        plan.target_type = template.get("target_type", "both")
        plan.template_bindings = TEMPLATE_BINDINGS
        plan.steps = resolve_template_steps(
            template.get("steps") or [],
            project_id=PROJECT_ID,
            plan_bindings=TEMPLATE_BINDINGS,
            default_environment_id=ENV_TEST,
        )
        plan.cleanup_steps = resolve_template_steps(
            template.get("cleanup_steps") or [],
            project_id=PROJECT_ID,
            plan_bindings=TEMPLATE_BINDINGS,
            default_environment_id=ENV_TEST,
        )
        plan.default_environment_id = ENV_TEST
        plan.template_icon = template.get("icon", "")
        plan.template_params_schema = template.get("params_schema") or {}
        plan.is_active = True
        plan.template_key = key
        plan.save()

        removed = (
            DataGenerationPlan.objects.filter(
                project_id=PROJECT_ID,
                template_key=key,
                is_template=True,
            )
            .exclude(id=plan.id)
            .delete()[0]
        )
        if removed:
            print(f"  removed {removed} duplicate plan(s) for {key}")

        print(f"  synced {label} plan: {key} (id={plan.id})")


def _sync_business_template_plans() -> None:
    _sync_template_plans(BUILTIN_BUSINESS_TEMPLATES, "biz")


def _sync_step_test_template_plans() -> None:
    _sync_template_plans(BUILTIN_STEP_TEST_TEMPLATES, "test_step")


def main() -> None:
    print("=== Sync business template interfaces ===")
    _update_create_ticket_interface()
    _update_update_subject_interface()
    _update_assign_interface()
    _update_transfer_interface()
    _update_claim_and_resolve_interfaces()
    _update_ticket_detail_interface()

    print("\n=== Deactivate legacy duplicate template plans ===")
    _deactivate_legacy_template_plans()

    print("\n=== Sync biz_* template plans to DB ===")
    _sync_business_template_plans()

    print("\n=== Sync test_step_* template plans to DB ===")
    _sync_step_test_template_plans()

    print("\nDone.")


if __name__ == "__main__":
    main()
