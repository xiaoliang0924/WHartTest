from django.db import migrations


def synchronize_module_order(apps, schema_editor):
    TestCaseModule = apps.get_model("testcases", "TestCaseModule")
    sibling_groups = (
        TestCaseModule.objects.order_by("project_id", "parent_id")
        .values_list("project_id", "parent_id")
        .distinct()
    )

    for project_id, parent_id in sibling_groups:
        modules = list(
            TestCaseModule.objects.filter(
                project_id=project_id,
                parent_id=parent_id,
            )
        )
        modules.sort(
            key=lambda module: (
                module.sort_order or module.order or module.id,
                module.id,
            )
        )
        for index, module in enumerate(modules, start=1):
            module.sort_order = index
            module.order = index
        if modules:
            TestCaseModule.objects.bulk_update(modules, ["sort_order", "order"])


class Migration(migrations.Migration):
    dependencies = [
        ("testcases", "0027_alter_testcase_options"),
        ("testcases", "0022_alter_testcasestep_expected_result"),
    ]

    operations = [
        migrations.RunPython(synchronize_module_order, migrations.RunPython.noop),
        migrations.AlterModelOptions(
            name="testcasemodule",
            options={
                "ordering": [
                    "project",
                    "parent_id",
                    "order",
                    "sort_order",
                    "id",
                ],
                "verbose_name": "用例模块",
                "verbose_name_plural": "用例模块",
            },
        ),
    ]
