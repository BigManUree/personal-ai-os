<script setup lang="ts">
// Mirrors app/main.py GOAL_KINDS / GOAL_STATUSES.
const KINDS = ['long_term', 'milestone']
const STATUSES = ['active', 'done', 'dropped']

const props = defineProps<{ form: any; projects: any[]; saving: boolean; editing: boolean }>()
const emit = defineEmits<{ (e: 'submit'): void; (e: 'cancel'): void }>()
</script>

<template>
  <form class="space-y-3" @submit.prevent="emit('submit')">
    <div>
      <label class="label">标题 *</label>
      <input v-model="props.form.title" name="title" class="input" maxlength="300" placeholder="目标标题" />
    </div>
    <div>
      <label class="label">描述</label>
      <textarea v-model="props.form.description" name="description" class="input" rows="2" maxlength="8000" />
    </div>
    <div class="grid grid-cols-2 gap-3">
      <div>
        <label class="label">类型</label>
        <select v-model="props.form.kind" name="kind" class="input">
          <option v-for="k in KINDS" :key="k" :value="k">{{ k }}</option>
        </select>
      </div>
      <div>
        <label class="label">状态</label>
        <select v-model="props.form.status" name="status" class="input">
          <option v-for="s in STATUSES" :key="s" :value="s">{{ s }}</option>
        </select>
      </div>
    </div>
    <div>
      <label class="label">衡量标准</label>
      <input v-model="props.form.measure" name="measure" class="input" maxlength="1000" placeholder="怎么算完成" />
    </div>
    <div class="grid grid-cols-2 gap-3">
      <div>
        <label class="label">目标日期</label>
        <input v-model="props.form.target_date" name="target_date" type="date" class="input" />
      </div>
      <div>
        <label class="label">关联项目</label>
        <select v-model="props.form.project_id" name="project_id" class="input">
          <option :value="null">不关联</option>
          <option v-for="p in projects" :key="p.id" :value="p.id">{{ p.name }}</option>
        </select>
      </div>
    </div>
    <div class="flex gap-2 pt-1">
      <button type="submit" class="btn-primary btn-sm" :disabled="saving">
        {{ saving ? '保存中…' : (editing ? '保存修改' : '创建目标') }}
      </button>
      <button type="button" class="btn-secondary btn-sm" :disabled="saving" @click="emit('cancel')">取消</button>
    </div>
  </form>
</template>
