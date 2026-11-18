<template>
  <section class="page" data-module="monitor">
    <header class="page-head">
      <div><h2>采集监控</h2><p class="page-desc">按采集规则发布快照的版本号汇总抓取条数与选择器错误，消费快照后把差异回写采集规则。</p></div>
      <div class="page-actions">
        <button class="btn primary" type="button" data-testid="sync-snapshots" @click="syncSnapshots">
          消费发布快照并回写
        </button>
      </div>
    </header>

    <p v-if="message" class="sync-message" :class="{ 'error-text': syncError }" data-testid="sync-message">{{ message }}</p>

    <div class="analysis-summary">
      <article v-for="m in summaryMetrics" :key="m.label" class="analysis-card" data-testid="kpi-card">
        <span style="font-size:12px;color:var(--muted)">{{ m.label }}</span>
        <strong style="font-size:22px;display:block">{{ m.value }}</strong>
        <span v-if="m.trend !== 0" :class="m.trend > 0 ? 'trend-up' : 'trend-down'" style="font-size:12px">
          {{ m.trend > 0 ? '↑' : '↓' }}{{ Math.abs(m.trend) }}%
        </span>
      </article>
    </div>

    <section>
      <h3 class="section-title">按选择器版本汇总</h3>
      <table class="data-table" data-testid="version-table">
        <thead>
          <tr><th>面板编号</th><th>规则编号</th><th>版本号</th><th>快照状态</th><th>抓取条数</th><th>选择器错误</th><th>成功率</th><th>差异</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in versionRows" :key="row.id" :data-testid="`version-${row.id}`">
            <td>{{ row['面板编号'] }}</td>
            <td>{{ row['规则编号'] }}</td>
            <td>{{ row['版本号'] }}</td>
            <td>{{ row['快照状态'] }}</td>
            <td>{{ row['抓取条数'] }}</td>
            <td>{{ row['选择器错误'] }}</td>
            <td>{{ row['成功率'] }}</td>
            <td>{{ row['差异'] }}</td>
          </tr>
          <tr v-if="!versionRows.length">
            <td colspan="8" class="empty-state">暂无发布快照，请先在网页采集规则中提交选择器测试并发布</td>
          </tr>
        </tbody>
      </table>
    </section>

    <form class="analysis-form" @submit.prevent="runAnalysis">
      <label v-for="field in paramFields" :key="field"><span>{{ field }}</span>
        <select v-if="field.includes('方法')||field.includes('策略')||field.includes('粒度')" v-model="params[field]"><option v-for="o in options[field]" :key="o" :value="o">{{ o }}</option></select>
        <input v-else v-model="params[field]" :placeholder="`请输入${field}`" />
      </label>
      <button class="btn primary" type="submit" data-testid="run-analysis">运行分析</button>
    </form>
    <div v-if="chartData.length" class="analysis-chart">
      <div v-for="(bar, i) in chartData" :key="i" class="analysis-bar" :style="{ height: barHeight(bar.value) + 'px' }"><span>{{ bar.label }}</span></div>
    </div>
    <table class="data-table" v-if="results.length">
      <thead><tr><th v-for="c in resultColumns" :key="c">{{ c }}</th></tr></thead>
      <tbody><tr v-for="row in results" :key="row.id"><td v-for="c in resultColumns" :key="c">{{ row[c] ?? '—' }}</td></tr></tbody>
    </table>
    <p v-if="!results.length && !loading" style="text-align:center;color:var(--muted)">请配置参数后运行分析</p>

    <section>
      <h3 class="section-title">待办工作台</h3>
      <table class="data-table" data-testid="todo-table">
        <thead>
          <tr><th>待办编号</th><th>关联规则</th><th>版本号</th><th>核对差异</th><th>抓取条数</th><th>选择器错误</th><th>状态</th><th>可执行动作</th></tr>
        </thead>
        <tbody>
          <tr v-for="todo in todos" :key="todo.id" :data-testid="`todo-${todo.id}`" :data-status="String(todo.status)">
            <td>{{ todo['待办编号'] }}</td>
            <td>{{ todo['关联规则'] }}</td>
            <td>{{ todo['版本号'] }}</td>
            <td>{{ todo['核对差异'] }}</td>
            <td>{{ todo['抓取条数'] }}</td>
            <td>{{ todo['选择器错误'] }}</td>
            <td>{{ todo.status }}</td>
            <td class="row-actions">
              <button
                v-for="action in todoActions(todo)"
                :key="action"
                class="link"
                type="button"
                :data-testid="`todo-${todo.id}-${action}`"
                @click="runTodoAction(todo, action)"
              >{{ action }}</button>
            </td>
          </tr>
          <tr v-if="!todos.length">
            <td colspan="8" class="empty-state">暂无待办，消费发布快照后会自动生成</td>
          </tr>
        </tbody>
      </table>
    </section>
  </section>
</template>
<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { request } from '@/api/client'
const ENDPOINT = '/api/monitor'
const summaryMetrics = ref<{label:string;value:number|string;trend:number}[]>([])
const versionRows = ref<any[]>([])
const todos = ref<any[]>([])
const results = ref<any[]>([]); const chartData = ref<{label:string;value:number}[]>([])
const loading = ref(false)
const message = ref(''); const syncError = ref(false)
const params = ref<Record<string,string>>({})
const paramFields = ["面板编号", "采集源总数", "今日采集量", "成功率", "平均延迟", "失败重试", "异常告警", "更新时间"].filter((f: string) => !['编号','名称','版本'].includes(f)).slice(0, 4)
const options: Record<string,string[]> = { '统计粒度': ['按日','按周','按月'], '分组字段': ['全量','按类型','按区域'], '比较方法': ['环比','同比','绝对值'], '时间范围': ['最近7天','最近30天','最近90天'] }
const resultColumns = computed(() => ["分组", "规则编号", "版本号", "抓取条数", "选择器错误", "成功率"])
function barHeight(v: number) { const max = Math.max(...chartData.value.map(d => d.value), 1); return Math.max(10, (v / max) * 140) }
function todoActions(todo: any): string[] {
  if (todo.status === '待处理') return ['开始处理', '忽略待办']
  if (todo.status === '处理中') return ['标记已处理', '忽略待办']
  return []
}
async function fetchSummary() {
  try {
    const res = await request(`${ENDPOINT}/summary`)
    const data = await res.json()
    summaryMetrics.value = data.metrics || []
    versionRows.value = data.versions || []
  } catch(e) {}
}
async function fetchTodos() {
  try {
    const res = await request(`${ENDPOINT}/todos`)
    if (!res.ok) return
    todos.value = (await res.json()).items || []
  } catch(e) {}
}
async function syncSnapshots() {
  message.value = ''
  syncError.value = false
  try {
    const res = await request(`${ENDPOINT}/sync`, { method: 'POST', body: JSON.stringify({ 操作人: '值班管理员' }) })
    const data = await res.json()
    message.value = data.message || '发布快照消费完成'
    syncError.value = res.ok === false
    await Promise.all([fetchSummary(), fetchTodos()])
  } catch (error) {
    syncError.value = true
    message.value = error instanceof Error ? error.message : '发布快照消费失败'
  }
}
async function runTodoAction(todo: any, action: string) {
  message.value = ''
  syncError.value = false
  try {
    const res = await request(`${ENDPOINT}/todos/${todo.id}/actions`, { method: 'POST', body: JSON.stringify({ action }) })
    const data = await res.json()
    message.value = data.message || '待办动作已提交'
    syncError.value = data.ok === false
    await fetchTodos()
  } catch (error) {
    syncError.value = true
    message.value = error instanceof Error ? error.message : '待办动作失败'
  }
}
async function runAnalysis() {
  loading.value = true
  try {
    const res = await request(`${ENDPOINT}/run`, { method: 'POST', body: JSON.stringify({ values: params.value }) })
    const data = await res.json()
    results.value = data.items || []
    chartData.value = (data.items || []).map((r: any) => ({ label: String(r['版本号'] ?? r['分组']), value: Number(r['抓取条数']) || 0 })).slice(0, 6)
  } catch(e) { results.value = [] }
  loading.value = false
}
onMounted(async () => {
  await Promise.all([fetchSummary(), fetchTodos()])
})
</script>

<style scoped>
.section-title { font-size: 15px; margin: 18px 0 8px; }
.sync-message { background: #f0f6ff; border: 1px solid #cfe0ff; border-radius: 6px; padding: 8px 10px; font-size: 13px; }
.sync-message.error-text { background: #fff4f2; border-color: #f6c6bf; }
</style>
