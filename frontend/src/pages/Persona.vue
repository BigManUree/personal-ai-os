<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api } from '../api'
import { useToast } from '../toast'
import Modal from '../components/Modal.vue'
import ProfileItemForm from '../components/ProfileItemForm.vue'

const { show } = useToast()

const TABS = [
  { value: '', label: '全部' },
  { value: 'pending', label: '候选' },
  { value: 'confirmed', label: '已确认' },
  { value: 'expired', label: '已过期' },
  { value: 'rejected', label: '已拒绝' },
]

const profileText = ref<any>(null)
const items = ref<any[]>([])
const loading = ref(true)
const statusFilter = ref('')

const busyKey = ref('')
const confirmKey = ref('')

const editor = ref(false)
const editingId = ref<number | null>(null)
const saving = ref(false)
const form = ref<any>({})

const proposing = ref(false)

const expandedHistory = ref<number[]>([])
const histories = ref<Record<number, any>>({})
const historyLoading = ref<number | null>(null)

// The PATCH endpoint updates field-by-field (`is not None`), so sending the
// whole form is safe and an explicit null clears a date.
function profileItemBody(f: any) {
  return {
    category: String(f.category ?? 'basic'),
    title: String(f.title ?? ''),
    content: String(f.content ?? '').trim(),
    confidence: Number(f.confidence ?? 1),
    source_type: String(f.source_type ?? 'manual'),
    source_detail: String(f.source_detail ?? ''),
    valid_from: f.valid_from ? String(f.valid_from) : null,
    valid_until: f.valid_until ? String(f.valid_until) : null,
  }
}

async function load() {
  loading.value = true
  // The server hides rejected items unless status=rejected is passed explicitly;
  // the 「已拒绝」 tab exists exactly so rejected rows stay auditable.
  const qs = statusFilter.value ? `?status=${statusFilter.value}` : ''
  const results = await Promise.allSettled([
    api('/api/profile'),
    api('/api/profile-items' + qs),
  ])
  const [p, list] = results
  profileText.value = p.status === 'fulfilled' ? p.value : null
  items.value = list.status === 'fulfilled' ? (list.value as any[]) : []
  if (results.some(r => r.status === 'rejected')) show('数据加载失败，请刷新重试', 'error')
  loading.value = false
}

function setFilter(value: string) {
  if (statusFilter.value === value) return
  statusFilter.value = value
  load()
}

// ---------- status transitions (server-side state machine) ----------
async function transition(item: any, action: 'confirm' | 'reject' | 'expire') {
  if (busyKey.value) return
  busyKey.value = `${action}-${item.id}`
  try {
    await api(`/api/profile-items/${item.id}/${action}`, { method: 'POST' })
    const label = action === 'confirm' ? '已确认为个人事实' : action === 'reject' ? '已拒绝该条目' : '已标记过期'
    show(label, 'success')
    confirmKey.value = ''
    await load()
  } catch (e: any) {
    show(e.message, 'error')
    await load()
  } finally { busyKey.value = '' }
}

async function removeItem(item: any) {
  if (busyKey.value) return
  busyKey.value = `delete-${item.id}`
  try {
    await api(`/api/profile-items/${item.id}`, { method: 'DELETE' })
    show('条目已删除', 'success')
    confirmKey.value = ''
    await load()
  } catch (e: any) { show(e.message, 'error') }
  finally { busyKey.value = '' }
}

// ---------- editor ----------
function closeEditor() {
  editor.value = false
  editingId.value = null
  form.value = {}
}

function openForm(item?: any) {
  editingId.value = item ? item.id : null
  form.value = item
    ? {
        category: item.category || 'basic',
        title: item.title || '',
        content: item.content || '',
        confidence: item.confidence ?? 1,
        source_type: item.source_type || 'manual',
        source_detail: item.source_detail || '',
        valid_from: item.valid_from || '',
        valid_until: item.valid_until || '',
      }
    : {
        category: 'basic', title: '', content: '', confidence: 1,
        source_type: 'manual', source_detail: '', valid_from: '', valid_until: '',
      }
  editor.value = true
}

async function submitItem() {
  if (saving.value) return
  const body = profileItemBody(form.value)
  if (!body.content) { show('内容不能为空', 'error'); return }
  saving.value = true
  try {
    if (editingId.value === null) {
      await api('/api/profile-items', { method: 'POST', body: JSON.stringify(body) })
      show('条目已新增（手动录入直接生效）', 'success')
    } else {
      await api(`/api/profile-items/${editingId.value}`, { method: 'PATCH', body: JSON.stringify(body) })
      show('条目已保存', 'success')
    }
    closeEditor()
    await load()
  } catch (e: any) { show(e.message, 'error') }
  finally { saving.value = false }
}

// ---------- history ----------
async function toggleHistory(item: any) {
  const id = item.id
  const idx = expandedHistory.value.indexOf(id)
  if (idx >= 0) { expandedHistory.value.splice(idx, 1); return }
  expandedHistory.value.push(id)
  if (histories.value[id]) return
  historyLoading.value = id
  try {
    histories.value[id] = await api(`/api/profile-items/${id}/history`)
  } catch (e: any) {
    show(e.message, 'error')
    expandedHistory.value = expandedHistory.value.filter(x => x !== id)
  } finally { historyLoading.value = null }
}

// ---------- cloud proposals ----------
async function propose() {
  if (proposing.value) return
  proposing.value = true
  try {
    const res: any = await api('/api/profile-items/propose', { method: 'POST' })
    const count = typeof res?.proposed === 'number' ? res.proposed : 0
    show(`生成完成，新增 ${count} 条候选`, 'success')
    statusFilter.value = 'pending'
    await load()
  } catch (e: any) { show(e.message, 'error') }
  finally { proposing.value = false }
}

onMounted(load)
</script>

<template>
  <div class="mx-auto max-w-4xl space-y-6 p-6">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h2 class="text-xl font-semibold">个人画像</h2>
        <p class="mt-1 text-xs" style="color: rgb(var(--fg-muted))">
          证据驱动的画像条目：候选需人工确认才成为事实；确认、修改、过期都会写入修订历史。
        </p>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <button class="btn-primary btn-sm" @click="openForm()">新增条目</button>
        <button class="btn-secondary btn-sm" :disabled="proposing" @click="propose">
          {{ proposing ? '生成中…' : '生成推测' }}
        </button>
        <span class="text-xs" style="color: rgb(var(--fg-muted))">生成推测需要服务端 <code>LLM_API_KEY</code></span>
      </div>
    </div>

    <!-- read-only text profile kept as a summary -->
    <details v-if="profileText" class="card p-4">
      <summary class="cursor-pointer text-sm font-medium">档案文本摘要</summary>
      <div class="mt-3 space-y-2 text-sm">
        <div>
          <span class="label">昵称</span>{{ profileText.display_name || '—' }}
        </div>
        <div v-if="profileText.goals">
          <span class="label">目标</span>
          <div class="whitespace-pre-wrap" style="color: rgb(var(--fg-secondary))">{{ profileText.goals }}</div>
        </div>
        <div v-if="profileText.preferences">
          <span class="label">偏好</span>
          <div class="whitespace-pre-wrap" style="color: rgb(var(--fg-secondary))">{{ profileText.preferences }}</div>
        </div>
      </div>
    </details>

    <section class="card p-4" data-section="profile-items">
      <div class="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h3 class="font-medium">画像条目 ({{ items.length }})</h3>
        <div class="flex flex-wrap gap-1">
          <button v-for="tab in TABS" :key="tab.value" class="btn-sm"
                  :class="statusFilter === tab.value ? 'btn-primary' : 'btn-secondary'"
                  @click="setFilter(tab.value)">{{ tab.label }}</button>
        </div>
      </div>

      <div v-if="loading" class="space-y-2">
        <div v-for="i in 3" :key="i" class="skeleton h-16" />
      </div>

      <div v-else-if="!items.length" class="py-8 text-center text-sm" style="color: rgb(var(--fg-muted))">
        {{ statusFilter === 'rejected' ? '没有已拒绝的条目' : statusFilter === 'pending' ? '暂无候选，可点右上角「生成推测」' : '暂无条目' }}
      </div>

      <div v-else class="space-y-2">
        <div v-for="item in items" :key="item.id" class="rounded-md border border-border p-3" :data-item-id="item.id">
          <div class="flex flex-wrap items-center gap-2">
            <span class="tag">{{ item.category }}</span>
            <span class="tag" :class="item.status === 'pending' ? '!text-amber-500' : ''">{{ item.status }}</span>
            <span v-if="item.is_user_stated" class="tag">用户确认</span>
            <span class="text-xs" style="color: rgb(var(--fg-muted))">置信度 {{ item.confidence }}</span>
            <div class="ml-auto flex flex-wrap gap-1">
              <template v-if="item.status === 'pending'">
                <button class="btn-primary btn-sm" :disabled="busyKey === `confirm-${item.id}`"
                        @click="transition(item, 'confirm')">确认</button>
                <template v-if="confirmKey === `reject-${item.id}`">
                  <button class="btn-sm" style="color: rgb(var(--danger))" :disabled="busyKey === `reject-${item.id}`"
                          @click="transition(item, 'reject')">确认拒绝</button>
                  <button class="btn-ghost btn-sm" @click="confirmKey = ''">取消</button>
                </template>
                <button v-else class="btn-ghost btn-sm" @click="confirmKey = `reject-${item.id}`">拒绝</button>
              </template>
              <template v-if="item.status === 'confirmed'">
                <template v-if="confirmKey === `expire-${item.id}`">
                  <button class="btn-sm" :disabled="busyKey === `expire-${item.id}`"
                          @click="transition(item, 'expire')">确认过期</button>
                  <button class="btn-ghost btn-sm" @click="confirmKey = ''">取消</button>
                </template>
                <button v-else class="btn-secondary btn-sm" @click="confirmKey = `expire-${item.id}`">标记过期</button>
              </template>
              <button v-if="item.status === 'confirmed' || item.status === 'expired'"
                      class="btn-secondary btn-sm" @click="openForm(item)">编辑</button>
              <template v-if="item.status !== 'rejected'">
                <template v-if="confirmKey === `delete-${item.id}`">
                  <button class="btn-sm" style="color: rgb(var(--danger))" :disabled="busyKey === `delete-${item.id}`"
                          @click="removeItem(item)">确认删除</button>
                  <button class="btn-ghost btn-sm" @click="confirmKey = ''">取消</button>
                </template>
                <button v-else class="btn-ghost btn-sm" @click="confirmKey = `delete-${item.id}`">删除</button>
              </template>
              <button class="btn-ghost btn-sm" @click="toggleHistory(item)">
                {{ expandedHistory.includes(item.id) ? '收起历史' : '历史' }}
              </button>
            </div>
          </div>

          <div v-if="item.title" class="mt-1 text-sm font-medium">{{ item.title }}</div>
          <div class="mt-1 whitespace-pre-wrap text-sm" style="color: rgb(var(--fg-secondary))">{{ item.content }}</div>
          <div class="mt-1 flex flex-wrap gap-3 text-xs" style="color: rgb(var(--fg-muted))">
            <span>来源：{{ item.source_type }}</span>
            <span v-if="item.source_detail">{{ item.source_detail }}</span>
            <span v-if="item.valid_from || item.valid_until">
              有效 {{ item.valid_from || '…' }} → {{ item.valid_until || '…' }}
            </span>
          </div>
          <div v-if="item.status === 'rejected'" class="mt-2 text-xs" style="color: rgb(var(--danger))">
            已拒绝的条目不可编辑；如需记录类似内容，请重新新增。
          </div>

          <div v-if="expandedHistory.includes(item.id)" class="mt-3 space-y-1 border-t border-border pt-3">
            <div v-if="historyLoading === item.id" class="skeleton h-8" />
            <template v-else-if="histories[item.id]">
              <div v-if="!histories[item.id].revisions?.length" class="text-xs" style="color: rgb(var(--fg-muted))">
                无修订记录
              </div>
              <div v-for="rev in histories[item.id].revisions" :key="rev.id" class="text-xs">
                <span class="tag">{{ rev.event }}</span>
                <span class="ml-1">{{ typeof rev.detail === 'string' ? rev.detail : JSON.stringify(rev.detail) }}</span>
                <span class="ml-1" style="color: rgb(var(--fg-muted))">{{ String(rev.created_at).slice(0, 16) }}</span>
              </div>
            </template>
          </div>
        </div>
      </div>
    </section>

    <Modal :open="editor" :title="editingId === null ? '新增画像条目' : '编辑画像条目'" @close="closeEditor">
      <p class="text-xs" style="color: rgb(var(--fg-muted))">
        手动录入的条目直接生效并标记为「用户确认」；候选推测的 category 只能由模型给出（inference 不在手动选项中）。
      </p>
      <ProfileItemForm :form="form" :saving="saving" :editing="editingId !== null"
                       @submit="submitItem" @cancel="closeEditor" />
    </Modal>
  </div>
</template>
