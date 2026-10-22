<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../api'
import { useToast } from '../toast'

const { show } = useToast()

// Fixed key order so the layout does not shuffle between reloads. Keys come
// from the server's _zero_counts() over RECOMMENDATION_STATUSES etc.
const STATUS_KEYS = ['pending', 'accepted', 'ignored', 'rejected', 'expired']
const RATING_KEYS = ['much_better', 'better', 'as_expected', 'worse', 'much_worse', 'unclear']
const EXEC_KEYS = ['planned', 'completed', 'partially_completed', 'not_completed', 'no_longer_relevant']
const WINDOWS = [7, 30, 90]
const RUN_LIMIT = 10

const days = ref(30)
const loading = ref(true)
const stats = ref<any>(null)
const runs = ref<any[]>([])
const statsError = ref('')
const runsError = ref('')

// The statistics service returns either None (denominator <= 0) or an object
// {numerator, denominator, value, sample_status} — see _ratio() in
// app/recommendation_stats.py:118. Rendering a missing ratio as 0% would claim
// "measured zero" when the truth is "never measured".
function fmtRatio(ratio: any): string {
  if (ratio === null || ratio === undefined) return '—'
  const value = typeof ratio === 'object' ? ratio.value : ratio
  const n = Number(value)
  return Number.isFinite(n) ? `${(n * 100).toFixed(1)}%` : '—'
}

function ratioParts(ratio: any): string {
  if (!ratio || typeof ratio !== 'object') return ''
  return `${fmtNum(ratio.numerator)}/${fmtNum(ratio.denominator)}`
}

function ratioStatus(ratio: any): string {
  return ratio && typeof ratio === 'object' ? String(ratio.sample_status || '') : ''
}

function fmtNum(value: any): string {
  if (value === null || value === undefined) return '—'
  const n = Number(value)
  return Number.isFinite(n) ? String(n) : '—'
}

function count(map: any, key: string): number {
  const n = Number(map?.[key] ?? 0)
  return Number.isFinite(n) ? n : 0
}

function errorText(value: any): string {
  const text = String(value ?? '')
  return text.length > 200 ? text.slice(0, 200) + '…' : text
}

function load() {
  loading.value = true
  statsError.value = ''
  runsError.value = ''
  const end = new Date()
  const start = new Date(end.getTime() - days.value * 86400000)
  const qs = `?start_at=${encodeURIComponent(start.toISOString())}&end_at=${encodeURIComponent(end.toISOString())}`
  Promise.allSettled([
    api('/api/recommendation-statistics' + qs),
    api(`/api/recommendation-runs?limit=${RUN_LIMIT}`),
  ]).then((results) => {
    const [s, r] = results
    if (s.status === 'fulfilled') {
      stats.value = s.value
    } else {
      stats.value = null
      statsError.value = String((s.reason as any)?.message || s.reason || '统计加载失败')
    }
    if (r.status === 'fulfilled') {
      runs.value = Array.isArray(r.value) ? (r.value as any[]) : []
    } else {
      runs.value = []
      runsError.value = String((r.reason as any)?.message || r.reason || '生成历史加载失败')
    }
    loading.value = false
  })
}

function setWindow(d: number) {
  if (days.value === d) return
  days.value = d
  load()
}

onMounted(load)
</script>

<template>
  <div class="card p-4 space-y-4" data-section="recommendation-insights">
    <div class="flex flex-wrap items-center justify-between gap-2">
      <div>
        <h3 class="font-medium">统计与生成历史</h3>
        <p class="mt-1 text-xs" style="color: rgb(var(--fg-muted))">
          只读统计；分母为 0 的比率显示「—」，代表没有观测，不代表效果为零。
        </p>
      </div>
      <div class="flex gap-1">
        <button v-for="d in WINDOWS" :key="d" class="btn-sm"
                :class="days === d ? 'btn-primary' : 'btn-secondary'"
                @click="setWindow(d)">近 {{ d }} 天</button>
        <button class="btn-secondary btn-sm" :disabled="loading" @click="load">刷新</button>
      </div>
    </div>

    <div v-if="loading" class="space-y-2">
      <div class="skeleton h-10" />
      <div class="skeleton h-10" />
    </div>

    <template v-else>
      <div v-if="statsError" class="text-xs" style="color: rgb(var(--danger))">{{ statsError }}</div>
      <div v-else-if="stats" class="space-y-4">
        <div class="grid gap-2 text-xs sm:grid-cols-4">
          <div class="rounded-md border border-border p-2">
            <div style="color: rgb(var(--fg-muted))">推荐总数</div>
            <div class="text-base font-medium">{{ fmtNum(stats.recommendations?.total) }}</div>
          </div>
          <div class="rounded-md border border-border p-2">
            <div style="color: rgb(var(--fg-muted))">反馈率</div>
            <div class="text-base font-medium">{{ fmtRatio(stats.feedback?.feedback_rate) }}</div>
            <div style="color: rgb(var(--fg-muted))">
              {{ ratioParts(stats.feedback?.feedback_rate) }} 条推荐有反馈 · 有用率 {{ fmtRatio(stats.feedback?.helpful_rate) }}
            </div>
          </div>
          <div class="rounded-md border border-border p-2">
            <div style="color: rgb(var(--fg-muted))">当前采纳率</div>
            <div class="text-base font-medium">{{ fmtRatio(stats.adoption?.current_accepted_rate) }}</div>
            <div style="color: rgb(var(--fg-muted))">采纳 {{ fmtNum(stats.adoption?.current_accepted_count) }} 条</div>
          </div>
          <div class="rounded-md border border-border p-2">
            <div style="color: rgb(var(--fg-muted))">结果登记率</div>
            <div class="text-base font-medium">{{ fmtRatio(stats.outcomes?.registration_rate) }}</div>
            <div style="color: rgb(var(--fg-muted))">已登记 {{ fmtNum(stats.outcomes?.registered_outcome_count) }} / {{ fmtNum(stats.outcomes?.current_accepted_count) }}</div>
          </div>
        </div>

        <div class="grid gap-3 sm:grid-cols-3">
          <div>
            <div class="mb-1 text-xs font-medium">状态分布</div>
            <div v-for="k in STATUS_KEYS" :key="k" class="flex justify-between text-xs">
              <span style="color: rgb(var(--fg-secondary))">{{ k }}</span>
              <span>{{ count(stats.recommendations?.status_counts, k) }}</span>
            </div>
            <div class="mt-1 flex justify-between text-xs">
              <span style="color: rgb(var(--fg-muted))">证据覆盖率</span>
              <span>{{ fmtRatio(stats.recommendations?.evidence_present_rate) }}</span>
            </div>
          </div>
          <div>
            <div class="mb-1 text-xs font-medium">执行状态</div>
            <div v-for="k in EXEC_KEYS" :key="k" class="flex justify-between text-xs">
              <span style="color: rgb(var(--fg-secondary))">{{ k }}</span>
              <span>{{ count(stats.outcomes?.execution_status_counts, k) }}</span>
            </div>
            <div class="mt-1 flex justify-between text-xs">
              <span style="color: rgb(var(--fg-muted))">完成率</span>
              <span>{{ fmtRatio(stats.outcomes?.completion_rate) }}</span>
            </div>
          </div>
          <div>
            <div class="mb-1 text-xs font-medium">效果评级</div>
            <div v-for="k in RATING_KEYS" :key="k" class="flex justify-between text-xs">
              <span style="color: rgb(var(--fg-secondary))">{{ k }}</span>
              <span>{{ count(stats.ratings?.counts, k) }} · {{ fmtRatio(stats.ratings?.rates?.[k]) }}</span>
            </div>
          </div>
        </div>

        <div>
          <div class="mb-1 text-xs font-medium">指标对比（仅统计同时给出期望值与实际值的记录）</div>
          <div v-if="!stats.outcomes?.comparable_metric_groups?.length" class="text-xs" style="color: rgb(var(--fg-muted))">
            暂无成对指标数据
          </div>
          <table v-else class="w-full text-xs">
            <thead>
              <tr style="color: rgb(var(--fg-muted))">
                <th class="text-left font-normal">指标</th>
                <th class="text-left font-normal">单位</th>
                <th class="text-right font-normal">样本</th>
                <th class="text-right font-normal">期望均值</th>
                <th class="text-right font-normal">实际均值</th>
                <th class="text-right font-normal">差值</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="g in stats.outcomes.comparable_metric_groups" :key="`${g.metric}-${g.unit}`">
                <td>{{ g.metric }}</td>
                <td>{{ g.unit }}</td>
                <td class="text-right">{{ g.sample_count }}<span v-if="g.sample_status !== 'ok'" style="color: rgb(var(--fg-muted))">（样本不足）</span></td>
                <td class="text-right">{{ g.average_expected_value }}</td>
                <td class="text-right">{{ g.average_observed_value }}</td>
                <td class="text-right">{{ g.average_difference }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="text-xs space-y-1">
          <div>
            样本状态：<span class="tag">{{ stats.quality?.sample_status }}</span>
            <span class="ml-1" style="color: rgb(var(--fg-muted))">
              推荐样本 {{ fmtNum(stats.quality?.recommendation_sample_size) }} 条（少于 10 条判为样本不足）
            </span>
          </div>
          <div v-if="stats.quality?.sample_status === 'insufficient_data'" style="color: rgb(var(--danger))">
            样本量不足，比率仅供参考，不宜据此下结论。
          </div>
          <ul class="list-disc pl-4" style="color: rgb(var(--fg-muted))">
            <li v-for="(item, i) in stats.limitations || []" :key="i">{{ item }}</li>
          </ul>
        </div>
      </div>

      <div class="border-t border-border pt-3">
        <div class="mb-2 flex items-center justify-between">
          <span class="text-xs font-medium">最近 {{ RUN_LIMIT }} 次生成</span>
          <span v-if="runsError" class="text-xs" style="color: rgb(var(--danger))">{{ runsError }}</span>
        </div>
        <div v-if="!runs.length && !runsError" class="text-xs" style="color: rgb(var(--fg-muted))">暂无生成记录</div>
        <div v-else-if="runs.length" class="overflow-x-auto">
          <table class="w-full text-xs">
            <thead>
              <tr style="color: rgb(var(--fg-muted))">
                <th class="text-left font-normal">时间</th>
                <th class="text-left font-normal">状态</th>
                <th class="text-right font-normal">窗口</th>
                <th class="text-right font-normal">候选</th>
                <th class="text-right font-normal">新增</th>
                <th class="text-right font-normal">重复</th>
                <th class="text-right font-normal">抑制</th>
                <th class="text-right font-normal">过期</th>
                <th class="text-left font-normal">模型</th>
                <th class="text-left font-normal">错误</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="run in runs" :key="run.id">
                <td>{{ String(run.started_at || '').slice(0, 16) }}</td>
                <td><span class="tag">{{ run.status }}</span></td>
                <td class="text-right">{{ fmtNum(run.window_days) }}</td>
                <td class="text-right">{{ fmtNum(run.candidates) }}</td>
                <td class="text-right">{{ fmtNum(run.created) }}</td>
                <td class="text-right">{{ fmtNum(run.duplicates) }}</td>
                <td class="text-right">{{ fmtNum(run.suppressed_rejected) }}</td>
                <td class="text-right">{{ fmtNum(run.expired_marked) }}</td>
                <td>{{ Number(run.use_model) ? '是' : '否' }}</td>
                <td :title="run.error || ''" class="font-mono" style="color: rgb(var(--danger))">{{ errorText(run.error) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>
  </div>
</template>
