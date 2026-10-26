import { useEffect, useRef, useState } from 'react'

export type DogState = 'running' | 'sitting'

type Phase = 'idle' | 'approach' | 'chase' | 'away' | 'return'
type Vec = { x: number; y: number }

const SIZE = 64
const CAT_SIZE = 56
// Минимальная дистанция до курсора: собака никогда не подходит ближе.
const GAP = 56
// Доля оставшегося пути (сверх дистанции GAP), проходимая за кадр.
const FOLLOW = 0.08
// Через сколько миллисекунд без движения курсора собака садится.
const IDLE_MS = 1200
// Как часто появляется кошка.
const SCENE_INTERVAL_MS = 60_000
// Сколько собака отсутствует после погони.
const AWAY_MS = 10_000
// Дистанция, на которой кошка останавливается перед собакой.
const STOP_MIN = 100
const STOP_MAX = 150
// Скорости в пикселях в секунду.
const APPROACH_SPEED = 300
const CAT_RUN_SPEED = 380
const DOG_RUN_SPEED = 400
// Пауза перед погоней, когда кошка остановилась.
const FACE_OFF_S = 0.6
// Страховка: погоня не длится дольше этого времени.
const CHASE_MAX_S = 12
// Страховка: возвращение собаки не длится дольше этого времени, даже если курсор движется.
const RETURN_MAX_S = 5

function randomBetween(min: number, max: number) {
  return min + Math.random() * Math.max(0, max - min)
}

// Точка чуть за краем экрана: 0 — сверху, 1 — справа, 2 — снизу, 3 — слева.
function edgePoint(edge: number, w: number, h: number): Vec {
  switch (edge) {
    case 0:
      return { x: randomBetween(SIZE, w - SIZE), y: -SIZE }
    case 1:
      return { x: w + SIZE, y: randomBetween(SIZE, h - SIZE) }
    case 2:
      return { x: randomBetween(SIZE, w - SIZE), y: h + SIZE }
    default:
      return { x: -SIZE, y: randomBetween(SIZE, h - SIZE) }
  }
}

function isOffscreen(p: Vec) {
  return p.x < -SIZE || p.x > window.innerWidth + SIZE || p.y < -SIZE || p.y > window.innerHeight + SIZE
}

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

function RunningCat() {
  return (
    <svg viewBox="0 0 64 64" width={CAT_SIZE} height={CAT_SIZE} data-asset="cat-running" focusable="false">
      <path d="M14 34 Q3 30 7 15" fill="none" stroke="#6e6e78" strokeWidth="4" strokeLinecap="round" />
      <g stroke="#6e6e78" strokeWidth="4" strokeLinecap="round">
        <line className="cat-leg" x1="20" y1="42" x2="20" y2="55" />
        <line className="cat-leg cat-leg-b" x1="26" y1="42" x2="26" y2="55" />
        <line className="cat-leg cat-leg-b" x1="38" y1="42" x2="38" y2="55" />
        <line className="cat-leg" x1="44" y1="42" x2="44" y2="55" />
      </g>
      <ellipse cx="30" cy="36" rx="17" ry="8.5" fill="#9a9aa6" />
      <path d="M43 25 L44 13 L50 21 Z" fill="#8a8a96" />
      <path d="M56 25 L55 13 L49 21 Z" fill="#8a8a96" />
      <circle cx="50" cy="29" r="8" fill="#a8a8b4" />
      <circle cx="53" cy="27.5" r="1.4" fill="#222" />
      <circle cx="57.5" cy="31" r="1.2" fill="#d66" />
      <g stroke="#555" strokeWidth="0.8" strokeLinecap="round">
        <line x1="54" y1="32" x2="62" y2="30" />
        <line x1="54" y1="33.5" x2="62" y2="34" />
      </g>
    </svg>
  )
}

export function DogFollower() {
  const rootRef = useRef<HTMLDivElement>(null)
  const catRef = useRef<HTMLDivElement>(null)
  const [state, setState] = useState<DogState>('sitting')
  const [facing, setFacing] = useState<'left' | 'right'>('right')
  const [catVisible, setCatVisible] = useState(false)
  const [catFacing, setCatFacing] = useState<'left' | 'right'>('right')

  useEffect(() => {
    const root = rootRef.current
    if (!root) return

    const pos = {
      x: Math.max(SIZE, window.innerWidth - 96),
      y: Math.max(SIZE, window.innerHeight - 96),
    }
    const cat: Vec = { x: -1000, y: -1000 }
    let cursor: { x: number; y: number } | null = null
    let sitting = true
    let timer: ReturnType<typeof setTimeout> | undefined
    let awayTimer: ReturnType<typeof setTimeout> | undefined
    let frame = 0
    let last: number | undefined

    let phase: Phase = 'idle'
    let originEdge = 0
    let stopDist = STOP_MIN
    let dir: Vec = { x: 1, y: 0 }
    let faceOffTime = 0
    let chaseTime = 0
    let returnTime = 0
    // Минутный таймер сработал, когда сцена ещё шла: запустить кошку сразу после возврата в покой.
    let pendingScene = false

    const render = () => {
      root.style.transform = `translate3d(${pos.x - SIZE / 2}px, ${pos.y - SIZE / 2}px, 0)`
      root.dataset.x = pos.x.toFixed(1)
      root.dataset.y = pos.y.toFixed(1)
      root.dataset.scene = phase
      if (cursor) {
        root.dataset.distance = Math.hypot(cursor.x - pos.x, cursor.y - pos.y).toFixed(1)
      }
    }

    const renderCat = () => {
      const el = catRef.current
      if (!el) return
      el.style.transform = `translate3d(${cat.x - CAT_SIZE / 2}px, ${cat.y - CAT_SIZE / 2}px, 0)`
      el.dataset.x = cat.x.toFixed(1)
      el.dataset.y = cat.y.toFixed(1)
      el.dataset.distance = Math.hypot(cat.x - pos.x, cat.y - pos.y).toFixed(1)
    }

    const armIdle = () => {
      if (timer !== undefined) clearTimeout(timer)
      timer = setTimeout(() => {
        if (phase === 'idle') {
          sitting = true
          setState('sitting')
        }
      }, IDLE_MS)
    }

    const wake = () => {
      if (sitting) {
        sitting = false
        setState('running')
      }
      armIdle()
    }

    const onMove = (event: MouseEvent) => {
      cursor = { x: event.clientX, y: event.clientY }
      if (phase === 'idle') wake()
    }

    const startScene = () => {
      if (phase !== 'idle') return
      pendingScene = false
      const w = window.innerWidth
      const h = window.innerHeight
      originEdge = Math.floor(Math.random() * 4)
      stopDist = randomBetween(STOP_MIN, STOP_MAX)
      let start = edgePoint(originEdge, w, h)
      let dx = start.x - pos.x
      let dy = start.y - pos.y
      let d = Math.hypot(dx, dy) || 1
      // Если собака у самого края, кошка стартует дальше за краем, чтобы пройти путь до остановки.
      if (d < stopDist + SIZE) {
        start = { x: pos.x + (dx / d) * (stopDist + SIZE), y: pos.y + (dy / d) * (stopDist + SIZE) }
        dx = start.x - pos.x
        dy = start.y - pos.y
        d = Math.hypot(dx, dy) || 1
      }
      dir = { x: dx / d, y: dy / d }
      cat.x = start.x
      cat.y = start.y
      if (timer !== undefined) clearTimeout(timer)
      sitting = true
      phase = 'approach'
      faceOffTime = 0
      chaseTime = 0
      setState('sitting')
      setFacing(dir.x >= 0 ? 'right' : 'left')
      setCatFacing(dir.x <= 0 ? 'right' : 'left')
      setCatVisible(true)
      render()
    }

    const returnDog = () => {
      const w = window.innerWidth
      const h = window.innerHeight
      const start = edgePoint((originEdge + 2) % 4, w, h)
      pos.x = start.x
      pos.y = start.y
      if (!cursor) cursor = { x: w / 2, y: h / 2 }
      phase = 'return'
      returnTime = 0
      sitting = false
      setState('running')
      root.style.visibility = ''
      render()
    }

    const endChase = () => {
      phase = 'away'
      root.style.visibility = 'hidden'
      setCatVisible(false)
      render()
      awayTimer = setTimeout(returnDog, AWAY_MS)
    }

    const followStep = (dt: number) => {
      if (!cursor) return
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
      if (phase === 'return') {
        returnTime += dt
        // Возврат завершается по сближению или по тайм-ауту: при движущемся курсоре сближение может не наступить.
        if (Math.abs(dist - GAP) < 6 || returnTime > RETURN_MAX_S) {
          phase = 'idle'
          armIdle()
          render()
        }
      }
    }

    const tick = (ts: number) => {
      const dt = last === undefined ? 0 : Math.min(Math.max((ts - last) / 1000, 0), 0.05)
      last = ts

      if (phase === 'idle') {
        if (pendingScene) {
          startScene()
        } else if (cursor && !sitting) {
          followStep(dt)
        }
      } else if (phase === 'return') {
        followStep(dt)
      } else if (phase === 'approach') {
        const remaining = Math.hypot(cat.x - pos.x, cat.y - pos.y) - stopDist
        const step = APPROACH_SPEED * dt
        if (remaining > 0) {
          if (step >= remaining) {
            cat.x = pos.x + dir.x * stopDist
            cat.y = pos.y + dir.y * stopDist
          } else {
            cat.x -= dir.x * step
            cat.y -= dir.y * step
          }
        } else {
          // Кошка остановилась: короткая пауза, затем собака бросается в погоню.
          faceOffTime += dt
          if (faceOffTime >= FACE_OFF_S) {
            phase = 'chase'
            setState('running')
            setFacing(dir.x >= 0 ? 'right' : 'left')
            setCatFacing(dir.x >= 0 ? 'right' : 'left')
          }
        }
        renderCat()
        render()
      } else if (phase === 'chase') {
        cat.x += dir.x * CAT_RUN_SPEED * dt
        cat.y += dir.y * CAT_RUN_SPEED * dt
        pos.x += dir.x * DOG_RUN_SPEED * dt
        pos.y += dir.y * DOG_RUN_SPEED * dt
        chaseTime += dt
        renderCat()
        render()
        if ((isOffscreen(pos) && isOffscreen(cat)) || chaseTime > CHASE_MAX_S) endChase()
      }
      frame = window.requestAnimationFrame(tick)
    }

    render()
    window.addEventListener('mousemove', onMove, { passive: true })
    const sceneTimer = setInterval(() => {
      if (phase === 'idle') startScene()
      else pendingScene = true
    }, SCENE_INTERVAL_MS)
    if (typeof window.requestAnimationFrame === 'function') {
      frame = window.requestAnimationFrame(tick)
    }
    return () => {
      window.removeEventListener('mousemove', onMove)
      clearInterval(sceneTimer)
      if (timer !== undefined) clearTimeout(timer)
      if (awayTimer !== undefined) clearTimeout(awayTimer)
      if (frame && typeof window.cancelAnimationFrame === 'function') window.cancelAnimationFrame(frame)
    }
  }, [])

  return (
    <>
      <div ref={rootRef} className="dog-follower" data-testid="dog-follower" data-state={state} aria-hidden="true">
        <div className="dog-flip" style={facing === 'left' ? { transform: 'scaleX(-1)' } : undefined}>
          {state === 'running' ? <RunningDog /> : <SittingDog />}
        </div>
      </div>
      {catVisible && (
        <div ref={catRef} className="cat-runner" data-testid="cat-runner" aria-hidden="true">
          <div className="cat-flip" style={catFacing === 'left' ? { transform: 'scaleX(-1)' } : undefined}>
            <RunningCat />
          </div>
        </div>
      )}
    </>
  )
}
