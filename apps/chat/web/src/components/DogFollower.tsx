import { useEffect, useRef, useState } from 'react'

export type DogState = 'running' | 'sitting'

const SIZE = 64
// Минимальная дистанция до курсора: собака никогда не подходит ближе.
const GAP = 56
// Доля оставшегося пути (сверх дистанции GAP), проходимая за кадр.
const FOLLOW = 0.08
// Через сколько миллисекунд без движения курсора собака садится.
const IDLE_MS = 1200

function RunningDog() {
  return (
    <svg viewBox="0 0 64 64" width={SIZE} height={SIZE} data-asset="dog-running" focusable="false">
      <path d="M14 32 Q4 26 8 16" fill="none" stroke="#8a5522" strokeWidth="4" strokeLinecap="round" />
      <g stroke="#8a5522" strokeWidth="4.5" strokeLinecap="round">
        <line className="dog-leg" x1="20" y1="42" x2="20" y2="55" />
        <line className="dog-leg dog-leg-b" x1="26" y1="42" x2="26" y2="55" />
        <line className="dog-leg dog-leg-b" x1="38" y1="42" x2="38" y2="55" />
        <line className="dog-leg" x1="44" y1="42" x2="44" y2="55" />
      </g>
      <ellipse cx="30" cy="36" rx="17" ry="9" fill="#c8873a" />
      <circle cx="50" cy="28" r="8" fill="#d99a4e" />
      <ellipse cx="57" cy="31" rx="5" ry="3.5" fill="#e8b878" />
      <circle cx="61" cy="29.5" r="1.6" fill="#222" />
      <circle cx="52" cy="26" r="1.3" fill="#222" />
      <path d="M44 22 Q41 32 46 33 Q48 27 47 22 Z" fill="#8a5522" />
    </svg>
  )
}

function SittingDog() {
  return (
    <svg viewBox="0 0 64 64" width={SIZE} height={SIZE} data-asset="dog-thinking" focusable="false">
      <g fill="#6b4423">
        <ellipse cx="10" cy="58" rx="6" ry="3" />
        <ellipse cx="10" cy="54.5" rx="4.2" ry="2.6" />
        <circle cx="10" cy="51.5" r="2.2" />
      </g>
      <path d="M18 44 Q10 38 14 30" fill="none" stroke="#8a5522" strokeWidth="4" strokeLinecap="round" />
      <line x1="40" y1="48" x2="40" y2="59" stroke="#8a5522" strokeWidth="4.5" strokeLinecap="round" />
      <line x1="46" y1="48" x2="46" y2="59" stroke="#8a5522" strokeWidth="4.5" strokeLinecap="round" />
      <ellipse cx="28" cy="46" rx="14" ry="9" fill="#c8873a" />
      <ellipse cx="21" cy="52" rx="8" ry="6" fill="#b87832" />
      <circle cx="46" cy="39" r="8" fill="#d99a4e" />
      <ellipse cx="53" cy="42" rx="5" ry="3.5" fill="#e8b878" />
      <circle cx="57" cy="40.5" r="1.6" fill="#222" />
      <path d="M47.5 36.5 Q49 35.5 50.5 36.5" fill="none" stroke="#222" strokeWidth="1.2" strokeLinecap="round" />
      <path d="M40 33 Q37 43 42 44 Q44 38 43 33 Z" fill="#8a5522" />
      <circle cx="47" cy="26" r="1.4" fill="#fff" stroke="#888" strokeWidth="0.8" />
      <circle cx="49" cy="21" r="2" fill="#fff" stroke="#888" strokeWidth="0.8" />
      <g fill="#fff" stroke="#888" strokeWidth="0.8">
        <circle cx="44" cy="9" r="5" />
        <circle cx="51" cy="7" r="6" />
        <circle cx="58" cy="10" r="5" />
      </g>
      <ellipse cx="51" cy="10" rx="11" ry="4" fill="#fff" />
      <g fill="#666">
        <circle cx="46" cy="9" r="1.1" />
        <circle cx="51" cy="9" r="1.1" />
        <circle cx="56" cy="9" r="1.1" />
      </g>
    </svg>
  )
}

export function DogFollower() {
  const rootRef = useRef<HTMLDivElement>(null)
  const [state, setState] = useState<DogState>('sitting')
  const [facing, setFacing] = useState<'left' | 'right'>('right')

  useEffect(() => {
    const root = rootRef.current
    if (!root) return

    const pos = {
      x: Math.max(SIZE, window.innerWidth - 96),
      y: Math.max(SIZE, window.innerHeight - 96),
    }
    let cursor: { x: number; y: number } | null = null
    let sitting = true
    let timer: ReturnType<typeof setTimeout> | undefined
    let frame = 0

    const render = () => {
      root.style.transform = `translate3d(${pos.x - SIZE / 2}px, ${pos.y - SIZE / 2}px, 0)`
      root.dataset.x = pos.x.toFixed(1)
      root.dataset.y = pos.y.toFixed(1)
      if (cursor) {
        root.dataset.distance = Math.hypot(cursor.x - pos.x, cursor.y - pos.y).toFixed(1)
      }
    }

    const onMove = (event: MouseEvent) => {
      cursor = { x: event.clientX, y: event.clientY }
      if (sitting) {
        sitting = false
        setState('running')
      }
      if (timer !== undefined) clearTimeout(timer)
      timer = setTimeout(() => {
        sitting = true
        setState('sitting')
      }, IDLE_MS)
    }

    const tick = () => {
      if (cursor && !sitting) {
        const dx = cursor.x - pos.x
        const dy = cursor.y - pos.y
        const dist = Math.hypot(dx, dy)
        const ux = dist > 0 ? dx / dist : 1
        const uy = dist > 0 ? dy / dist : 0
        // dist > GAP: догоняет, но лишь до дистанции GAP; dist < GAP: отступает назад.
        const step = (dist - GAP) * FOLLOW
        pos.x += ux * step
        pos.y += uy * step
        if (Math.abs(dx) > 2) setFacing(dx > 0 ? 'right' : 'left')
        render()
      }
      frame = window.requestAnimationFrame(tick)
    }

    render()
    window.addEventListener('mousemove', onMove, { passive: true })
    if (typeof window.requestAnimationFrame === 'function') {
      frame = window.requestAnimationFrame(tick)
    }
    return () => {
      window.removeEventListener('mousemove', onMove)
      if (timer !== undefined) clearTimeout(timer)
      if (frame && typeof window.cancelAnimationFrame === 'function') window.cancelAnimationFrame(frame)
    }
  }, [])

  return (
    <div ref={rootRef} className="dog-follower" data-testid="dog-follower" data-state={state} aria-hidden="true">
      <div className="dog-flip" style={facing === 'left' ? { transform: 'scaleX(-1)' } : undefined}>
        {state === 'running' ? <RunningDog /> : <SittingDog />}
      </div>
    </div>
  )
}
