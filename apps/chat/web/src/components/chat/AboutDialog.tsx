import { Info } from 'lucide-react'
import { ABOUT } from '@/about'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'

export function AboutDialog() {
  return (
    <Dialog>
      <DialogTrigger asChild>
        <Button variant="ghost" size="icon" className="shrink-0" aria-label={ABOUT.title} title={ABOUT.title}>
          <Info aria-hidden="true" />
        </Button>
      </DialogTrigger>
      <DialogContent className="max-h-[85dvh] overflow-y-auto [overflow-wrap:anywhere]">
        <DialogHeader>
          <DialogTitle>{ABOUT.title}</DialogTitle>
          <DialogDescription>{ABOUT.summary}</DialogDescription>
        </DialogHeader>
        <section aria-labelledby="about-features" className="grid gap-2">
          <h3 id="about-features" className="text-sm font-semibold">
            Что можно делать
          </h3>
          <ul className="grid list-disc gap-1.5 pl-5 text-sm">
            {ABOUT.features.map((feature) => (
              <li key={feature}>{feature}</li>
            ))}
          </ul>
        </section>
        <section aria-labelledby="about-technologies" className="grid gap-2">
          <h3 id="about-technologies" className="text-sm font-semibold">
            Технологии
          </h3>
          <dl className="grid gap-x-3 gap-y-1.5 text-sm sm:grid-cols-[auto_1fr]">
            {ABOUT.technologies.map((item) => (
              <div key={item.label} className="contents">
                <dt className="font-medium">{item.label}</dt>
                <dd className="mb-1 text-muted-foreground sm:mb-0">{item.value}</dd>
              </div>
            ))}
          </dl>
        </section>
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="outline">Закрыть</Button>
          </DialogClose>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
