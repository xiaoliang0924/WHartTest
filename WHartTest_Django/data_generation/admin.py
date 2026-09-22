from django.contrib import admin

from .models import DataGenerationPlan, DataGenerationRun


@admin.register(DataGenerationPlan)
class DataGenerationPlanAdmin(admin.ModelAdmin):
    list_display = ['id', 'name', 'project', 'target_type', 'is_active', 'updated_at']
    list_filter = ['is_active', 'target_type', 'project']
    search_fields = ['name', 'description']


@admin.register(DataGenerationRun)
class DataGenerationRunAdmin(admin.ModelAdmin):
    list_display = ['id', 'plan', 'status', 'trigger_type', 'started_at', 'finished_at']
    list_filter = ['status', 'trigger_type', 'project']
    search_fields = ['plan__name', 'error_message']
