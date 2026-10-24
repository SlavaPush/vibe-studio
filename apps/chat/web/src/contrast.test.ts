/// <reference types="node" />
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const css = readFileSync('src/index.css', 'utf8')

type Tokens = Record<string, [number, number, number]>

function tokens(selector: string): Tokens {
  const start = css.indexOf(`${selector} {`)
  const block = css.slice(start, css.indexOf('}', start))
  const result: Tokens = {}
  for (const match of block.matchAll(/--([\w-]+):\s*oklch\(([\d.]+)\s+([\d.]+)\s+([\d.]+)\)/g)) {
    result[match[1]] = [Number(match[2]), Number(match[3]), Number(match[4])]
  }
  return result
}

function luminance([l, c, h]: [number, number, number]): number {
  const a = c * Math.cos((h * Math.PI) / 180)
  const b = c * Math.sin((h * Math.PI) / 180)
  const lms = [
    l + 0.3963377774 * a + 0.2158037573 * b,
    l - 0.1055613458 * a - 0.0638541728 * b,
    l - 0.0894841775 * a - 1.291485548 * b,
  ].map((value) => value ** 3)
  const [r, g, bl] = [
    4.0767416621 * lms[0] - 3.3077115913 * lms[1] + 0.2309699292 * lms[2],
    -1.2684380046 * lms[0] + 2.6097574011 * lms[1] - 0.3413193965 * lms[2],
    -0.0041960863 * lms[0] - 0.7034186147 * lms[1] + 1.707614701 * lms[2],
  ].map((value) => Math.min(Math.max(value, 0), 1))
  return 0.2126 * r + 0.7152 * g + 0.0722 * bl
}

function contrast(theme: Tokens, foreground: string, background: string): number {
  const [high, low] = [luminance(theme[foreground]), luminance(theme[background])].sort((x, y) => y - x)
  return (high + 0.05) / (low + 0.05)
}

const TEXT_PAIRS = [
  ['foreground', 'background'],
  ['muted-foreground', 'background'],
  ['muted-foreground', 'muted'],
  ['destructive', 'background'],
  ['primary-foreground', 'primary'],
] as const

const RING_BACKGROUNDS = ['background', 'card', 'popover'] as const

describe('контраст токенов index.css', () => {
  describe.each([
    ['светлая тема', ':root'],
    ['тёмная тема', '.dark'],
  ])('%s', (_, selector) => {
    const theme = tokens(selector)

    it.each(TEXT_PAIRS)('%s на %s не меньше 4.5:1', (foreground, background) => {
      expect(contrast(theme, foreground, background)).toBeGreaterThanOrEqual(4.5)
    })

    it.each(RING_BACKGROUNDS)('кольцо фокуса на %s не меньше 3:1', (background) => {
      expect(contrast(theme, 'ring', background)).toBeGreaterThanOrEqual(3)
    })
  })

  it('отключает анимации при prefers-reduced-motion', () => {
    expect(css).toContain('@media (prefers-reduced-motion: reduce)')
  })
})
