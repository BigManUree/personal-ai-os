<script setup lang="ts">
import { onMounted, onUnmounted } from 'vue'

const props = defineProps<{ open: boolean; title: string }>()
const emit = defineEmits<{ (e: 'close'): void }>()

// Esc closes the dialog, but only while it is actually open. Without the
// `props.open` guard a stale Esc press would reset the parent form.
function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape' && props.open) emit('close')
}

onMounted(() => window.addEventListener('keydown', onKey))
onUnmounted(() => window.removeEventListener('keydown', onKey))
</script>

<template>
  <div
    v-if="open"
    class="fixed inset-0 z-40 flex items-start justify-center overflow-y-auto bg-black/40 p-4"
    @click.self="emit('close')"
  >
    <div class="card mt-16 w-full max-w-lg space-y-3 p-5">
      <div class="flex items-center justify-between">
        <h3 class="font-medium">{{ title }}</h3>
        <button class="btn-ghost btn-sm" @click="emit('close')">关闭</button>
      </div>
      <slot />
    </div>
  </div>
</template>
