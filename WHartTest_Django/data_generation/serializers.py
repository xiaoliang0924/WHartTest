from rest_framework import serializers

from .models import DataGenerationPlan, DataGenerationRun
from .plan_validation import ensure_plan_has_environment


class DataGenerationPlanSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(
        source='created_by.username',
        read_only=True,
        default=None,
    )
    default_environment_name = serializers.CharField(
        source='default_environment.name',
        read_only=True,
        default=None,
    )
    step_count = serializers.SerializerMethodField()
    cleanup_step_count = serializers.SerializerMethodField()

    class Meta:
        model = DataGenerationPlan
        fields = [
            'id',
            'project',
            'name',
            'description',
            'target_type',
            'steps',
            'cleanup_steps',
            'default_environment',
            'default_environment_name',
            'is_active',
            'is_template',
            'template_key',
            'template_icon',
            'template_params_schema',
            'template_bindings',
            'created_by',
            'created_by_name',
            'step_count',
            'cleanup_step_count',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'project',
            'created_by',
            'created_by_name',
            'step_count',
            'cleanup_step_count',
            'created_at',
            'updated_at',
        ]

    def get_step_count(self, obj):
        steps = obj.steps if isinstance(obj.steps, list) else []
        return len(steps)

    def get_cleanup_step_count(self, obj):
        steps = obj.cleanup_steps if isinstance(obj.cleanup_steps, list) else []
        return len(steps)

    def validate_name(self, value):
        project_id = self.context.get('project_id')
        queryset = DataGenerationPlan.objects.filter(
            project_id=project_id,
            name=value,
        )
        if self.instance:
            queryset = queryset.exclude(pk=self.instance.pk)
        if project_id and queryset.exists():
            raise serializers.ValidationError('当前项目已存在同名造数计划')
        return value

    def validate_steps(self, value):
        return self._validate_step_list(value, field_name='steps')

    def validate_cleanup_steps(self, value):
        if value in (None, ''):
            return []
        return self._validate_step_list(value, field_name='cleanup_steps')

    def _validate_step_list(self, value, field_name: str):
        if not isinstance(value, list):
            raise serializers.ValidationError(f'{field_name} 必须是数组')
        allowed = {
            'api_call',
            'set_env_var',
            'set_public_data',
            'sql',
            'custom_function',
            'delay',
        }
        for index, step in enumerate(value, start=1):
            if not isinstance(step, dict):
                raise serializers.ValidationError(f'{field_name} 步骤 #{index} 必须是对象')
            step_type = step.get('type')
            if step_type not in allowed:
                raise serializers.ValidationError(
                    f'{field_name} 步骤 #{index} 类型不支持: {step_type}'
                )
            if step_type == 'api_call' and not step.get('interface_id') and not step.get('interface_ref'):
                raise serializers.ValidationError(
                    f'{field_name} 步骤 #{index} api_call 缺少 interface_id 或 interface_ref'
                )
            if step_type == 'sql' and not step.get('database_config_id') and not step.get('database_config_ref'):
                raise serializers.ValidationError(
                    f'{field_name} 步骤 #{index} sql 缺少 database_config_id 或 database_config_ref'
                )
            if step_type == 'custom_function' and not step.get('function_id') and not step.get('function_ref'):
                raise serializers.ValidationError(
                    f'{field_name} 步骤 #{index} custom_function 缺少 function_id 或 function_ref'
                )
        return value

    def validate_default_environment(self, value):
        if value is None:
            return value
        project_id = self.context.get('project_id')
        if project_id and str(value.project_id) != str(project_id):
            raise serializers.ValidationError('默认环境必须属于当前项目')
        return value

    def validate(self, attrs):
        steps = attrs.get('steps')
        if steps is None and self.instance is not None:
            steps = self.instance.steps
        cleanup_steps = attrs.get('cleanup_steps')
        if cleanup_steps is None and self.instance is not None:
            cleanup_steps = self.instance.cleanup_steps

        default_environment = attrs.get('default_environment')
        if default_environment is None and self.instance is not None:
            default_environment = self.instance.default_environment

        default_environment_id = (
            default_environment.id if default_environment is not None else None
        )
        ensure_plan_has_environment(
            steps=steps,
            cleanup_steps=cleanup_steps,
            default_environment_id=default_environment_id,
        )
        return attrs


class DataGenerationRunSerializer(serializers.ModelSerializer):
    plan_name = serializers.CharField(source='plan.name', read_only=True)
    triggered_by_name = serializers.CharField(
        source='triggered_by.username',
        read_only=True,
        default=None,
    )
    duration = serializers.SerializerMethodField()
    failed_step_index = serializers.SerializerMethodField()

    class Meta:
        model = DataGenerationRun
        fields = [
            'id',
            'plan',
            'plan_name',
            'project',
            'status',
            'trigger_type',
            'test_execution',
            'input_params',
            'output_snapshot',
            'step_logs',
            'failed_step_index',
            'error_message',
            'is_cleaned',
            'cleanup_status',
            'cleanup_logs',
            'cleanup_error_message',
            'parent_run',
            'triggered_by',
            'triggered_by_name',
            'started_at',
            'finished_at',
            'duration',
            'created_at',
        ]
        read_only_fields = fields

    def get_duration(self, obj):
        if obj.started_at and obj.finished_at:
            return (obj.finished_at - obj.started_at).total_seconds()
        return None

    def get_failed_step_index(self, obj):
        for entry in obj.step_logs or []:
            if isinstance(entry, dict) and entry.get('status') == 'failed':
                return entry.get('index')
        return None


class DataGenerationRunRequestSerializer(serializers.Serializer):
    input_params = serializers.JSONField(required=False, default=dict)


class DataGenerationGeneratePlanSerializer(serializers.Serializer):
    description = serializers.CharField(required=True, allow_blank=False, max_length=2000)
    default_environment = serializers.IntegerField(required=False, allow_null=True)
    suite_id = serializers.IntegerField(required=False, allow_null=True)
    use_llm = serializers.BooleanField(required=False, default=True)


class DataGenerationAnalyzeSuiteSerializer(serializers.Serializer):
    suite_id = serializers.IntegerField(required=True)
    environment_id = serializers.IntegerField(required=False, allow_null=True)


class DataGenerationTemplateRunSerializer(serializers.Serializer):
    template_key = serializers.CharField(required=True)
    input_params = serializers.JSONField(required=False, default=dict)
    default_environment = serializers.IntegerField(required=False, allow_null=True)
