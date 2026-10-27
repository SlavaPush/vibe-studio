const CAT_SIZE = 64
const DOG_SIZE = 44

// Сцена: кошка (крупнее, с татуировками) и собака с чемоданом играют в карты за решёткой.
export function JailScene() {
  return (
    <div
      className="jail-scene"
      data-testid="jail-scene"
      role="group"
      aria-label="Кошка и собака с чемоданом играют в карты за решёткой"
    >
      <div className="jail-cell">
        <svg
          className="jail-cat"
          data-testid="jail-cat"
          data-asset="cat-jail"
          viewBox="0 0 64 64"
          width={CAT_SIZE}
          height={CAT_SIZE}
          role="img"
          aria-label="Кошка с татуировками"
          focusable="false"
        >
          <ellipse cx="32" cy="50" rx="22" ry="14" fill="#9a9aa6" />
          <path d="M16 28 L18 8 L28 20 Z" fill="#8a8a96" />
          <path d="M48 28 L46 8 L36 20 Z" fill="#8a8a96" />
          <circle cx="32" cy="30" r="16" fill="#a8a8b4" />
          <circle cx="26" cy="28" r="2" fill="#222" />
          <circle cx="38" cy="28" r="2" fill="#222" />
          <path d="M30 34 L34 34 L32 37 Z" fill="#d66" />
          <g stroke="#555" strokeWidth="0.8" strokeLinecap="round">
            <line x1="22" y1="35" x2="12" y2="33" />
            <line x1="22" y1="37" x2="12" y2="38" />
            <line x1="42" y1="35" x2="52" y2="33" />
            <line x1="42" y1="37" x2="52" y2="38" />
          </g>
          <g
            data-testid="cat-tattoos"
            aria-label="Татуировки"
            fill="none"
            stroke="#2b5fa8"
            strokeWidth="1.6"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M12 46 Q9 42 12 41 Q14 41 14 43 Q14 41 16 41 Q19 42 16 46 L14 49 Z" />
            <path d="M48 42 L48 52 M45 45 L51 45 M44 50 Q48 55 52 50" />
            <path d="M19 22 L22 24 M19 25 L22 27" />
            <path d="M26 56 L38 56 M28 59 L36 59" />
          </g>
        </svg>
        <svg
          className="jail-cards"
          data-testid="jail-cards"
          data-asset="cards"
          viewBox="0 0 28 28"
          width={28}
          height={28}
          role="img"
          aria-label="Игральные карты"
          focusable="false"
        >
          <g transform="rotate(-14 14 14)">
            <rect x="5" y="5" width="14" height="19" rx="2" fill="#fff" stroke="#444" strokeWidth="1" />
            <circle cx="12" cy="14" r="2.6" fill="#c33" />
          </g>
          <g transform="rotate(12 14 14)">
            <rect x="9" y="4" width="14" height="19" rx="2" fill="#fff" stroke="#444" strokeWidth="1" />
            <path d="M16 9 L20 15 L12 15 Z" fill="#222" />
          </g>
        </svg>
        <svg
          className="jail-dog"
          data-testid="jail-dog"
          data-asset="dog-jail"
          viewBox="0 0 64 64"
          width={DOG_SIZE}
          height={DOG_SIZE}
          role="img"
          aria-label="Собака"
          focusable="false"
        >
          <ellipse cx="32" cy="50" rx="20" ry="13" fill="#c8873a" />
          <path d="M14 24 Q10 40 20 42 Q22 32 20 22 Z" fill="#8a5522" />
          <path d="M50 24 Q54 40 44 42 Q42 32 44 22 Z" fill="#8a5522" />
          <circle cx="32" cy="32" r="14" fill="#d99a4e" />
          <ellipse cx="32" cy="39" rx="7" ry="5" fill="#e8b878" />
          <circle cx="26" cy="29" r="2" fill="#222" />
          <circle cx="38" cy="29" r="2" fill="#222" />
          <ellipse cx="32" cy="37" rx="2.4" ry="1.8" fill="#222" />
        </svg>
        <svg
          className="jail-suitcase"
          data-testid="jail-suitcase"
          data-asset="suitcase"
          viewBox="0 0 32 26"
          width={32}
          height={26}
          role="img"
          aria-label="Чемодан"
          focusable="false"
        >
          <path d="M11 7 L11 3 Q11 2 12 2 L20 2 Q21 2 21 3 L21 7" fill="none" stroke="#3b2a14" strokeWidth="2" />
          <rect x="2" y="7" width="28" height="17" rx="3" fill="#a0662a" stroke="#3b2a14" strokeWidth="1.5" />
          <rect x="8" y="7" width="2" height="17" fill="#3b2a14" />
          <rect x="22" y="7" width="2" height="17" fill="#3b2a14" />
          <rect x="14" y="13" width="4" height="3" fill="#e0c050" />
        </svg>
        <div className="jail-bars" data-testid="jail-bars" role="img" aria-label="Тюремная решётка" />
      </div>
    </div>
  )
}
