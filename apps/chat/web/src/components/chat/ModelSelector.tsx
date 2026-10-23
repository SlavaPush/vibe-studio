import type { ModelChoice, ModelId, ModelProfile, TextModelId } from '@/api/types'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'

interface ModelSelectorProps {
  models: ModelProfile[]
  value: ModelChoice
  onChange: (value: ModelChoice) => void
}

const isTextModel = (id: ModelId): id is TextModelId => id !== 'gpt-image-2.5-flare'

export function ModelSelector({ models, value, onChange }: ModelSelectorProps) {
  const textModels = models.flatMap((model) =>
    model.kind === 'text' && model.selectable && isTextModel(model.id)
      ? [{ value: model.id, label: model.label }]
      : [],
  )
  const choices: ModelChoice[] = ['auto', 'image', ...textModels.map((model) => model.value)]

  return (
    <div className="flex items-center gap-2">
      <Label htmlFor="model-select">Модель</Label>
      <Select
        value={value}
        onValueChange={(next) => {
          const choice = choices.find((item) => item === next)
          if (choice) onChange(choice)
        }}
      >
        <SelectTrigger id="model-select" className="w-56" aria-describedby="model-select-hint">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="auto">Авто</SelectItem>
          <SelectGroup>
            <SelectLabel>Текстовые модели</SelectLabel>
            {textModels.map((model) => (
              <SelectItem key={model.value} value={model.value}>
                {model.label}
              </SelectItem>
            ))}
          </SelectGroup>
          <SelectGroup>
            <SelectLabel>Изображения</SelectLabel>
            <SelectItem value="image">Генерация изображения</SelectItem>
          </SelectGroup>
        </SelectContent>
      </Select>
      <span id="model-select-hint" className="sr-only">
        Уровень усилий выбирает сервер
      </span>
    </div>
  )
}
