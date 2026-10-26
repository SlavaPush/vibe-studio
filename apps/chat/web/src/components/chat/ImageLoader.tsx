import { ImageIcon } from 'lucide-react'

export function ImageLoader() {
  return (
    <div
      role="status"
      aria-label="Генерация изображения"
      data-testid="image-loader"
      className="flex w-64 max-w-full flex-col gap-2"
    >
      <div className="relative flex aspect-square w-full items-center justify-center overflow-hidden rounded-lg border bg-muted">
        <div
          aria-hidden="true"
          className="image-loader-shimmer absolute inset-0 -translate-x-full bg-linear-to-r from-transparent via-foreground/10 to-transparent animate-image-shimmer motion-reduce:animate-none"
        />
        <ImageIcon
          aria-hidden="true"
          className="relative size-10 animate-pulse text-muted-foreground motion-reduce:animate-none"
        />
      </div>
      <p className="text-xs text-muted-foreground">Генерирую изображение…</p>
    </div>
  )
}
