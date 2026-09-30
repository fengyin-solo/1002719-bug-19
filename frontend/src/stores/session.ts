import { defineStore } from 'pinia'

export const ROLE_ACCEPTOR = '修洗验收员'
export const ROLE_READONLY = '只读员'

export type SessionState = {
  operator: string
  yard: string
  roles: string[]
  shiftLabel: string
  scope: string
}

export const useSessionStore = defineStore('session', {
  state: (): SessionState => ({
    operator: '赵验收',
    yard: '东区堆场',
    roles: [ROLE_ACCEPTOR],
    shiftLabel: '白班 08:00-20:00',
    scope: '港口集装箱作业管理平台',
  }),
  getters: {
    canOperate: (state) => state.operator.length > 0,
    isReadonly: (state) => state.roles.includes(ROLE_READONLY),
  },
  actions: {
    setShift(label: string) {
      this.shiftLabel = label
    },
    // 交接/换岗后切换当前操作人；已验收记录上的验收人是后端快照，不会被这里覆盖
    setIdentity(payload: { operator: string; yard: string; roles: string[] }) {
      this.operator = payload.operator
      this.yard = payload.yard
      this.roles = payload.roles
    },
  },
})
