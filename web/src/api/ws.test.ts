/** WS 客户端纯函数单测:指数退避(design.md 6.3)。 */

import { describe, expect, it } from 'vitest'
import { reconnectDelayMs } from './ws'

describe('reconnectDelayMs(指数退避 1s→2s→…→30s 上限)', () => {
  it('按 2 的幂增长', () => {
    expect(reconnectDelayMs(1)).toBe(1000)
    expect(reconnectDelayMs(2)).toBe(2000)
    expect(reconnectDelayMs(3)).toBe(4000)
    expect(reconnectDelayMs(5)).toBe(16000)
  })

  it('上限 30 秒', () => {
    expect(reconnectDelayMs(6)).toBe(30000)
    expect(reconnectDelayMs(10)).toBe(30000)
    expect(reconnectDelayMs(50)).toBe(30000)
  })
})
