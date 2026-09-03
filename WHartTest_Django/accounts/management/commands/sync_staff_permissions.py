"""为 is_staff 用户补齐新增模块的 Django 权限。"""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management.base import BaseCommand

User = get_user_model()


class Command(BaseCommand):
    help = '为所有 is_staff 用户同步完整 Django 模型权限（新 app migrate 后执行）'

    def handle(self, *args, **options):
        all_permissions = Permission.objects.all()
        total = all_permissions.count()
        updated = 0

        for user in User.objects.filter(is_staff=True, is_superuser=False):
            before = user.user_permissions.count()
            if before < total:
                user.user_permissions.set(all_permissions)
                updated += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f'{user.username}: {before} -> {total} 个权限'
                    )
                )

        self.stdout.write(
            self.style.SUCCESS(f'完成，共更新 {updated} 个 staff 用户')
        )
