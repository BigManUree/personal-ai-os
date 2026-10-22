<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { api } from '../api'
import { useToast } from '../toast'
const { show } = useToast()

const profile = ref<any>(null)
const loading = ref(true)

async function load() {
  loading.value = true
  try { profile.value = await api('/api/profile') }
  catch (e: any) { show(e.message, 'error') }
  finally { loading.value = false }
}
onMounted(load)
</script>

<template>
  <div class="max-w-3xl mx-auto p-6 space-y-6">
    <h2 class="text-xl font-semibold">个人档案</h2>
    <div v-if="loading" class="skeleton h-40" />
    <div v-else-if="!profile" class="card p-8 text-center" style="color: rgb(var(--fg-muted))">暂无档案</div>
    <div v-else class="card p-6 space-y-4">
      <div v-for="(v, k) in profile" :key="k" v-if="typeof v !== 'object' && v">
        <div class="label">{{ k }}</div>
        <div class="text-sm">{{ v }}</div>
      </div>
    </div>
  </div>
</template>
