<template>
  <section class="page" data-module="crawler">
    <header class="page-head">
      <div>
        <h2>网页采集规则管理</h2>
        <p class="page-desc">维护采集规则草稿；选择器测试通过后生成待发布候选快照，显式确认后才切换生产采集读取的规则版本。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记采集规则</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出网页采集规则清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>生效版本</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row['生效版本'] ?? '未发布' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" :data-testid="`snapshots-${row.id}`" @click="openSnapshots(row)">
              快照与测试
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无网页采集规则数据，可先登记采集规则</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记采集规则</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="activeRule" class="modal-backdrop" data-testid="snapshot-panel">
      <div class="modal snapshot-modal">
        <header class="snapshot-head">
          <div>
            <h3>{{ activeRule['规则编号'] }} · 规则版本 {{ activeRule['规则版本'] ?? '—' }}</h3>
            <p class="page-desc">
              生产采集只读取一个已测试通过的发布快照；当前生效版本：
              <strong data-testid="effective-version">{{ activeRule['生效版本'] || '未发布' }}</strong>
            </p>
          </div>
          <button class="btn ghost" type="button" data-testid="close-snapshots" @click="closeSnapshots">关闭</button>
        </header>

        <p v-if="panelMessage" class="panel-message" :class="{ 'error-text': panelError }" data-testid="panel-message">
          {{ panelMessage }}
        </p>

        <section>
          <h4>当前草稿（未被任何采集任务读取）</h4>
          <dl class="draft-block" data-testid="draft-block">
            <div><dt>规则版本</dt><dd>{{ activeRule['规则版本'] ?? 'v1' }}</dd></div>
            <div><dt>目标网址</dt><dd>{{ activeRule['目标网址'] ?? '—' }}</dd></div>
            <div><dt>页面选择器</dt><dd>{{ activeRule['页面选择器'] ?? '—' }}</dd></div>
            <div><dt>正文选择器</dt><dd>{{ activeRule['正文选择器'] ?? '—' }}</dd></div>
            <div><dt>翻页参数</dt><dd>{{ activeRule['翻页参数'] ?? '—' }}</dd></div>
            <div><dt>草稿状态</dt><dd>{{ activeRule.status }}（生产读取 {{ activeRule['生效版本'] || '无' }}）</dd></div>
          </dl>
        </section>

        <section>
          <h4>规则快照（草稿 / 待发布 / 已发布）</h4>
          <table class="data-table compact">
            <thead>
              <tr>
                <th>版本号</th><th>状态</th><th>目标网址</th><th>页面选择器</th>
                <th>翻页参数</th><th>测试结果</th><th>摘要</th><th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="snap in snapshots" :key="String(snap.id)" :data-testid="`snapshot-${snap.id}`" :data-snapshot-status="String(snap.status)">
                <td>{{ snap['版本号'] }}</td>
                <td>{{ snap.status }}</td>
                <td>{{ snap['目标网址'] }}</td>
                <td class="ellipsis">{{ snap['页面选择器'] }}</td>
                <td class="ellipsis">{{ snap['翻页参数'] }}</td>
                <td>{{ snap['测试结果'] }}</td>
                <td class="ellipsis" :title="String(snap['摘要'])">{{ String(snap['摘要'] ?? '').slice(0, 10) }}</td>
                <td class="row-actions">
                  <button
                    v-for="act in snapshotActions(snap)"
                    :key="act"
                    class="link"
                    type="button"
                    :data-testid="`snapshot-${snap.id}-${act}`"
                    @click="runSnapshotAction(snap, act)"
                  >{{ act }}</button>
                  <button class="link" type="button" :data-testid="`snapshot-${snap.id}-审计`" @click="loadAudit(snap)">审计</button>
                </td>
              </tr>
              <tr v-if="!snapshots.length">
                <td colspan="8" class="empty-state">暂无快照，请先提交选择器测试</td>
              </tr>
            </tbody>
          </table>
        </section>

        <section>
          <h4>选择器测试</h4>
          <form class="test-form" @submit.prevent="submitSelectorTest">
            <label class="create-field">
              <span>规则版本</span>
              <input v-model="testForm['规则版本']" data-testid="testform-version" placeholder="留空自动递增，如 v3" />
            </label>
            <label class="create-field">
              <span>目标网址</span>
              <input v-model="testForm['目标网址']" data-testid="testform-url" />
            </label>
            <label class="create-field">
              <span>页面选择器</span>
              <input v-model="testForm['页面选择器']" data-testid="testform-page-selector" />
            </label>
            <label class="create-field">
              <span>正文选择器</span>
              <input v-model="testForm['正文选择器']" data-testid="testform-body-selector" />
            </label>
            <label class="create-field">
              <span>翻页参数</span>
              <input v-model="testForm['翻页参数']" data-testid="testform-pagination" placeholder="如 page=1&size=20" />
            </label>
            <div class="page-actions">
              <button class="btn" type="button" data-testid="testform-parse" @click="previewParse">解析参数预览</button>
              <button class="btn primary" type="submit" data-testid="testform-submit" :disabled="testing">
                {{ testing ? '测试中…' : '提交选择器测试' }}
              </button>
            </div>
          </form>
          <pre v-if="parsePreview" class="parse-preview" data-testid="parse-preview">{{ parsePreview }}</pre>
          <table class="data-table compact">
            <thead><tr><th>测试编号</th><th>版本号</th><th>结果</th><th>抓取条数</th><th>选择器错误</th><th>测试时间</th><th>候选快照</th></tr></thead>
            <tbody>
              <tr v-for="test in tests" :key="String(test.id)" :data-testid="`test-${test.id}`">
                <td>{{ test['测试编号'] }}</td>
                <td>{{ test['版本号'] }}</td>
                <td>{{ test['结果'] }}</td>
                <td>{{ test['抓取条数'] }}</td>
                <td>{{ test['选择器错误'] }}</td>
                <td>{{ test['测试时间'] }}</td>
                <td>{{ test['快照ID'] ?? '—' }}</td>
              </tr>
              <tr v-if="!tests.length">
                <td colspan="7" class="empty-state">暂无测试记录</td>
              </tr>
            </tbody>
          </table>
        </section>

        <section v-if="auditRows.length">
          <h4>快照审计（旧摘要可查）</h4>
          <table class="data-table compact" data-testid="audit-table">
            <thead><tr><th>时间</th><th>动作</th><th>原状态</th><th>新状态</th><th>说明</th><th>原摘要</th></tr></thead>
            <tbody>
              <tr v-for="(record, index) in auditRows" :key="index">
                <td>{{ record['时间'] }}</td>
                <td>{{ record['动作'] }}</td>
                <td>{{ record['原状态'] ?? '—' }}</td>
                <td>{{ record['新状态'] ?? '—' }}</td>
                <td>{{ record['说明'] }}</td>
                <td class="ellipsis">{{ String(record['原摘要'] ?? '').slice(0, 10) }}</td>
              </tr>
            </tbody>
          </table>
        </section>
      </div>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条网页采集规则记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, any>

const ENDPOINT = '/api/crawler'
const columns = ["规则编号", "目标网址", "页面选择器", "正文选择器", "翻页参数", "抓取频率", "字符编码", "去重策略"]
const actions = ["提交测试", "发布规则", "停用规则"]
const statuses = ["草稿", "测试中", "已发布", "已停用"]
const metricNames = ["规则总数", "已发布规则", "测试中规则"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})

const activeRule = ref<Row | null>(null)
const snapshots = ref<Row[]>([])
const tests = ref<Row[]>([])
const auditRows = ref<Row[]>([])
const panelMessage = ref('')
const panelError = ref(false)
const testing = ref(false)
const testForm = ref<Record<string, string>>({})
const parsePreview = ref('')

const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === '已发布').length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === '测试中').length },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'crawler.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('网页采集规则动作未生效，请稍后重试')
    }
    const payload = await response.json()
    if (payload.ok === false) {
      errorMessage.value = payload.message || '网页采集规则动作未生效'
    }
    await reload()
    if (activeRule.value && Number(activeRule.value.id) === Number(row.id)) {
      await openSnapshots(row)
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '网页采集规则操作失败'
  }
}

function snapshotActions(snapshot: Row): string[] {
  const status = String(snapshot.status)
  if (status === '已归档') return ['重算摘要']
  const list: string[] = []
  if (status === '待发布' || status === '待生效') list.push('确认发布')
  if (status === '已发布') list.push('停用')
  if (status !== '已发布') list.push('归档')
  list.push('重算摘要')
  return list
}

async function openSnapshots(row: Row) {
  panelMessage.value = ''
  panelError.value = false
  auditRows.value = []
  activeRule.value = { ...row }
  testForm.value = {
    '规则版本': '',
    '目标网址': String(row['目标网址'] ?? ''),
    '页面选择器': String(row['页面选择器'] ?? ''),
    '正文选择器': String(row['正文选择器'] ?? ''),
    '翻页参数': String(row['翻页参数'] ?? ''),
  }
  await refreshPanel(Number(row.id))
}

async function refreshPanel(ruleId: number) {
  try {
    const [snapshotResponse, testResponse] = await Promise.all([
      request(`${ENDPOINT}/snapshots?rule_id=${ruleId}`),
      request(`${ENDPOINT}/tests?rule_id=${ruleId}`),
    ])
    if (snapshotResponse.ok) snapshots.value = (await snapshotResponse.json()).items ?? []
    if (testResponse.ok) tests.value = (await testResponse.json()).items ?? []
  } catch (error) {
    panelError.value = true
    panelMessage.value = error instanceof Error ? error.message : '快照读取失败'
  }
}

function closeSnapshots() {
  activeRule.value = null
  snapshots.value = []
  tests.value = []
  auditRows.value = []
  panelMessage.value = ''
}

async function runSnapshotAction(snapshot: Row, action: string) {
  panelMessage.value = ''
  panelError.value = false
  try {
    const response = await request(`${ENDPOINT}/snapshots/${snapshot.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action, expected_version: snapshot['版本号'] }),
    })
    const payload = await response.json()
    panelError.value = !payload.ok
    panelMessage.value = payload.message || '快照动作已提交'
    if (activeRule.value) {
      const ruleId = Number(activeRule.value.id)
      await reload()
      const refreshed = rows.value.find((item) => Number(item.id) === ruleId)
      if (refreshed) activeRule.value = { ...refreshed }
      await refreshPanel(ruleId)
    }
  } catch (error) {
    panelError.value = true
    panelMessage.value = error instanceof Error ? error.message : '快照动作失败'
  }
}

async function loadAudit(snapshot: Row) {
  try {
    const response = await request(`${ENDPOINT}/snapshots/${snapshot.id}/audit`)
    if (!response.ok) throw new Error('审计记录读取失败')
    auditRows.value = (await response.json()).items ?? []
  } catch (error) {
    panelError.value = true
    panelMessage.value = error instanceof Error ? error.message : '审计记录读取失败'
  }
}

async function previewParse() {
  panelMessage.value = ''
  panelError.value = false
  parsePreview.value = ''
  try {
    const response = await request(`${ENDPOINT}/parse`, {
      method: 'POST',
      body: JSON.stringify({ 规则ID: activeRule.value?.id, ...testForm.value }),
    })
    const payload = await response.json()
    panelError.value = !payload.ok
    panelMessage.value = payload.message || '解析完成'
    parsePreview.value = JSON.stringify(payload.preview ?? {}, null, 2)
  } catch (error) {
    panelError.value = true
    panelMessage.value = error instanceof Error ? error.message : '参数解析失败'
  }
}

async function submitSelectorTest() {
  if (!activeRule.value) return
  testing.value = true
  panelMessage.value = ''
  panelError.value = false
  parsePreview.value = ''
  try {
    const response = await request(`${ENDPOINT}/${activeRule.value.id}/tests`, {
      method: 'POST',
      body: JSON.stringify({ ...testForm.value, 自动测试: true }),
    })
    const payload = await response.json()
    panelError.value = !payload.ok
    panelMessage.value = payload.message || '选择器测试已提交'
    await reload()
    await refreshPanel(Number(activeRule.value.id))
  } catch (error) {
    panelError.value = true
    panelMessage.value = error instanceof Error ? error.message : '选择器测试提交失败'
  } finally {
    testing.value = false
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('采集规则列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '网页采集规则列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.snapshot-modal { width: min(1080px, 100%); }
.snapshot-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.snapshot-modal h4 { margin: 18px 0 8px; font-size: 14px; }
.compact th, .compact td { padding: 6px 8px; font-size: 12px; }
.ellipsis { max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.panel-message { background: #f0f6ff; border: 1px solid #cfe0ff; border-radius: 6px; padding: 8px 10px; font-size: 13px; }
.panel-message.error-text { background: #fff4f2; border-color: #f6c6bf; }
.test-form { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px 12px; }
.test-form .page-actions { grid-column: 1 / -1; display: flex; justify-content: flex-end; }
.draft-block { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px 12px; margin: 0; padding: 10px 12px; background: #fafbfc; border: 1px solid var(--border); border-radius: 8px; }
.draft-block dt { font-size: 12px; color: var(--muted); }
.draft-block dd { margin: 2px 0 0; font-size: 13px; word-break: break-all; }
.parse-preview { background: #0f172a; color: #e2e8f0; border-radius: 6px; padding: 10px 12px; font-size: 12px; max-height: 220px; overflow: auto; margin: 0 0 8px; }
</style>
