from django.db import models
from django.db.models import Max
from django.contrib.auth.models import User
from django.utils.translation import gettext_lazy as _
from django.core.exceptions import ValidationError
from projects.models import Project # 确保从正确的应用导入Project模型
import os


def testcase_screenshot_path(instance, filename):
    """
    生成测试用例截屏的文件路径
    路径格式: testcase_screenshots/{project_id}/{testcase_id}/{filename}
    """
    return f"testcase_screenshots/{instance.test_case.project.id}/{instance.test_case.id}/{filename}"

class TestCase(models.Model):
    """
    用例模型
    """
    LEVEL_CHOICES = [
        ('P0', _('P0')),
        ('P1', _('P1')),
        ('P2', _('P2')),
        ('P3', _('P3')),
    ]

    TEST_TYPE_CHOICES = [
        ('smoke', _('冒烟测试')),
        ('functional', _('功能测试')),
        ('boundary', _('边界测试')),
        ('exception', _('异常测试')),
        ('permission', _('权限测试')),
        ('security', _('安全测试')),
        ('compatibility', _('兼容性测试')),
    ]

    REVIEW_STATUS_CHOICES = [
        ('pending_review', _('待审核')),
        ('approved', _('通过')),
        ('needs_optimization', _('优化')),
        ('optimization_pending_review', _('优化待审核')),
        ('pending_product_confirmation', _('待产品确认')),
        ('unavailable', _('不可用')),
    ]

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='testcases',
        verbose_name=_('所属项目')
    )
    module = models.ForeignKey(
        'TestCaseModule',
        on_delete=models.PROTECT, # 有用例时不能删除模块
        null=False,  # 不允许为空
        blank=False, # 表单中必填
        related_name='testcases',
        verbose_name=_('所属模块')
    )
    name = models.CharField(_('用例名称'), max_length=255)
    precondition = models.TextField(_('前置描述'), blank=True, null=True)
    level = models.CharField(
        _('用例等级'),
        max_length=2,
        choices=LEVEL_CHOICES,
        default='P2' # 可以设置一个默认等级
    )
    creator = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_testcases',
        verbose_name=_('创建人')
    )
    created_at = models.DateTimeField(_('创建时间'), auto_now_add=True)
    updated_at = models.DateTimeField(_('更新时间'), auto_now=True)
    notes = models.TextField(_('备注'), blank=True, null=True)
    screenshot = models.ImageField(
        _('截屏图片'),
        upload_to='testcase_screenshots/',
        blank=True,
        null=True,
        help_text=_('测试用例的截屏图片')
    )
    review_status = models.CharField(
        _('审核状态'),
        max_length=30,
        choices=REVIEW_STATUS_CHOICES,
        default='pending_review',
        blank=True,
        null=True,
    )
    test_type = models.CharField(
        _('测试类型'),
        max_length=20,
        choices=TEST_TYPE_CHOICES,
        default='functional',
        blank=True,
    )

    sort_order = models.PositiveIntegerField(_('鎺掑簭'), default=0, db_index=True)

    class Meta:
        verbose_name = _('用例')
        verbose_name_plural = _('用例')
        ordering = ['project', 'sort_order', 'id']

    def __str__(self):
        return f"{self.project.name} - {self.name}"

    def save(self, *args, **kwargs):
        if self._state.adding and not self.sort_order and self.project_id:
            max_sort_order = (
                TestCase.objects.filter(project_id=self.project_id)
                .aggregate(max_sort_order=Max("sort_order"))
                .get("max_sort_order")
                or 0
            )
            self.sort_order = max_sort_order + 1
        super().save(*args, **kwargs)

class TestCaseStep(models.Model):
    """
    用例步骤模型
    """
    test_case = models.ForeignKey(
        TestCase,
        on_delete=models.CASCADE,
        related_name='steps',
        verbose_name=_('所属用例')
    )
    step_number = models.PositiveIntegerField(_('步骤编号'))
    description = models.TextField(_('步骤描述'))
    expected_result = models.TextField(_('预期结果'), blank=True, default='')
    creator = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_testcase_steps',
        verbose_name=_('创建人')
    )
    created_at = models.DateTimeField(_('创建时间'), auto_now_add=True)
    updated_at = models.DateTimeField(_('更新时间'), auto_now=True)

    class Meta:
        verbose_name = _('用例步骤')
        verbose_name_plural = _('用例步骤')
        ordering = ['test_case', 'step_number']
        unique_together = ('test_case', 'step_number') #确保同一用例下的步骤编号唯一

    def __str__(self):
        return f"{self.test_case.name} - Step {self.step_number}"


MAX_MODULE_LEVEL = 6


class TestCaseModule(models.Model):
    """
    用例模块模型，最多支持 6 级
    """
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='testcase_modules',
        verbose_name=_('所属项目')
    )
    name = models.CharField(_('模块名称'), max_length=100)
    parent = models.ForeignKey(
        'self',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='children',
        verbose_name=_('父模块')
    )
    level = models.PositiveSmallIntegerField(_('模块级别'), default=1)
    sort_order = models.PositiveIntegerField(_('排序'), default=0, db_index=True)
    creator = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_testcase_modules',
        verbose_name=_('创建人')
    )
    created_at = models.DateTimeField(_('创建时间'), auto_now_add=True)
    updated_at = models.DateTimeField(_('更新时间'), auto_now=True)
    order = models.IntegerField(_('排序'), default=0)

    class Meta:
        verbose_name = _('用例模块')
        verbose_name_plural = _('用例模块')
        ordering = ['project', 'parent_id', 'order', 'sort_order', 'id']
        unique_together = ('project', 'parent', 'name')  # 确保同一父模块下的子模块名称唯一

    def __str__(self):
        if self.parent:
            return f"{self.parent} > {self.name}"
        return self.name

    def clean(self):
        """验证模块级别不超过上限"""
        if self.level > MAX_MODULE_LEVEL:
            raise ValidationError(_('模块级别不能超过6级'))

        # 验证父模块属于同一个项目
        if self.parent and self.parent.project_id != self.project_id:
            raise ValidationError(_('父模块必须属于同一个项目'))

        # 验证父模块的级别比当前模块低一级
        if self.parent:
            self.level = self.parent.level + 1
        else:
            self.level = 1

    def save(self, *args, **kwargs):
        old_level = None
        if self.pk:
            try:
                old_level = TestCaseModule.objects.get(pk=self.pk).level
            except TestCaseModule.DoesNotExist:
                pass
        self.clean()
        super().save(*args, **kwargs)
        if old_level is not None and old_level != self.level:
            self.update_descendants_level()

    def get_all_descendant_ids(self):
        """
        获取当前模块及其所有子模块的ID列表（递归）
        """
        ids = [self.id]
        for child in self.children.all():
            ids.extend(child.get_all_descendant_ids())
        return ids

    def get_max_depth(self):
        """
        获取当前模块子树的最大深度（包括当前模块自己，深度最小为1）
        """
        children = self.children.all()
        if not children:
            return 1
        return 1 + max(child.get_max_depth() for child in children)

    def update_descendants_level(self):
        """
        递归更新所有子模块的级别
        """
        for child in self.children.all():
            child.save()


class TestCaseScreenshot(models.Model):
    """
    测试用例截屏模型 - 支持一个用例多张截屏
    """
    test_case = models.ForeignKey(
        TestCase,
        on_delete=models.CASCADE,
        related_name='screenshots',
        verbose_name=_('测试用例')
    )
    screenshot = models.ImageField(
        _('截屏图片'),
        upload_to=testcase_screenshot_path,
        help_text=_('测试用例的截屏图片')
    )
    title = models.CharField(_('图片标题'), max_length=255, blank=True, null=True)
    description = models.TextField(_('图片描述'), blank=True, null=True)
    step_number = models.PositiveIntegerField(_('对应步骤'), blank=True, null=True)
    created_at = models.DateTimeField(_('上传时间'), auto_now_add=True)

    # MCP执行相关信息
    mcp_session_id = models.CharField(_('MCP会话ID'), max_length=255, blank=True, null=True)
    page_url = models.URLField(_('页面URL'), max_length=2000, blank=True, null=True)

    # 上传人信息
    uploader = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='uploaded_screenshots',
        verbose_name=_('上传人')
    )

    class Meta:
        verbose_name = _('测试用例截屏')
        verbose_name_plural = _('测试用例截屏')
        ordering = ['test_case', 'step_number', 'created_at']

    def __str__(self):
        if self.title:
            return f"{self.test_case.name} - {self.title}"
        elif self.step_number:
            return f"{self.test_case.name} - Step {self.step_number}"
        return f"{self.test_case.name} - {self.created_at.strftime('%Y-%m-%d %H:%M:%S')}"

    def delete(self, *args, **kwargs):
        """删除模型时同时删除文件"""
        if self.screenshot:
            if os.path.isfile(self.screenshot.path):
                os.remove(self.screenshot.path)
        super().delete(*args, **kwargs)


class TestSuite(models.Model):
    """
    测试套件模型 - 用于批量执行测试用例
    """
    name = models.CharField(_('套件名称'), max_length=255)
    description = models.TextField(_('套件描述'), blank=True, null=True)
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name='test_suites',
        verbose_name=_('所属项目')
    )
    testcases = models.ManyToManyField(
        TestCase,
        related_name='test_suites',
        verbose_name=_('测试用例'),
        blank=True
    )
    creator = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_test_suites',
        verbose_name=_('创建人')
    )
    # 并发执行配置
    max_concurrent_tasks = models.PositiveSmallIntegerField(
        _('最大并发数'),
        default=1,
        help_text=_('同时执行的测试用例数量，1表示串行执行，建议值2-5')
    )
    pre_data_plan = models.ForeignKey(
        'data_generation.DataGenerationPlan',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='bound_test_suites',
        verbose_name=_('造数计划'),
        help_text=_('套件执行前自动运行的造数计划'),
    )
    pre_data_params = models.JSONField(
        _('造数参数'),
        default=dict,
        blank=True,
        help_text=_('传递给造数计划的运行时参数'),
    )
    pre_data_environment = models.ForeignKey(
        'api_environments.ApiEnvironment',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='bound_test_suites',
        verbose_name=_('造数 API 环境'),
        help_text=_('造数步骤默认使用的 API 环境'),
    )
    pre_data_fail_fast = models.BooleanField(
        _('造数失败阻断'),
        default=True,
        help_text=_('造数失败时是否阻断套件执行'),
    )
    post_data_cleanup = models.BooleanField(
        _('造数后自动清理'),
        default=False,
        help_text=_('套件执行完成后自动运行造数计划的 cleanup_steps'),
    )
    created_at = models.DateTimeField(_('创建时间'), auto_now_add=True)
    updated_at = models.DateTimeField(_('更新时间'), auto_now=True)
    
    class Meta:
        verbose_name = _('测试套件')
        verbose_name_plural = _('测试套件')
        ordering = ['-created_at']
        unique_together = ('project', 'name')
    
    def __str__(self):
        return f"{self.project.name} - {self.name}"


class TestExecution(models.Model):
    """
    测试执行记录模型 - 记录测试套件的执行情况
    """
    STATUS_CHOICES = [
        ('pending', _('等待中')),
        ('running', _('执行中')),
        ('completed', _('已完成')),
        ('failed', _('失败')),
        ('cancelled', _('已取消')),
    ]
    
    suite = models.ForeignKey(
        TestSuite,
        on_delete=models.CASCADE,
        related_name='executions',
        verbose_name=_('测试套件')
    )
    status = models.CharField(
        _('执行状态'),
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending'
    )
    executor = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name='test_executions',
        verbose_name=_('执行人')
    )
    started_at = models.DateTimeField(_('开始时间'), null=True, blank=True)
    completed_at = models.DateTimeField(_('完成时间'), null=True, blank=True)
    total_count = models.PositiveIntegerField(_('总用例数'), default=0)
    passed_count = models.PositiveIntegerField(_('通过数'), default=0)
    failed_count = models.PositiveIntegerField(_('失败数'), default=0)
    skipped_count = models.PositiveIntegerField(_('跳过数'), default=0)
    error_count = models.PositiveIntegerField(_('错误数'), default=0)
    
    # Celery任务ID,用于追踪和取消任务
    celery_task_id = models.CharField(_('任务ID'), max_length=255, blank=True, null=True)

    # 是否为功能测试用例生成Playwright脚本
    generate_playwright_script = models.BooleanField(
        _('生成脚本'),
        default=False,
        help_text=_('执行功能测试用例时是否自动生成Playwright脚本')
    )
    data_generation_run = models.ForeignKey(
        'data_generation.DataGenerationRun',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='linked_test_executions',
        verbose_name=_('造数执行记录'),
    )

    created_at = models.DateTimeField(_('创建时间'), auto_now_add=True)
    updated_at = models.DateTimeField(_('更新时间'), auto_now=True)
    
    class Meta:
        verbose_name = _('测试执行记录')
        verbose_name_plural = _('测试执行记录')
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.suite.name} - {self.get_status_display()} - {self.created_at.strftime('%Y-%m-%d %H:%M:%S')}"
    
    @property
    def duration(self):
        """计算执行时长(秒)"""
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return None
    
    @property
    def pass_rate(self):
        """计算通过率"""
        if self.total_count > 0:
            return round((self.passed_count / self.total_count) * 100, 2)
        return 0.0


class TestCaseResult(models.Model):
    """
    测试用例执行结果模型 - 记录单个用例的执行结果
    """
    STATUS_CHOICES = [
        ('pending', _('等待中')),
        ('running', _('执行中')),
        ('pass', _('通过')),
        ('fail', _('失败')),
        ('skip', _('跳过')),
        ('error', _('错误')),
    ]
    
    execution = models.ForeignKey(
        TestExecution,
        on_delete=models.CASCADE,
        related_name='results',
        verbose_name=_('测试执行')
    )
    testcase = models.ForeignKey(
        TestCase,
        on_delete=models.CASCADE,
        related_name='execution_results',
        verbose_name=_('测试用例')
    )
    status = models.CharField(
        _('执行状态'),
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending'
    )
    error_message = models.TextField(_('错误信息'), blank=True, null=True)
    stack_trace = models.TextField(_('堆栈跟踪'), blank=True, null=True)
    
    # 执行时间统计
    started_at = models.DateTimeField(_('开始时间'), null=True, blank=True)
    completed_at = models.DateTimeField(_('完成时间'), null=True, blank=True)
    execution_time = models.FloatField(_('执行耗时(秒)'), null=True, blank=True)
    
    # MCP相关信息
    mcp_session_id = models.CharField(_('MCP会话ID'), max_length=255, blank=True, null=True)
    
    # 截图信息(JSON格式存储截图路径列表)
    screenshots = models.JSONField(_('截图列表'), default=list, blank=True)
    
    # 执行日志
    execution_log = models.TextField(_('执行日志'), blank=True, null=True)
    
    created_at = models.DateTimeField(_('创建时间'), auto_now_add=True)
    updated_at = models.DateTimeField(_('更新时间'), auto_now=True)
    
    class Meta:
        verbose_name = _('测试用例执行结果')
        verbose_name_plural = _('测试用例执行结果')
        ordering = ['execution', 'created_at']
        unique_together = ('execution', 'testcase')
    
    def __str__(self):
        return f"{self.testcase.name} - {self.get_status_display()}"
    
    @property
    def duration(self):
        """计算执行时长(秒)"""
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return self.execution_time


class ManualTestRun(models.Model):
    """人工测试的分派批次，不与自动化测试执行记录混用。"""

    STATUS_CHOICES = [
        ("pending", _("待执行")),
        ("in_progress", _("执行中")),
        ("completed", _("已完成")),
    ]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="manual_test_runs", verbose_name=_("所属项目"))
    test_suite = models.ForeignKey(
        "TestSuite",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="manual_test_runs",
        verbose_name=_("来源测试套件"),
    )
    name = models.CharField(_("执行批次名称"), max_length=255)
    description = models.TextField(_("说明"), blank=True, null=True)
    environment = models.CharField(_("执行环境"), max_length=100, blank=True, default="")
    version = models.CharField(_("版本号"), max_length=100, blank=True, default="")
    deadline = models.DateTimeField(_("截止日期"), blank=True, null=True)
    status = models.CharField(_("执行状态"), max_length=20, choices=STATUS_CHOICES, default="pending")
    creator = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name="created_manual_test_runs", verbose_name=_("创建人"))
    total_count = models.PositiveIntegerField(_("用例总数"), default=0)
    passed_count = models.PositiveIntegerField(_("通过数"), default=0)
    failed_count = models.PositiveIntegerField(_("不通过数"), default=0)
    blocked_count = models.PositiveIntegerField(_("阻塞数"), default=0)
    skip_count = models.PositiveIntegerField(_("跳过数"), default=0)
    pending_count = models.PositiveIntegerField(_("待执行数"), default=0)
    created_at = models.DateTimeField(_("创建时间"), auto_now_add=True)
    updated_at = models.DateTimeField(_("更新时间"), auto_now=True)

    class Meta:
        verbose_name = _("人工用例执行批次")
        verbose_name_plural = _("人工用例执行批次")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.project.name} - {self.name}"

    def refresh_statistics(self):
        # Do not use the related-manager cache here. Assignment changes in the
        # same request must be reflected in the task counters immediately.
        assignments = ManualTestAssignment.objects.filter(run_id=self.pk)
        total_count = assignments.count()
        passed_count = assignments.filter(status="pass").count()
        failed_count = assignments.filter(status="fail").count()
        blocked_count = assignments.filter(status="blocked").count()
        skip_count = assignments.filter(status="skip").count()
        pending_count = assignments.filter(status="pending").count()
        status = "completed" if total_count and not pending_count else "in_progress" if total_count != pending_count else "pending"
        ManualTestRun.objects.filter(pk=self.pk).update(
            total_count=total_count,
            passed_count=passed_count,
            failed_count=failed_count,
            blocked_count=blocked_count,
            skip_count=skip_count,
            pending_count=pending_count,
            status=status,
        )
        self.total_count = total_count
        self.passed_count = passed_count
        self.failed_count = failed_count
        self.blocked_count = blocked_count
        self.skip_count = skip_count
        self.pending_count = pending_count
        self.status = status


class ManualTestAssignment(models.Model):
    """一条由测试人员手工确认的用例执行记录。"""

    STATUS_CHOICES = [
        ("pending", _("待执行")),
        ("pass", _("通过")),
        ("fail", _("不通过")),
        ("blocked", _("阻塞")),
        ("skip", _("跳过")),
    ]

    run = models.ForeignKey(ManualTestRun, on_delete=models.CASCADE, related_name="assignments", verbose_name=_("执行批次"))
    testcase = models.ForeignKey(
        TestCase,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="manual_test_assignments",
        verbose_name=_("测试用例"),
    )
    testcase_snapshot = models.JSONField(_("测试用例快照"), default=dict, blank=True)
    assignee = models.ForeignKey(User, on_delete=models.PROTECT, related_name="manual_test_assignments", verbose_name=_("测试人员"))
    status = models.CharField(_("执行结果"), max_length=20, choices=STATUS_CHOICES, default="pending")
    failure_reason = models.TextField(_("失败原因"), blank=True, null=True)
    comment = models.TextField(_("执行备注"), blank=True, null=True)
    step_results = models.JSONField(_("步骤执行结果"), default=list, blank=True)
    evidence_files = models.JSONField(_("失败证据"), default=list, blank=True)
    defect_title = models.CharField(_("关联缺陷标题"), max_length=255, blank=True, default="")
    defect_url = models.URLField(_("关联缺陷链接"), max_length=500, blank=True, default="")
    executed_at = models.DateTimeField(_("执行时间"), blank=True, null=True)
    created_at = models.DateTimeField(_("分派时间"), auto_now_add=True)
    updated_at = models.DateTimeField(_("更新时间"), auto_now=True)

    class Meta:
        verbose_name = _("人工用例执行记录")
        verbose_name_plural = _("人工用例执行记录")
        ordering = ["status", "created_at", "id"]
        constraints = [models.UniqueConstraint(fields=["run", "testcase"], name="unique_manual_run_testcase")]

    def __str__(self):
        testcase_name = (
            self.testcase.name
            if self.testcase_id
            else self.testcase_snapshot.get("name", f"已删除用例 #{self.testcase_snapshot.get('id', '-')}")
        )
        return f"{self.run.name} - {testcase_name}"


class TestCaseRunRecord(models.Model):
    """用例管理模块中单条用例的 AI 执行记录（与测试套件批量执行隔离）。"""

    STATUS_CHOICES = [
        ("running", _("执行中")),
        ("pass", _("通过")),
        ("fail", _("失败")),
        ("error", _("错误")),
        ("stopped", _("已停止")),
    ]

    testcase = models.ForeignKey(
        TestCase,
        on_delete=models.CASCADE,
        related_name="run_records",
        verbose_name=_("测试用例"),
    )
    executor = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name="testcase_run_records",
        verbose_name=_("执行人"),
    )
    session_id = models.CharField(_("会话 ID"), max_length=255, unique=True, db_index=True)
    status = models.CharField(
        _("执行状态"),
        max_length=20,
        choices=STATUS_CHOICES,
        default="running",
    )
    summary = models.TextField(_("结果摘要"), blank=True, default="")
    step_results = models.JSONField(_("步骤结果"), default=list, blank=True)
    execution_log = models.TextField(_("执行日志"), blank=True, default="")
    generate_playwright_script = models.BooleanField(_("生成脚本"), default=False)
    started_at = models.DateTimeField(_("开始时间"), auto_now_add=True)
    completed_at = models.DateTimeField(_("完成时间"), null=True, blank=True)

    class Meta:
        verbose_name = _("用例执行记录")
        verbose_name_plural = _("用例执行记录")
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.testcase.name} - {self.get_status_display()} - {self.started_at:%Y-%m-%d %H:%M:%S}"
