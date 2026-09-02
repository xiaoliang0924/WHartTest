<template>
  <a-card class="template-card" :bordered="true">
    <template #title>
      <div class="template-card-title" :title="template.name">{{ template.name }}</div>
    </template>

    <div class="template-card-body">
      <p class="template-desc">{{ template.description || '暂无描述' }}</p>

      <div class="template-form-area">
        <a-form v-if="paramEntries.length" layout="vertical" size="small" class="template-form">
          <a-form-item
            v-for="[key, schema] in paramEntries"
            :key="key"
            :label="schema.label || key"
          >
            <a-input
              v-if="schema.type !== 'number'"
              :model-value="String(formValues?.[key] ?? schema.default ?? '')"
              @update:model-value="(val) => updateField(key, val)"
            />
            <a-input-number
              v-else
              :model-value="Number(formValues?.[key] ?? schema.default ?? 0)"
              style="width: 100%"
              @update:model-value="(val) => updateField(key, val ?? 0)"
            />
          </a-form-item>
        </a-form>
        <div v-else class="template-no-params">无需填写参数，直接执行即可</div>
      </div>

      <a-button
        type="primary"
        long
        class="template-run-btn"
        :loading="loading"
        @click="emit('run')"
      >
        一键执行
      </a-button>
    </div>
  </a-card>
</template>

<script setup lang="ts">
import { computed } from 'vue';
import type { DataGenerationTemplate } from '@/features/data-generation/services/dataGenerationService';

const props = defineProps<{
  template: DataGenerationTemplate;
  formValues?: Record<string, unknown>;
  loading?: boolean;
}>();

const emit = defineEmits<{
  (e: 'run'): void;
}>();

const paramEntries = computed(() => Object.entries(props.template.params_schema || {}));

function updateField(key: string, value: unknown) {
  if (!props.formValues) return;
  props.formValues[key] = value;
}
</script>

<style scoped>
.template-card {
  height: 100%;
  display: flex;
  flex-direction: column;
}

.template-card :deep(.arco-card-body) {
  flex: 1;
  display: flex;
  flex-direction: column;
  padding: 12px 16px 16px;
}

.template-card-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 14px;
}

.template-card-body {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 220px;
}

.template-desc {
  margin: 0 0 12px;
  min-height: 40px;
  max-height: 40px;
  overflow: hidden;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  color: var(--color-text-2);
  font-size: 13px;
  line-height: 20px;
}

.template-form-area {
  flex: 1;
  min-height: 72px;
}

.template-form :deep(.arco-form-item) {
  margin-bottom: 8px;
}

.template-no-params {
  display: flex;
  align-items: center;
  height: 72px;
  padding: 0 12px;
  border-radius: 6px;
  background: var(--color-fill-1);
  color: var(--color-text-3);
  font-size: 13px;
}

.template-run-btn {
  margin-top: auto;
}
</style>
