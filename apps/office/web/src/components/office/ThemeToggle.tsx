import type { ReactNode } from 'react'
import { ToggleGroup } from 'radix-ui'
import { cn } from 'cn'
import { setTheme, useTheme, type ThemeChoice } from '@/theme'

function Icon({ children }: { children: ReactNode }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {children}
    </svg>
  )
}

const OPTIONS: { value: ThemeChoice; label: string; icon: ReactNode }[] = [
  {
    value: 'system',
    label: 'Системная тема',
    icon: (
      <Icon>
        <rect width="20" height="14" x="2" y="3" rx="2" />
        <path d="M8 21h8" />
        <path d="M12 17v4" />
      </Icon>
    ),
  },
  {
    value: 'light',
    label: 'Светлая тема',
    icon: (
      <Icon>
        <circle cx="12" cy="12" r="4" />
        <path d="M12 2v2M12 20v2m-7.07-17.07 1.41 1.41m11.32 11.32 1.41 1.41M2 12h2m16 0h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" />
      </Icon>
    ),
  },
  {
    value: 'dark',
    label: 'Тёмная тема',
    icon: (
      <Icon>
        <path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z" />
      </Icon>
    ),
  },
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
      className={cn('inline-flex items-center gap-0.5 rounded-md border p-0.5', className)}
    >
      {OPTIONS.map(({ value, label, icon }) => (
        <ToggleGroup.Item
          key={value}
          value={value}
          aria-label={label}
          title={label}
          className="inline-flex size-11 items-center justify-center rounded-sm text-muted-foreground transition-colors desk:size-8 data-[state=off]:hover:bg-accent data-[state=off]:hover:text-accent-foreground data-[state=on]:bg-primary data-[state=on]:text-primary-foreground [&_svg]:size-4"
        >
          {icon}
        </ToggleGroup.Item>
      ))}
    </ToggleGroup.Root>
  )
}
