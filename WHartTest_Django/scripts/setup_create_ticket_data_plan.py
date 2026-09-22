"""Create data generation plan for create-ticket API and test run."""
import json
import os
import time

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "wharttest_django.settings")
django.setup()

from django.contrib.auth import get_user_model
from api_interfaces.models import ApiInterface
from data_generation.models import DataGenerationPlan
from data_generation.services import execute_plan

PROJECT_ID = 1
ENVIRONMENT_ID = 4
INTERFACE_ID = 445
PLAN_NAME = "创建待分配工单_TYPE_A"

User = get_user_model()
user = User.objects.filter(is_superuser=True).first() or User.objects.first()

iface = ApiInterface.objects.get(pk=INTERFACE_ID, project_id=PROJECT_ID)
iface.body = {
    "type": "raw",
    "content": json.dumps(
        {
            "ticketType": "TYPE_A",
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
print(f"Updated interface {INTERFACE_ID} body to use $summary variable")

steps = [
    {
        "type": "api_call",
        "name": "创建工单 TYPE_A",
        "interface_id": INTERFACE_ID,
        "environment_id": ENVIRONMENT_ID,
        "variables": {
            "summary": "{{summary}}",
        },
        "extract": {
            "ticketId": "ticketId",
            "ticketNo": "ticketNo",
        },
    },
    {
        "type": "set_env_var",
        "name": "写入环境变量",
        "environment_id": ENVIRONMENT_ID,
        "variables": {
            "ticketId": "{{ticketId}}",
            "ticketNo": "{{ticketNo}}",
            "processingTicketId": "{{ticketId}}",
        },
    },
    {
        "type": "set_public_data",
        "name": "写入UI公共数据",
        "items": [
            {"key": "ticketId", "value": "{{ticketId}}", "type": 0},
            {"key": "ticketNo", "value": "{{ticketNo}}", "type": 0},
            {"key": "work_order_id", "value": "{{ticketId}}", "type": 0},
        ],
    },
]

plan, created = DataGenerationPlan.objects.update_or_create(
    project_id=PROJECT_ID,
    name=PLAN_NAME,
    defaults={
        "description": "调用 POST /api/tickets 创建 TYPE_A 工单，并写入 ticketId/ticketNo 到环境变量与公共数据",
        "target_type": DataGenerationPlan.TARGET_BOTH,
        "steps": steps,
        "default_environment_id": ENVIRONMENT_ID,
        "is_active": True,
        "created_by": user,
    },
)
print(f"Plan {'created' if created else 'updated'}: id={plan.id} name={plan.name}")

unique_summary = f"造数{int(time.time()) % 100000}"[:20]
run = execute_plan(
    plan,
    input_params={"summary": unique_summary},
    triggered_by=user,
    default_environment_id=ENVIRONMENT_ID,
)
print(f"summary used: {unique_summary}")
print(f"Run id={run.id} status={run.status}")
if run.status != "success":
    print("ERROR:", run.error_message)
    print("step_logs:", json.dumps(run.step_logs, ensure_ascii=False, indent=2))
else:
    print("output:", json.dumps(run.output_snapshot, ensure_ascii=False, indent=2))
