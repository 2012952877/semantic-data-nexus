import type { ResultCell, ResultColumn } from '@/domain'

const fixedPointPattern = /^(-?)(\d+)(?:\.(\d+))?$/

const groupInteger = (value: string) => value.replace(/\B(?=(\d{3})+(?!\d))/g, ',')

const multiplyFixedPointByHundred = (value: string) => {
  const match = fixedPointPattern.exec(value)
  if (!match) return value
  const [, sign = '', integer = '', fraction = ''] = match
  const digits = `${integer}${fraction}`.padEnd(integer.length + 2, '0')
  const decimalIndex = integer.length + 2
  const whole = digits.slice(0, decimalIndex).replace(/^0+(?=\d)/, '') || '0'
  const remainder = digits.slice(decimalIndex)
  return `${sign}${whole}${remainder ? `.${remainder}` : ''}`
}

const formatFixedPoint = (value: string, prefix = '', suffix = '') => {
  const match = fixedPointPattern.exec(value)
  if (!match) return value
  const [, sign = '', integer = '', fraction] = match
  return `${sign}${prefix}${groupInteger(integer)}${fraction === undefined ? '' : `.${fraction}`}${suffix}`
}

export const formatResultValue = (value: ResultCell, column: ResultColumn) => {
  if (value === null) return '—'
  if (column.dataType === 'decimal' && typeof value === 'string') {
    if (column.format === 'currency') return formatFixedPoint(value, '¥')
    if (column.format === 'percent') return formatFixedPoint(multiplyFixedPointByHundred(value), '', '%')
    if (column.format === 'number') return formatFixedPoint(value)
  }
  if (column.format === 'currency' && typeof value === 'number') {
    return new Intl.NumberFormat('zh-CN', {
      style: 'currency',
      currency: 'CNY',
      maximumFractionDigits: 0,
    }).format(value)
  }
  if (column.format === 'percent' && typeof value === 'number') {
    return new Intl.NumberFormat('zh-CN', {
      style: 'percent',
      minimumFractionDigits: 1,
      maximumFractionDigits: 1,
    }).format(value)
  }
  return String(value)
}
