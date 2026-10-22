<script setup lang="ts">
import { ref, onMounted, inject } from 'vue'
import { api } from '../api'
const navigate = inject<(id: string) => void>('navigate')

const stats = ref({ memories: '—', docs: '—', approvals: '—' })
const loading = ref(true)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [memRes, docRes, appRes] = await Promise.all([
      api<any[]>('/api/memories').catch(() => []),
      api<any[]>('/api/documents').catch(() => []),
      api<any[]>('/api/approvals').catch(() => []),
    ])
    stats.value = {
      memories: String(memRes.length || 0),
      docs: String(docRes.length || 0),
      approvals: String(appRes.filter((a: any) => a.status === 'pending').length || 0),
    }
  } catch (e: any) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}
onMounted(load)
</script>

<template>
  <div class="max-w-5xl mx-auto p-6 space-y-6">
    <div>
      <h2 class="text-xl font-kai font-semibold">欢迎回来</h2>
      <p class="text-sm" style="color: rgb(var(--fg-secondary))">你的个人 AI 工作空间，从这里开始。</p>
    </div>

    <!-- Stats -->
    <div v-if="loading" class="grid grid-cols-3 gap-4">
      <div v-for="i in 3" :key="i" class="skeleton h-20" />
    </div>
    <div v-else class="grid grid-cols-1 sm:grid-cols-3 gap-4">
      <div v-for="(v, k) in { '长期记忆': stats.memories, '知识资料': stats.docs, '待审批': stats.approvals }" :key="k"
        class="card p-4 cursor-pointer hover:border-accent transition-colors"
        @click="navigate(k === '长期记忆' ? 'memories' : k === '知识资料' ? 'knowledge' : 'approvals')">
        <div class="text-xs" style="color: rgb(var(--fg-muted))">{{ k }}</div>
        <div class="text-2xl font-semibold mt-1">{{ v }}</div>
      </div>
    </div>

    <!-- Quick actions -->
    <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
      <div class="card p-5">
        <h3 class="font-medium mb-2">开始协作</h3>
        <p class="text-sm mb-4" style="color: rgb(var(--fg-secondary))">与 AI 对话，基于你的真实资料获得建议。</p>
        <button class="btn-primary" @click="navigate('chat')">＋ 开始对话</button>
      </div>
      <div class="card p-5">
        <h3 class="font-medium mb-2">待处理事项</h3>
        <p class="text-sm mb-4" style="color: rgb(var(--fg-secondary))">
          {{ stats.approvals !== '0' ? `${stats.approvals} 条待审批操作等待你确认` : '当前没有待审批事项' }}
        </p>
        <button class="btn-secondary" @click="navigate('approvals')">查看审批</button>
      </div>
    </div>
  </div>
</template>
