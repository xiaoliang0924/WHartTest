from rest_framework import serializers

from .models import DataGenerationPlan, DataGenerationRun


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

    class Meta:
        model = DataGenerationPlan
        fields = [
            'id',
            'project',
            'name',
            'description',
            'target_type',
            'steps',
            'default_environment',
            'default_environment_name',
            'is_active',
            'created_by',
            'created_by_name',
            'step_count',
            'created_at',
            'updated_at',
        ]
        read_only_fields = [
            'id',
            'project',
            'created_by',
            'created_by_name',
            'step_count',
            'created_at',
            'updated_at',
        ]

    def get_step_count(self, obj):
        steps = obj.steps if isinstance(obj.steps, list) else []
        return len(steps)

    def validate_steps(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError('steps 必须是数组')
        allowed = {'api_call', 'set_env_var', 'set_public_data'}
        for index, step in enumerate(value, start=1):
            if not isinstance(step, dict):
                raise serializers.ValidationError(f'步骤 #{index} 必须是对象')
            step_type = step.get('type')
            if step_type not in allowed:
                raise serializers.ValidationError(
                    f'步骤 #{index} 类型不支持: {step_type}'
                )
            if step_type == 'api_call' and not step.get('interface_id'):
                raise serializers.ValidationError(
                    f'步骤 #{index} api_call 缺少 interface_id'
                )
        return value

    def validate_default_environment(self, value):
        if value is None:
            return value
        project_id = self.context.get('project_id')
        if project_id and value.project_id != project_id:
            raise serializers.ValidationError('默认环境必须属于当前项目')
        return value


class DataGenerationRunSerializer(serializers.ModelSerializer):
    plan_name = serializers.CharField(source='plan.name', read_only=True)
    triggered_by_name = serializers.CharField(
        source='triggered_by.username',
        read_only=True,
        default=None,
    )
    duration = serializers.SerializerMethodField()

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
            'error_message',
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


class DataGenerationRunRequestSerializer(serializers.Serializer):
    input_params = serializers.JSONField(required=False, default=dict)
