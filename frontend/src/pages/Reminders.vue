<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { api } from '../api'
import { useToast } from '../toast'
const { show } = useToast()

const list = ref<any[]>([])
const loading = ref(true)
const filter = ref('')

// Mirrors app/reminders.py:51-59 TRANSITIONS. The server is authoritative; this
// table only decides which buttons render. A mismatch shows up as a 409 detail,
// which the card displays verbatim instead of hiding.
const ACTIONS_BY_STATUS: Record<string, string[]> = {
  due: ['acknowledge', 'snooze', 'complete', 'dismiss', 'cancel'],
  snoozed: ['complete', 'dismiss', 'cancel'],
  acknowledged: ['complete', 'dismiss', 'cancel'],
  scheduled: ['cancel'],
}

const STATUS_LABELS: Record<string, string> = {
  scheduled: '待触发',
  due: '待处理',
  snoozed: '已延期',
  acknowledged: '已知悉',
  completed: '已完成',
  dismissed: '已忽略',
  cancelled: '已取消',
  expired: '已过期',
}

const ACTION_LABELS: Record<string, string> = {
  acknowledge: '知悉',
  snooze: '延期',
  complete: '完成',
  dismiss: '忽略',
  cancel: '取消',
}

// CANCEL_REASONS 的 UI 子集：只暴露"人手动取消"这一条语义，其余原因是
// 调度器在源对象变化时自己写的，人工不应伪造。
const CANCEL_REASON = 'user_cancelled'
const SNOOZE_PRESETS = [
  { minutes: 15, label: '15 分钟' },
  { minutes: 60, label: '1 小时' },
  { minutes: 180, label: '3 小时' },
  { minutes: 1440, label: '1 天' },
]
const MAX_SNOOZE_MINUTES = 10080 // 7 * 24 * 60，与 app/reminders.py 一致

const openPanel = ref<{ id: number; kind: 'snooze' | 'cancel' } | null>(null)
const customMinutes = ref('')
const busyId = ref<number | null>(null)
const cardError = ref<Record<number, string>>({})

function actionsFor(status: string): string[] {
  return ACTIONS_BY_STATUS[status] || []
}

function statusLabel(status: string): string {
  return STATUS_LABELS[status] || status || '—'
}

function fmtTime(value?: string | null): string {
  if (!value) return '—'
  const text = String(value).replace('T', ' ')
  return text.slice(0, 16)
}

function togglePanel(id: number, kind: 'snooze' | 'cancel') {
  customMinutes.value = ''
  openPanel.value = openPanel.value && openPanel.value.id === id && openPanel.value.kind === kind
    ? null
    : { id, kind }
}

async function load() {
  loading.value = true
  try {
    const qs = filter.value ? `?status=${filter.value}` : ''
    list.value = await api('/api/reminders' + qs)
  } catch (e: any) { show(e.message, 'error') }
  finally { loading.value = false }
}

async function submit(id: number, body: Record<string, any>) {
  busyId.value = id
  const next = { ...cardError.value }
  delete next[id]
  cardError.value = next
  try {
    await api(`/api/reminders/${id}/transition`, { method: 'POST', body: JSON.stringify(body) })
    openPanel.value = null
    show('操作成功', 'success')
    await load()
  } catch (e: any) {
    // The state machine lives on the server. Show its detail as-is and refresh,
    // so the buttons cannot keep offering an action the server just rejected.
    cardError.value = { ...cardError.value, [id]: e.message }
    show(e.message, 'error')
    await load()
  } finally { busyId.value = null }
}

async function act(id: number, action: string) {
  if (action === 'snooze' || action === 'cancel') { togglePanel(id, action as any); return }
  await submit(id, { action })
}

async function snooze(id: number, minutes: number) {
  if (!Number.isInteger(minutes) || minutes < 1 || minutes > MAX_SNOOZE_MINUTES) {
    show(`延期时长必须是 1..${MAX_SNOOZE_MINUTES} 分钟的整数`, 'error')
    return
  }
  await submit(id, { action: 'snooze', snooze_minutes: minutes })
}

async function snoozeCustom(id: number) {
  const raw = customMinutes.value.trim()
  const minutes = Number(raw)
  await snooze(id, Number.isFinite(minutes) ? Math.trunc(minutes) : NaN)
}

onMounted(load)
</script>

<template>
  <div class="max-w-4xl mx-auto p-6 space-y-6">
    <h2 class="text-xl font-semibold">提醒中心</h2>
    <div class="flex gap-2">
      <button class="btn-secondary btn-sm" :class="{ '!bg-accent-soft': !filter }" @click="filter='';load()">全部</button>
      <button class="btn-secondary btn-sm" :class="{ '!bg-accent-soft': filter==='due' }" @click="filter='due';load()">待处理</button>
      <button class="btn-secondary btn-sm" :class="{ '!bg-accent-soft': filter==='snoozed' }" @click="filter='snoozed';load()">已延期</button>
      <button class="btn-secondary btn-sm" :class="{ '!bg-accent-soft': filter==='acknowledged' }" @click="filter='acknowledged';load()">已知悉</button>
    </div>
    <div v-if="loading" class="space-y-2"><div v-for="i in 3" :key="i" class="skeleton h-16" /></div>
    <div v-else-if="!list.length" class="card p-8 text-center" style="color: rgb(var(--fg-muted))">
      暂无提醒。调度器默认关闭，开启规则后会自动生成。
    </div>
    <div v-else class="space-y-3">
      <div v-for="r in list" :key="r.id" class="card p-4" :data-reminder-id="r.id" :data-status="r.status">
        <div class="flex items-center justify-between gap-3">
          <div class="min-w-0">
            <div class="font-medium text-sm truncate">{{ r.title || '提醒' }}</div>
            <div class="text-xs mt-1" style="color: rgb(var(--fg-muted))">
              {{ r.source_type }} · 计划 {{ fmtTime(r.scheduled_at) }}
            </div>
          </div>
          <span class="tag shrink-0" :data-status-label="r.status">{{ statusLabel(r.status) }}</span>
        </div>

        <div v-if="r.content" class="text-sm mt-2" style="color: rgb(var(--fg-secondary))">{{ r.content }}</div>

        <div v-if="r.status === 'snoozed'" class="text-xs mt-2" style="color: rgb(var(--fg-muted))"
             data-field="snoozed-until">
          延期至 {{ fmtTime(r.snoozed_until) }}
        </div>

        <div v-if="cardError[r.id]" class="text-xs mt-2" data-field="card-error">
          {{ cardError[r.id] }}
        </div>

        <div v-if="actionsFor(r.status).length" class="flex flex-wrap gap-2 mt-3">
          <button
            v-for="action in actionsFor(r.status)"
            :key="action"
            :class="['btn-sm', action === 'complete' ? 'btn-primary' : (action === 'snooze' ? 'btn-secondary' : 'btn-ghost')]"
            :data-action="action"
            :disabled="busyId === r.id"
            @click="act(r.id, action)"
          >{{ ACTION_LABELS[action] || action }}</button>
        </div>

        <!-- Snooze panel: preset minutes plus a free-form value. -->
        <form
          v-if="openPanel && openPanel.id === r.id && openPanel.kind === 'snooze'"
          class="mt-3 rounded-md border border-border p-3 space-y-2"
          data-panel="snooze"
          @submit.prevent="snoozeCustom(r.id)"
        >
          <div class="text-xs" style="color: rgb(var(--fg-muted))">延期多久？（1..{{ MAX_SNOOZE_MINUTES }} 分钟）</div>
          <div class="flex flex-wrap gap-2">
            <button
              v-for="p in SNOOZE_PRESETS" :key="p.minutes" type="button"
              class="btn-secondary btn-sm" :data-snooze="p.minutes"
              :disabled="busyId === r.id"
              @click="snooze(r.id, p.minutes)"
            >{{ p.label }}</button>
          </div>
          <div class="flex gap-2">
            <input
              v-model="customMinutes" name="snooze_minutes" type="number" min="1" :max="MAX_SNOOZE_MINUTES"
              class="input flex-1 text-sm" placeholder="自定义分钟数"
            />
            <button type="submit" class="btn-primary btn-sm" :disabled="busyId === r.id">确定</button>
            <button type="button" class="btn-ghost btn-sm" @click="openPanel = null">取消</button>
          </div>
        </form>

        <div
          v-if="openPanel && openPanel.id === r.id && openPanel.kind === 'cancel'"
          class="mt-3 flex flex-wrap items-center gap-2 rounded-md border border-border p-3"
          data-panel="cancel"
        >
          <span class="text-xs" style="color: rgb(var(--fg-muted))">确认取消这条提醒？（原因：{{ CANCEL_REASON }}）</span>
          <button class="btn-secondary btn-sm" :disabled="busyId === r.id"
                  data-action="cancel-confirm"
                  @click="submit(r.id, { action: 'cancel', cancel_reason: CANCEL_REASON })">确认取消</button>
          <button class="btn-ghost btn-sm" @click="openPanel = null">返回</button>
        </div>
      </div>
    </div>
  </div>
</template>
