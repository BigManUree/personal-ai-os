import { createApp } from 'vue'
import App from './App.vue'
import './style.css'

// Theme: respect saved preference or system
const saved = localStorage.getItem('paos-theme')
if (saved === 'dark' || (!saved && window.matchMedia('(prefers-color-scheme: dark)').matches)) {
  document.documentElement.classList.add('dark')
}

createApp(App).mount('#app')
