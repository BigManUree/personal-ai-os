<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'
import { useToast } from '../toast'
import Modal from '../components/Modal.vue'
import ProjectForm from '../components/ProjectForm.vue'
import GoalForm from '../components/GoalForm.vue'
import TaskForm from '../components/TaskForm.vue'

const { show } = useToast()

// Mirrors app/main.py PROJECT_EVENT_TYPES.
const EVENT_TYPES = ['progress', 'decision', 'issue', 'learn', 'note']

const projects = ref<any[]>([])
const goals = ref<any[]>([])
const tasks = ref<any[]>([])
const loading = ref(true)

const projectFilter = ref('')
const goalFilter = ref('active')
const taskFilter = ref('')

const expandedId = ref<number | null>(null)
const detail = ref<any | null>(null)
const detailLoading = ref(false)

const editor = ref<'' | 'project' | 'goal' | 'task'>('')
const editingId = ref<number | null>(null)
const saving = ref(false)
const form = ref<any>({})

// One non-empty key at a time keeps destructive/shortcut actions serialized.
const busyKey = ref('')
const confirmKey = ref('')

const evType = ref('progress')
const evContent = ref('')

const today = new Date().toISOString().slice(0, 10)

// ---------- request bodies ----------
// The backend PATCH handlers reuse the POST models and run a full-column
// UPDATE, so a request that omits a field overwrites it with the model default
// (app/main.py:1995-2000). Every write must therefore go through these mappers.

function projectBody(p: any, over: Record<string, any> = {}) {
  const s = { ...p, ...over }
  return {
    name: String(s.name ?? '').trim(),
    description: String(s.description ?? ''),
    tech_stack: String(s.tech_stack ?? ''),
    repo_url: String(s.repo_url ?? ''),
    status: String(s.status ?? 'active'),
  }
}

function goalBody(g: any, over: Record<string, any> = {}) {
  const s = { ...g, ...over }
  return {
    title: String(s.title ?? '').trim(),
    description: String(s.description ?? ''),
    kind: String(s.kind ?? 'long_term'),
    status: String(s.status ?? 'active'),
    measure: String(s.measure ?? ''),
    target_date: s.target_date ? String(s.target_date) : null,
    project_id: s.project_id ?? null,
  }
}

function taskBody(t: any, over: Record<string, any> = {}) {
  const s = { ...t, ...over }
  return {
    title: String(s.title ?? '').trim(),
    detail: String(s.detail ?? ''),
    status: String(s.status ?? 'todo'),
    priority: String(s.priority ?? 'normal'),
    due_date: s.due_date ? String(s.due_date) : null,
    project_id: s.project_id ?? null,
    goal_id: s.goal_id ?? null,
  }
}

// ---------- derived ----------
const filteredProjects = computed(() =>
  projectFilter.value ? projects.value.filter(p => p.status === projectFilter.value) : projects.value)
const filteredGoals = computed(() =>
  goalFilter.value ? goals.value.filter(g => g.status === goalFilter.value) : goals.value)
const filteredTasks = computed(() =>
  taskFilter.value ? tasks.value.filter(t => t.status === taskFilter.value) : tasks.value)

function isOverdue(t: any) {
  return !!t.due_date && t.due_date < today && t.status !== 'done' && t.status !== 'cancelled'
}

function projectName(id: number | null) {
  if (id === null || id === undefined) return ''
  return projects.value.find(p => p.id === id)?.name || ''
}

// ---------- loading ----------
async function load() {
  loading.value = true
  const results = await Promise.allSettled([
    api('/api/projects'),
    api('/api/goals'),
    api('/api/tasks'),
  ])
  const [p, g, t] = results
  projects.value = p.status === 'fulfilled' ? (p.value as any[]) : []
  goals.value = g.status === 'fulfilled' ? (g.value as any[]) : []
  tasks.value = t.status === 'fulfilled' ? (t.value as any[]) : []
  if (results.some(r => r.status === 'rejected')) show('部分数据加载失败，请刷新重试', 'error')
  loading.value = false
}

function merge(kind: 'projects' | 'goals' | 'tasks', id: number, updated: any) {
  const list: any[] = { projects: projects.value, goals: goals.value, tasks: tasks.value }[kind]
  const idx = list.findIndex(x => x.id === id)
  // The task PATCH response has no project_name column; spreading first keeps it.
  if (idx >= 0) list[idx] = { ...list[idx], ...updated }
}

// ---------- editors ----------
function closeEditor() {
  editor.value = ''
  editingId.value = null
  form.value = {}
}

function openProjectForm(p?: any) {
  editingId.value = p ? p.id : null
  form.value = p
    ? {
        name: p.name || '',
        description: p.description || '',
        tech_stack: p.tech_stack || '',
        repo_url: p.repo_url || '',
        status: p.status || 'active',
      }
    : { name: '', description: '', tech_stack: '', repo_url: '', status: 'active' }
  editor.value = 'project'
}

function openGoalForm(g?: any) {
  editingId.value = g ? g.id : null
  form.value = g
    ? {
        title: g.title || '',
        description: g.description || '',
        kind: g.kind || 'long_term',
        status: g.status || 'active',
        measure: g.measure || '',
        target_date: g.target_date || '',
        project_id: g.project_id ?? null,
      }
    : { title: '', description: '', kind: 'long_term', status: 'active', measure: '', target_date: '', project_id: null }
  editor.value = 'goal'
}

function openTaskForm(t?: any) {
  editingId.value = t ? t.id : null
  form.value = t
    ? {
        title: t.title || '',
        detail: t.detail || '',
        status: t.status || 'todo',
        priority: t.priority || 'normal',
        due_date: t.due_date || '',
        project_id: t.project_id ?? null,
        goal_id: t.goal_id ?? null,
      }
    : { title: '', detail: '', status: 'todo', priority: 'normal', due_date: '', project_id: null, goal_id: null }
  editor.value = 'task'
}

async function submitProject() {
  if (saving.value) return
  const body = projectBody(form.value)
  if (!body.name) { show('项目名称不能为空', 'error'); return }
  saving.value = true
  try {
    if (editingId.value === null) {
      await api('/api/projects', { method: 'POST', body: JSON.stringify(body) })
      show('项目已创建', 'success')
    } else {
      await api(`/api/projects/${editingId.value}`, { method: 'PATCH', body: JSON.stringify(body) })
      show('项目已保存', 'success')
    }
    closeEditor()
    await load()
  } catch (e: any) { show(e.message, 'error') }
  finally { saving.value = false }
}

async function submitGoal() {
  if (saving.value) return
  const body = goalBody(form.value)
  if (!body.title) { show('目标标题不能为空', 'error'); return }
  saving.value = true
  try {
    if (editingId.value === null) {
      await api('/api/goals', { method: 'POST', body: JSON.stringify(body) })
      show('目标已创建', 'success')
    } else {
      await api(`/api/goals/${editingId.value}`, { method: 'PATCH', body: JSON.stringify(body) })
      show('目标已保存', 'success')
    }
    closeEditor()
    await load()
  } catch (e: any) { show(e.message, 'error') }
  finally { saving.value = false }
}

async function submitTask() {
  if (saving.value) return
  const body = taskBody(form.value)
  if (!body.title) { show('任务标题不能为空', 'error'); return }
  saving.value = true
  try {
    if (editingId.value === null) {
      await api('/api/tasks', { method: 'POST', body: JSON.stringify(body) })
      show('任务已创建', 'success')
    } else {
      await api(`/api/tasks/${editingId.value}`, { method: 'PATCH', body: JSON.stringify(body) })
      show('任务已保存', 'success')
    }
    closeEditor()
    await load()
  } catch (e: any) { show(e.message, 'error') }
  finally { saving.value = false }
}

// ---------- shortcuts ----------
async function setTaskStatus(t: any, status: string) {
  if (busyKey.value) return
  busyKey.value = `task-${t.id}`
  try {
    const updated: any = await api(`/api/tasks/${t.id}`, {
      method: 'PATCH',
      body: JSON.stringify(taskBody(t, { status })),
    })
    merge('tasks', t.id, updated)
    show('任务状态已更新', 'success')
    // A shortcut can move the row out of the active tab; reload to keep the list honest.
    if (taskFilter.value && taskFilter.value !== status) await load()
  } catch (e: any) {
    show(e.message, 'error')
    await load()
  } finally { busyKey.value = '' }
}

async function setGoalStatus(g: any, status: string) {
  if (busyKey.value) return
  busyKey.value = `goal-${g.id}`
  try {
    await api(`/api/goals/${g.id}`, { method: 'PATCH', body: JSON.stringify(goalBody(g, { status })) })
    show('目标状态已更新', 'success')
    await load()
  } catch (e: any) { show(e.message, 'error'); await load() }
  finally { busyKey.value = '' }
}

// ---------- delete ----------
async function removeProject(p: any) {
  if (busyKey.value) return
  busyKey.value = `project-${p.id}`
  try {
    await api(`/api/projects/${p.id}`, { method: 'DELETE' })
    show('项目已删除', 'success')
    if (expandedId.value === p.id) { expandedId.value = null; detail.value = null }
    confirmKey.value = ''
    await load()
  } catch (e: any) { show(e.message, 'error') }
  finally { busyKey.value = '' }
}

async function removeGoal(g: any) {
  if (busyKey.value) return
  busyKey.value = `goal-${g.id}`
  try {
    await api(`/api/goals/${g.id}`, { method: 'DELETE' })
    show('目标已删除', 'success')
    confirmKey.value = ''
    await load()
  } catch (e: any) { show(e.message, 'error') }
  finally { busyKey.value = '' }
}

async function removeTask(t: any) {
  if (busyKey.value) return
  busyKey.value = `task-${t.id}`
  try {
    await api(`/api/tasks/${t.id}`, { method: 'DELETE' })
    show('任务已删除', 'success')
    confirmKey.value = ''
    await load()
  } catch (e: any) { show(e.message, 'error') }
  finally { busyKey.value = '' }
}

// ---------- project detail ----------
async function toggleDetail(p: any) {
  if (expandedId.value === p.id) {
    expandedId.value = null
    detail.value = null
    return
  }
  expandedId.value = p.id
  detail.value = null
  detailLoading.value = true
  evContent.value = ''
  try {
    detail.value = await api(`/api/projects/${p.id}`)
  } catch (e: any) { show(e.message, 'error') }
  finally { detailLoading.value = false }
}

async function addEvent(p: any) {
  const content = evContent.value.trim()
  if (!content) { show('事件内容不能为空', 'error'); return }
  if (busyKey.value) return
  busyKey.value = `event-${p.id}`
  try {
    const created: any = await api(`/api/projects/${p.id}/events`, {
      method: 'POST',
      body: JSON.stringify({ event_type: evType.value, content }),
    })
    if (detail.value && detail.value.id === p.id) detail.value.events.unshift(created)
    p.event_count = (p.event_count || 0) + 1
    evContent.value = ''
    show('事件已记录', 'success')
  } catch (e: any) { show(e.message, 'error') }
  finally { busyKey.value = '' }
}

onMounted(load)
</script>

<template>
  <div class="mx-auto max-w-4xl space-y-6 p-6">
    <div class="flex items-start justify-between gap-3">
      <div>
        <h2 class="text-xl font-semibold">项目与任务</h2>
        <p class="mt-1 text-xs" style="color: rgb(var(--fg-muted))">
          项目 / 目标 / 任务的写操作全部落在本地 SQLite；改状态、记事件都会写入行为事件流。
        </p>
      </div>
      <button class="btn-primary btn-sm" @click="openProjectForm()">新建项目</button>
    </div>

    <div v-if="loading" class="space-y-2">
      <div v-for="i in 4" :key="i" class="skeleton h-16" />
    </div>

    <template v-else>
      <!-- ================= projects ================= -->
      <section class="card p-4" data-section="projects">
        <div class="mb-3 flex items-center justify-between gap-2">
          <h3 class="font-medium">项目 ({{ projects.length }})</h3>
          <div class="flex flex-wrap gap-1">
            <button class="btn-sm" :class="projectFilter === '' ? 'tab-on' : 'btn-secondary'"
                    @click="projectFilter = ''">全部</button>
            <button v-for="s in ['planning', 'active', 'paused', 'completed', 'archived']" :key="s"
                    class="btn-sm" :class="projectFilter === s ? 'tab-on' : 'btn-secondary'"
                    @click="projectFilter = s">{{ s }}</button>
          </div>
        </div>

        <div v-if="!filteredProjects.length" class="py-6 text-center text-sm" style="color: rgb(var(--fg-muted))">
          {{ projectFilter ? '该状态下没有项目' : '暂无项目，点右上角「新建项目」开始' }}
        </div>

        <div v-for="p in filteredProjects" :key="p.id" class="mb-2 rounded-md border border-border p-3">
          <div class="flex flex-wrap items-center gap-2">
            <span class="font-medium text-sm">{{ p.name }}</span>
            <span class="tag">{{ p.status }}</span>
            <span class="text-xs" style="color: rgb(var(--fg-muted))">
              {{ p.event_count || 0 }} 事件 · {{ p.goal_count || 0 }} 进行中目标 · {{ p.open_task_count || 0 }} 未完成任务
            </span>
            <div class="ml-auto flex flex-wrap gap-1">
              <button class="btn-ghost btn-sm" @click="toggleDetail(p)">
                {{ expandedId === p.id ? '收起' : '详情' }}
              </button>
              <button class="btn-secondary btn-sm" @click="openProjectForm(p)">编辑</button>
              <template v-if="confirmKey === `project-${p.id}`">
                <button class="btn-sm" style="color: rgb(var(--danger))" :disabled="busyKey === `project-${p.id}`"
                        @click="removeProject(p)">确认删除</button>
                <button class="btn-ghost btn-sm" @click="confirmKey = ''">取消</button>
              </template>
              <button v-else class="btn-ghost btn-sm" @click="confirmKey = `project-${p.id}`">删除</button>
            </div>
          </div>

          <div v-if="confirmKey === `project-${p.id}`" class="mt-2 text-xs" style="color: rgb(var(--danger))">
            删除后该项目的事件会一并删除；关联的任务与目标会保留，但会脱离本项目。
          </div>

          <div v-if="p.description" class="mt-1 text-sm" style="color: rgb(var(--fg-secondary))">{{ p.description }}</div>
          <div class="mt-1 flex flex-wrap gap-3 text-xs" style="color: rgb(var(--fg-muted))">
            <span v-if="p.tech_stack">技术栈：{{ p.tech_stack }}</span>
            <span v-if="p.repo_url">仓库：{{ p.repo_url }}</span>
          </div>

          <!-- detail -->
          <div v-if="expandedId === p.id" class="mt-3 space-y-3 border-t border-border pt-3">
            <div v-if="detailLoading" class="skeleton h-12" />
            <template v-else-if="detail">
              <div>
                <div class="mb-1 text-xs font-medium">记录事件 ({{ detail.events.length }})</div>
                <div v-if="!detail.events.length" class="text-xs" style="color: rgb(var(--fg-muted))">暂无事件</div>
                <ul class="space-y-1">
                  <li v-for="ev in detail.events" :key="ev.id" class="text-xs">
                    <span class="tag">{{ ev.event_type }}</span>
                    <span class="ml-1">{{ ev.content }}</span>
                    <span class="ml-1" style="color: rgb(var(--fg-muted))">{{ String(ev.created_at).slice(0, 16) }}</span>
                  </li>
                </ul>
                <div class="mt-2 flex flex-wrap items-center gap-2">
                  <select v-model="evType" name="event_type" class="input w-32 text-xs">
                    <option v-for="t in EVENT_TYPES" :key="t" :value="t">{{ t }}</option>
                  </select>
                  <input v-model="evContent" name="event_content" class="input flex-1 text-xs" maxlength="4000" placeholder="记一条进展 / 决策 / 问题…" />
                  <button class="btn-secondary btn-sm" :disabled="busyKey === `event-${p.id}`" @click="addEvent(p)">记录</button>
                </div>
              </div>
              <div class="grid gap-2 sm:grid-cols-2">
                <div>
                  <div class="mb-1 text-xs font-medium">目标 ({{ detail.goals.length }})</div>
                  <div v-if="!detail.goals.length" class="text-xs" style="color: rgb(var(--fg-muted))">无</div>
                  <div v-for="g in detail.goals" :key="g.id" class="text-xs">
                    {{ g.title }} <span class="tag">{{ g.status }}</span>
                  </div>
                </div>
                <div>
                  <div class="mb-1 text-xs font-medium">任务 ({{ detail.tasks.length }})</div>
                  <div v-if="!detail.tasks.length" class="text-xs" style="color: rgb(var(--fg-muted))">无</div>
                  <div v-for="t in detail.tasks" :key="t.id" class="text-xs">
                    {{ t.title }} <span class="tag">{{ t.status }}</span>
                  </div>
                </div>
              </div>
            </template>
          </div>
        </div>
      </section>

      <!-- ================= goals ================= -->
      <section class="card p-4" data-section="goals">
        <div class="mb-3 flex items-center justify-between gap-2">
          <h3 class="font-medium">目标 ({{ goals.length }})</h3>
          <div class="flex flex-wrap gap-1">
            <button class="btn-sm" :class="goalFilter === '' ? 'tab-on' : 'btn-secondary'"
                    @click="goalFilter = ''">全部</button>
            <button v-for="s in ['active', 'done', 'dropped']" :key="s" class="btn-sm"
                    :class="goalFilter === s ? 'tab-on' : 'btn-secondary'"
                    @click="goalFilter = s">{{ s }}</button>
            <button class="btn-primary btn-sm" @click="openGoalForm()">新建目标</button>
          </div>
        </div>

        <div v-if="!filteredGoals.length" class="py-6 text-center text-sm" style="color: rgb(var(--fg-muted))">
          {{ goalFilter ? '该状态下没有目标' : '暂无目标' }}
        </div>

        <div v-for="g in filteredGoals" :key="g.id" class="mb-2 rounded-md border border-border p-3">
          <div class="flex flex-wrap items-center gap-2">
            <span class="text-sm font-medium">{{ g.title }}</span>
            <span class="tag">{{ g.kind }}</span>
            <span class="tag">{{ g.status }}</span>
            <span v-if="g.target_date" class="text-xs" style="color: rgb(var(--fg-muted))">目标日 {{ g.target_date }}</span>
            <span v-if="g.project_id" class="text-xs" style="color: rgb(var(--fg-muted))">· {{ projectName(g.project_id) }}</span>
            <div class="ml-auto flex flex-wrap gap-1">
              <button v-if="g.status !== 'done'" class="btn-secondary btn-sm" :disabled="busyKey === `goal-${g.id}`"
                      @click="setGoalStatus(g, 'done')">标记完成</button>
              <button v-if="g.status !== 'active'" class="btn-secondary btn-sm" :disabled="busyKey === `goal-${g.id}`"
                      @click="setGoalStatus(g, 'active')">恢复</button>
              <button v-if="g.status !== 'dropped'" class="btn-ghost btn-sm" :disabled="busyKey === `goal-${g.id}`"
                      @click="setGoalStatus(g, 'dropped')">放弃</button>
              <button class="btn-secondary btn-sm" @click="openGoalForm(g)">编辑</button>
              <template v-if="confirmKey === `goal-${g.id}`">
                <button class="btn-sm" style="color: rgb(var(--danger))" :disabled="busyKey === `goal-${g.id}`"
                        @click="removeGoal(g)">确认删除</button>
                <button class="btn-ghost btn-sm" @click="confirmKey = ''">取消</button>
              </template>
              <button v-else class="btn-ghost btn-sm" @click="confirmKey = `goal-${g.id}`">删除</button>
            </div>
          </div>
          <div v-if="confirmKey === `goal-${g.id}`" class="mt-2 text-xs" style="color: rgb(var(--danger))">
            删除后关联任务会解除目标关联，但任务本身保留。
          </div>
          <div v-if="g.measure" class="mt-1 text-xs" style="color: rgb(var(--fg-secondary))">衡量：{{ g.measure }}</div>
        </div>
      </section>

      <!-- ================= tasks ================= -->
      <section class="card p-4" data-section="tasks">
        <div class="mb-3 flex items-center justify-between gap-2">
          <h3 class="font-medium">任务 ({{ tasks.length }})</h3>
          <div class="flex flex-wrap gap-1">
            <button class="btn-sm" :class="taskFilter === '' ? 'tab-on' : 'btn-secondary'"
                    @click="taskFilter = ''">全部</button>
            <button v-for="s in ['todo', 'doing', 'done', 'cancelled']" :key="s" class="btn-sm"
                    :class="taskFilter === s ? 'tab-on' : 'btn-secondary'"
                    @click="taskFilter = s">{{ s }}</button>
            <button class="btn-primary btn-sm" @click="openTaskForm()">新建任务</button>
          </div>
        </div>

        <div v-if="!filteredTasks.length" class="py-6 text-center text-sm" style="color: rgb(var(--fg-muted))">
          {{ taskFilter ? '该状态下没有任务' : '暂无任务' }}
        </div>

        <div v-for="t in filteredTasks" :key="t.id" class="mb-2 rounded-md border border-border p-3">
          <div class="flex flex-wrap items-center gap-2">
            <span class="text-sm" :style="t.status === 'done' || t.status === 'cancelled' ? 'text-decoration: line-through; opacity: .6' : ''">
              {{ t.title }}
            </span>
            <span class="tag">{{ t.status }}</span>
            <span class="tag" :class="t.priority === 'high' ? '!text-red-500' : ''">{{ t.priority }}</span>
            <span v-if="t.project_name" class="text-xs" style="color: rgb(var(--fg-muted))">{{ t.project_name }}</span>
            <span v-if="t.due_date" class="text-xs"
                  :style="isOverdue(t) ? 'color: rgb(var(--danger))' : 'color: rgb(var(--fg-muted))'">
              {{ isOverdue(t) ? '已过期 ' : '' }}{{ t.due_date }}
            </span>
            <div class="ml-auto flex flex-wrap gap-1">
              <button v-if="t.status === 'todo'" class="btn-secondary btn-sm" :disabled="busyKey === `task-${t.id}`"
                      @click="setTaskStatus(t, 'doing')">开始</button>
              <button v-if="t.status === 'todo' || t.status === 'doing'" class="btn-primary btn-sm"
                      :disabled="busyKey === `task-${t.id}`" @click="setTaskStatus(t, 'done')">完成</button>
              <button v-if="t.status === 'done'" class="btn-secondary btn-sm" :disabled="busyKey === `task-${t.id}`"
                      @click="setTaskStatus(t, 'todo')">重新打开</button>
              <button v-if="t.status !== 'cancelled'" class="btn-ghost btn-sm" :disabled="busyKey === `task-${t.id}`"
                      @click="setTaskStatus(t, 'cancelled')">取消</button>
              <button class="btn-secondary btn-sm" @click="openTaskForm(t)">编辑</button>
              <template v-if="confirmKey === `task-${t.id}`">
                <button class="btn-sm" style="color: rgb(var(--danger))" :disabled="busyKey === `task-${t.id}`"
                        @click="removeTask(t)">确认删除</button>
                <button class="btn-ghost btn-sm" @click="confirmKey = ''">取消</button>
              </template>
              <button v-else class="btn-ghost btn-sm" @click="confirmKey = `task-${t.id}`">删除</button>
            </div>
          </div>
          <div v-if="confirmKey === `task-${t.id}`" class="mt-2 text-xs" style="color: rgb(var(--danger))">
            删除后不可恢复。
          </div>
          <div v-if="t.detail" class="mt-1 whitespace-pre-wrap text-xs" style="color: rgb(var(--fg-secondary))">{{ t.detail }}</div>
        </div>
      </section>
    </template>

    <Modal :open="editor === 'project'" :title="editingId === null ? '新建项目' : '编辑项目'" @close="closeEditor">
      <ProjectForm :form="form" :saving="saving" :editing="editingId !== null"
                   @submit="submitProject" @cancel="closeEditor" />
    </Modal>
    <Modal :open="editor === 'goal'" :title="editingId === null ? '新建目标' : '编辑目标'" @close="closeEditor">
      <GoalForm :form="form" :projects="projects" :saving="saving" :editing="editingId !== null"
                @submit="submitGoal" @cancel="closeEditor" />
    </Modal>
    <Modal :open="editor === 'task'" :title="editingId === null ? '新建任务' : '编辑任务'" @close="closeEditor">
      <TaskForm :form="form" :projects="projects" :goals="goals" :saving="saving" :editing="editingId !== null"
                @submit="submitTask" @cancel="closeEditor" />
    </Modal>
  </div>
</template>
