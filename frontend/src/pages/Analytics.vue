<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { api } from '../api'
import { useToast } from '../toast'
const { show } = useToast()

const TIMEZONE = 'Asia/Shanghai'

const overview = ref<any>(null)
const loading = ref(true)

const comparison = ref<any>(null)
const compareDays = ref(7)
const compareLoading = ref(false)
const compareError = ref('')

const METRIC_LABELS: Record<string, string> = {
  documents_created: '新建资料',
  project_activities: '项目进展',
  goals_created: '新建目标',
  tasks_created: '新建任务',
  tasks_completed: '完成任务',
  tasks_reopened: '重开任务',
  tasks_cancelled: '取消任务',
  connector_sync_attempts: '连接器同步尝试',
  connector_sync_successes: '同步成功',
  connector_sync_failures: '同步失败',
}

const STATUS_LABELS: Record<string, string> = {
  ok: '可比',
  insufficient_data: '样本不足',
  no_data: '无数据',
}

// percent_change is null when the previous window is zero: a percentage of zero
// has no value, so rendering 0% would claim "no change" instead of "undefined".
function fmtPercent(value: any): string {
  if (value === null || value === undefined) return '—'
  const n = Number(value)
  return Number.isFinite(n) ? `${(n * 100).toFixed(1)}%` : '—'
}

function fmtDelta(value: any): string {
  if (value === null || value === undefined) return '—'
  const n = Number(value)
  return Number.isFinite(n) && n > 0 ? `+${n}` : String(value)
}

function arrow(direction: string): string {
  if (direction === 'up') return '↑'
  if (direction === 'down') return '↓'
  return '→'
}

function metricsOf(payload: any): Array<[string, any]> {
  return Object.entries(payload?.metrics || {})
}

function fmtRange(window: any): string {
  if (!window) return '—'
  return `${String(window.start).slice(0, 10)} → ${String(window.end).slice(0, 10)}`
}

async function load() {
  loading.value = true
  try { overview.value = await api('/api/analytics/overview?days=30&timezone=' + TIMEZONE) }
  catch (e: any) { show(e.message, 'error') }
  finally { loading.value = false }
}

async function loadComparison(days = compareDays.value) {
  compareDays.value = days
  compareLoading.value = true
  compareError.value = ''
  try {
    comparison.value = await api(`/api/analytics/comparison?window_days=${days}&timezone=${TIMEZONE}`)
  } catch (e: any) {
    // Keep the previous table visible: a rejected window must not look like
    // "no activity".
    compareError.value = e.message
    show(e.message, 'error')
  } finally { compareLoading.value = false }
}

onMounted(() => { load(); loadComparison() })
</script>

<template>
  <div class="max-w-4xl mx-auto p-6 space-y-6">
    <h2 class="text-xl font-semibold">行为洞察</h2>
    <p class="text-sm" style="color: rgb(var(--fg-secondary))">基于事件日志的可核验统计，不调用模型。</p>

    <div v-if="loading" class="space-y-2"><div v-for="i in 3" :key="i" class="skeleton h-24" /></div>
    <div v-else-if="!overview" class="card p-8 text-center" style="color: rgb(var(--fg-muted))">暂无数据</div>
    <template v-else>
      <!-- Metric cards -->
      <div class="grid grid-cols-2 sm:grid-cols-3 gap-4">
        <div v-for="(m, k) in overview.metrics" :key="k" class="card p-4" v-if="m && m.value != null">
          <div class="text-xs" style="color: rgb(var(--fg-muted))">{{ k }}</div>
          <div class="text-xl font-semibold mt-1">
            {{ m.unit === 'ratio' ? Math.round((m.value || 0) * 100) + '%' : m.value }}
          </div>
        </div>
      </div>
      <!-- Daily table -->
      <div class="card p-4">
        <h3 class="font-medium mb-3">每日趋势</h3>
        <div v-if="!overview.daily?.length" class="text-sm py-4 text-center" style="color: rgb(var(--fg-muted))">无每日数据</div>
        <div v-else class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead><tr class="text-left" style="color: rgb(var(--fg-muted))">
              <th class="pb-2">日期</th><th class="pb-2">新建</th><th class="pb-2">完成</th><th class="pb-2">资料</th>
            </tr></thead>
            <tbody>
              <tr v-for="d in overview.daily.slice(-10).reverse()" :key="d.date" class="border-t border-border">
                <td class="py-2">{{ d.date }}</td>
                <td>{{ d.tasks_created || 0 }}</td>
                <td>{{ d.tasks_completed || 0 }}</td>
                <td>{{ d.documents_created || 0 }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>

    <!-- Period comparison: two adjacent equal windows. -->
    <section class="card p-4" data-section="comparison">
      <div class="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h3 class="font-medium">周期对比</h3>
        <div class="flex gap-2">
          <button
            v-for="d in [7, 30]" :key="d"
            class="btn-secondary btn-sm" :class="{ '!bg-accent-soft': compareDays === d }"
            :data-window="d"
            @click="loadComparison(d)"
          >最近 {{ d }} 天 vs 前 {{ d }} 天</button>
        </div>
      </div>

      <div v-if="compareError" class="text-xs mb-2" data-field="comparison-error">{{ compareError }}</div>

      <div v-if="compareLoading && !comparison" class="space-y-2">
        <div v-for="i in 3" :key="i" class="skeleton h-10" />
      </div>
      <div v-else-if="!comparison" class="text-sm py-4 text-center" style="color: rgb(var(--fg-muted))">
        暂无对比数据
      </div>
      <template v-else>
        <div class="text-xs mb-3 space-y-1" style="color: rgb(var(--fg-muted))">
          <div data-field="current-range">本期：{{ fmtRange(comparison.current) }}</div>
          <div data-field="previous-range">上期：{{ fmtRange(comparison.previous) }}</div>
          <div>口径版本：{{ comparison.definition_version }} · 时区 {{ comparison.timezone }}</div>
        </div>

        <div v-if="!metricsOf(comparison).length" class="text-sm py-4 text-center" style="color: rgb(var(--fg-muted))">
          无可比指标
        </div>
        <div v-else class="overflow-x-auto">
          <table class="w-full text-sm" data-table="comparison">
            <thead>
              <tr class="text-left" style="color: rgb(var(--fg-muted))">
                <th class="pb-2">指标</th>
                <th class="pb-2 text-right">上期</th>
                <th class="pb-2 text-right">本期</th>
                <th class="pb-2 text-right">变化量</th>
                <th class="pb-2 text-right">幅度</th>
                <th class="pb-2">判定</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="[key, m] in metricsOf(comparison)" :key="key" class="border-t border-border"
                  :data-metric="key">
                <td class="py-2">{{ METRIC_LABELS[key] || key }}</td>
                <td class="text-right">{{ m.previous?.value ?? 0 }}</td>
                <td class="text-right">{{ m.current?.value ?? 0 }}</td>
                <td class="text-right">{{ fmtDelta(m.delta) }}</td>
                <td class="text-right" :data-percent="key">{{ fmtPercent(m.percent_change) }}</td>
                <td class="whitespace-nowrap">
                  <span class="text-xs mr-1" :data-direction="m.direction">{{ arrow(m.direction) }}</span>
                  <span v-if="m.significant" class="tag">显著</span>
                  <span v-else class="text-xs" style="color: rgb(var(--fg-muted))">
                    {{ STATUS_LABELS[m.status] || m.status }}
                  </span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
    </section>
  </div>
</template>
