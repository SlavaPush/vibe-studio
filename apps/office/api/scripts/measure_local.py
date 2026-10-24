"""Замер профиля local: время и токены типовых шагов ролей на Ollama через шлюз офиса без БД задач.

Запуск из каталога api: PYTHONPATH=. uv run python scripts/measure_local.py [--runs N] [--steps шаг ...]
Шаги: supervisor/specify, developer/implement, qa/qa, critic/review. Облачные модели не вызываются:
шаг, маршрут которого в профиле local ведёт не в Ollama, не запускается (код возврата 2).
Код возврата: 0 — все ответы завершены и проходят схему; 1 — хотя бы один ответ невалиден или вызов не удался.
"""

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.models_gateway import ModelCall, ModelsGateway, ProviderError
from app.roles import (
    CRITIC,
    DEVELOPER,
    QA,
    QA_TEST_DIR,
    SUPERVISOR,
    CriticReview,
    Entry,
    QaTurn,
    SpecDraft,
    Turn,
    assemble,
    provider_schema,
)
from app.schemas import AgentRole, ModelProfile

TASK_TEXT = (
    "Под полем ввода сообщения показывать счётчик символов: сколько введено из 4000. "
    "Если текст длиннее 4000 символов, счётчик выделяется красным, а кнопка отправки недоступна."
)

SPEC = (
    "Под полем ввода сообщения отображается счётчик символов «N / 4000». При превышении предела "
    "счётчик выделяется цветом ошибки, кнопка отправки недоступна.\n\n"
    "Критерии приёмки:\n"
    "1. Под пустым полем ввода сообщения виден счётчик «0 / 4000».\n"
    "2. При вводе текста счётчик показывает число введённых символов.\n"
    "3. При длине текста больше 4000 символов у счётчика атрибут aria-invalid=\"true\" и цвет ошибки.\n"
    "4. При длине текста больше 4000 символов кнопка «Отправить» недоступна."
)

LISTING = "\n".join(
    [
        "web/src/App.test.tsx",
        "web/src/App.tsx",
        "web/src/api/client.ts",
        "web/src/api/types.ts",
        "web/src/components/ThemeToggle.test.tsx",
        "web/src/components/ThemeToggle.tsx",
        "web/src/components/chat/ChatThread.test.tsx",
        "web/src/components/chat/ChatThread.tsx",
        "web/src/components/chat/ConversationList.tsx",
        "web/src/components/chat/LoginScreen.tsx",
        "web/src/components/chat/MessageActions.tsx",
        "web/src/components/chat/MessageComposer.test.tsx",
        "web/src/components/chat/MessageComposer.tsx",
        "web/src/components/chat/MessageQueue.test.tsx",
        "web/src/components/chat/MessageQueue.tsx",
        "web/src/components/chat/ModelSelector.test.tsx",
        "web/src/components/chat/ModelSelector.tsx",
        "web/src/components/chat/Notices.tsx",
        "web/src/components/ui/alert.tsx",
        "web/src/components/ui/button.tsx",
        "web/src/components/ui/card.tsx",
        "web/src/components/ui/dialog.tsx",
        "web/src/components/ui/input.tsx",
        "web/src/components/ui/label.tsx",
        "web/src/components/ui/select.tsx",
        "web/src/components/ui/textarea.tsx",
        "web/src/index.css",
        "web/src/lib/utils.ts",
        "web/src/main.tsx",
        "web/src/store/assistantAdapter.ts",
        "web/src/store/chatController.test.ts",
        "web/src/store/chatController.ts",
        "web/src/store/conversations.test.ts",
        "web/src/store/conversations.ts",
        "web/src/store/drafts.ts",
        "web/src/store/versionWatch.test.ts",
        "web/src/store/versionWatch.ts",
        "web/src/test/fixtures.ts",
        "web/src/test/matchMedia.ts",
        "web/src/test/setup.ts",
        "web/src/theme.test.ts",
        "web/src/theme.ts",
    ]
)

COMPONENT_PATH = "web/src/components/chat/MessageComposer.tsx"

COMPONENT = """import { ImageIcon, SendHorizontal, Square } from 'lucide-react'
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ChangeEvent,
  type FormEvent,
  type KeyboardEvent,
} from 'react'
import type { ModelOption } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { ModelSelector } from '@/components/chat/ModelSelector'
import { loadDraft, saveDraft, clearDraft } from '@/store/drafts'
import { cn } from '@/lib/utils'

const MAX_ROWS = 8
const LINE_HEIGHT = 24

interface MessageComposerProps {
  conversationId: string | null
  models: ModelOption[]
  model: string
  busy: boolean
  disabled?: boolean
  imageMode: boolean
  onModelChange: (model: string) => void
  onImageModeChange: (enabled: boolean) => void
  onSend: (text: string) => void
  onStop: () => void
}

export function MessageComposer({
  conversationId,
  models,
  model,
  busy,
  disabled = false,
  imageMode,
  onModelChange,
  onImageModeChange,
  onSend,
  onStop,
}: MessageComposerProps) {
  const [text, setText] = useState('')
  const [composing, setComposing] = useState(false)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    setText(conversationId ? loadDraft(conversationId) : '')
  }, [conversationId])

  useEffect(() => {
    if (!conversationId) return
    const timer = window.setTimeout(() => saveDraft(conversationId, text), 300)
    return () => window.clearTimeout(timer)
  }, [conversationId, text])

  useLayoutEffect(() => {
    const element = textareaRef.current
    if (!element) return
    element.style.height = 'auto'
    const height = Math.min(element.scrollHeight, MAX_ROWS * LINE_HEIGHT)
    element.style.height = `${height}px`
  }, [text])

  const trimmed = text.trim()
  const canSend = !busy && !disabled && trimmed.length > 0

  const submit = useCallback(() => {
    if (!canSend) return
    onSend(trimmed)
    setText('')
    if (conversationId) clearDraft(conversationId)
    textareaRef.current?.focus()
  }, [canSend, conversationId, onSend, trimmed])

  const onSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    submit()
  }

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey && !composing) {
      event.preventDefault()
      submit()
    }
  }

  const onChange = (event: ChangeEvent<HTMLTextAreaElement>) => {
    setText(event.target.value)
  }

  return (
    <form onSubmit={onSubmit} className="border-t bg-background p-3" aria-label="Новое сообщение">
      <div className="mb-2 flex items-center gap-2">
        <ModelSelector models={models} value={model} onChange={onModelChange} disabled={busy} />
        <Button
          type="button"
          variant={imageMode ? 'secondary' : 'ghost'}
          size="sm"
          aria-pressed={imageMode}
          onClick={() => onImageModeChange(!imageMode)}
          disabled={busy}
        >
          <ImageIcon aria-hidden="true" />
          Изображение
        </Button>
      </div>
      <Label htmlFor="message-input" className="sr-only">
        Сообщение
      </Label>
      <div className="flex items-end gap-2">
        <Textarea
          id="message-input"
          ref={textareaRef}
          value={text}
          rows={1}
          placeholder={imageMode ? 'Опишите изображение' : 'Напишите сообщение'}
          onChange={onChange}
          onKeyDown={onKeyDown}
          onCompositionStart={() => setComposing(true)}
          onCompositionEnd={() => setComposing(false)}
          disabled={disabled}
          className={cn('min-h-10 resize-none', disabled && 'opacity-60')}
        />
        {busy ? (
          <Button type="button" variant="outline" size="icon" aria-label="Остановить" onClick={onStop}>
            <Square aria-hidden="true" />
          </Button>
        ) : (
          <Button type="submit" size="icon" aria-label="Отправить" disabled={!canSend}>
            <SendHorizontal aria-hidden="true" />
          </Button>
        )}
      </div>
      <p className="mt-1 text-xs text-muted-foreground">Enter — отправить, Shift+Enter — новая строка</p>
    </form>
  )
}
"""

CHECKS = "\n".join(
    [
        "npm ci --no-audit --no-fund: код выхода 0",
        "npm run lint: код выхода 0",
        "npm run test: код выхода 0",
        "npm run build: код выхода 0",
    ]
)

DIFF = """diff --git a/web/src/components/chat/MessageComposer.tsx b/web/src/components/chat/MessageComposer.tsx
index 3f2a9c1..8b7e4d0 100644
--- a/web/src/components/chat/MessageComposer.tsx
+++ b/web/src/components/chat/MessageComposer.tsx
@@ -19,6 +19,7 @@ import { cn } from '@/lib/utils'

 const MAX_ROWS = 8
 const LINE_HEIGHT = 24
+export const MAX_MESSAGE_LENGTH = 4000

 interface MessageComposerProps {
   conversationId: string | null
@@ -69,7 +70,9 @@ export function MessageComposer({
   }, [text])

   const trimmed = text.trim()
-  const canSend = !busy && !disabled && trimmed.length > 0
+  const length = text.length
+  const tooLong = length > MAX_MESSAGE_LENGTH
+  const canSend = !busy && !disabled && trimmed.length > 0 && !tooLong

   const submit = useCallback(() => {
     if (!canSend) return
@@ -117,6 +120,7 @@ export function MessageComposer({
           onCompositionStart={() => setComposing(true)}
           onCompositionEnd={() => setComposing(false)}
           disabled={disabled}
+          aria-describedby="message-counter"
           className={cn('min-h-10 resize-none', disabled && 'opacity-60')}
         />
         {busy ? (
@@ -129,7 +133,21 @@ export function MessageComposer({
           </Button>
         )}
       </div>
-      <p className="mt-1 text-xs text-muted-foreground">Enter — отправить, Shift+Enter — новая строка</p>
+      <div className="mt-1 flex items-center justify-between text-xs">
+        <p className="text-muted-foreground">Enter — отправить, Shift+Enter — новая строка</p>
+        <span
+          id="message-counter"
+          data-testid="message-counter"
+          aria-live="polite"
+          aria-invalid={tooLong}
+          className={cn('tabular-nums', tooLong ? 'text-destructive' : 'text-muted-foreground')}
+        >
+          {length} / {MAX_MESSAGE_LENGTH}
+        </span>
+      </div>
     </form>
   )
 }
diff --git a/web/src/components/chat/MessageComposer.test.tsx b/web/src/components/chat/MessageComposer.test.tsx
index 51c0d2e..a4f1b93 100644
--- a/web/src/components/chat/MessageComposer.test.tsx
+++ b/web/src/components/chat/MessageComposer.test.tsx
@@ -41,3 +41,22 @@ describe('MessageComposer', () => {
     expect(onSend).toHaveBeenCalledWith('Привет')
   })
+
+  it('показывает счётчик символов', async () => {
+    render(<MessageComposer {...props} />)
+    expect(screen.getByTestId('message-counter')).toHaveTextContent('0 / 4000')
+    await userEvent.type(screen.getByLabelText('Сообщение'), 'abc')
+    expect(screen.getByTestId('message-counter')).toHaveTextContent('3 / 4000')
+  })
+
+  it('запрещает отправку длинного текста', () => {
+    render(<MessageComposer {...props} />)
+    fireEvent.change(screen.getByLabelText('Сообщение'), { target: { value: 'x'.repeat(4001) } })
+    expect(screen.getByTestId('message-counter')).toHaveAttribute('aria-invalid', 'true')
+    expect(screen.getByRole('button', { name: 'Отправить' })).toBeDisabled()
+  })
 })
"""


def spec_entries() -> list[Entry]:
    return [
        Entry("Текст задачи участника", TASK_TEXT, required=True),
        Entry("Требования", SPEC, required=True),
    ]


@dataclass(frozen=True)
class Step:
    name: str
    role: AgentRole
    step: str
    system: str
    reply_type: type[BaseModel]
    fixed: list[Entry]
    turns: list[list[Entry]]


STEPS = [
    Step(
        "supervisor/specify",
        AgentRole.supervisor,
        "spec",
        SUPERVISOR,
        SpecDraft,
        [Entry("Текст задачи участника", TASK_TEXT, required=True), Entry("Файлы web/src", LISTING)],
        [],
    ),
    Step(
        "developer/implement",
        AgentRole.developer,
        "implement",
        DEVELOPER,
        Turn,
        spec_entries() + [Entry("Файлы web/src", LISTING)],
        [
            [
                Entry("Ход 1: ответ роли", "Читаю компонент поля ввода сообщения."),
                Entry(f"Результат действия 1: read_file {COMPONENT_PATH}", f"ok\n{COMPONENT}"),
            ]
        ],
    ),
    Step(
        "qa/qa",
        AgentRole.qa,
        "qa",
        QA,
        QaTurn,
        spec_entries()
        + [Entry("Файл теста QA", f"{QA_TEST_DIR}/tsk_measure.pw.ts", required=True), Entry("Файлы web/src", LISTING)],
        [],
    ),
    Step(
        "critic/review",
        AgentRole.critic,
        "review",
        CRITIC,
        CriticReview,
        spec_entries() + [Entry("Отчёт проверок", CHECKS, required=True), Entry("Diff изменений", DIFF)],
        [],
    ),
]
STEP_NAMES = [step.name for step in STEPS]


@dataclass(frozen=True)
class Sample:
    step: str
    run: int
    seconds: float
    input_tokens: int
    output_tokens: int
    tokens_per_second: float | None
    load_seconds: float | None
    done_reason: str | None
    valid: bool
    error: str | None = None


def measure_once(gateway: ModelsGateway, step: Step, run: int) -> Sample:
    target = gateway.targets(ModelProfile.local, step.role)[0]
    user = assemble(step.system, step.fixed, step.turns, gateway.input_budget(ModelProfile.local, step.role))
    call = ModelCall(
        task_id="measure",
        role=step.role,
        step=step.step,
        target=target,
        system=step.system,
        user=user,
        schema=provider_schema(step.reply_type),
        timeout=gateway.timeout(target.provider),
    )
    started = time.monotonic()
    try:
        reply = gateway.call_provider(call)
    except ProviderError as error:
        return Sample(step.name, run, round(time.monotonic() - started, 1), 0, 0, None, None, None, False, str(error))
    seconds = round(time.monotonic() - started, 1)
    error = None
    if not reply.complete:
        error = "incomplete answer"
    else:
        try:
            step.reply_type.model_validate(json.loads(reply.text))
        except (ValueError, ValidationError) as exc:
            error = f"schema: {type(exc).__name__}"
    rate = None
    if reply.eval_duration_ns:
        rate = round(reply.output_tokens / (reply.eval_duration_ns / 1e9), 1)
    load = round(reply.load_duration_ns / 1e9, 1) if reply.load_duration_ns is not None else None
    return Sample(
        step.name,
        run,
        seconds,
        reply.input_tokens,
        reply.output_tokens,
        rate,
        load,
        reply.done_reason,
        error is None,
        error,
    )


def summarize(samples: list[Sample]) -> list[dict[str, Any]]:
    rows = []
    for name in dict.fromkeys(sample.step for sample in samples):
        group = [sample for sample in samples if sample.step == name]
        seconds = [sample.seconds for sample in group]
        rates = [sample.tokens_per_second for sample in group if sample.tokens_per_second is not None]
        rows.append(
            {
                "step": name,
                "runs": len(group),
                "valid": sum(sample.valid for sample in group),
                "median_seconds": round(statistics.median(seconds), 1),
                "min_seconds": min(seconds),
                "max_seconds": max(seconds),
                "median_input_tokens": int(statistics.median(sample.input_tokens for sample in group)),
                "median_output_tokens": int(statistics.median(sample.output_tokens for sample in group)),
                "median_tokens_per_second": round(statistics.median(rates), 1) if rates else None,
            }
        )
    return rows


def show(value: Any) -> str:
    return "-" if value is None else str(value)


def loaded_models(gateway: ModelsGateway) -> list[str] | None:
    try:
        response = gateway.http_client().get(f"{gateway.settings.ollama_url.rstrip('/')}/api/ps", timeout=10)
        response.raise_for_status()
        return [item.get("name", "?") for item in response.json().get("models", [])]
    except (httpx.HTTPError, ValueError, AttributeError):
        return None


def run(gateway: ModelsGateway, steps: list[Step], runs: int) -> int:
    cloud = [step.name for step in steps if gateway.targets(ModelProfile.local, step.role)[0].provider != "ollama"]
    if cloud:
        print(f"шаги вне Ollama в профиле local, замер не выполняется: {cloud}", file=sys.stderr)
        return 2
    models = loaded_models(gateway)
    if models is None:
        print("загруженные модели: не удалось получить /api/ps")
    else:
        print(f"загруженные модели: {', '.join(models) or 'нет'}")
        if len(models) > 1:
            print(f"предупреждение: загружено моделей {len(models)}, замер искажён конкуренцией за память")
    header = ("шаг", "№", "время, с", "вход", "выход", "ток/с", "загрузка, с", "done_reason", "схема")
    print("\t".join(header))
    samples = []
    for step in steps:
        for number in range(1, runs + 1):
            sample = measure_once(gateway, step, number)
            samples.append(sample)
            status = "ok" if sample.valid else f"нет ({sample.error})"
            print(
                "\t".join(
                    [
                        sample.step,
                        str(sample.run),
                        str(sample.seconds),
                        str(sample.input_tokens),
                        str(sample.output_tokens),
                        show(sample.tokens_per_second),
                        show(sample.load_seconds),
                        show(sample.done_reason),
                        status,
                    ]
                ),
                flush=True,
            )
    print()
    print("\t".join(("шаг", "валидных", "медиана, с", "мин, с", "макс, с", "вход (медиана)", "выход (медиана)", "ток/с (медиана)")))
    for row in summarize(samples):
        print(
            "\t".join(
                [
                    row["step"],
                    f"{row['valid']}/{row['runs']}",
                    str(row["median_seconds"]),
                    str(row["min_seconds"]),
                    str(row["max_seconds"]),
                    str(row["median_input_tokens"]),
                    str(row["median_output_tokens"]),
                    show(row["median_tokens_per_second"]),
                ]
            )
        )
    return 0 if all(sample.valid for sample in samples) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Замер профиля local на Ollama")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--steps", nargs="+", choices=STEP_NAMES, default=STEP_NAMES)
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs должен быть не меньше 1")
    steps = [step for step in STEPS if step.name in args.steps]
    return run(ModelsGateway(Settings.from_env()), steps, args.runs)


if __name__ == "__main__":
    sys.exit(main())
