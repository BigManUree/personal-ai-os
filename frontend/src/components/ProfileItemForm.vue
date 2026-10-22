<script setup lang="ts">
// Mirrors app/profile_propose.py:32-49. The manual category list deliberately
// excludes `inference`: a human typing an item in is stating a fact, and the
// backend rejects it anyway (_validate_manual_fields, app/main.py:2214).
const MANUAL_CATEGORIES = ['basic', 'skill', 'interest', 'goal', 'preference', 'observation']
const SOURCE_TYPES = ['manual', 'memory', 'document', 'project', 'goal', 'task']

const props = defineProps<{ form: any; saving: boolean; editing: boolean }>()
const emit = defineEmits<{ (e: 'submit'): void; (e: 'cancel'): void }>()
</script>

<template>
  <form class="space-y-3" @submit.prevent="emit('submit')">
    <div class="grid grid-cols-2 gap-3">
      <div>
        <label class="label">类别</label>
        <select v-model="props.form.category" name="category" class="input text-xs">
          <option v-for="c in MANUAL_CATEGORIES" :key="c" :value="c">{{ c }}</option>
        </select>
      </div>
      <div>
        <label class="label">来源类型</label>
        <select v-model="props.form.source_type" name="source_type" class="input text-xs">
          <option v-for="s in SOURCE_TYPES" :key="s" :value="s">{{ s }}</option>
        </select>
      </div>
    </div>
    <div>
      <label class="label">标题（可空）</label>
      <input v-model="props.form.title" name="title" class="input text-xs" maxlength="200" />
    </div>
    <div>
      <label class="label">内容 *</label>
      <textarea v-model="props.form.content" name="content" class="input text-xs" rows="3" maxlength="2000"
                placeholder="例如：熟悉 FastAPI 与 SQLite" />
    </div>
    <div class="grid grid-cols-3 gap-3">
      <div>
        <label class="label">置信度 (0–1)</label>
        <input v-model.number="props.form.confidence" name="confidence" type="number" min="0" max="1" step="0.1"
               class="input text-xs" />
      </div>
      <div>
        <label class="label">生效日</label>
        <input v-model="props.form.valid_from" name="valid_from" type="date" class="input text-xs" />
      </div>
      <div>
        <label class="label">失效日</label>
        <input v-model="props.form.valid_until" name="valid_until" type="date" class="input text-xs" />
      </div>
    </div>
    <div>
      <label class="label">来源说明（可空）</label>
      <input v-model="props.form.source_detail" name="source_detail" class="input text-xs" maxlength="500" />
    </div>
    <div class="flex gap-2 pt-1">
      <button type="submit" class="btn-primary btn-sm" :disabled="saving">
        {{ saving ? '保存中…' : (editing ? '保存修改' : '新增条目') }}
      </button>
      <button type="button" class="btn-secondary btn-sm" :disabled="saving" @click="emit('cancel')">取消</button>
    </div>
  </form>
</template>
