<template>
  <section class="page" data-module="repair">
    <header class="page-head">
      <div>
        <h2>箱体修洗管理</h2>
        <p class="page-desc">验收按堆场归属收紧：只有本堆场的修洗验收员能点验收，只读岗不能改损伤类型与修理等级；重复提交只算一次。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" :disabled="session.isReadonly" @click="openCreate">登记修洗任务</button>
        <button class="btn" type="button" @click="exportRows">导出箱体修洗清单</button>
      </div>
    </header>

    <!-- 当前操作人：切换堆场/角色可复现“越权当场驳回”，换人可验证交接后仍认出原验收人 -->
    <div class="identity-bar">
      <label>
        操作人
        <input v-model="identity.name" list="operator-names" />
        <datalist id="operator-names">
          <option value="赵验收" />
          <option value="钱验收" />
          <option value="甲组验收" />
          <option value="乙组验收" />
        </datalist>
      </label>
      <label>
        所属堆场
        <select v-model="identity.yard">
          <option>东区堆场</option>
          <option>西区堆场</option>
        </select>
      </label>
      <label>
        角色（可多选，冲突时按更严的只读）
        <span class="role-toggles">
          <label class="role-chip">
            <input type="checkbox" :value="ROLE_ACCEPTOR" v-model="identity.roles" />修洗验收员
          </label>
          <label class="role-chip">
            <input type="checkbox" :value="ROLE_READONLY" v-model="identity.roles" />只读员
          </label>
        </span>
      </label>
      <button class="btn ghost" type="button" @click="applyIdentity">交接/换人</button>
      <span v-if="session.isReadonly" class="role-flag">当前为只读岗，所有写操作会被驳回</span>
    </div>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card" :class="{ hot: item.label === '待验收箱' }">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <label class="filter-item">
        <span>状态</span>
        <select v-model="filters.status">
          <option value="">全部</option>
          <option v-for="s in statuses" :key="s">{{ s }}</option>
        </select>
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
            {{ displayCell(row, column) }}
          </td>
          <td class="row-actions">
            <button class="link" type="button" @click="openDetail(row)">明细</button>
            <button
              v-for="action in nextActionOf(row)"
              :key="action"
              class="link"
              type="button"
              :disabled="!canMutate(row)"
              :title="canMutate(row) ? action : actionDisabledReason(row)"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无箱体修洗数据，可先登记修洗任务</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条箱体修洗记录</span>
      <span v-if="message" :class="messageOk ? 'ok-text' : 'error-text'">{{ message }}</span>
    </footer>

    <!-- 明细：与列表读同一份后端数据，验收人两处必然一致 -->
    <div v-if="detail" class="modal-mask" @click.self="detail = null">
      <div class="modal">
        <h3>修洗任务明细 · {{ detail['任务编号'] }}</h3>
        <dl class="detail-grid">
          <template v-for="column in detailColumns" :key="column">
            <dt>{{ column }}</dt>
            <dd>{{ displayCell(detail, column) }}</dd>
          </template>
          <dt>数据版本</dt>
          <dd>v{{ detail.version ?? 1 }}</dd>
        </dl>
        <div class="modal-foot">
          <button class="btn" type="button" :disabled="!canEdit(detail)" @click="openEdit(detail)">编辑明细</button>
          <button class="btn ghost" type="button" @click="detail = null">关闭</button>
        </div>
      </div>
    </div>

    <!-- 编辑：只读岗改损伤类型/修理等级会被后端当场驳回 -->
    <div v-if="editing" class="modal-mask" @click.self="editing = null">
      <div class="modal">
        <h3>编辑修洗任务 · {{ editing['任务编号'] }}</h3>
        <p class="modal-tip">状态、所属堆场、验收人员只能由验收环节写入；只读岗无权修改损伤类型与修理等级。</p>
        <div class="form-grid">
          <label v-for="field in editFields" :key="field">
            <span>{{ field }}{{ field === '损伤类型' || field === '修理等级' ? '（受保护）' : '' }}</span>
            <input v-model="editForm[field]" />
          </label>
        </div>
        <div class="modal-foot">
          <button class="btn primary" type="button" @click="saveEdit">保存</button>
          <button class="btn ghost" type="button" @click="editing = null">取消</button>
        </div>
      </div>
    </div>

    <!-- 登记 -->
    <div v-if="creating" class="modal-mask" @click.self="creating = false">
      <div class="modal">
        <h3>登记修洗任务</h3>
        <div class="form-grid">
          <label v-for="field in createFields" :key="field">
            <span>{{ field }}</span>
            <input v-model="createForm[field]" :placeholder="`请输入${field}`" />
          </label>
        </div>
        <div class="modal-foot">
          <button class="btn primary" type="button" @click="saveCreate">提交登记</button>
          <button class="btn ghost" type="button" @click="creating = false">取消</button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'
import { ROLE_ACCEPTOR, ROLE_READONLY, useSessionStore } from '@/stores/session'

type Row = Record<string, string | number | null>
type Stats = { label: string; value: number }[]

const ENDPOINT = '/api/repair'
const columns = ['任务编号', '箱号', '所属堆场', '损伤类型', '修理等级', '修理人员', '清洗方式', '验收人员', '修洗状态']
const detailColumns = [...columns, '验收堆场', '验收时间']
const editFields = ['箱号', '损伤类型', '修理等级', '修理人员', '清洗方式']
const createFields = ['任务编号', '箱号', '所属堆场', '损伤类型', '修理等级', '修理人员', '清洗方式']
const statuses = ['待修洗', '修理中', '待验收', '已完成']
const nextActionByStatus: Record<string, string[]> = {
  待修洗: ['安排修洗'],
  修理中: ['开始修理'],
  待验收: ['验收完成'],
  已完成: [],
}

const session = useSessionStore()
const identity = reactive({
  name: session.operator,
  yard: session.yard,
  roles: [...session.roles],
})

const rows = ref<Row[]>([])
const total = ref(0)
const message = ref('')
const messageOk = ref(false)
const filters = ref<Record<string, string>>({})
const filterFields = ['任务编号', '箱号', '所属堆场']
const stats = ref<Stats>([
  { label: '待修洗箱', value: 0 },
  { label: '修理中箱', value: 0 },
  { label: '待验收箱', value: 0 },
  { label: '已完成箱', value: 0 },
])

const detail = ref<Row | null>(null)
const editing = ref<Row | null>(null)
const editForm = reactive<Record<string, string>>({})
const creating = ref(false)
const createForm = reactive<Record<string, string>>({ 所属堆场: session.yard })

function flash(text: string, ok = false) {
  message.value = text
  messageOk.value = ok
}

function displayCell(row: Row, column: string): string {
  const value = row[column]
  if (value === null || value === undefined || value === '') {
    return column === '验收人员' && row.status === '已完成' ? '—' : '—'
  }
  return String(value)
}

// 已完成不再出动作按钮；其余状态只允许点“下一步”，从界面上杜绝跳级与回退
function nextActionOf(row: Row): string[] {
  return nextActionByStatus[String(row.status)] ?? []
}

function canMutate(row: Row): boolean {
  if (session.isReadonly) return false
  if (String(row['所属堆场']) !== session.yard) return false
  return true
}

function canEdit(row: Row): boolean {
  return canMutate(row) && String(row.status) !== '已完成'
}

function actionDisabledReason(row: Row): string {
  if (session.isReadonly) return '只读岗不能执行状态流转'
  if (String(row['所属堆场']) !== session.yard) return `该任务归属${row['所属堆场']}，验收只能本堆场做`
  return ''
}

function actorPayload() {
  return { name: session.operator, yard: session.yard, roles: session.roles }
}

// 每次提交一个新 request_id：后端幂等账本据此识别“同一条重复提交”
function newRequestId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) return crypto.randomUUID()
  return `req-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function applyIdentity() {
  session.setIdentity({
    operator: identity.name.trim() || '未署名',
    yard: identity.yard,
    roles: identity.roles.length ? identity.roles : [],
  })
  flash(`已切换为：${session.operator} · ${session.yard} · ${session.roles.join('、') || '无角色'}`, true)
  void reload()
}

function openDetail(row: Row) {
  detail.value = null
  // 明细单独取后端，保证与列表两处验收人同源一致
  request(`${ENDPOINT}/${row.id}`)
    .then((response) => (response.ok ? response.json() : Promise.reject(new Error('明细读取失败'))))
    .then((data: Row) => {
      detail.value = data
    })
    .catch((error: unknown) => flash(error instanceof Error ? error.message : '明细读取失败'))
}

function openEdit(row: Row) {
  editing.value = row
  editForm['箱号'] = String(row['箱号'] ?? '')
  editForm['损伤类型'] = String(row['损伤类型'] ?? '')
  editForm['修理等级'] = String(row['修理等级'] ?? '')
  editForm['修理人员'] = String(row['修理人员'] ?? '')
  editForm['清洗方式'] = String(row['清洗方式'] ?? '')
}

function openCreate() {
  creating.value = true
}

async function reload() {
  const query = new URLSearchParams()
  Object.entries(filters.value).forEach(([key, value]) => {
    if (value) query.set(key, value)
  })
  try {
    const [listResponse, statsResponse] = await Promise.all([
      request(`${ENDPOINT}?${query.toString()}`),
      request(`${ENDPOINT}/stats`),
    ])
    if (!listResponse.ok || !statsResponse.ok) throw new Error('修洗任务列表读取失败')
    const payload = await listResponse.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    const counters = await statsResponse.json()
    stats.value = [
      { label: '待修洗箱', value: counters['待修洗'] ?? 0 },
      { label: '修理中箱', value: counters['修理中'] ?? 0 },
      { label: '待验收箱', value: counters['待验收'] ?? 0 },
      { label: '已完成箱', value: counters['已完成'] ?? 0 },
    ]
    if (detail.value) {
      const latest = rows.value.find((row) => String(row.id) === String(detail.value?.id))
      if (latest) detail.value = latest
    }
  } catch (error) {
    flash(error instanceof Error ? error.message : '箱体修洗列表读取失败')
  }
}

async function runAction(action: string, row: Row) {
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({
        action,
        actor: actorPayload(),
        request_id: newRequestId(),
        expected_version: typeof row.version === 'number' ? row.version : Number(row.version) || null,
      }),
    })
    const payload = await response.json()
    // 越权、只读、并发落败都以 ok=false 回来，直接展示后端写明的原因，不谎报成功
    if (!payload.ok) {
      flash(payload.message || '操作被驳回')
      await reload()
      return
    }
    flash(
      payload.deduplicated ? `该提交已处理过，未重复记账：${payload.message}` : payload.message,
      true,
    )
    await reload()
  } catch (error) {
    flash(error instanceof Error ? error.message : '箱体修洗操作失败')
  }
}

async function saveEdit() {
  if (!editing.value) return
  const values: Record<string, string> = {}
  editFields.forEach((field) => {
    if (editForm[field]) values[field] = editForm[field]
  })
  try {
    const response = await request(`${ENDPOINT}/${editing.value.id}`, {
      method: 'PATCH',
      body: JSON.stringify({
        values,
        actor: actorPayload(),
        request_id: newRequestId(),
        expected_version: Number(editing.value.version) || null,
      }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      flash(payload.message || '修改被驳回')
      return
    }
    editing.value = null
    flash(payload.message, true)
    await reload()
  } catch (error) {
    flash(error instanceof Error ? error.message : '修改失败')
  }
}

async function saveCreate() {
  const values: Record<string, string> = {}
  createFields.forEach((field) => {
    if (createForm[field]) values[field] = createForm[field]
  })
  try {
    const response = await request(`${ENDPOINT}`, {
      method: 'POST',
      body: JSON.stringify({ values, actor: actorPayload(), request_id: newRequestId() }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      flash(payload.message || '登记被驳回')
      return
    }
    creating.value = false
    flash(payload.deduplicated ? payload.message : '修洗任务已登记', true)
    await reload()
  } catch (error) {
    flash(error instanceof Error ? error.message : '登记失败')
  }
}

onMounted(reload)
</script>

<style scoped>
.identity-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 12px;
  font-size: 13px;
}
.identity-bar label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
  color: var(--muted);
}
.identity-bar input,
.identity-bar select {
  font-size: 13px;
  padding: 4px 8px;
  border: 1px solid var(--border);
  border-radius: 6px;
}
.role-toggles {
  display: flex;
  gap: 8px;
  flex-direction: row !important;
}
.role-chip {
  flex-direction: row !important;
  align-items: center;
  gap: 4px;
  color: #1f2937 !important;
  white-space: nowrap;
}
.role-flag {
  color: #b42318;
  font-size: 12px;
}
.stat-card.hot {
  border-color: var(--brand);
}
.ok-text {
  color: #157347;
}
.link:disabled {
  color: #a3aec2;
  cursor: not-allowed;
}
.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 20;
}
.modal {
  width: 560px;
  max-width: calc(100vw - 32px);
  background: #fff;
  border-radius: 10px;
  padding: 18px 20px;
}
.modal h3 {
  margin: 0 0 10px;
  font-size: 15px;
}
.modal-tip {
  font-size: 12px;
  color: var(--muted);
  margin: 0 0 10px;
}
.detail-grid {
  display: grid;
  grid-template-columns: 110px 1fr;
  gap: 6px 12px;
  margin: 0;
  font-size: 13px;
}
.detail-grid dt {
  color: var(--muted);
}
.detail-grid dd {
  margin: 0;
}
.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px 14px;
}
.form-grid label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
  color: var(--muted);
}
.form-grid input {
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 13px;
}
.modal-foot {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 14px;
}
</style>
