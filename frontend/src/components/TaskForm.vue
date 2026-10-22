<script setup lang="ts">
import { computed } from 'vue'

// Mirrors app/main.py TASK_STATUSES / TASK_PRIORITIES.
const STATUSES = ['todo', 'doing', 'done', 'cancelled']
const PRIORITIES = ['low', 'normal', 'high']

const props = defineProps<{ form: any; projects: any[]; goals: any[]; saving: boolean; editing: boolean }>()
const emit = defineEmits<{ (e: 'submit'): void; (e: 'cancel'): void }>()

// Goals carry project_id; when a project is selected only its goals are offered.
// Unlinked goals stay reachable by clearing the project filter.
const visibleGoals = computed(() => {
  const pid = props.form.project_id
  if (pid === null || pid === undefined) return props.goals
  return props.goals.filter((g: any) => g.project_id === pid || g.project_id === null)
})
</script>

<template>
  <form class="space-y-3" @submit.prevent="emit('submit')">
    <div>
      <label class="label">标题 *</label>
      <input v-model="props.form.title" name="title" class="input" maxlength="300" placeholder="任务标题" />
    </div>
    <div>
      <label class="label">详情</label>
      <textarea v-model="props.form.detail" name="detail" class="input" rows="3" maxlength="8000" />
    </div>
    <div class="grid grid-cols-2 gap-3">
      <div>
        <label class="label">状态</label>
        <select v-model="props.form.status" name="status" class="input">
          <option v-for="s in STATUSES" :key="s" :value="s">{{ s }}</option>
        </select>
      </div>
      <div>
        <label class="label">优先级</label>
        <select v-model="props.form.priority" name="priority" class="input">
          <option v-for="p in PRIORITIES" :key="p" :value="p">{{ p }}</option>
        </select>
      </div>
    </div>
    <div class="grid grid-cols-2 gap-3">
      <div>
        <label class="label">截止日期</label>
        <input v-model="props.form.due_date" name="due_date" type="date" class="input" />
      </div>
      <div>
        <label class="label">关联项目</label>
        <select v-model="props.form.project_id" name="project_id" class="input">
          <option :value="null">不关联</option>
          <option v-for="p in projects" :key="p.id" :value="p.id">{{ p.name }}</option>
        </select>
      </div>
    </div>
    <div>
      <label class="label">关联目标</label>
      <select v-model="props.form.goal_id" name="goal_id" class="input">
        <option :value="null">不关联</option>
        <option v-for="g in visibleGoals" :key="g.id" :value="g.id">{{ g.title }}</option>
      </select>
    </div>
    <div class="flex gap-2 pt-1">
      <button type="submit" class="btn-primary btn-sm" :disabled="saving">
        {{ saving ? '保存中…' : (editing ? '保存修改' : '创建任务') }}
      </button>
      <button type="button" class="btn-secondary btn-sm" :disabled="saving" @click="emit('cancel')">取消</button>
    </div>
  </form>
</template>
