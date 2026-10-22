<script setup lang="ts">
import { ref, computed, provide, watch } from 'vue'
import Overview from './pages/Overview.vue'
import Chat from './pages/Chat.vue'
import Memories from './pages/Memories.vue'
import Knowledge from './pages/Knowledge.vue'
import Connectors from './pages/Connectors.vue'
import Work from './pages/Work.vue'
import Persona from './pages/Persona.vue'
import Analytics from './pages/Analytics.vue'
import Recommendations from './pages/Recommendations.vue'
import Profile from './pages/Profile.vue'
import Approvals from './pages/Approvals.vue'
import Reminders from './pages/Reminders.vue'
import Settings from './pages/Settings.vue'
import { useToast } from './toast'

const isMobile = ref(window.innerWidth <= 768)
const sidebarOpen = ref(!isMobile.value)
const darkMode = ref(document.documentElement.classList.contains('dark'))

window.addEventListener('resize', () => {
  isMobile.value = window.innerWidth <= 768
})

// When crossing the mobile/desktop boundary, sync sidebar state.
watch(isMobile, (mobile) => {
  sidebarOpen.value = !mobile
})

const groups = [
  { label: '工作台', items: [
    { id: 'overview', icon: '⌂', label: '总览' },
    { id: 'chat', icon: '◉', label: 'AI 助手' },
  ]},
  { label: '个人数据', items: [
    { id: 'memories', icon: '◈', label: '长期记忆', badge: 0 },
    { id: 'knowledge', icon: '▤', label: '知识库' },
    { id: 'connectors', icon: '⇅', label: '数据源' },
  ]},
  { label: '个人成长', items: [
    { id: 'work', icon: '◫', label: '项目与任务' },
    { id: 'persona', icon: '◑', label: '个人画像' },
    { id: 'analytics', icon: '∿', label: '行为洞察' },
  ]},
  { label: '智能管理', items: [
    { id: 'recommendations', icon: '✦', label: '智能建议' },
    { id: 'profile', icon: '◎', label: '个人档案' },
    { id: 'approvals', icon: '✓', label: '操作审批', badge: 0 },
  ]},
  { label: '自动化', items: [
    { id: 'reminders', icon: '⏰', label: '提醒中心' },
    { id: 'settings', icon: '⚙', label: '设置' },
  ]},
]

const components: Record<string, any> = {
  overview: Overview, chat: Chat, memories: Memories, knowledge: Knowledge,
  connectors: Connectors, work: Work, persona: Persona, analytics: Analytics,
  recommendations: Recommendations, profile: Profile, approvals: Approvals,
  reminders: Reminders, settings: Settings,
}

const validPages = new Set(Object.keys(components))

function pageFromUrl(): string {
  const hash = location.hash.replace(/^#\//, '')
  return validPages.has(hash) ? hash : 'overview'
}

const currentPage = ref(pageFromUrl())

window.addEventListener('popstate', () => {
  currentPage.value = pageFromUrl()
})

const pageTitle = computed(() => {
  for (const g of groups) {
    const item = g.items.find(i => i.id === currentPage.value)
    if (item) return item.label
  }
  return ''
})

function navigate(id: string) {
  currentPage.value = id
  if (isMobile.value) sidebarOpen.value = false
  history.pushState({ page: id }, '', '#/' + id)
}

function toggleTheme() {
  darkMode.value = !darkMode.value
  document.documentElement.classList.toggle('dark', darkMode.value)
  localStorage.setItem('paos-theme', darkMode.value ? 'dark' : 'light')
}

const { toasts } = useToast()

provide('navigate', navigate)
</script>

<template>
  <div class="flex h-screen overflow-hidden">
    <!-- Sidebar -->
    <aside
      v-if="sidebarOpen"
      class="w-56 shrink-0 border-r border-border flex flex-col transition-all"
      :class="isMobile ? 'fixed left-0 top-0 h-full z-20 shadow-xl' : ''"
      style="background: rgb(var(--sidebar-bg))"
    >
      <div class="px-4 py-4 flex items-center gap-2 border-b border-border">
        <span class="text-lg">✦</span>
        <div>
          <div class="font-semibold text-sm">Personal AI OS</div>
          <div class="text-xs" style="color: rgb(var(--fg-muted))">本地优先 · 单人工作台</div>
        </div>
      </div>
      <nav class="flex-1 overflow-y-auto px-2 py-3 space-y-4">
        <div v-for="group in groups" :key="group.label">
          <div class="px-2 mb-1.5 text-[10px] font-semibold uppercase tracking-wider" style="color: rgb(var(--fg-muted))">
            {{ group.label }}
          </div>
          <div class="space-y-0.5">
            <div
              v-for="item in group.items"
              :key="item.id"
              class="nav-item"
              :class="{ active: currentPage === item.id }"
              @click="navigate(item.id)"
            >
              <span class="text-sm w-4 text-center">{{ item.icon }}</span>
              <span class="flex-1">{{ item.label }}</span>
              <span
                v-if="item.badge"
                class="ml-auto bg-red-500 text-white text-[10px] rounded-full w-4 h-4 flex items-center justify-center"
              >{{ item.badge }}</span>
            </div>
          </div>
        </div>
      </nav>
      <div class="px-3 py-3 border-t border-border">
        <button class="btn-ghost w-full justify-start text-xs" @click="toggleTheme">
          {{ darkMode ? '☀ 浅色模式' : '☾ 深色模式' }}
        </button>
      </div>
    </aside>

    <!-- Mobile overlay -->
    <div
      v-if="sidebarOpen && isMobile"
      class="fixed inset-0 bg-black/30 z-10"
      @click="sidebarOpen = false"
    />

    <!-- Main content -->
    <main class="flex-1 flex flex-col overflow-hidden">
      <!-- Topbar -->
      <header class="h-12 border-b border-border flex items-center px-4 gap-3 shrink-0 bg-surface">
        <button class="btn-ghost btn-sm lg:hidden" @click="sidebarOpen = !sidebarOpen">☰</button>
        <h1 class="text-sm font-semibold">{{ pageTitle }}</h1>
        <div class="ml-auto flex items-center gap-2">
          <span class="tag">本地运行</span>
        </div>
      </header>
      <!-- Page content -->
      <div class="flex-1 overflow-y-auto">
        <component :is="components[currentPage]" :key="currentPage" class="page-enter" />
      </div>
    </main>
  </div>
  <!-- Toast notifications -->
  <div class="fixed top-4 right-4 z-50 space-y-2">
    <div
      v-for="t in toasts.list"
      :key="t.id"
      class="px-4 py-2 rounded-lg text-sm shadow-lg text-white"
      :class="t.type === 'success' ? 'bg-green-600' : t.type === 'error' ? 'bg-red-600' : 'bg-gray-700'"
    >{{ t.message }}</div>
  </div>
</template>
