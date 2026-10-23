import { SCENARIOS, type Scenario } from '@/mocks/scenarios'

export function ScenarioPanel({ current }: { current: Scenario }) {
  return (
    <footer className="border-t bg-muted px-4 py-2 text-xs text-muted-foreground">
      <nav aria-label="Сценарии тестовых данных" className="flex flex-wrap items-center gap-3">
        <span>Тестовые данные, сеть не используется. Сценарий:</span>
        {Object.entries(SCENARIOS).map(([key, label]) => (
          <a
            key={key}
            href={`?scenario=${key}`}
            aria-current={key === current ? 'page' : undefined}
            className="rounded-sm underline-offset-4 hover:underline aria-[current=page]:font-semibold aria-[current=page]:text-foreground"
          >
            {label}
          </a>
        ))}
      </nav>
    </footer>
  )
}
