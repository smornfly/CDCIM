import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'dashboard', component: () => import('@/views/Dashboard.vue') },
    { path: '/alarms', name: 'alarms', component: () => import('@/views/Alarms.vue') },
  ],
})

export default router
