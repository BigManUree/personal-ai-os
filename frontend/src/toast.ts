import { reactive } from 'vue'

export interface Toast { id: number; message: string; type: 'success' | 'error' | 'info' }
const state = reactive({ list: [] as Toast[] })
let id = 0

export function useToast() {
  function show(message: string, type: Toast['type'] = 'info') {
    const t: Toast = { id: ++id, message, type }
    state.list.push(t)
    setTimeout(() => {
      const i = state.list.findIndex(x => x.id === t.id)
      if (i >= 0) state.list.splice(i, 1)
    }, 3000)
  }
  return { toasts: state, show }
}
