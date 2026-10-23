;(function () {
  var choice = 'system'
  try {
    choice = window.localStorage.getItem('vibe-chat-theme') || 'system'
  } catch {
    choice = 'system'
  }
  var dark =
    choice === 'dark' || (choice !== 'light' && window.matchMedia('(prefers-color-scheme: dark)').matches)
  document.documentElement.classList.toggle('dark', dark)
})()
