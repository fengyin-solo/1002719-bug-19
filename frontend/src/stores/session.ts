import { defineStore } from 'pinia'

/** 验收归属：本堆场 + 修洗验收员；只读岗与其他角色冲突时以更严的只读限制为准。 */
export const ACCEPT_ROLE = '修洗验收员'
export const READONLY_ROLE = '只读岗'

export type Actor = {
  name: string
  yard: string
  roles: string[]
}

/** 演示用身份：覆盖本堆场验收、外堆场验收、只读岗等情形。 */
export const ACTOR_PRESETS: Actor[] = [
  { name: '王验收', yard: '一号堆场', roles: [ACCEPT_ROLE] },
  { name: '赵值班', yard: '二号堆场', roles: [ACCEPT_ROLE] },
  { name: '周查看', yard: '一号堆场', roles: [READONLY_ROLE] },
  { name: '王验收（兼只读）', yard: '一号堆场', roles: [ACCEPT_ROLE, READONLY_ROLE] },
  { name: '钱外场', yard: '三号堆场', roles: [ACCEPT_ROLE] },
]

export const useSessionStore = defineStore('session', {
  state: () => ({
    operator: '值班管理员',
    shiftLabel: '白班 08:00-20:00',
    scope: '港口集装箱作业管理平台',
    name: '王验收',
    yard: '一号堆场',
    roles: [ACCEPT_ROLE] as string[],
  }),
  getters: {
    canOperate: (state) => state.operator.length > 0,
    isReadonly: (state) => state.roles.includes(READONLY_ROLE),
    canAccept: (state) => state.roles.includes(ACCEPT_ROLE) && !state.roles.includes(READONLY_ROLE),
    actor: (state): Actor => ({ name: state.name, yard: state.yard, roles: [...state.roles] }),
  },
  actions: {
    setShift(label: string) {
      this.shiftLabel = label
    },
    setActor(actor: Actor) {
      this.name = actor.name
      this.yard = actor.yard
      this.roles = [...actor.roles]
      this.operator = `${actor.name}（${actor.yard}）`
    },
  },
})
