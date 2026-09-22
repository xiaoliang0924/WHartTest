# Generated manually for test case ordering support.

from django.db import migrations, models


def initialize_testcase_sort_order(apps, schema_editor):
    TestCase = apps.get_model("testcases", "TestCase")
    project_ids = (
        TestCase.objects.order_by("project_id")
        .values_list("project_id", flat=True)
        .distinct()
    )

    for project_id in project_ids:
        testcases = TestCase.objects.filter(project_id=project_id).order_by("id")
        for index, testcase in enumerate(testcases, start=1):
            testcase.sort_order = index
            testcase.save(update_fields=["sort_order"])


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("testcases", "0021_testcasemodule_sort_order"),
    ]

    operations = [
        migrations.AddField(
            model_name="testcase",
            name="sort_order",
            field=models.PositiveIntegerField(db_index=True, default=0, verbose_name="鎺掑簭"),
        ),
        migrations.RunPython(initialize_testcase_sort_order, migrations.RunPython.noop),
        migrations.AlterModelOptions(
            name="testcase",
            options={
                "ordering": ["project", "sort_order", "id"],
                "verbose_name": "鐢ㄤ緥",
                "verbose_name_plural": "鐢ㄤ緥",
            },
        ),
    ]
