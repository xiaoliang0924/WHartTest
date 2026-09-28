from django.db import migrations


def replace_legacy_generation_examples(apps, schema_editor):
    UserPrompt = apps.get_model("prompts", "UserPrompt")

    replacements = (
        (
            "使用账号802714/000000登录系统(https://test.bot.by56.com/work-order/login)",
            "使用当前项目凭据中的目标账号登录系统（使用该账号对应的 system_url）",
        ),
        (
            "使用李亮账号(admin/admin123)登录系统(http://test.bot.by56.com/work-order/login)",
            "使用当前项目凭据中的目标账号登录系统（使用该账号对应的 system_url）",
        ),
        (
            "Log in with 802714/000000 at https://test.bot.by56.com/work-order/login",
            "Log in with the target account from the current project's credentials at its configured system_url",
        ),
        (
            "Log in with Li Liang (admin/admin123) at http://test.bot.by56.com/work-order/login",
            "Log in with the target account from the current project's credentials at its configured system_url",
        ),
    )

    for prompt in UserPrompt.objects.filter(content__contains="test.bot.by56.com"):
        content = prompt.content
        for old_text, new_text in replacements:
            content = content.replace(old_text, new_text)
        if content != prompt.content:
            prompt.content = content
            prompt.save(update_fields=["content", "updated_at"])


class Migration(migrations.Migration):
    dependencies = [
        ("prompts", "0010_alter_userprompt_prompt_type"),
    ]

    operations = [
        migrations.RunPython(
            replace_legacy_generation_examples,
            migrations.RunPython.noop,
        ),
    ]
