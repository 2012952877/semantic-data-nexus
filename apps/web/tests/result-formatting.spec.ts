import { formatResultValue } from '@/components/resultFormatting'
import type { ResultColumn } from '@/domain'

const decimalColumn = (
  format: ResultColumn['format'],
): ResultColumn => ({
  key: 'value',
  label: '值',
  dataType: 'decimal',
  format,
})

describe('result formatting', () => {
  it('formats fixed-point currency without losing precision or scale', () => {
    expect(formatResultValue(
      '1234567890123456.1200',
      decimalColumn('currency'),
    )).toBe('¥1,234,567,890,123,456.1200')
  })

  it.each([
    ['0.1234', '12.34%'],
    ['1.0800', '108.00%'],
    ['0.0000000000000000000000000001', '0.00000000000000000000000001%'],
    ['-0.018', '-1.8%'],
  ])('scales fixed-point percentage %s exactly', (value, expected) => {
    expect(formatResultValue(value, decimalColumn('percent'))).toBe(expected)
  })
})
