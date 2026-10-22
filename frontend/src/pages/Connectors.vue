<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { api } from '../api'
import { useToast } from '../toast'
const { show } = useToast()

const sources = ref<any[]>([])
const types = ref<any[]>([])
const loading = ref(true)
const logsOpen = ref<number | null>(null)
const logs = ref<any[]>([])

// Credential state: one panel at a time, one input value, never echoed back.
const credPanel = ref<{ id: number; mode: 'set' | 'clear' } | null>(null)
const credSecret = ref('')
const credBusy = ref<number | null>(null)

// Form state
const showForm = ref(false)
const editingId = ref<number | null>(null)
const formKind = ref('local_dir')
const formName = ref('')
const formSubmitting = ref(false)
// local_dir fields
const fRoot = ref('')
const fRecursive = ref(true)
// github fields
const fOwner = ref('')
const fRepo = ref('')
const fBranch = ref('')
// github PAT (create-only; PATCH has no secret field by design)
const fPat = ref('')
// http_api fields
const fUrl = ref('')
// mock fields
const fMockItems = ref('')

async function load() {
  loading.value = true
  try {
    const [s, t] = await Promise.all([api('/api/data-sources'), api('/api/connector-types')])
    sources.value = s; types.value = t
  } catch (e: any) { show(e.message, 'error') }
  finally { loading.value = false }
}

async function toggleLogs(id: number) {
  if (logsOpen.value === id) { logsOpen.value = null; return }
  logsOpen.value = id; logs.value = []
  try { logs.value = await api(`/api/data-sources/${id}/sync-logs`) }
  catch (e: any) { show(e.message, 'error') }
}

async function sync(id: number) {
  try {
    const r = await api(`/api/data-sources/${id}/sync`, { method: 'POST' })
    show(`同步完成: 新增 ${r.created} 更新 ${r.updated}`, 'success')
    load()
  } catch (e: any) { show(e.message, 'error') }
}

function resetForm() {
  fRoot.value = ''; fRecursive.value = true
  fOwner.value = ''; fRepo.value = ''; fBranch.value = ''
  fPat.value = ''
  fUrl.value = ''; fMockItems.value = '[]'
}

function openCreate() {
  editingId.value = null
  formKind.value = types.value[0]?.kind || 'local_dir'
  formName.value = ''
  resetForm()
  showForm.value = true
}

function openEdit(s: any) {
  editingId.value = s.id
  formKind.value = s.kind
  formName.value = s.name
  const c = s.config || {}
  fRoot.value = c.root || c.path || ''
  fRecursive.value = c.recursive !== false
  fOwner.value = c.owner || ''
  fRepo.value = c.repo || ''
  fBranch.value = c.branch || ''
  fPat.value = '' // never echoed back: the store is write-only
  fUrl.value = c.url || ''
  fMockItems.value = JSON.stringify(c.items || [], null, 2)
  showForm.value = true
}

function cancelForm() { showForm.value = false; editingId.value = null }

function buildConfig(): Record<string, any> {
  switch (formKind.value) {
    case 'local_dir':
      return { root: fRoot.value.trim(), recursive: fRecursive.value }
    case 'github':
      const cfg: Record<string, any> = { owner: fOwner.value.trim(), repo: fRepo.value.trim(), include: ['readme', 'docs'] }
      if (fBranch.value.trim()) cfg.branch = fBranch.value.trim()
      return cfg
    case 'http_api':
      return { url: fUrl.value.trim() }
    case 'mock':
      try { return { items: JSON.parse(fMockItems.value || '[]') } } catch { return {} }
    default:
      return {}
  }
}

async function submitForm() {
  if (formSubmitting.value) return
  if (!formName.value.trim()) { show('请填写名称', 'error'); return }
  if (formKind.value === 'local_dir' && !fRoot.value.trim()) { show('请填写授权目录路径', 'error'); return }
  if (formKind.value === 'github' && (!fOwner.value.trim() || !fRepo.value.trim())) { show('请填写 owner 和 repo', 'error'); return }
  if (formKind.value === 'http_api' && !fUrl.value.trim()) { show('请填写 URL', 'error'); return }
  formSubmitting.value = true
  try {
    const config = buildConfig()
    if (editingId.value) {
      // PATCH has no secret field: credentials only go through the dedicated
      // endpoint, so an edit can never overwrite one by accident.
      await api(`/api/data-sources/${editingId.value}`, { method: 'PATCH', body: JSON.stringify({ name: formName.value.trim(), config }) })
      show('数据源已更新', 'success')
    } else {
      const payload: Record<string, any> = { kind: formKind.value, name: formName.value.trim(), config }
      if (formKind.value === 'github' && fPat.value.trim()) payload.secret = fPat.value.trim()
      await api('/api/data-sources', { method: 'POST', body: JSON.stringify(payload) })
      show('数据源已创建', 'success')
    }
    showForm.value = false
    load()
  } catch (e: any) { show(e.message, 'error') }
  finally { formSubmitting.value = false }
}

// ------------------------------------------------------------- credentials

function toggleCredPanel(id: number, mode: 'set' | 'clear') {
  credSecret.value = ''
  credPanel.value = credPanel.value && credPanel.value.id === id && credPanel.value.mode === mode
    ? null
    : { id, mode }
}

function replaceSource(updated: any) {
  const idx = sources.value.findIndex((s) => s.id === updated?.id)
  if (idx >= 0) sources.value[idx] = updated
}

async function saveCredential(id: number) {
  const secret = credSecret.value.trim()
  if (!secret) { show('凭据内容不能为空', 'error'); return }
  if (secret.length > 4000) { show('凭据过长（上限 4000 字符）', 'error'); return }
  credBusy.value = id
  try {
    const updated = await api(`/api/data-sources/${id}/credential`, {
      method: 'PUT', body: JSON.stringify({ secret }),
    })
    replaceSource(updated)
    credSecret.value = '' // write-only: drop the plaintext from memory/UI
    credPanel.value = null
    show('凭据已保存（Fernet 加密存储）', 'success')
  } catch (e: any) { show(e.message, 'error') }
  finally { credBusy.value = null }
}

async function clearCredential(id: number) {
  credBusy.value = id
  try {
    await api(`/api/data-sources/${id}/credential`, { method: 'DELETE' })
    const current = sources.value.find((s) => s.id === id)
    if (current) replaceSource({ ...current, has_credential: false })
    credPanel.value = null
    show('凭据已清除', 'success')
  } catch (e: any) { show(e.message, 'error') }
  finally { credBusy.value = null }
}

onMounted(load)
</script>

<template>
  <div class="max-w-4xl mx-auto p-6 space-y-6">
    <div class="flex items-center justify-between">
      <div>
        <h2 class="text-xl font-semibold">数据源</h2>
        <p class="text-sm mt-1" style="color: rgb(var(--fg-secondary))">连接器只负责只读获取与增量同步。</p>
      </div>
      <button class="btn-primary" @click="openCreate">+ 新建数据源</button>
    </div>

    <!-- Create/Edit form -->
    <div v-if="showForm" class="card p-4 space-y-3">
      <h3 class="font-medium">{{ editingId ? '编辑数据源' : '新建数据源' }}</h3>
      <div class="grid grid-cols-2 gap-3">
        <div>
          <label class="text-xs" style="color: rgb(var(--fg-muted))">类型</label>
          <select v-model="formKind" name="kind" class="input mt-1" :disabled="!!editingId">
            <option v-for="t in types" :key="t.kind" :value="t.kind">{{ t.label || t.kind }}</option>
          </select>
        </div>
        <div>
          <label class="text-xs" style="color: rgb(var(--fg-muted))">名称</label>
          <input v-model="formName" name="name" class="input mt-1" placeholder="数据源名称" />
        </div>
      </div>

      <!-- local_dir -->
      <div v-if="formKind === 'local_dir'" class="space-y-2">
        <label class="text-xs" style="color: rgb(var(--fg-muted))">授权目录（绝对路径）</label>
        <input v-model="fRoot" name="root" class="input" placeholder="例如 D:\Documents\notes" />
        <label class="flex items-center gap-2 text-sm mt-1">
          <input type="checkbox" v-model="fRecursive" /> 递归子目录
        </label>
      </div>

      <!-- github -->
      <div v-if="formKind === 'github'" class="grid grid-cols-2 gap-3">
        <div>
          <label class="text-xs" style="color: rgb(var(--fg-muted))">Owner</label>
          <input v-model="fOwner" name="owner" class="input mt-1" placeholder="octocat" />
        </div>
        <div>
          <label class="text-xs" style="color: rgb(var(--fg-muted))">Repo</label>
          <input v-model="fRepo" name="repo" class="input mt-1" placeholder="hello-world" />
        </div>
        <div class="col-span-2">
          <label class="text-xs" style="color: rgb(var(--fg-muted))">分支（可选，默认 main）</label>
          <input v-model="fBranch" name="branch" class="input mt-1" placeholder="main" />
        </div>
        <div v-if="!editingId" class="col-span-2">
          <label class="text-xs" style="color: rgb(var(--fg-muted))">访问令牌 PAT（可选，Fernet 加密存储，保存后不可查看）</label>
          <input v-model="fPat" name="secret" type="password" class="input mt-1" autocomplete="new-password"
                 placeholder="github_pat_…" />
        </div>
      </div>

      <!-- http_api -->
      <div v-if="formKind === 'http_api'">
        <label class="text-xs" style="color: rgb(var(--fg-muted))">API URL</label>
        <input v-model="fUrl" name="url" class="input mt-1" placeholder="https://api.example.com/data" />
      </div>

      <!-- mock -->
      <div v-if="formKind === 'mock'">
        <label class="text-xs" style="color: rgb(var(--fg-muted))">测试数据 (JSON)</label>
        <textarea v-model="fMockItems" name="mock_items" class="input mt-1 font-mono" rows="3" style="font-size: 12px" />
      </div>

      <div class="flex gap-2">
        <button class="btn-primary" :disabled="formSubmitting" @click="submitForm">
          {{ formSubmitting ? '提交中…' : (editingId ? '保存' : '创建') }}
        </button>
        <button class="btn-secondary" @click="cancelForm">取消</button>
      </div>
    </div>

    <div v-if="loading" class="space-y-2"><div v-for="i in 2" :key="i" class="skeleton h-20" /></div>
    <div v-else-if="!sources.length" class="card p-8 text-center" style="color: rgb(var(--fg-muted))">
      还没有数据源。支持本地目录、GitHub 仓库和通用 HTTP API。
    </div>
    <div v-else class="space-y-3">
      <div v-for="s in sources" :key="s.id" class="card p-4" :data-source-id="s.id">
        <div class="flex items-center justify-between">
          <div class="min-w-0">
            <div class="font-medium truncate">{{ s.name }} <span class="tag ml-1">{{ s.kind }}</span>
              <span class="tag ml-1" data-field="cred-badge">
                {{ s.has_credential ? '凭据已配置' : '凭据未配置' }}
              </span>
            </div>
            <div class="text-xs mt-1" style="color: rgb(var(--fg-muted))">
              资源 {{ s.active_item_count }} 条 · {{ s.last_synced_at ? '上次同步 ' + s.last_synced_at.slice(0, 16).replace('T', ' ') : '从未同步' }}
            </div>
          </div>
          <div class="flex gap-2 shrink-0">
            <button class="btn-secondary btn-sm" @click="openEdit(s)">编辑</button>
            <button class="btn-secondary btn-sm" data-action="cred" @click="toggleCredPanel(s.id, 'set')">
              {{ s.has_credential ? '更换凭据' : '设置凭据' }}
            </button>
            <button class="btn-secondary btn-sm" @click="sync(s.id)">立即同步</button>
            <button class="btn-ghost btn-sm" @click="toggleLogs(s.id)">{{ logsOpen === s.id ? '隐藏日志' : '同步日志' }}</button>
          </div>
        </div>

        <!-- Set / replace credential -->
        <form
          v-if="credPanel && credPanel.id === s.id && credPanel.mode === 'set'"
          class="mt-3 rounded-md border border-border p-3 space-y-2"
          data-panel="credential"
          @submit.prevent="saveCredential(s.id)"
        >
          <div class="text-xs" style="color: rgb(var(--fg-muted))">
            凭据（如 GitHub 只读 PAT）。Fernet 加密存储，保存后不可再查看。
          </div>
          <div class="flex gap-2">
            <input v-model="credSecret" name="secret" type="password" autocomplete="new-password"
                   class="input flex-1" placeholder="粘贴令牌，提交后不可查看" />
            <button type="submit" class="btn-primary btn-sm" :disabled="credBusy === s.id">保存</button>
            <button type="button" class="btn-ghost btn-sm" @click="credPanel = null">取消</button>
          </div>
          <div v-if="s.has_credential" class="flex items-center justify-between">
            <span class="text-xs" style="color: rgb(var(--fg-muted))">已有一份凭据，保存将直接替换。</span>
            <button type="button" class="btn-ghost btn-sm" data-action="cred-clear"
                    @click="toggleCredPanel(s.id, 'clear')">清除凭据</button>
          </div>
        </form>

        <!-- Clear credential (two-step confirm) -->
        <div
          v-if="credPanel && credPanel.id === s.id && credPanel.mode === 'clear'"
          class="mt-3 flex flex-wrap items-center gap-2 rounded-md border border-border p-3"
          data-panel="cred-clear"
        >
          <span class="text-xs" style="color: rgb(var(--fg-muted))">确认清除这条数据源的凭据？清除后需要凭据的连接器将无法同步。</span>
          <button class="btn-secondary btn-sm" :disabled="credBusy === s.id" data-action="cred-clear-confirm"
                  @click="clearCredential(s.id)">确认清除</button>
          <button class="btn-ghost btn-sm" @click="credPanel = null">返回</button>
        </div>

        <div v-if="logsOpen === s.id" class="mt-3 pt-3 border-t border-border space-y-1">
          <div v-if="!logs.length" class="text-xs" style="color: rgb(var(--fg-muted))">暂无同步记录</div>
          <div v-for="l in logs" :key="l.id" class="text-xs p-1.5 rounded" style="background: rgb(var(--elevated))">
            {{ l.started_at?.slice(0, 16).replace('T', ' ') }} ·
            <span :style="{ color: l.status === 'success' ? 'green' : 'red' }">{{ l.status }}</span>
            · 新增 {{ l.created || 0 }} 更新 {{ l.updated || 0 }}{{ l.error ? ' · ' + l.error : '' }}
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
