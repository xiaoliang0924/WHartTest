---
name: data-generation
description: WHartTest 造数管理工具。用于创建、更新、执行造数计划，查看执行记录，运行快速模板，分析套件变量缺口，以及执行清理步骤。当需要准备测试数据、写入环境变量/UI 公共数据、或在套件执行前自动造数时使用。
---

# WHartTest 造数管理

## 常用动作

```bash
python data_generation_tools.py --action list_plans --project_id 1
python data_generation_tools.py --action run_plan --project_id 1 --plan_id 1 --payload '{"input_params":{"summary":"测试工单"}}'
python data_generation_tools.py --action run_template --project_id 1 --payload '{"template_key":"create_ticket_type_a","input_params":{"summary":"回归工单"}}'
python data_generation_tools.py --action analyze_suite --project_id 1 --payload '{"suite_id":1,"environment_id":4}'
python data_generation_tools.py --action cleanup_run --project_id 1 --run_id 2
```

## 步骤类型

- `api_call` / `set_env_var` / `set_public_data`
- `sql` / `custom_function` / `delay`

## 原则

- 先查现有计划，再创建
- 执行成功后展示 output_snapshot
- 清理前确认用户意图
