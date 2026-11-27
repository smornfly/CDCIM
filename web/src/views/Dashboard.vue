<script setup lang="ts">
import { onMounted, ref } from 'vue'
import * as echarts from 'echarts'
import { getHistory, getLatest } from '@/api/client'

const pointId = ref(7001)
const latest = ref<{ ts: string; value: number | null } | null>(null)
const error = ref('')
const chartEl = ref<HTMLDivElement | null>(null)
let chart: echarts.ECharts | null = null

function toIso(d: Date): string { return d.toISOString() }

async function load(): Promise<void> {
  error.value = ''
  const end = new Date()
  const start = new Date(end.getTime() - 6 * 60 * 60 * 1000)
  try {
    const [head, series] = await Promise.all([
      getLatest(pointId.value),
      getHistory(pointId.value, toIso(start), toIso(end), '1m'),
    ])
    latest.value = head
    if (!chartEl.value) return
    chart ??= echarts.init(chartEl.value)
    chart.setOption({
      backgroundColor: 'transparent',
      xAxis: { type: 'category', data: series.map((p) => p.ts) },
      yAxis: { type: 'value' },
      series: [{ type: 'line', data: series.map((p) => p.value), smooth: true }],
      tooltip: { trigger: 'axis' },
    })
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  }
}

onMounted(load)
</script>

<template>
  <section class="card">
    <h2>测点 {{ pointId }} 总览</h2>
    <p v-if="error" class="err">{{ error }}</p>
    <p v-else>最新值：{{ latest?.value ?? '—' }}（{{ latest?.ts ?? '—' }}）</p>
    <div ref="chartEl" style="height: 320px" />
    <p class="hint">P4 只接最小查询链；告警与 3D 在 P5。</p>
  </section>
</template>

<style scoped>
.err { color: #f85149; }
.hint { color: #8b949e; font-size: 12px; }
</style>
