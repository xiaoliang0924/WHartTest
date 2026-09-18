<template>
  <div class="testcase-progress-card">
    <div class="progress-card-main">
      <div class="progress-title">测试步骤（{{ currentStep }}/{{ totalSteps }}）</div>
      <div class="progress-summary">
        <span>进行中 1</span>
        <span>已完成 {{ completedSteps }}</span>
        <span>待开始 {{ pendingSteps }}</span>
      </div>
    </div>
    <div class="progress-card-time">{{ elapsedText }}</div>
    <div class="progress-percent">{{ percent }}%</div>
    <div class="progress-track" aria-label="测试执行进度">
      <div class="progress-value" :style="{ width: `${percent}%` }"></div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';

const props = withDefaults(defineProps<{
  currentStep: number;
  totalSteps: number;
  startedAt?: number;
}>(), {
  startedAt: 0,
});

const now = ref(Date.now());
let timer: ReturnType<typeof setInterval> | undefined;

const safeTotal = computed(() => Math.max(1, props.totalSteps || 1));
const currentStep = computed(() => Math.min(safeTotal.value, Math.max(1, props.currentStep || 1)));
const completedSteps = computed(() => Math.max(0, currentStep.value - 1));
const pendingSteps = computed(() => Math.max(0, safeTotal.value - currentStep.value));
const percent = computed(() => Math.round((currentStep.value / safeTotal.value) * 100));
const elapsedText = computed(() => {
  if (!props.startedAt) return '执行中';
  const seconds = Math.max(0, Math.floor((now.value - props.startedAt) / 1000));
  const minutes = Math.floor(seconds / 60);
  return `${String(minutes).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
});

onMounted(() => { timer = setInterval(() => { now.value = Date.now(); }, 1000); });
onBeforeUnmount(() => { if (timer) clearInterval(timer); });
</script>

<style scoped>
.testcase-progress-card { position: relative; margin: 0 18px 10px; padding: 14px 18px 16px; border: 1px solid #e6edf1; border-radius: 10px; background: #fff; box-shadow: 0 2px 10px rgb(31 56 88 / 7%); }
.progress-card-main { display: flex; align-items: baseline; gap: 18px; }
.progress-title { color: #25313d; font-size: 15px; font-weight: 600; }
.progress-summary { display: flex; gap: 12px; color: #9099a5; font-size: 12px; }
.progress-summary span:first-child { color: #36a867; }
.progress-card-time { position: absolute; right: 58px; top: 14px; color: #6b7785; font-variant-numeric: tabular-nums; }
.progress-percent { position: absolute; right: 18px; top: 14px; color: #36a867; font-size: 13px; font-weight: 600; }
.progress-track { height: 6px; margin-top: 14px; overflow: hidden; border-radius: 999px; background: #edf1f3; }
.progress-value { height: 100%; border-radius: inherit; background: linear-gradient(90deg, #27c46c, #4bcf7d); transition: width .25s ease; }
</style>
