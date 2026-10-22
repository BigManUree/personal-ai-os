<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { api } from '../api'
import { useToast } from '../toast'
const { show } = useToast()

const list = ref<any[]>([])
const loading = ref(true)
const processingId = ref<number | null>(null)

async function load() {
  loading.value = true
  try { list.value = await api('/api/approvals') }
  catch (e: any) { show(e.message, 'error') }
  finally { loading.value = false }
}

async function review(id: number, decision: string) {
  if (processingId.value !== null) return
  processingId.value = id
  try {
    await api(`/api/approvals/${id}/review`, { method: 'POST', body: JSON.stringify({ decision }) })
    show('已处理', 'success'); load()
  } catch (e: any) { show(e.message, 'error') }
  finally { processingId.value = null }
}

onMounted(load)
</script>

<template>
  <div class="max-w-4xl mx-auto p-6 space-y-6">
    <h2 class="text-xl font-semibold">操作审批</h2>
    <p class="text-sm" style="color: rgb(var(--fg-secondary))">AI 提议的操作需要你确认后才会执行。</p>
    <div v-if="loading" class="space-y-2"><div v-for="i in 3" :key="i" class="skeleton h-20" /></div>
    <div v-else-if="!list.length" class="card p-8 text-center" style="color: rgb(var(--fg-muted))">暂无审批记录</div>
    <div v-else class="space-y-3">
      <div v-for="a in list" :key="a.id" class="card p-4">
        <div class="flex items-center justify-between">
          <div>
            <div class="font-medium text-sm">#{{ a.id }} · {{ a.action_type }}</div>
            <div class="text-xs mt-1" style="color: rgb(var(--fg-muted))">{{ a.created_at?.slice(0, 16) }}</div>
          </div>
          <span class="tag" :class="a.status === 'pending' ? '!text-yellow-600' : a.status === 'approved' ? '!text-green-600' : '!text-red-500'">
            {{ a.status }}
          </span>
        </div>
        <pre class="mt-2 text-xs p-2 rounded overflow-x-auto" style="background: rgb(var(--elevated))">{{ JSON.stringify(a.payload, null, 2) }}</pre>
        <div v-if="a.status === 'pending'" class="flex gap-2 mt-3">
          <button class="btn-primary btn-sm" :disabled="processingId !== null" @click="review(a.id, 'approved')">批准</button>
          <button class="btn-danger btn-sm" :disabled="processingId !== null" @click="review(a.id, 'rejected')">拒绝</button>
        </div>
      </div>
    </div>
  </div>
</template>
