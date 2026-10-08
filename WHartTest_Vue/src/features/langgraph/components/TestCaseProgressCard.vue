<template>
  <div class="testcase-progress-card">
    <div class="progress-card-main">
      <div class="progress-title">测试步骤（{{ currentStep ? `${currentStep}/${totalSteps}` : `准备中/${totalSteps}` }}）</div>
      <div class="progress-summary">
        <span :class="{ waiting: state === 'pending', 'waiting-llm': state === 'waiting_llm' }">{{ stateLabel }}</span>
        <span>已完成 {{ completedSteps }}</span>
        <span>待开始 {{ pendingSteps }}</span>
      </div>
    </div>
    <div v-if="message" class="progress-message">{{ message }}</div>
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
  state?: 'pending' | 'running' | 'completed' | 'waiting_llm' | 'generating_ui_case';
  message?: string;
}>(), {
  startedAt: 0,
  state: 'pending',
  message: '',
});

const now = ref(Date.now());
let timer: ReturnType<typeof setInterval> | undefined;

const safeTotal = computed(() => Math.max(1, props.totalSteps || 1));
const currentStep = computed(() => Math.min(safeTotal.value, Math.max(0, props.currentStep || 0)));
const completedSteps = computed(() => (props.state === 'completed' || props.state === 'generating_ui_case')
  ? currentStep.value
  : Math.max(0, currentStep.value - 1));
const hasActiveStep = computed(() => currentStep.value > completedSteps.value
  && (props.state === 'running' || props.state === 'waiting_llm'));
const pendingSteps = computed(() => Math.max(0, safeTotal.value - completedSteps.value - (hasActiveStep.value ? 1 : 0)));
// Keep completion counts exact, while allowing the bar to show that the active
// step is underway. Otherwise step 1 stays at 0% throughout a slow model turn.
const percent = computed(() => {
  const activeStepFraction = hasActiveStep.value ? 0.5 : 0;
  return Math.min(100, Math.round(((completedSteps.value + activeStepFraction) / safeTotal.value) * 100));
});
const stateLabel = computed(() => {
  if (props.state === 'generating_ui_case') return '功能通过，UI 用例生成中';
  if (props.state === 'running') return '浏览器执行中';
  if (props.state === 'waiting_llm') return '等待模型响应';
  if (props.state === 'completed') return '正在确认下一步';
  return '等待浏览器任务';
});
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
.progress-summary span.waiting-llm { color: #d48806; }
.progress-summary span.waiting { color: #9099a5; }
.progress-message { margin-top: 7px; color: #6b7785; font-size: 12px; }
.progress-card-time { position: absolute; right: 58px; top: 14px; color: #6b7785; font-variant-numeric: tabular-nums; }
.progress-percent { position: absolute; right: 18px; top: 14px; color: #36a867; font-size: 13px; font-weight: 600; }
.progress-track { height: 6px; margin-top: 14px; overflow: hidden; border-radius: 999px; background: #edf1f3; }
.progress-value { height: 100%; border-radius: inherit; background: linear-gradient(90deg, #27c46c, #4bcf7d); transition: width .25s ease; }
</style>
