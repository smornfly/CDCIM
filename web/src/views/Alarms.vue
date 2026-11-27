<script setup lang="ts">
import { ref } from 'vue'

interface Row {
  id: number
  device_code: string
  level: string
  status: string
  occurred_at: string
}
const rows = ref<Row[]>([])
const note = ref('P4 未实现告警查询接口（返回 501），表格仅展示结构；P5 接入。')
</script>

<template>
  <section class="card">
    <h2>告警列表（占位）</h2>
    <p class="note">{{ note }}</p>
    <table>
      <thead><tr><th>ID</th><th>设备</th><th>级别</th><th>状态</th><th>时间</th></tr></thead>
      <tbody>
        <tr v-if="rows.length === 0"><td colspan="5">暂无数据（接口未实现）</td></tr>
        <tr v-for="r in rows" :key="r.id">
          <td>{{ r.id }}</td><td>{{ r.device_code }}</td><td>{{ r.level }}</td>
          <td>{{ r.status }}</td><td>{{ r.occurred_at }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<style scoped>
.note { color: #d29922; font-size: 13px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 8px; border-bottom: 1px solid #30363d; }
</style>
