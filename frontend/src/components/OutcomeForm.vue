<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../api'
import { useToast } from '../toast'

const { show } = useToast()

// Mirrors app/recommendation_feedback.py:29-59. Inline (no metadata endpoint)
// and kept in sync by tests/test_recommendation_insights_ui.py.
const EXECUTION_STATUSES = ['planned', 'completed', 'partially_completed', 'not_completed', 'no_longer_relevant']
const RESULT_RATINGS = ['much_better', 'better', 'as_expected', 'worse', 'much_worse', 'unclear']
const METRICS = [
  'documents_created', 'project_activities', 'goals_created', 'tasks_created',
  'tasks_completed', 'tasks_reopened', 'tasks_cancelled', 'net_completion_ratio',
  'connector_sync_success_rate',
]
const UNITS = ['count', 'ratio', 'minutes']
const MAX_METRICS = 10

const props = defineProps<{ recId: number; expired: boolean }>()

const loading = ref(true)
const loadError = ref('')
const saving = ref(false)
const deleting = ref(false)
const hasOutcome = ref(false)
const confirmDelete = ref(false)

const executionStatus = ref('')
const resultRating = ref('unclear')
const note = ref('')
// Rows keep only what the user edits; source/attribution/window/captured_at are
// fixed by the contract and stamped at save time.
const metrics = ref<any[]>([])

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const res: any = await api(`/api/recommendations/${props.recId}/outcome`)
    const oc = res?.outcome
    hasOutcome.value = !!oc
    executionStatus.value = oc?.execution_status || ''
    resultRating.value = oc?.result_rating || 'unclear'
    note.value = oc?.actual_effect_note || ''
    metrics.value = Array.isArray(oc?.metric_observations)
      ? oc.metric_observations.map((m: any) => ({
          metric: m?.metric || 'tasks_completed',
          unit: m?.unit || 'count',
          expected_value: m?.expected_value ?? '',
          observed_value: m?.observed_value ?? '',
        }))
      : []
  } catch (e: any) { loadError.value = e.message }
  finally { loading.value = false }
}

function addMetric() {
  if (metrics.value.length >= MAX_METRICS) return
  metrics.value.push({ metric: 'tasks_completed', unit: 'count', expected_value: '', observed_value: '' })
}

function removeMetric(index: number) {
  metrics.value.splice(index, 1)
}

function toNumberOrNull(value: any) {
  if (value === '' || value === null || value === undefined) return null
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

async function save() {
  if (saving.value || props.expired) return
  // First save requires execution_status: OutcomeDraft has no default for it.
  if (!executionStatus.value) { show('请先选择执行状态，保存结果必须填写', 'error'); return }
  if (note.value.length > 1000) { show('备注最多 1000 字', 'error'); return }

  // metric_observations is replaced wholesale by the service, so the form always
  // submits its full list (including an empty one) to make row deletion real.
  const observations = metrics.value
    .filter(m => m.metric && m.unit)
    .slice(0, MAX_METRICS)
    .map(m => ({
      metric: m.metric,
      unit: m.unit,
      expected_value: toNumberOrNull(m.expected_value),
      observed_value: toNumberOrNull(m.observed_value),
      window: {},
      source: 'user_reported',
      captured_at: new Date().toISOString(),
      attribution: 'not_inferred',
    }))

  saving.value = true
  try {
    const res: any = await api(`/api/recommendations/${props.recId}/outcome`, {
      method: 'PUT',
      body: JSON.stringify({
        execution_status: executionStatus.value,
        result_rating: resultRating.value,
        actual_effect_note: note.value,
        metric_observations: observations,
      }),
    })
    hasOutcome.value = true
    const oc = res?.outcome
    if (oc) {
      executionStatus.value = oc.execution_status || executionStatus.value
      resultRating.value = oc.result_rating || resultRating.value
      note.value = oc.actual_effect_note ?? note.value
    }
    show(res?.status === 'created' ? '结果已登记' : '结果已更新', 'success')
  } catch (e: any) { show(e.message, 'error') }
  finally { saving.value = false }
}

async function remove() {
  if (deleting.value) return
  deleting.value = true
  try {
    await api(`/api/recommendations/${props.recId}/outcome`, { method: 'DELETE' })
    hasOutcome.value = false
    confirmDelete.value = false
    executionStatus.value = ''
    resultRating.value = 'unclear'
    note.value = ''
    metrics.value = []
    show('结果已删除', 'success')
  } catch (e: any) { show(e.message, 'error') }
  finally { deleting.value = false }
}

onMounted(load)
</script>

<template>
  <div class="mt-3 space-y-2 border-t border-border pt-3" data-block="outcome">
    <div class="flex flex-wrap items-center gap-2">
      <span class="text-xs font-medium">登记实际效果</span>
      <span class="tag">{{ hasOutcome ? '已登记' : '未登记' }}</span>
      <span v-if="expired" class="text-xs" style="color: rgb(var(--danger))">建议已过期，结果不可再修改</span>
    </div>

    <div v-if="loading" class="skeleton h-10" />
    <div v-else-if="loadError" class="text-xs" style="color: rgb(var(--danger))">{{ loadError }}</div>
    <template v-else>
      <div class="grid gap-2 sm:grid-cols-2">
        <div>
          <label class="label">执行状态 *</label>
          <select v-model="executionStatus" name="execution_status" class="input text-xs">
            <option value="">未选择</option>
            <option v-for="s in EXECUTION_STATUSES" :key="s" :value="s">{{ s }}</option>
          </select>
        </div>
        <div>
          <label class="label">效果评级</label>
          <select v-model="resultRating" name="result_rating" class="input text-xs">
            <option v-for="r in RESULT_RATINGS" :key="r" :value="r">{{ r }}</option>
          </select>
        </div>
      </div>

      <div>
        <label class="label">实际效果说明</label>
        <textarea v-model="note" name="actual_effect_note" class="input text-xs" rows="2" maxlength="1000"
                  placeholder="执行后实际发生了什么（不推断因果）" />
      </div>

      <div class="space-y-2">
        <div class="flex items-center justify-between">
          <span class="text-xs" style="color: rgb(var(--fg-muted))">指标观察（{{ metrics.length }}/{{ MAX_METRICS }}）</span>
          <button class="btn-secondary btn-sm" :disabled="metrics.length >= MAX_METRICS" @click="addMetric">添加指标</button>
        </div>
        <div v-if="!metrics.length" class="text-xs" style="color: rgb(var(--fg-muted))">未添加指标，可只登记状态与评级</div>
        <div v-for="(m, i) in metrics" :key="i" class="grid items-end gap-2 sm:grid-cols-5">
          <div class="sm:col-span-2">
            <label class="label">指标</label>
            <select v-model="m.metric" name="metric" class="input text-xs">
              <option v-for="item in METRICS" :key="item" :value="item">{{ item }}</option>
            </select>
          </div>
          <div>
            <label class="label">单位</label>
            <select v-model="m.unit" name="unit" class="input text-xs">
              <option v-for="u in UNITS" :key="u" :value="u">{{ u }}</option>
            </select>
          </div>
          <div>
            <label class="label">期望值</label>
            <input v-model.number="m.expected_value" name="expected_value" type="number" step="any" class="input text-xs" />
          </div>
          <div class="flex items-end gap-1">
            <div class="flex-1">
              <label class="label">实际值</label>
              <input v-model.number="m.observed_value" name="observed_value" type="number" step="any" class="input text-xs" />
            </div>
            <button class="btn-ghost btn-sm" title="删除该指标" @click="removeMetric(i)">×</button>
          </div>
        </div>
      </div>

      <div class="flex flex-wrap items-center gap-2">
        <button class="btn-primary btn-sm" :disabled="saving || expired" @click="save">
          {{ saving ? '保存中…' : (hasOutcome ? '更新结果' : '保存结果') }}
        </button>
        <template v-if="hasOutcome">
          <template v-if="confirmDelete">
            <span class="text-xs" style="color: rgb(var(--danger))">删除后不可恢复</span>
            <button class="btn-sm" style="color: rgb(var(--danger))" :disabled="deleting" @click="remove">确认删除结果</button>
            <button class="btn-ghost btn-sm" @click="confirmDelete = false">取消</button>
          </template>
          <button v-else class="btn-ghost btn-sm" @click="confirmDelete = true">删除结果</button>
        </template>
        <span class="text-xs" style="color: rgb(var(--fg-muted))">因果关系不推断，只记录你观察到的数值</span>
      </div>
    </template>
  </div>
</template>
