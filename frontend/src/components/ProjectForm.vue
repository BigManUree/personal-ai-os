<script setup lang="ts">
// Mirrors app/main.py PROJECT_STATUSES. Kept inline (not fetched) because the
// backend has no metadata endpoint; the pytest UI assertions keep them in sync.
const STATUSES = ['planning', 'active', 'paused', 'completed', 'archived']

const props = defineProps<{ form: any; saving: boolean; editing: boolean }>()
const emit = defineEmits<{ (e: 'submit'): void; (e: 'cancel'): void }>()
</script>

<template>
  <form class="space-y-3" @submit.prevent="emit('submit')">
    <div>
      <label class="label">名称 *</label>
      <input v-model="props.form.name" name="name" class="input" maxlength="200" placeholder="项目名称" />
    </div>
    <div>
      <label class="label">描述</label>
      <textarea v-model="props.form.description" name="description" class="input" rows="3" maxlength="8000" placeholder="这个项目要解决什么" />
    </div>
    <div>
      <label class="label">技术栈</label>
      <input v-model="props.form.tech_stack" name="tech_stack" class="input" maxlength="1000" placeholder="FastAPI / SQLite" />
    </div>
    <div>
      <label class="label">仓库地址</label>
      <input v-model="props.form.repo_url" name="repo_url" class="input" maxlength="500" placeholder="https://…" />
    </div>
    <div>
      <label class="label">状态</label>
      <select v-model="props.form.status" name="status" class="input">
        <option v-for="s in STATUSES" :key="s" :value="s">{{ s }}</option>
      </select>
    </div>
    <div class="flex gap-2 pt-1">
      <button type="submit" class="btn-primary btn-sm" :disabled="saving">
        {{ saving ? '保存中…' : (editing ? '保存修改' : '创建项目') }}
      </button>
      <button type="button" class="btn-secondary btn-sm" :disabled="saving" @click="emit('cancel')">取消</button>
    </div>
  </form>
</template>
