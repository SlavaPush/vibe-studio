export interface AboutTechnology {
  label: string
  value: string
}

export interface AboutContent {
  title: string
  summary: string
  features: string[]
  technologies: AboutTechnology[]
}

export const ABOUT: AboutContent = {
  title: 'О приложении',
  summary:
    'Нейроофис — команда AI-агентов, которая развивает чат vibe-chat. Участники ставят задачи, агенты пишут и проверяют код и выпускают новую версию чата.',
  features: [
    'Поставить задачу по развитию чата. Фильтр приёма отклоняет посторонние и опасные задачи.',
    'Следить за работой: одновременно выполняются до трёх задач, каждая в отдельной рабочей копии репозитория (git worktree).',
    'Задачу ведут шесть ролей: исследователь, Supervisor, разработчик, QA, критик и агент ревью PR.',
    'Подтвердить требования к своей задаче: без подтверждения автора разработка не начинается.',
    'Код проверяется в изолированном контейнере Docker: lint, тесты и сборка идут без доступа к сети. QA пишет тест Playwright, который не проходит на прежней версии и проходит на новой.',
    'PR открывает GitHub App офиса. PR сливается только после обязательных проверок CI и одобрения агента ревью PR.',
    'Очередь выпуска публикует чат на Render и проверяет опубликованную версию. Если проверка не прошла, офис автоматически откатывает чат к прежней версии.',
  ],
  technologies: [
    { label: 'Интерфейс', value: 'React, TypeScript, Vite, shadcn/ui' },
    { label: 'Сервер', value: 'FastAPI, LangGraph, Python 3.12' },
    {
      label: 'Модели',
      value: 'Anthropic Claude — основной провайдер, OpenAI — резерв; локальная модель Ollama qwen3:8b в профилях local и hybrid',
    },
    { label: 'Хранение', value: 'SQLite локально, PostgreSQL при деплое' },
    { label: 'Деплой', value: 'Офис работает локально; чат выпускает на Render через GitHub App и Render API' },
    { label: 'Проверки', value: 'Docker и Playwright, GitHub Actions, ruleset ветки main' },
  ],
}
