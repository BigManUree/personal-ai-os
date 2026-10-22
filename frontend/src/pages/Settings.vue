<script setup lang="ts">
import { ref, onMounted, reactive } from 'vue'
import { api } from '../api'
import { useToast } from '../toast'
const { show } = useToast()

// Field names mirror app/reminders.py::SETTINGS_FIELDS. The API rejects unknown
// keys, so the payload is built from this whitelist instead of the whole object
// (the GET response also carries created_at/updated_at).
const EDITABLE = [
  'timezone',
  'task_due_enabled', 'task_stalled_enabled', 'recommendation_followup_enabled',
  'daily_review_enabled', 'daily_review_at',
  'weekly_review_enabled', 'weekly_review_weekday', 'weekly_review_at',
  'quiet_hours_enabled', 'quiet_start', 'quiet_end',
] as const

type EditableField = typeof EDITABLE[number]

const settings = reactive<Record<EditableField, any>>({
  timezone: 'Asia/Shanghai',
  task_due_enabled: false,
  task_stalled_enabled: false,
  recommendation_followup_enabled: false,
  daily_review_enabled: false,
  daily_review_at: '20:00',
  weekly_review_enabled: false,
  weekly_review_weekday: 5,
  weekly_review_at: '20:00',
  quiet_hours_enabled: false,
  quiet_start: '22:00',
  quiet_end: '08:00',
})

const WEEKDAYS = [
  { value: 1, label: '周一' }, { value: 2, label: '周二' }, { value: 3, label: '周三' },
  { value: 4, label: '周四' }, { value: 5, label: '周五' }, { value: 6, label: '周六' },
  { value: 7, label: '周日' },
]

const loading = ref(true)
const saving = ref(false)
const updatedAt = ref('')

async function load() {
  loading.value = true
  try {
    const s: any = await api('/api/reminder-settings')
    for (const key of EDITABLE) {
      if (s[key] !== undefined && s[key] !== null) settings[key] = s[key]
    }
    settings.weekly_review_weekday = Number(settings.weekly_review_weekday) || 5
    updatedAt.value = s.updated_at || ''
  } catch (e: any) { show(e.message, 'error') }
  finally { loading.value = false }
}

async function save() {
  saving.value = true
  try {
    const payload: Record<string, any> = {}
    for (const key of EDITABLE) {
      payload[key] = key === 'weekly_review_weekday'
        ? Number(settings[key])
        : settings[key]
    }
    const updated: any = await api('/api/reminder-settings', {
      method: 'PUT',
      body: JSON.stringify(payload),
    })
    updatedAt.value = updated.updated_at || ''
    show('设置已保存', 'success')
  } catch (e: any) { show(e.message, 'error') }
  finally { saving.value = false }
}

onMounted(load)
</script>

<template>
  <div class="max-w-2xl mx-auto p-6 space-y-6">
    <div class="flex items-center justify-between">
      <h2 class="text-xl font-semibold">设置</h2>
      <span v-if="updatedAt" class="text-xs" style="color: rgb(var(--fg-muted))">
        最近更新 {{ updatedAt.slice(0, 16).replace('T', ' ') }}
      </span>
    </div>

    <div v-if="loading" class="skeleton h-64" />
    <template v-else>
      <div class="card p-6 space-y-5">
        <h3 class="font-medium">提醒规则</h3>

        <div class="space-y-2">
          <label class="flex items-center gap-2 text-sm cursor-pointer">
            <input type="checkbox" v-model="settings.daily_review_enabled" /> 每日回顾
          </label>
          <div v-if="settings.daily_review_enabled" class="flex items-center gap-2 pl-6">
            <span class="text-xs" style="color: rgb(var(--fg-muted))">时间</span>
            <input v-model="settings.daily_review_at" class="input w-28" placeholder="20:00" />
          </div>
        </div>

        <div class="space-y-2">
          <label class="flex items-center gap-2 text-sm cursor-pointer">
            <input type="checkbox" v-model="settings.weekly_review_enabled" /> 每周回顾
          </label>
          <div v-if="settings.weekly_review_enabled" class="flex items-center gap-2 pl-6">
            <select v-model.number="settings.weekly_review_weekday" class="input w-28">
              <option v-for="d in WEEKDAYS" :key="d.value" :value="d.value">{{ d.label }}</option>
            </select>
            <input v-model="settings.weekly_review_at" class="input w-28" placeholder="20:00" />
          </div>
        </div>

        <label class="flex items-center gap-2 text-sm cursor-pointer">
          <input type="checkbox" v-model="settings.task_due_enabled" /> 任务截止临近 / 逾期提醒
        </label>
        <label class="flex items-center gap-2 text-sm cursor-pointer">
          <input type="checkbox" v-model="settings.task_stalled_enabled" /> 任务停滞提醒
        </label>
        <label class="flex items-center gap-2 text-sm cursor-pointer">
          <input type="checkbox" v-model="settings.recommendation_followup_enabled" /> 建议采纳后跟进
        </label>
      </div>

      <div class="card p-6 space-y-4">
        <h3 class="font-medium">安静时段</h3>
        <label class="flex items-center gap-2 text-sm cursor-pointer">
          <input type="checkbox" v-model="settings.quiet_hours_enabled" /> 启用安静时段（到期提醒顺延）
        </label>
        <div v-if="settings.quiet_hours_enabled" class="flex gap-4">
          <div class="flex-1">
            <label class="label">开始</label>
            <input v-model="settings.quiet_start" class="input" placeholder="22:00" />
          </div>
          <div class="flex-1">
            <label class="label">结束</label>
            <input v-model="settings.quiet_end" class="input" placeholder="08:00" />
          </div>
        </div>
        <p class="text-xs" style="color: rgb(var(--fg-muted))">
          开始与结束不能相同；跨零点（如 22:00–08:00）按跨夜处理。
        </p>
      </div>

      <div class="card p-6 space-y-3">
        <h3 class="font-medium">时区</h3>
        <input v-model="settings.timezone" class="input" placeholder="Asia/Shanghai" />
        <p class="text-xs" style="color: rgb(var(--fg-muted))">
          IANA 时区名，用于计算回顾时间与安静时段。
        </p>
      </div>

      <div class="flex items-center gap-3">
        <button class="btn-primary" @click="save" :disabled="saving">
          {{ saving ? '保存中…' : '保存设置' }}
        </button>
        <button class="btn-secondary" @click="load" :disabled="saving">重新加载</button>
      </div>

      <div class="card p-4 text-xs" style="color: rgb(var(--fg-muted))">
        🔐 数据保存在本机。API Key 和令牌加密存储，不在此页面显示。
        调度器和通知服务默认关闭（REMINDER_SCHEDULER_ENABLED=0），修改规则后需在启动环境里显式启用才会生效。
      </div>
    </template>
  </div>
</template>
