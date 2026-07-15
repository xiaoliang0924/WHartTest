from rest_framework import serializers
from .models import ApiCustomFunction


class ApiCustomFunctionSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(
        source='created_by.username', read_only=True, default=''
    )

    class Meta:
        model = ApiCustomFunction
        fields = [
            'id', 'name', 'code', 'description', 'project',
            'is_active', 'created_by', 'created_by_name',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['created_by', 'created_at', 'updated_at', 'project']

    def validate_code(self, value):
        if len(value) > 10000:
            raise serializers.ValidationError(
                "Function code must not exceed 10000 characters."
            )
        # 去掉注释行和空行后，检查是否包含 def 函数定义
        stripped_lines = [
            line for line in value.strip().splitlines()
            if line.strip() and not line.strip().startswith('#')
        ]
        if not stripped_lines or not any(line.strip().startswith('def ') for line in stripped_lines):
            raise serializers.ValidationError(
                "代码中必须包含至少一个 'def' 函数定义 (Code must contain at least one 'def' function definition)."
            )
        forbidden_keywords = [
            'eval(', 'exec(', 'execfile(',
            'import os', 'import subprocess', 'import sys',
            '__import__', 'open(', 'file(',
            'remove(', 'rmdir(', 'unlink(',
            'system(', 'popen(', 'spawn',
        ]
        for keyword in forbidden_keywords:
            if keyword in value:
                raise serializers.ValidationError(
                    f"Code must not contain dangerous function or module: {keyword}"
                )
        return value
