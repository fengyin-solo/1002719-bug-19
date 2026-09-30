<template>
  <section class="page" data-module="repair">
    <header class="page-head">
      <div>
        <h2>箱体修洗管理</h2>
        <p class="page-desc">维护修洗任务，围绕任务编号、箱号、损伤类型、修理等级做登记、筛选与状态流转。验收按堆场归属收紧。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记修洗任务</button>
        <button class="btn" type="button" @click="exportRows">导出箱体修洗清单</button>
      </div>
    </header>

    <div class="actor-bar">
      <span class="actor-label">当前身份（演示切换）：</span>
      <select v-model="actorPreset" class="actor-select" @change="applyActor">
        <option v-for="(item, index) in actorPresets" :key="item.name" :value="index">
          {{ item.name }} · {{ item.yard }} · {{ item.roles.join('、') || '无角色' }}
        </option>
      </select>
      <span class="actor-hint">验收只能由本堆场「{{ ACCEPT_ROLE }}」执行；{{ READONLY_ROLE }}不得改损伤类型与修理等级。</span>
    </div>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card" :class="{ 'stat-hot': item.hot }">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">
            <button v-if="column === '任务编号'" class="link" type="button" @click="showDetail(row)">
              {{ row[column] ?? '—' }}
            </button>
            <template v-else>{{ row[column] ?? '—' }}</template>
          </td>
          <td class="row-actions">
            <button
              v-for="action in allowedActions(row)"
              :key="action"
              class="link"
              type="button"
              :disabled="inFlight === `${row.id}:${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" @click="openEdit(row)">改判定</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无箱体修洗数据，可先登记修洗任务</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条箱体修洗记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <div v-if="editing" class="modal-mask" @click.self="closeEdit">
      <div class="modal-card">
        <h3>修改判定字段 · {{ editing.row['任务编号'] }}</h3>
        <p class="modal-tip">所属堆场：{{ editing.row['所属堆场'] }} ｜ 当前身份：{{ session.name }}（{{ session.roles.join('、') || '无角色' }}）</p>
        <label class="filter-item">
          <span>损伤类型</span>
          <input v-model="editing.values['损伤类型']" />
        </label>
        <label class="filter-item">
          <span>修理等级</span>
          <input v-model="editing.values['修理等级']" />
        </label>
        <div class="modal-actions">
          <button class="btn" type="button" @click="closeEdit">取消</button>
          <button class="btn primary" type="button" :disabled="editing.saving" @click="saveEdit">保存</button>
        </div>
      </div>
    </div>

    <div v-if="detail" class="modal-mask" @click.self="detail = null">
      <div class="modal-card">
        <h3>任务明细 · {{ detail['任务编号'] }}</h3>
        <p class="modal-tip">列表与明细同源，验收人交接后仍以首次验收落账人为准。</p>
        <table class="data-table">
          <tbody>
            <tr v-for="column in columns" :key="column">
              <th>{{ column }}</th>
              <td>{{ detail[column] ?? '—' }}</td>
            </tr>
            <tr>
              <th>状态</th>
              <td>{{ detail.status }}</td>
            </tr>
          </tbody>
        </table>
        <div class="modal-actions">
          <button class="btn primary" type="button" @click="detail = null">关闭</button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'
import {
  ACCEPT_ROLE,
  ACTOR_PRESETS,
  READONLY_ROLE,
  useSessionStore,
} from '@/stores/session'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/repair'
const columns = ['任务编号', '箱号', '所属堆场', '损伤类型', '修理等级', '修理人员', '清洗方式', '验收人员']
// 每个状态只亮出它真正能执行的下一步，已验收不再有动作按钮，杜绝逆向流转。
const NEXT_ACTIONS: Record<string, string[]> = {
  待修洗: ['安排修洗'],
  修理中: ['开始修理'],
  待验收: ['验收完成'],
  已完成: [],
}

const session = useSessionStore()
const actorPresets = ACTOR_PRESETS

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const inFlight = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

// 看板数字：初值为 0，每次列表刷新时由 /stats 按明细重算覆盖。
const stats = ref([
  { label: '待修洗箱', value: 0 },
  { label: '修理中箱', value: 0 },
  { label: '待验收箱', value: 0, hot: true },
  { label: '已完成箱', value: 0 },
])

// 每个「任务+动作」固定一个幂等号：连点/重放同一条只算一次。
const requestKeys = reactive<Record<string, string>>({})

const editing = ref<{ row: Row; values: Record<string, string>; saving: boolean } | null>(null)
const detail = ref<Row | null>(null)
const actorPreset = ref(0)

function allowedActions(row: Row): string[] {
  return NEXT_ACTIONS[String(row.status)] ?? []
}

function applyActor(event: Event) {
  const index = Number((event.target as HTMLSelectElement).value)
  session.setActor(ACTOR_PRESETS[index])
  errorMessage.value = ''
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '修洗任务登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  const key = `${row.id}:${action}`
  inFlight.value = key
  if (!requestKeys[key]) {
    requestKeys[key] = (crypto.randomUUID?.() ?? `req-${Date.now()}-${Math.random()}`)
  }
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({
        values: { action },
        request_id: requestKeys[key],
        actor: session.actor,
      }),
    })
    const payload = await response.json().catch(() => null)
    // 业务驳回同样走 200 + ok:false，必须看账单不认 HTTP 状态。
    if (!response.ok || !payload?.ok) {
      throw new Error(payload?.message || '箱体修洗动作未生效，请稍后重试')
    }
    // 成功后作废幂等号；下一轮新状态是新的一次提交。
    delete requestKeys[key]
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '箱体修洗操作失败'
    await reload()
  } finally {
    inFlight.value = ''
  }
}

function openEdit(row: Row) {
  editing.value = {
    row,
    values: {
      损伤类型: String(row['损伤类型'] ?? ''),
      修理等级: String(row['修理等级'] ?? ''),
    },
    saving: false,
  }
}

function closeEdit() {
  editing.value = null
}

async function saveEdit() {
  if (!editing.value) {
    return
  }
  const { row, values } = editing.value
  editing.value.saving = true
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}`, {
      method: 'PATCH',
      body: JSON.stringify({ values, actor: session.actor }),
    })
    const payload = await response.json().catch(() => null)
    if (!response.ok || !payload?.ok) {
      throw new Error(payload?.message || '判定字段未更新')
    }
    editing.value = null
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '判定字段修改失败'
  } finally {
    if (editing.value) {
      editing.value.saving = false
    }
  }
}

async function showDetail(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}`)
    if (!response.ok) {
      throw new Error('修洗任务明细读取失败')
    }
    detail.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '修洗任务明细读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const params = new URLSearchParams()
  if (filters.value['任务编号']) params.set('keyword', filters.value['任务编号'])
  if (filters.value['箱号']) params.set('keyword', filters.value['箱号'])
  if (filters.value['所属堆场']) params.set('yard', filters.value['所属堆场'])
  const query = params.toString()
  try {
    const [listResponse, statsResponse] = await Promise.all([
      request(`${ENDPOINT}?${query}`),
      request(`${ENDPOINT}/stats${filters.value['所属堆场'] ? `?yard=${encodeURIComponent(filters.value['所属堆场'])}` : ''}`),
    ])
    if (!listResponse.ok) {
      throw new Error('修洗任务列表读取失败')
    }
    const payload = await listResponse.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length

    // 看板待验收数随明细重算，不再使用可写脏的缓存标志。
    if (statsResponse.ok) {
      const statsPayload = await statsResponse.json()
      const counts = statsPayload.counts ?? {}
      stats.value = [
        { label: '待修洗箱', value: counts['待修洗'] ?? 0 },
        { label: '修理中箱', value: counts['修理中'] ?? 0 },
        { label: '待验收箱', value: counts['待验收'] ?? 0, hot: true },
        { label: '已完成箱', value: counts['已完成'] ?? 0 },
      ]
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '箱体修洗列表读取失败'
  }
}

onMounted(reload)
</script>
