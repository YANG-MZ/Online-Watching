/** ClockEstimator 单测(sync-protocol.md §3)。 */

import { describe, expect, it } from 'vitest'
import { ClockEstimator, median } from './clock'

describe('median', () => {
  it('奇数个取中位,偶数个取平均', () => {
    expect(median([3, 1, 2])).toBe(2)
    expect(median([4, 1, 2, 3])).toBe(2.5)
  })
})

describe('ClockEstimator', () => {
  it('无样本时估算为 null,serverNow 不偏移', () => {
    const c = new ClockEstimator()
    expect(c.estimate()).toBeNull()
    expect(c.serverNow(1000)).toBe(1000)
  })

  it('样本超过 8 个时仅保留最近 8 个', () => {
    const c = new ClockEstimator()
    for (let i = 0; i < 12; i++) c.add(1.0, i)
    expect(c.size).toBe(8)
    // 最早 4 个(0~3)被丢弃,保留 4~11;rtt 相同取最早 3 个(4,5,6)→ 中位 5
    expect(c.estimate()).toBe(5)
  })

  it('取 rtt 最小的 3 个样本的 offset 中位数', () => {
    const c = new ClockEstimator()
    c.add(10, 100) // rtt 大 → 不可信
    c.add(10, 100)
    c.add(10, 100)
    c.add(1, 5)
    c.add(2, 7)
    c.add(3, 9)
    expect(c.estimate()).toBe(7) // 最好 3 个:5/7/9 → 中位 7
  })

  it('serverNow 应用估算偏差', () => {
    const c = new ClockEstimator()
    c.add(1, 3)
    expect(c.serverNow(1000)).toBeCloseTo(1003)
  })
})
