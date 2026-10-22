<script setup lang="ts">
import { ref, onMounted, nextTick } from 'vue'
import { api, esc } from '../api'
import { useToast } from '../toast'
const { show } = useToast()

const messages = ref<any[]>([{ role: 'assistant', content: '你好！我是你的个人 AI 助手。先在「个人档案」填写目标，再开始对话。' }])
const input = ref('')
const sending = ref(false)
const convList = ref<any[]>([])
const currentConvId = ref<number | null>(null)
const convLoading = ref(true)

async function loadConvs() {
  try {
    convList.value = await api('/api/conversations')
  } catch { convList.value = [] } finally { convLoading.value = false }
}

async function loadConv(id: number) {
  currentConvId.value = id
  try {
    const msgs = await api(`/api/conversations/${id}/messages`)
    messages.value = msgs.map((m: any) => ({ role: m.role, content: m.content }))
  } catch (e: any) { show(e.message, 'error') }
  await nextTick()
  scrollBottom()
}

function scrollBottom() {
  const el = document.getElementById('chat-msgs')
  if (el) el.scrollTop = el.scrollHeight
}

async function send() {
  const text = input.value.trim()
  if (!text || sending.value) return
  input.value = ''
  messages.value.push({ role: 'user', content: text })
  sending.value = true
  await nextTick(); scrollBottom()
  try {
    const data: any = await api('/api/chat', {
      method: 'POST', body: JSON.stringify({ message: text, conversation_id: currentConvId.value }),
    })
    currentConvId.value = data.conversation_id
    messages.value.push({ role: 'assistant', content: data.answer })
    loadConvs()
  } catch (e: any) {
    messages.value.push({ role: 'assistant', content: '请求失败：' + e.message })
  } finally {
    sending.value = false
    await nextTick(); scrollBottom()
  }
}

function newChat() {
  currentConvId.value = null
  messages.value = [{ role: 'assistant', content: '新对话已开始。' }]
}

onMounted(loadConvs)
</script>

<template>
  <div class="flex h-full">
    <!-- Conversation list -->
    <div class="w-60 shrink-0 border-r border-border overflow-y-auto p-3 space-y-1" style="background: rgb(var(--sidebar-bg))">
      <button class="btn-primary w-full mb-3" @click="newChat">＋ 新对话</button>
      <div v-if="convLoading" class="space-y-2">
        <div v-for="i in 4" :key="i" class="skeleton h-10" />
      </div>
      <div v-else-if="!convList.length" class="text-sm" style="color: rgb(var(--fg-muted))">暂无历史对话</div>
      <div v-for="c in convList" :key="c.id"
        class="p-2 rounded-md cursor-pointer text-sm hover:bg-elevated transition-colors"
        :class="{ 'bg-accent-soft': c.id === currentConvId }"
        @click="loadConv(c.id)">
        <div class="font-medium truncate">{{ c.title || '未命名对话' }}</div>
        <div class="text-xs truncate" style="color: rgb(var(--fg-muted))">{{ c.last_message?.slice(0, 40) }}</div>
      </div>
    </div>
    <!-- Chat area -->
    <div class="flex-1 flex flex-col">
      <div id="chat-msgs" class="flex-1 overflow-y-auto p-6 space-y-4 max-w-3xl mx-auto w-full">
        <div v-for="(m, i) in messages" :key="i"
          class="flex" :class="{ 'justify-end': m.role === 'user' }">
          <div class="max-w-[80%] rounded-lg px-4 py-2.5"
            :class="m.role === 'user' ? 'bg-accent text-white' : 'bg-elevated border border-border'">
            <div class="whitespace-pre-wrap text-sm">{{ m.content }}</div>
          </div>
        </div>
        <div v-if="sending" class="text-sm" style="color: rgb(var(--fg-muted))">AI 正在思考…</div>
      </div>
      <div class="border-t border-border p-4">
        <form class="flex gap-2 max-w-3xl mx-auto" @submit.prevent="send">
          <input v-model="input" class="input flex-1" placeholder="问问你的 AI…" :disabled="sending" />
          <button class="btn-primary" type="submit" :disabled="sending">发送</button>
        </form>
      </div>
    </div>
  </div>
</template>
