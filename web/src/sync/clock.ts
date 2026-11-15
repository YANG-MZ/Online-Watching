/**
 * 时钟偏差估计(sync-protocol.md §3)。
 *
 * 利用心跳通道样本:
 *   rtt    = t1 - t0
 *   offset = server_ts + rtt/2 - t1   (假设上下行时延对称)
 * 保留最近 8 个样本,取 rtt 最小的 3 个样本中 offset 的中位数。
 */

export interface ClockSample {
  rtt: number
  offset: number
}

const MAX_SAMPLES = 8
const BEST_COUNT = 3

export function median(nums: number[]): number {
  if (nums.length === 0) return NaN
  const sorted = [...nums].sort((a, b) => a - b)
  const mid = Math.floor(sorted.length / 2)
  return sorted.length % 2 === 0 ? (sorted[mid - 1] + sorted[mid]) / 2 : sorted[mid]
}

export class ClockEstimator {
  private samples: ClockSample[] = []

  add(rtt: number, offset: number): void {
    this.samples.push({ rtt, offset })
    if (this.samples.length > MAX_SAMPLES) {
      this.samples.shift()
    }
  }

  get size(): number {
    return this.samples.length
  }

  /** 估算本地时钟相对服务端时钟的偏差;无样本返回 null。 */
  estimate(): number | null {
    if (this.samples.length === 0) return null
    const best = [...this.samples]
      .sort((a, b) => a.rtt - b.rtt)
      .slice(0, BEST_COUNT)
    return median(best.map((s) => s.offset))
  }

  /** 估算当前服务端时刻(unix 秒)。 */
  serverNow(unixNow: number = Date.now() / 1000): number {
    return unixNow + (this.estimate() ?? 0)
  }
}
