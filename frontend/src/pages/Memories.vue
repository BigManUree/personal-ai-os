<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { api, esc } from '../api'
import { useToast } from '../toast'
const { show } = useToast()

const list = ref<any[]>([])
const candidates = ref<any[]>([])
const insights = ref<any>(null)
const loading = ref(true)
const form = ref({ content: '', category: 'general', confidence: '1' })
const reviewingId = ref<number | null>(null)

async function load() {
  loading.value = true
  try {
    const [memRes, candRes, insRes] = await Promise.all([
      api('/api/memories'),
      api('/api/memory-candidates?status=pending').catch(() => []),
      api('/api/memory-insights').catch(() => null),
    ])
    list.value = memRes
    candidates.value = candRes
    insights.value = insRes
  } catch (e: any) { show(e.message, 'error') }
  finally { loading.value = false }
}

async function addMemory() {
  if (!form.value.content.trim()) return
  try {
    await api('/api/memories', { method: 'POST', body: JSON.stringify({
      content: form.value.content, category: form.value.category, source: 'manual',
      confidence: Number(form.value.confidence), confirmed: true,
    })})
    form.value.content = ''
    show('记忆已保存', 'success')
    load()
  } catch (e: any) { show(e.message, 'error') }
}

async function reviewCandidate(id: number, action: 'approve' | 'reject') {
  if (reviewingId.value !== null) return
  reviewingId.value = id
  try {
    const path = action === 'approve' ? 'approve' : 'reject'
    await api(`/api/memory-candidates/${id}/${path}`, { method: 'POST' })
    show(action === 'approve' ? '已确认' : '已拒绝', 'success')
    load()
  } catch (e: any) { show(e.message, 'error') }
  finally { reviewingId.value = null }
}

async function removeMemory(id: number) {
  if (!confirm('确定删除？')) return
  try { await api('/api/memories/' + id, { method: 'DELETE' }); load() } catch (e: any) { show(e.message, 'error') }
}

onMounted(load)
</script>

<template>
  <div class="max-w-4xl mx-auto p-6 space-y-6">
    <div>
      <h2 class="text-xl font-semibold">长期记忆</h2>
      <p class="text-sm" style="color: rgb(var(--fg-secondary))">你确认保存的信息，AI 在回答时会参考。</p>
    </div>

    <!-- Insights -->
    <div v-if="insights" class="grid grid-cols-3 gap-4">
      <div class="card p-3 text-center">
        <div class="text-lg font-semibold">{{ insights.memories_confirmed ?? '—' }}</div>
        <div class="text-xs" style="color: rgb(var(--fg-muted))">已确认记忆</div>
      </div>
      <div class="card p-3 text-center">
        <div class="text-lg font-semibold">{{ insights.candidates_by_status?.pending ?? '—' }}</div>
        <div class="text-xs" style="color: rgb(var(--fg-muted))">待审核候选</div>
      </div>
      <div class="card p-3 text-center">
        <div class="text-lg font-semibold">{{ insights.acceptance_rate != null ? Math.round(insights.acceptance_rate * 100) + '%' : '—' }}</div>
        <div class="text-xs" style="color: rgb(var(--fg-muted))">采纳率</div>
      </div>
    </div>

    <!-- Add form -->
    <div class="card p-4">
      <div class="label">添加记忆</div>
      <textarea v-model="form.content" class="input mb-2" rows="2" placeholder="例如：我更喜欢先看整体架构再分模块实现"></textarea>
      <div class="flex gap-2">
        <select v-model="form.category" class="input w-auto">
          <option value="preference">偏好</option><option value="goal">目标</option>
          <option value="work">工作</option><option value="project">项目</option>
          <option value="habit">习惯</option><option value="general">其他</option>
        </select>
        <select v-model="form.confidence" class="input w-auto">
          <option value="1">已确认</option><option value="0.7">较可信</option><option value="0.4">待验证</option>
        </select>
        <button class="btn-primary" @click="addMemory">保存</button>
      </div>
    </div>

    <!-- Pending candidates -->
    <div v-if="candidates.length" class="card p-4">
      <h3 class="font-medium mb-3">待审核候选 ({{ candidates.length }})</h3>
      <div class="space-y-2">
        <div v-for="c in candidates" :key="c.id" class="p-3 rounded-md border border-border">
          <div class="text-sm">{{ c.content }}</div>
          <div class="flex gap-2 mt-2">
            <button class="btn-primary btn-sm" :disabled="reviewingId !== null" @click="reviewCandidate(c.id, 'approve')">确认</button>
            <button class="btn-secondary btn-sm" :disabled="reviewingId !== null" @click="reviewCandidate(c.id, 'reject')">拒绝</button>
          </div>
        </div>
      </div>
    </div>

    <!-- Memory list -->
    <div class="card p-4">
      <h3 class="font-medium mb-3">已保存记忆 ({{ list.length }})</h3>
      <div v-if="loading" class="space-y-2">
        <div v-for="i in 3" :key="i" class="skeleton h-12" />
      </div>
      <div v-else-if="!list.length" class="text-sm py-8 text-center" style="color: rgb(var(--fg-muted))">还没有记忆，添加第一条吧。</div>
      <div v-else class="space-y-2">
        <div v-for="m in list" :key="m.id" class="flex items-start justify-between p-3 rounded-md border border-border">
          <div class="flex-1">
            <div class="text-sm">{{ m.content }}</div>
            <div class="text-xs mt-1" style="color: rgb(var(--fg-muted))">{{ m.category }} · 可信度 {{ m.confidence }}</div>
          </div>
          <button class="btn-ghost btn-sm text-red-500" @click="removeMemory(m.id)">删除</button>
        </div>
      </div>
    </div>
  </div>
</template>
