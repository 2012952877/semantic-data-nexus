import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const css = readFileSync(resolve(process.cwd(), 'src/styles.css'), 'utf8')

const token = (name: string) => {
  const match = new RegExp(`--${name}:\\s*(#[0-9a-f]{6})`, 'i').exec(css)
  if (!match?.[1]) throw new Error(`Missing color token: ${name}`)
  return match[1]
}

const luminance = (hex: string) => {
  const channels = [1, 3, 5].map((offset) => Number.parseInt(hex.slice(offset, offset + 2), 16) / 255)
  const linear = channels.map((value) =>
    value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4)
  return 0.2126 * linear[0]! + 0.7152 * linear[1]! + 0.0722 * linear[2]!
}

const contrast = (foreground: string, background: string) => {
  const lighter = Math.max(luminance(foreground), luminance(background))
  const darker = Math.min(luminance(foreground), luminance(background))
  return (lighter + 0.05) / (darker + 0.05)
}

describe('status text contrast', () => {
  it.each([
    ['status-light-pending', 'paper'],
    ['status-light-success', 'paper'],
    ['status-light-empty', 'paper'],
    ['status-light-failed', 'paper'],
    ['status-dark-pending', 'ink'],
    ['status-dark-success', 'ink'],
    ['status-dark-empty', 'ink'],
    ['status-dark-failed', 'ink'],
  ])('%s meets WCAG AA against %s', (foreground, background) => {
    expect(contrast(token(foreground), token(background))).toBeGreaterThanOrEqual(4.5)
  })
})
