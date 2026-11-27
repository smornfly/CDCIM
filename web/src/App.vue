<script setup lang="ts">
import { useRoute } from 'vue-router'

const route = useRoute()
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <strong>CDCIM · 数据中心动环监控</strong>
      <span class="phase">P4 骨架</span>
      <nav>
        <RouterLink to="/">总览</RouterLink>
        <RouterLink to="/alarms">告警</RouterLink>
      </nav>
    </header>
    <main>
      <RouterView />
    </main>
    <footer>当前路由：{{ route.path }} · 3D 场景在 P5 接入</footer>
  </div>
</template>

<style>
:root { font-family: system-ui, sans-serif; color-scheme: dark; }
body { margin: 0; background: #10141a; color: #e6edf3; }
.shell { max-width: 1180px; margin: 0 auto; padding: 16px; }
.topbar { display: flex; gap: 16px; align-items: center; padding: 12px 0; border-bottom: 1px solid #30363d; }
.topbar nav { margin-left: auto; display: flex; gap: 14px; }
.topbar a { color: #79c0ff; text-decoration: none; }
.phase { color: #d29922; font-size: 12px; border: 1px solid #d29922; padding: 2px 6px; border-radius: 4px; }
footer { margin-top: 24px; color: #8b949e; font-size: 12px; }
.card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; }
</style>
