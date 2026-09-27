// Vesta's small third-party helpers (marked, DOMPurify, MiniSearch, ts-fsrs), bundled into static/libs.js as window.VestaLibs.
// Kept apart from the TipTap bundle (index.js) so neither has to rebuild for the other.
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import MiniSearch from 'minisearch'
import { fsrs, generatorParameters, createEmptyCard, Rating, State } from 'ts-fsrs'

// GitHub-flavoured markdown (tables, strikethrough, autolinks), and a single line break
// kept as a line break, which is how the old renderer and chat models both treat it.
marked.setOptions({ gfm: true, breaks: true })

// Links in a reply open in a new tab and cannot reach back into Vesta.
DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'A' && node.getAttribute('href')) {
    node.setAttribute('target', '_blank')
    node.setAttribute('rel', 'noopener noreferrer')
  }
})

/**
 * Markdown from a model, as HTML safe to put on the page. marked does the formatting;
 * DOMPurify removes anything that could run (scripts, event handlers, javascript:
 * links). Images and forms are dropped too: a reply has no business loading a remote
 * picture or asking for input.
 */
function markdown(text) {
  const html = marked.parse(String(text || ''), { async: false })
  return DOMPurify.sanitize(html, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ['img', 'form', 'input', 'button', 'textarea', 'select', 'style', 'iframe'],
  })
}

// FSRS, the scheduler Anki moved to in 2023, for flashcards. Whole days only: no
// minute-long learning steps, because Vesta's due dates are dates and a missed card
// comes back later in the same sitting anyway. No fuzz, so the interval a button shows
// is the interval that is stored.
const FSRS = { fsrs, generatorParameters, createEmptyCard, Rating, State }

window.VestaLibs = { markdown, MiniSearch, FSRS }
