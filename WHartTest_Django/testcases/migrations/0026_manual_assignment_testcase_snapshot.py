from django.db import migrations, models
import django.db.models.deletion


def backfill_testcase_snapshots(apps, schema_editor):
    ManualTestAssignment = apps.get_model("testcases", "ManualTestAssignment")
    TestCaseStep = apps.get_model("testcases", "TestCaseStep")

    assignments = ManualTestAssignment.objects.select_related(
        "testcase__module",
        "testcase__creator",
    )
    for assignment in assignments.iterator():
        testcase = assignment.testcase
        creator = testcase.creator
        assignment.testcase_snapshot = {
            "id": testcase.id,
            "project": testcase.project_id,
            "module_id": testcase.module_id,
            "module_detail": testcase.module.name if testcase.module_id else None,
            "name": testcase.name,
            "precondition": testcase.precondition,
            "level": testcase.level,
            "notes": testcase.notes,
            "steps": [
                {
                    "id": step.id,
                    "step_number": step.step_number,
                    "description": step.description,
                    "expected_result": step.expected_result,
                    "creator": step.creator_id,
                }
                for step in TestCaseStep.objects.filter(test_case_id=testcase.id).order_by(
                    "step_number", "id"
                )
            ],
            "screenshot": str(testcase.screenshot) if testcase.screenshot else None,
            "screenshots": [],
            "creator": testcase.creator_id,
            "creator_detail": {
                "id": creator.id,
                "username": creator.username,
                "email": creator.email,
                "first_name": creator.first_name,
                "last_name": creator.last_name,
                "is_staff": creator.is_staff,
                "is_active": creator.is_active,
                "groups": [],
            }
            if creator
            else None,
            "created_at": testcase.created_at.isoformat() if testcase.created_at else None,
            "updated_at": testcase.updated_at.isoformat() if testcase.updated_at else None,
            "review_status": testcase.review_status,
            "test_type": testcase.test_type,
            "sort_order": testcase.sort_order,
        }
        assignment.save(update_fields=["testcase_snapshot"])


class Migration(migrations.Migration):
    dependencies = [
        ("testcases", "0025_manual_test_run_manual_test_assignment"),
    ]

    operations = [
        migrations.AddField(
            model_name="manualtestassignment",
            name="testcase_snapshot",
            field=models.JSONField(blank=True, default=dict, verbose_name="测试用例快照"),
        ),
        migrations.RunPython(backfill_testcase_snapshots, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="manualtestassignment",
            name="testcase",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="manual_test_assignments",
                to="testcases.testcase",
                verbose_name="测试用例",
            ),
        ),
    ]
