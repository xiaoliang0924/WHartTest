from django.contrib.auth.models import User
from django.db import models
from django.utils.translation import gettext_lazy as _


class DataGenerationPlan(models.Model):
    """造数计划：定义一组可复用的数据准备步骤。"""

    TARGET_API = 'api'
    TARGET_UI = 'ui'
    TARGET_BOTH = 'both'
    TARGET_CHOICES = [
        (TARGET_API, _('API')),
        (TARGET_UI, _('UI')),
        (TARGET_BOTH, _('API + UI')),
    ]

    project = models.ForeignKey(
        'projects.Project',
        on_delete=models.CASCADE,
        related_name='data_generation_plans',
        verbose_name=_('所属项目'),
    )
    name = models.CharField(_('计划名称'), max_length=255)
    description = models.TextField(_('描述'), blank=True, default='')
    target_type = models.CharField(
        _('目标类型'),
        max_length=20,
        choices=TARGET_CHOICES,
        default=TARGET_BOTH,
    )
    steps = models.JSONField(
        _('步骤配置'),
        default=list,
        help_text=_('JSON 步骤列表，支持 api_call / set_env_var / set_public_data / sql / custom_function / delay'),
    )
    cleanup_steps = models.JSONField(
        _('清理步骤'),
        default=list,
        blank=True,
        help_text=_('执行完成后可选的清理/回滚步骤'),
    )
    is_template = models.BooleanField(_('是否模板'), default=False)
    template_key = models.CharField(_('模板标识'), max_length=100, blank=True, default='')
    template_icon = models.CharField(_('模板图标'), max_length=50, blank=True, default='')
    template_params_schema = models.JSONField(_('模板参数定义'), default=dict, blank=True)
    template_bindings = models.JSONField(
        _('模板资源绑定'),
        default=dict,
        blank=True,
        help_text=_('按项目解析 interface_ref / environment_ref，如 interfaces.create_ticket=445'),
    )
    default_environment = models.ForeignKey(
        'api_environments.ApiEnvironment',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='data_generation_plans',
        verbose_name=_('默认 API 环境'),
    )
    is_active = models.BooleanField(_('启用'), default=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_data_generation_plans',
        verbose_name=_('创建人'),
    )
    created_at = models.DateTimeField(_('创建时间'), auto_now_add=True)
    updated_at = models.DateTimeField(_('更新时间'), auto_now=True)

    class Meta:
        verbose_name = _('造数计划')
        verbose_name_plural = _('造数计划')
        ordering = ['-updated_at']
        unique_together = ('project', 'name')

    def __str__(self):
        return f'{self.project.name} - {self.name}'


class DataGenerationRun(models.Model):
    """造数执行记录。"""

    STATUS_PENDING = 'pending'
    STATUS_RUNNING = 'running'
    STATUS_SUCCESS = 'success'
    STATUS_FAILED = 'failed'
    STATUS_CHOICES = [
        (STATUS_PENDING, _('等待中')),
        (STATUS_RUNNING, _('执行中')),
        (STATUS_SUCCESS, _('成功')),
        (STATUS_FAILED, _('失败')),
    ]

    TRIGGER_MANUAL = 'manual'
    TRIGGER_SUITE_PRE = 'suite_pre'
    TRIGGER_CASE_PRE = 'case_pre'
    TRIGGER_CLEANUP = 'cleanup'
    TRIGGER_CHOICES = [
        (TRIGGER_MANUAL, _('手动执行')),
        (TRIGGER_SUITE_PRE, _('套件前置')),
        (TRIGGER_CASE_PRE, _('用例前置')),
        (TRIGGER_CLEANUP, _('清理执行')),
    ]

    plan = models.ForeignKey(
        DataGenerationPlan,
        on_delete=models.CASCADE,
        related_name='runs',
        verbose_name=_('造数计划'),
    )
    project = models.ForeignKey(
        'projects.Project',
        on_delete=models.CASCADE,
        related_name='data_generation_runs',
        verbose_name=_('所属项目'),
    )
    status = models.CharField(
        _('状态'),
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
    )
    trigger_type = models.CharField(
        _('触发方式'),
        max_length=20,
        choices=TRIGGER_CHOICES,
        default=TRIGGER_MANUAL,
    )
    test_execution = models.ForeignKey(
        'testcases.TestExecution',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='data_generation_runs',
        verbose_name=_('关联测试执行'),
    )
    input_params = models.JSONField(_('输入参数'), default=dict, blank=True)
    output_snapshot = models.JSONField(_('输出快照'), default=dict, blank=True)
    step_logs = models.JSONField(_('步骤日志'), default=list, blank=True)
    error_message = models.TextField(_('错误信息'), blank=True, default='')
    is_cleaned = models.BooleanField(_('已清理'), default=False)
    cleanup_status = models.CharField(
        _('清理状态'),
        max_length=20,
        blank=True,
        default='',
    )
    CLEANUP_SUCCESS = 'success'
    CLEANUP_FAILED = 'failed'
    CLEANUP_SKIPPED = 'skipped'
    cleanup_logs = models.JSONField(_('清理日志'), default=list, blank=True)
    cleanup_error_message = models.TextField(_('清理错误'), blank=True, default='')
    parent_run = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='child_runs',
        verbose_name=_('来源执行记录'),
    )
    triggered_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='triggered_data_generation_runs',
        verbose_name=_('触发人'),
    )
    started_at = models.DateTimeField(_('开始时间'), null=True, blank=True)
    finished_at = models.DateTimeField(_('结束时间'), null=True, blank=True)
    created_at = models.DateTimeField(_('创建时间'), auto_now_add=True)

    class Meta:
        verbose_name = _('造数执行记录')
        verbose_name_plural = _('造数执行记录')
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.plan.name} - {self.get_status_display()} - {self.created_at:%Y-%m-%d %H:%M:%S}'
