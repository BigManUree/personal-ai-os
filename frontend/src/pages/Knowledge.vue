<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { api } from '../api'
import { useToast } from '../toast'
const { show } = useToast()

const list = ref<any[]>([])
const loading = ref(true)
const expanded = ref<number | null>(null)
const detail = ref<any>(null)
const docName = ref('')
const docContent = ref('')
const uploadFile = ref<File | null>(null)
const uploading = ref(false)
const addingDoc = ref(false)
const uploadStatus = ref('')

async function load() {
  loading.value = true
  try { list.value = await api('/api/documents') }
  catch (e: any) { show(e.message, 'error') }
  finally { loading.value = false }
}

async function toggleDetail(id: number) {
  if (expanded.value === id) { expanded.value = null; detail.value = null; return }
  expanded.value = id
  try { detail.value = await api('/api/documents/' + id) }
  catch (e: any) { show(e.message, 'error') }
}

async function addDoc() {
  if (!docName.value.trim() || !docContent.value.trim()) return
  if (addingDoc.value) return
  addingDoc.value = true
  try {
    await api('/api/documents/text', { method: 'POST', body: JSON.stringify({ filename: docName.value, content: docContent.value }) })
    docName.value = ''; docContent.value = ''
    show('资料已保存', 'success'); load()
  } catch (e: any) { show(e.message, 'error') }
  finally { addingDoc.value = false }
}

const MAX_BYTES = 10 * 1024 * 1024  // Must match backend MAX_FILE_BYTES
const ALLOWED_EXT = ['.pdf', '.docx', '.md', '.markdown', '.txt']

function onFileChange(e: Event) {
  const input = e.target as HTMLInputElement
  if (!input.files || !input.files[0]) return
  const file = input.files[0]
  const lower = file.name.toLowerCase()
  const extOk = ALLOWED_EXT.some(ext => lower.endsWith(ext))
  if (!extOk) {
    uploadFile.value = null
    uploadStatus.value = '不支持的文件类型，仅允许 PDF / DOCX / MD / TXT'
    show(uploadStatus.value, 'error')
    input.value = ''
    return
  }
  if (file.size > MAX_BYTES) {
    uploadFile.value = null
    uploadStatus.value = `文件超过 10MB 上限（当前 ${(file.size / 1024 / 1024).toFixed(1)}MB）`
    show(uploadStatus.value, 'error')
    input.value = ''
    return
  }
  uploadFile.value = file
  uploadStatus.value = `已选择 ${file.name}（${(file.size / 1024).toFixed(1)} KB）`
}

async function doUpload() {
  if (!uploadFile.value || uploading.value) return
  uploading.value = true
  uploadStatus.value = '正在上传并解析…'
  try {
    const form = new FormData()
    form.append('file', uploadFile.value)
    const res = await fetch('/api/documents/upload', { method: 'POST', body: form })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) {
      const detail = typeof data.detail === 'string' ? data.detail : (data.detail && data.detail.message) || `上传失败：${res.status}`
      uploadStatus.value = detail
      show(detail, 'error')
    } else {
      uploadStatus.value = `已导入《${data.filename}》，生成 ${data.chunk_count} 个片段`
      show('文件已导入知识库', 'success')
      uploadFile.value = null
      const input = document.querySelector('input[type=file]') as HTMLInputElement
      if (input) input.value = ''
      load()
    }
  } catch (e: any) {
    uploadStatus.value = '上传出错：' + e.message
    show(e.message, 'error')
  } finally {
    uploading.value = false
  }
}

async function removeDoc(id: number) {
  if (!confirm('确定删除？')) return
  try { await api('/api/documents/' + id, { method: 'DELETE' }); load() } catch (e: any) { show(e.message, 'error') }
}

onMounted(load)
</script>

<template>
  <div class="max-w-4xl mx-auto p-6 space-y-6">
    <h2 class="text-xl font-semibold">私人知识库</h2>

    <!-- File upload -->
    <div class="card p-4">
      <h3 class="font-medium mb-3">上传文件</h3>
      <div class="flex items-center gap-2">
        <input type="file" accept=".pdf,.docx,.md,.txt" @change="onFileChange" class="text-sm" />
        <button class="btn-primary" :disabled="!uploadFile || uploading" @click="doUpload">
          {{ uploading ? '上传中…' : '上传' }}
        </button>
      </div>
      <div v-if="uploadStatus" class="text-xs mt-2" style="color: rgb(var(--fg-secondary))">{{ uploadStatus }}</div>
      <div class="text-xs mt-1" style="color: rgb(var(--fg-muted))">支持 PDF / DOCX / MD / TXT，单个 ≤ 10MB</div>
    </div>

    <!-- Quick add -->
    <div class="card p-4">
      <h3 class="font-medium mb-3">粘贴文本</h3>
      <input v-model="docName" class="input mb-2" placeholder="资料名称，如：个人年度规划.md" />
      <textarea v-model="docContent" class="input mb-2" rows="3" placeholder="粘贴内容…"></textarea>
      <button class="btn-primary" :disabled="addingDoc" @click="addDoc">
        {{ addingDoc ? '保存中…' : '保存到知识库' }}
      </button>
    </div>

    <!-- Doc list -->
    <div class="card p-4">
      <h3 class="font-medium mb-3">已导入资料 ({{ list.length }})</h3>
      <div v-if="loading" class="space-y-2"><div v-for="i in 3" :key="i" class="skeleton h-16" /></div>
      <div v-else-if="!list.length" class="text-sm py-8 text-center" style="color: rgb(var(--fg-muted))">知识库为空</div>
      <div v-else class="space-y-2">
        <div v-for="d in list" :key="d.id" class="border border-border rounded-md">
          <div class="flex items-center justify-between p-3">
            <div class="flex-1 cursor-pointer" @click="toggleDetail(d.id)">
              <div class="font-medium text-sm">{{ d.filename }}</div>
              <div class="text-xs mt-0.5" style="color: rgb(var(--fg-muted))">
                {{ d.source_type === 'upload' ? '文件上传' : '文本录入' }} · {{ d.chunk_count || 0 }} 片段
              </div>
              <div v-if="d.excerpt" class="text-xs mt-1 line-clamp-2" style="color: rgb(var(--fg-secondary))">{{ d.excerpt }}</div>
            </div>
            <div class="flex gap-1 ml-2">
              <button class="btn-secondary btn-sm" @click="toggleDetail(d.id)">{{ expanded === d.id ? '收起' : '详情' }}</button>
              <button class="btn-ghost btn-sm text-red-500" @click="removeDoc(d.id)">删除</button>
            </div>
          </div>
          <div v-if="expanded === d.id && detail" class="border-t border-border p-3 space-y-2">
            <div v-for="ch in (detail.chunks || []).slice(0, 8)" :key="ch.id" class="text-xs p-2 rounded" style="background: rgb(var(--elevated))">
              <span class="text-fg-muted">片段 {{ ch.chunk_index + 1 }}{{ ch.heading ? ' · ' + ch.heading : '' }}</span>
              <div class="mt-1">{{ ch.excerpt }}…</div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
