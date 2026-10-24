import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { ApiError } from '@/api/types'
import { RunErrorNotice } from './Notices'

function apiError(code: ApiError['code'], retryable = false): ApiError {
  return { code, message: `Сообщение ${code}`, retryable, operation_id: 'op_hidden' }
}

describe('RunErrorNotice', () => {
  it.each([
    ['service_unavailable', 'Нет связи с сервером'],
    ['quota_exceeded', 'Лимит запросов исчерпан'],
    ['rate_limited', 'Слишком много запросов'],
    ['provider_unavailable', 'Модели временно недоступны'],
    ['provider_outcome_unknown', 'Ответ не завершён'],
    ['response_truncated', 'Ответ не завершён'],
    ['run_cancelled', 'Запрос отменён'],
    ['internal_error', 'Ошибка сервера'],
    ['storage_unavailable', 'Ошибка сервера'],
  ] as const)('для %s показывает заголовок «%s» без кода операции', (code, title) => {
    render(<RunErrorNotice error={apiError(code)} />)
    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent(title)
    expect(alert).toHaveTextContent(`Сообщение ${code}`)
    expect(alert).not.toHaveTextContent('op_hidden')
    expect(alert).not.toHaveTextContent('Код операции')
  })

  it('для повторяемой ошибки предлагает повторить запрос позже', () => {
    render(<RunErrorNotice error={apiError('rate_limited', true)} />)
    expect(screen.getByRole('alert')).toHaveTextContent('Запрос можно повторить позже.')
  })
})
