<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { api } from '../api'
import { useToast } from '../toast'
import OutcomeForm from '../components/OutcomeForm.vue'
import RecommendationInsights from '../components/RecommendationInsights.vue'
const { show } = useToast()

// Backend contract (app/recommendations.py):
//   accept : pending  -> accepted
//   ignore : pending  -> ignored
//   reject : pending  -> rejected   (30 天冷却)
//   reopen : accepted -> pending
// The list endpoint hides rejected/expired unless an explicit status is passed.
const TABS = [
  { value: '', label: '进行中' },
  { value: 'pending', label: '待处理' },
  { value: 'accepted', label: '已采纳' },
  { value: 'ignored', label: '已忽略' },
  { value: 'rejected', label: '已拒绝' },
  { value: 'expired', label: '已过期' },
]
const GEN_WINDOWS = [7, 14, 30]

const list = ref<any[]>([])
const total = ref(0)
const loading = ref(true)
const statusFilter = ref('')
const busyId = ref<number | null>(null)
const confirmRejectId = ref<number | null>(null)

const feedbacks = ref<Record<number, any>>({})
const feedbackOpen = ref<number | null>(null)
const feedbackNote = ref('')
const feedbackVotes = ref<Record<number, string>>({})
const submittingVote = ref<number | null>(null)
const submittingNote = ref<number | null>(null)

const genOpen = ref(false)
const genDays = ref(7)
const genUseModel = ref(false)
const genLoading = ref(false)
const lastGen = ref<any>(null)

const insightsOpen = ref(false)

// Shortcuts cannot move an accepted-but-expired recommendation: the outcome
// endpoints reject it with 409, so the form disables saving up front.
function isExpired(r: any): boolean {
  if (!r?.expires_at) return false
  const ts = new Date(r.expires_at).getTime()
  return Number.isFinite(ts) && ts < Date.now()
}

async function load() {
  loading.value = true
  try {
    const qs = statusFilter.value ? `?status=${statusFilter.value}` : ''
    const res: any = await api('/api/recommendations' + qs)
    list.value = res.items || []
    total.value = typeof res.total === 'number' ? res.total : list.value.length

    // Fetch feedback in parallel: the previous serial loop was O(N) round-trips.
    const pairs = await Promise.all(list.value.map(async (r) => {
      try {
        const fb: any = await api(`/api/recommendations/${r.id}/feedback`)
        return [r.id, fb.feedback] as const
      } catch {
        return [r.id, null] as const
      }
    }))
    feedbacks.value = Object.fromEntries(pairs) as Record<number, any>
    feedbackVotes.value = {}
    for (const r of list.value) {
      const vote = feedbacks.value[r.id]?.vote
      if (vote) feedbackVotes.value[r.id] = vote
    }
  } catch (e: any) { show(e.message, 'error') }
  finally { loading.value = false }
}

function setFilter(value: string) {
  if (statusFilter.value === value) return
  statusFilter.value = value
  confirmRejectId.value = null
  load()
}

function needsReloadAfter(updated: any): boolean {
  const status = updated?.status
  if (!status) return true
  if (!statusFilter.value) return ['ignored', 'rejected', 'expired'].includes(status)
  return status !== statusFilter.value
}

async function act(r: any, action: string) {
  if (busyId.value !== null) return
  busyId.value = r.id
  try {
    const updated: any = await api(`/api/recommendations/${r.id}/${action}`, { method: 'POST' })
    const label: Record<string, string> = { accept: '已采纳', ignore: '已忽略', reject: '已拒绝', reopen: '已撤回采纳' }
    show(label[action] || '操作成功', 'success')
    confirmRejectId.value = null
    const idx = list.value.findIndex(item => item.id === r.id)
    if (idx >= 0) list.value[idx] = { ...list.value[idx], ...updated }
    if (needsReloadAfter(updated)) await load()
  } catch (e: any) {
    // 409 = 状态已被其它操作改变，刷新让用户看到真实状态。
    show(e.message, 'error')
    await load()
  } finally {
    busyId.value = null
  }
}

async function generate() {
  if (genLoading.value) return
  genLoading.value = true
  try {
    const res: any = await api('/api/recommendations/generate', {
      method: 'POST',
      body: JSON.stringify({ days: genDays.value, use_model: genUseModel.value }),
    })
    lastGen.value = res
    const created = typeof res.created === 'number' ? res.created : (res.run?.created_count ?? 0)
    show(`生成完成：${res.status}，新增 ${created} 条`, 'success')
    // Keep the panel open so the window / gaps summary stays visible.
    statusFilter.value = ''
    await load()
  } catch (e: any) { show(e.message, 'error') }
  finally { genLoading.value = false }
}

async function vote(recId: number, vote: string) {
  if (submittingVote.value === recId) return
  submittingVote.value = recId
  try {
    await api(`/api/recommendations/${recId}/feedback`, {
      method: 'PUT',
      body: JSON.stringify({ vote })
    })
    feedbackVotes.value[recId] = vote
    show('反馈已提交', 'success')
  } catch (e: any) { show(e.message, 'error') }
  finally { submittingVote.value = null }
}

function toggleFeedback(recId: number) {
  feedbackOpen.value = feedbackOpen.value === recId ? null : recId
  feedbackNote.value = feedbacks.value[recId]?.note || ''
}

async function submitNote(recId: number) {
  if (submittingNote.value === recId) return
  const currentVote = feedbackVotes.value[recId]
  if (!currentVote) {
    show('请先选择"有用"或"无用"，再添加备注', 'error')
    return
  }
  submittingNote.value = recId
  try {
    await api(`/api/recommendations/${recId}/feedback`, {
      method: 'PUT',
      body: JSON.stringify({ vote: currentVote, note: feedbackNote.value })
    })
    show('备注已保存', 'success')
    feedbackOpen.value = null
  } catch (e: any) { show(e.message, 'error') }
  finally { submittingNote.value = null }
}

onMounted(load)
</script>

<template>
  <div class="max-w-4xl mx-auto p-6 space-y-6">
    <div class="flex items-center justify-between gap-3">
      <div>
        <h2 class="text-xl font-semibold">智能建议</h2>
        <p class="text-xs mt-1" style="color: rgb(var(--fg-muted))">
          共 {{ total }} 条 · 建议由确定性规则生成，可采纳/忽略/拒绝；拒绝后 30 天内不再生成同类建议。
        </p>
      </div>
      <div class="flex gap-2">
        <button class="btn-secondary btn-sm" @click="insightsOpen = !insightsOpen">
          {{ insightsOpen ? '收起统计' : '统计与历史' }}
        </button>
        <button class="btn-secondary btn-sm" @click="genOpen = !genOpen">
          {{ genOpen ? '收起' : '生成建议' }}
        </button>
      </div>
    </div>

    <div v-if="genOpen" class="card p-4 space-y-3">
      <div class="flex flex-wrap items-end gap-3">
        <div>
          <label class="label">统计窗口</label>
          <select v-model.number="genDays" class="input w-28">
            <option v-for="d in GEN_WINDOWS" :key="d" :value="d">近 {{ d }} 天</option>
          </select>
        </div>
        <label class="flex items-center gap-2 text-sm cursor-pointer pb-2">
          <input type="checkbox" v-model="genUseModel" /> 允许模型润色
        </label>
        <button class="btn-primary btn-sm" :disabled="genLoading" @click="generate">
          {{ genLoading ? '生成中…' : '开始生成' }}
        </button>
      </div>
      <p class="text-xs" style="color: rgb(var(--fg-muted))">
        模型润色还需要服务端设置 <code>LLM_API_KEY</code> 与 <code>RECOMMENDATIONS_USE_MODEL=1</code>；
        客户端勾选无法单独启用模型。
      </p>
      <div v-if="lastGen" class="text-xs space-y-1" style="color: rgb(var(--fg-secondary))">
        <div>上次结果：<span class="tag">{{ lastGen.status }}</span>
          窗口 {{ lastGen.window?.start?.slice(0, 10) }} → {{ lastGen.window?.end?.slice(0, 10) }}</div>
        <div v-if="lastGen.model_disabled_by_server">服务端未启用模型，已按规则生成。</div>
        <div v-if="lastGen.gaps && lastGen.gaps.length">数据缺口：{{ lastGen.gaps.join('、') }}</div>
      </div>
    </div>

    <RecommendationInsights v-if="insightsOpen" />

    <div class="flex flex-wrap gap-2">
      <button
        v-for="tab in TABS" :key="tab.value"
        class="btn-sm"
        :class="statusFilter === tab.value ? 'btn-primary' : 'btn-secondary'"
        @click="setFilter(tab.value)">{{ tab.label }}</button>
    </div>

    <div v-if="loading" class="space-y-2">
      <div v-for="i in 3" :key="i" class="skeleton h-20" />
    </div>
    <div v-else-if="!list.length" class="card p-8 text-center" style="color: rgb(var(--fg-muted))">
      {{ statusFilter ? '该状态下没有建议' : '暂无建议，可点右上角「生成建议」' }}
    </div>
    <div v-else class="space-y-3">
      <div v-for="r in list" :key="r.id" class="card p-4" :data-rec-id="r.id">
        <div class="flex flex-wrap items-center gap-2 mb-1">
          <span class="tag" :class="r.priority === 'high' ? '!text-red-500' : ''">{{ r.priority }}</span>
          <span class="tag">{{ r.category }}</span>
          <span class="tag">{{ r.status }}</span>
          <span v-if="r.coverage_status && r.coverage_status !== 'ok'" class="tag">
            覆盖 {{ r.coverage_status }}
          </span>
          <span v-if="r.expires_at" class="text-xs" style="color: rgb(var(--fg-muted))">
            有效至 {{ String(r.expires_at).slice(0, 10) }}
          </span>
        </div>
        <div class="font-medium text-sm">{{ r.title }}</div>
        <div class="text-sm mt-1" style="color: rgb(var(--fg-secondary))">{{ r.content }}</div>

        <details v-if="r.evidence && r.evidence.length" class="mt-2">
          <summary class="text-xs cursor-pointer" style="color: rgb(var(--fg-muted))">
            证据 {{ r.evidence.length }} 条
          </summary>
          <ul class="mt-1 space-y-1 text-xs" style="color: rgb(var(--fg-secondary))">
            <li v-for="(e, i) in r.evidence" :key="i">
              {{ e.label }}：{{ e.value }}<span v-if="e.detail">（{{ e.detail }}）</span>
            </li>
          </ul>
        </details>

        <div class="flex flex-wrap items-center gap-2 mt-3 pt-3 border-t border-border">
          <template v-if="r.status === 'pending'">
            <button class="btn-primary btn-sm" :disabled="busyId === r.id" @click="act(r, 'accept')">采纳</button>
            <button class="btn-secondary btn-sm" :disabled="busyId === r.id" @click="act(r, 'ignore')">忽略</button>
            <template v-if="confirmRejectId === r.id">
              <span class="text-xs" style="color: rgb(var(--danger))">拒绝后 30 天内不再生成同类建议</span>
              <button class="btn-sm" style="color: rgb(var(--danger))" :disabled="busyId === r.id"
                      @click="act(r, 'reject')">确认拒绝</button>
              <button class="btn-ghost btn-sm" @click="confirmRejectId = null">取消</button>
            </template>
            <button v-else class="btn-ghost btn-sm" :disabled="busyId === r.id"
                    @click="confirmRejectId = r.id">拒绝</button>
          </template>
          <template v-else-if="r.status === 'accepted'">
            <button class="btn-secondary btn-sm" :disabled="busyId === r.id" @click="act(r, 'reopen')">撤回采纳</button>
          </template>
          <span v-else class="text-xs" style="color: rgb(var(--fg-muted))">该建议已结束，不可再操作</span>
        </div>

        <OutcomeForm v-if="r.status === 'accepted'" :rec-id="r.id" :expired="isExpired(r)" />

        <!-- Feedback row -->
        <div class="flex items-center gap-2 mt-3 pt-3 border-t border-border">
          <button
            class="btn-sm"
            :class="feedbackVotes[r.id] === 'helpful' ? 'btn-primary' : 'btn-secondary'"
            :disabled="submittingVote === r.id"
            @click="vote(r.id, 'helpful')">有用</button>
          <button
            class="btn-sm"
            :class="feedbackVotes[r.id] === 'unhelpful' ? 'btn-primary' : 'btn-secondary'"
            :disabled="submittingVote === r.id"
            @click="vote(r.id, 'unhelpful')">无用</button>
          <button class="btn-ghost btn-sm" @click="toggleFeedback(r.id)">备注</button>
        </div>

        <!-- Feedback note editor -->
        <div v-if="feedbackOpen === r.id" class="mt-3 pt-3 border-t border-border space-y-2">
          <textarea v-model="feedbackNote" class="input text-xs" rows="2" placeholder="补充说明…" />
          <div class="flex gap-2">
            <button class="btn-primary btn-sm" :disabled="submittingNote === r.id" @click="submitNote(r.id)">
              {{ submittingNote === r.id ? '保存中…' : '保存备注' }}
            </button>
            <button class="btn-secondary btn-sm" @click="feedbackOpen = null">取消</button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
