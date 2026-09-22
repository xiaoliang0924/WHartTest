# Generated manually for module ordering support.

from django.db import migrations, models


def initialize_module_sort_order(apps, schema_editor):
    TestCaseModule = apps.get_model("testcases", "TestCaseModule")
    sibling_groups = (
        TestCaseModule.objects.order_by("project_id", "parent_id", "id")
        .values_list("project_id", "parent_id")
        .distinct()
    )

    for project_id, parent_id in sibling_groups:
        siblings = TestCaseModule.objects.filter(
            project_id=project_id,
            parent_id=parent_id,
        ).order_by("id")
        for index, module in enumerate(siblings, start=1):
            module.sort_order = index
            module.save(update_fields=["sort_order"])


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("testcases", "0020_add_test_type_field"),
    ]

    operations = [
        migrations.AddField(
            model_name="testcasemodule",
            name="sort_order",
            field=models.PositiveIntegerField(db_index=True, default=0, verbose_name="排序"),
        ),
        migrations.RunPython(initialize_module_sort_order, migrations.RunPython.noop),
        migrations.AlterModelOptions(
            name="testcasemodule",
            options={
                "ordering": ["project", "parent_id", "sort_order", "id"],
                "verbose_name": "用例模块",
                "verbose_name_plural": "用例模块",
            },
        ),
    ]
