import { Monitor, Moon, Sun } from 'lucide-react'
import { ToggleGroup } from 'radix-ui'
import { cn } from 'cn'
import { setTheme, useTheme, type ThemeChoice } from '@/theme'

const OPTIONS: { value: ThemeChoice; label: string; Icon: typeof Monitor }[] = [
  { value: 'system', label: 'Системная тема', Icon: Monitor },
  { value: 'light', label: 'Светлая тема', Icon: Sun },
  { value: 'dark', label: 'Тёмная тема', Icon: Moon },
]

export function ThemeToggle({ className }: { className?: string }) {
  const theme = useTheme()
  return (
    <ToggleGroup.Root
      type="single"
      value={theme}
      onValueChange={(next) => {
        const option = OPTIONS.find((item) => item.value === next)
        if (option) setTheme(option.value)
      }}
      aria-label="Тема оформления"
      className={cn('inline-flex items-center gap-0.5 rounded-full border border-input p-0.5', className)}
    >
      {OPTIONS.map(({ value, label, Icon }) => (
        <ToggleGroup.Item
          key={value}
          value={value}
          aria-label={label}
          title={label}
          className="inline-flex size-8 items-center justify-center rounded-full text-muted-foreground transition-colors data-[state=off]:hover:bg-accent data-[state=off]:hover:text-accent-foreground data-[state=on]:bg-brand data-[state=on]:text-brand-foreground [&_svg]:size-4"
        >
          <Icon aria-hidden="true" />
        </ToggleGroup.Item>
      ))}
    </ToggleGroup.Root>
  )
}
