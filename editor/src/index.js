/**
 * The notes editor, built on TipTap and bundled into static/editor.js.
 *
 * The first editor was the browser's own contenteditable driven by execCommand. That
 * API is deprecated and keeps its own undo history, which anything edited by hand
 * (font sizes, the tidying of <font> tags, the typed markers) never entered. So undo
 * skipped steps or stopped, and font size could not apply to text not yet typed.
 * TipTap keeps the document as data, so every change goes through one history and a
 * size picked before typing is simply a stored mark.
 *
 * Stored notes keep the markup the old editor wrote: `ul.rte-check` checklists,
 * `div.rte-callout`, `span.rte-math[data-tex]` and `a.rte-embed`. The read-only view
 * and the HTML export style those classes, and every note already saved uses them.
 *
 * Build: `npm install && npm run build` in this folder writes ../static/editor.js.
 * index.html loads it as a plain script and reads `window.VestaEditor`.
 */
import { Editor, Extension, Node, mergeAttributes } from '@tiptap/core'
import { StarterKit } from '@tiptap/starter-kit'
import { TextStyle, Color, FontFamily, FontSize, BackgroundColor } from '@tiptap/extension-text-style'
import { Highlight } from '@tiptap/extension-highlight'
import { TaskList, TaskItem } from '@tiptap/extension-list'
import { Table, TableRow, TableHeader, TableCell } from '@tiptap/extension-table'
import { Image } from '@tiptap/extension-image'
import { Placeholder } from '@tiptap/extensions'
import { BubbleMenu } from '@tiptap/extension-bubble-menu'
import { Suggestion } from '@tiptap/suggestion'
import { PluginKey } from '@tiptap/pm/state'

// ---- icons ---------------------------------------------------------------
// Lucide's outlines, drawn at 24 and scaled by CSS. Shared with the toolbar in
// index.html so the bar, the bubble and the / menu use one set.
const P = {
  undo: '<path d="M3 7v6h6"/><path d="M21 17a9 9 0 0 0-9-9 9 9 0 0 0-6 2.3L3 13"/>',
  redo: '<path d="M21 7v6h-6"/><path d="M3 17a9 9 0 0 1 9-9 9 9 0 0 1 6 2.3l3 2.7"/>',
  bold: '<path d="M6 12h9a4 4 0 0 1 0 8H7a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1h7a4 4 0 0 1 0 8"/>',
  italic: '<line x1="19" x2="10" y1="4" y2="4"/><line x1="14" x2="5" y1="20" y2="20"/><line x1="15" x2="9" y1="4" y2="20"/>',
  underline: '<path d="M6 4v6a6 6 0 0 0 12 0V4"/><line x1="4" x2="20" y1="20" y2="20"/>',
  strike: '<path d="M16 4H9a3 3 0 0 0-2.83 4"/><path d="M14 12a4 4 0 0 1 0 8H6"/><line x1="4" x2="20" y1="12" y2="12"/>',
  code: '<polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>',
  link: '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
  bullet: '<line x1="9" x2="21" y1="6" y2="6"/><line x1="9" x2="21" y1="12" y2="12"/><line x1="9" x2="21" y1="18" y2="18"/><circle cx="4" cy="6" r="1"/><circle cx="4" cy="12" r="1"/><circle cx="4" cy="18" r="1"/>',
  ordered: '<line x1="10" x2="21" y1="6" y2="6"/><line x1="10" x2="21" y1="12" y2="12"/><line x1="10" x2="21" y1="18" y2="18"/><path d="M4 6h1v4"/><path d="M4 10h2"/><path d="M6 18H4c0-1 2-2 2-3s-1-1.5-2-1"/>',
  task: '<rect width="7" height="7" x="3" y="4" rx="1.5"/><path d="m4.5 7.5 1.5 1.5 2.5-2.5"/><rect width="7" height="7" x="3" y="13" rx="1.5"/><path d="M14 7.5h7"/><path d="M14 16.5h7"/>',
  quote: '<path d="M17 6H3"/><path d="M21 12H8"/><path d="M21 18H8"/><path d="M3 12v6"/>',
  codeblock: '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="m10 9-3 3 3 3"/><path d="m14 15 3-3-3-3"/>',
  table: '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M3 9h18"/><path d="M3 15h18"/><path d="M12 3v18"/>',
  callout: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/><path d="M12 7v4"/><path d="M12 14h.01"/>',
  math: '<path d="M18 7V5a1 1 0 0 0-1-1H6.5a.5.5 0 0 0-.4.8l4.5 6a2 2 0 0 1 0 2.4l-4.5 6a.5.5 0 0 0 .4.8H17a1 1 0 0 0 1-1v-2"/>',
  divider: '<path d="M3 12h18"/><path d="M8 6h8" opacity=".35"/><path d="M8 18h8" opacity=".35"/>',
  image: '<rect width="18" height="18" x="3" y="3" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.09-3.09a2 2 0 0 0-2.82 0L6 21"/>',
  file: '<path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 18 8.84l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48"/>',
  text: '<path d="M4 7V4h16v3"/><path d="M9 20h6"/><path d="M12 4v16"/>',
  h1: '<path d="M4 12h8"/><path d="M4 18V6"/><path d="M12 18V6"/><path d="m17 12 3-2v8"/>',
  h2: '<path d="M4 12h8"/><path d="M4 18V6"/><path d="M12 18V6"/><path d="M21 18h-4c0-4 4-3 4-6 0-1.5-2-2.5-4-1"/>',
  h3: '<path d="M4 12h8"/><path d="M4 18V6"/><path d="M12 18V6"/><path d="M17.5 10.5c1.7-1 3.5 0 3.5 1.5a2 2 0 0 1-2 2"/><path d="M17 17.5c2 1.5 4 .3 4-1.5a2 2 0 0 0-2-2"/>',
  clear: '<path d="M4 7V4h16v3"/><path d="M5 20h6"/><path d="M13 4 8 20"/><path d="m15 15 5 5"/><path d="m20 15-5 5"/>',
  highlight: '<path d="m9 11-6 6v3h9l3-3"/><path d="m22 12-4.6 4.6a2 2 0 0 1-2.8 0l-5.2-5.2a2 2 0 0 1 0-2.8L14 4"/>',
  minus: '<path d="M5 12h14"/>',
  plus: '<path d="M5 12h14"/><path d="M12 5v14"/>',
  hide: '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M3 9h18"/><path d="m9 16 3-3 3 3"/>',
  show: '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M3 9h18"/><path d="m15 13-3 3-3-3"/>',
  rowAdd: '<path d="M3 5h18v6H3z"/><path d="M12 15v6"/><path d="M9 18h6"/>',
  colAdd: '<path d="M5 3v18h6V3z"/><path d="M15 12h6"/><path d="M18 9v6"/>',
  rowDel: '<path d="M3 5h18v6H3z"/><path d="M9 18h6"/>',
  colDel: '<path d="M5 3v18h6V3z"/><path d="M15 12h6"/>',
  trash: '<path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>'
}
const icon = name => '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
  + 'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + (P[name] || '') + '</svg>'

// ---- Vesta's own blocks -----------------------------------------------------

const CALLOUT_ICON = { info: 'ⓘ', warn: '⚠', good: '✓' }

/** A tinted box for something worth noticing. Its contents are ordinary blocks. */
const Callout = Node.create({
  name: 'callout',
  group: 'block',
  content: 'block+',
  defining: true,
  addAttributes() {
    return {
      tone: {
        default: 'info',
        parseHTML: el => el.getAttribute('data-tone') || 'info',
        renderHTML: a => ({ 'data-tone': a.tone })
      }
    }
  },
  parseHTML() {
    // The old editor wrote the text into a bare <div> beside the icon span.
    return [{ tag: 'div.rte-callout', contentElement: el => el.querySelector(':scope > div') || el }]
  },
  renderHTML({ node, HTMLAttributes }) {
    return ['div', mergeAttributes(HTMLAttributes, { class: 'rte-callout' }),
      ['span', { class: 'rte-callout-ic', contenteditable: 'false' }, CALLOUT_ICON[node.attrs.tone] || CALLOUT_ICON.info],
      ['div', { class: 'rte-callout-body' }, 0]]
  },
  addCommands() {
    return {
      toggleCallout: tone => ({ editor, commands }) =>
        editor.isActive(this.name) ? commands.lift(this.name) : commands.wrapIn(this.name, { tone: tone || 'info' })
    }
  },
  addKeyboardShortcuts() {
    return {
      // Backspace at the very start of a callout takes the box away and keeps the text,
      // the way it leaves a quote in Notion.
      Backspace: () => {
        const { $from, empty } = this.editor.state.selection
        if (!empty || $from.parentOffset !== 0) return false
        for (let d = $from.depth; d > 0; d--) {
          if ($from.node(d).type.name === this.name) {
            if ($from.index(d) !== 0 || $from.start(d + 1) !== $from.start($from.depth)) return false
            return this.editor.commands.lift(this.name)
          }
        }
        return false
      }
    }
  }
})

/**
 * An equation. Only the LaTeX is stored, in data-tex; KaTeX draws it on screen when
 * it has loaded (the app fetches it on first use), and the LaTeX shows until then.
 */
const MathInline = Node.create({
  name: 'math',
  group: 'inline',
  inline: true,
  atom: true,
  selectable: true,
  addOptions() {
    return { render: null, onEdit: null }
  },
  addAttributes() {
    return {
      tex: { default: '', parseHTML: el => el.getAttribute('data-tex') || el.textContent || '', renderHTML: () => ({}) },
      display: { default: false, parseHTML: el => el.classList.contains('block'), renderHTML: () => ({}) }
    }
  },
  parseHTML() {
    return [{ tag: 'span.rte-math', priority: 60 }]
  },
  renderHTML({ node }) {
    return ['span', { class: 'rte-math raw' + (node.attrs.display ? ' block' : ''), 'data-tex': node.attrs.tex }, node.attrs.tex]
  },
  addNodeView() {
    const opts = this.options
    return ({ node, getPos, editor }) => {
      let current = node
      const dom = document.createElement('span')
      dom.contentEditable = 'false'
      const paint = () => {
        dom.className = 'rte-math raw' + (current.attrs.display ? ' block' : '')
        dom.setAttribute('data-tex', current.attrs.tex)
        dom.textContent = current.attrs.tex
        if (opts.render) opts.render(dom)
      }
      paint()
      dom.addEventListener('click', e => {
        if (!editor.isEditable || !opts.onEdit) return
        e.preventDefault()
        opts.onEdit(current.attrs.tex, tex => {
          const pos = getPos()
          if (typeof pos !== 'number') return
          if (tex === null || tex === undefined) return
          if (!tex.trim()) {
            editor.chain().focus().deleteRange({ from: pos, to: pos + 1 }).run()
            return
          }
          editor.chain().focus().command(({ tr }) => {
            tr.setNodeMarkup(pos, undefined, { tex, display: tex.length > 24 })
            return true
          }).run()
        })
      })
      return {
        dom,
        update(next) {
          if (next.type !== current.type) return false
          current = next
          paint()
          return true
        },
        // KaTeX rewrites the inside; none of that is the document's business.
        ignoreMutation: () => true
      }
    }
  },
  addCommands() {
    return {
      insertMath: tex => ({ commands }) =>
        commands.insertContent({ type: this.name, attrs: { tex, display: tex.length > 24 } })
    }
  }
})

/**
 * A file from the class, shown as a chip. It points at the file rather than copying
 * it, and clicking it opens Vesta's own preview (the app's click handler reads
 * data-action="preview-file").
 */
const FileEmbed = Node.create({
  name: 'fileEmbed',
  group: 'inline',
  inline: true,
  atom: true,
  selectable: true,
  addAttributes() {
    const none = () => ({})
    return {
      id: { default: null, parseHTML: el => el.getAttribute('data-id'), renderHTML: none },
      fileId: { default: null, parseHTML: el => el.getAttribute('data-file-id'), renderHTML: none },
      title: {
        default: 'File',
        parseHTML: el => {
          const spans = el.querySelectorAll(':scope > span')
          return (spans.length ? spans[spans.length - 1] : el).textContent || 'File'
        },
        renderHTML: none
      },
      color: { default: '', parseHTML: el => { const ic = el.querySelector('.file-ic'); return ic ? ic.style.background : '' }, renderHTML: none },
      initials: { default: '', parseHTML: el => { const ic = el.querySelector('.file-ic'); return ic ? ic.textContent : '' }, renderHTML: none }
    }
  },
  parseHTML() {
    // Above the link mark's a[href], or the chip would come back as a plain link.
    return [{ tag: 'a.rte-embed', priority: 1000 }]
  },
  renderHTML({ node }) {
    const a = node.attrs
    return ['a', {
      class: 'rte-embed', contenteditable: 'false', href: '#',
      'data-action': 'preview-file', 'data-id': a.id, 'data-file-id': a.fileId || a.id
    },
    ['span', { class: 'file-ic', style: a.color ? 'background:' + a.color : null }, a.initials || ''],
    ['span', {}, a.title || 'File']]
  },
  addCommands() {
    return {
      insertFileEmbed: attrs => ({ commands }) => commands.insertContent({ type: this.name, attrs })
    }
  }
})

// Checklists keep the old markup's class, so notes written before read the same in
// the preview and the export, and old ones load with their ticks.
const VTaskList = TaskList.extend({
  parseHTML() {
    return [{ tag: 'ul[data-type="taskList"]', priority: 52 }, { tag: 'ul.rte-check', priority: 52 }]
  }
})
const VTaskItem = TaskItem.extend({
  addAttributes() {
    return {
      checked: {
        default: false,
        keepOnSplit: false,
        parseHTML: el => {
          const c = el.getAttribute('data-checked')
          if (c === '' || c === 'true') return true
          if (el.getAttribute('data-done') === '1') return true
          const box = el.querySelector(':scope > input[type="checkbox"]')
          return !!(box && box.hasAttribute('checked'))
        },
        renderHTML: a => ({ 'data-checked': a.checked, 'data-done': a.checked ? '1' : '0' })
      }
    }
  },
  parseHTML() {
    return [
      { tag: 'li[data-type="taskItem"]', priority: 52, contentElement: el => el.querySelector(':scope > div') || el },
      { tag: 'ul.rte-check > li', priority: 52, contentElement: el => el.querySelector(':scope > div') || el }
    ]
  }
})

/** Keys that are not any one block's business. */
const Keys = Extension.create({
  name: 'vestaKeys',
  priority: 50,           // after lists and tables, which want Tab first
  addOptions() {
    return { onLink: null }
  },
  addKeyboardShortcuts() {
    const inList = () => this.editor.isActive('listItem') || this.editor.isActive('taskItem')
    return {
      'Mod-Shift-x': () => this.editor.commands.toggleStrike(),
      'Mod-k': () => {
        if (!this.options.onLink) return false
        this.options.onLink()
        return true
      },
      // Tab must never throw the caret out of the page. In a list it nests (handled
      // above this); in code it indents; anywhere else it leaves a gap.
      Tab: () => {
        if (inList()) return true
        if (this.editor.isActive('codeBlock')) return this.editor.commands.insertContent('  ')
        return this.editor.commands.insertContent('    ')
      },
      'Shift-Tab': () => true
    }
  }
})

// ---- the / menu ---------------------------------------------------------------

/**
 * Every block the / menu offers. `run` is given a chain that has already removed the
 * typed "/query"; `app` items need something from the page (a dialog, the file
 * picker) and are handed back to it through onInsert.
 */
const BLOCKS = [
  { id: 'text', group: 'Basic', title: 'Text', hint: 'Plain writing', icon: 'text', keys: 'paragraph plain normal', run: c => c.setParagraph() },
  { id: 'h1', group: 'Basic', title: 'Heading 1', hint: 'Big section title', icon: 'h1', keys: 'title', run: c => c.setHeading({ level: 1 }) },
  { id: 'h2', group: 'Basic', title: 'Heading 2', hint: 'Medium section title', icon: 'h2', keys: 'subtitle', run: c => c.setHeading({ level: 2 }) },
  { id: 'h3', group: 'Basic', title: 'Heading 3', hint: 'Small section title', icon: 'h3', keys: 'subheading', run: c => c.setHeading({ level: 3 }) },
  { id: 'bullet', group: 'Basic', title: 'Bulleted list', hint: 'A simple list', icon: 'bullet', keys: 'unordered ul dash', run: c => c.toggleBulletList() },
  { id: 'ordered', group: 'Basic', title: 'Numbered list', hint: 'A list in order', icon: 'ordered', keys: 'ordered ol', run: c => c.toggleOrderedList() },
  { id: 'task', group: 'Basic', title: 'To-do list', hint: 'Tick things off', icon: 'task', keys: 'checklist checkbox todo', run: c => c.toggleTaskList() },
  { id: 'quote', group: 'Basic', title: 'Quote', hint: 'Set a passage apart', icon: 'quote', keys: 'blockquote cite', run: c => c.toggleBlockquote() },
  { id: 'callout', group: 'Basic', title: 'Callout', hint: 'Make something stand out', icon: 'callout', keys: 'note info box', run: c => c.toggleCallout('info') },
  { id: 'code', group: 'Basic', title: 'Code block', hint: 'Code, spaced as typed', icon: 'codeblock', keys: 'pre snippet', run: c => c.toggleCodeBlock() },
  { id: 'divider', group: 'Basic', title: 'Divider', hint: 'A line across the page', icon: 'divider', keys: 'hr rule line separator', run: c => c.setHorizontalRule() },
  { id: 'table', group: 'Insert', title: 'Table', hint: 'Rows and columns', icon: 'table', keys: 'grid', run: c => c.insertTable({ rows: 3, cols: 3, withHeaderRow: true }) },
  { id: 'math', group: 'Insert', title: 'Equation', hint: 'LaTeX, drawn properly', icon: 'math', keys: 'latex formula maths', app: true },
  { id: 'image', group: 'Insert', title: 'Image', hint: 'From a web address', icon: 'image', keys: 'picture photo', app: true },
  { id: 'embed', group: 'Insert', title: 'File from a class', hint: 'Link a file you uploaded', icon: 'file', keys: 'embed attach upload pdf', app: true },
  { id: 'link', group: 'Insert', title: 'Link', hint: 'A web address', icon: 'link', keys: 'url href', app: true }
]

function matchBlocks(query) {
  const q = (query || '').toLowerCase().trim()
  if (!q) return BLOCKS
  return BLOCKS.filter(b => (b.title + ' ' + (b.keys || '') + ' ' + b.id).toLowerCase().includes(q))
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[ch])
}

/** The popup's own DOM: grouped rows, one highlighted, driven by the keyboard. */
function slashRenderer() {
  let el = null, unmount = null, items = [], sel = 0, current = null

  const draw = () => {
    if (!el) return
    if (!items.length) {
      el.innerHTML = '<div class="ne-slash-empty">No block called that</div>'
      return
    }
    let html = '', lastGroup = null
    items.forEach((b, i) => {
      if (b.group !== lastGroup) {
        html += '<div class="ne-slash-group">' + escapeHtml(b.group) + '</div>'
        lastGroup = b.group
      }
      html += '<button type="button" class="ne-slash-item' + (i === sel ? ' on' : '') + '" data-i="' + i + '">'
        + '<span class="ne-slash-ic">' + icon(b.icon) + '</span>'
        + '<span class="ne-slash-txt"><span class="ne-slash-title">' + escapeHtml(b.title) + '</span>'
        + '<span class="ne-slash-hint">' + escapeHtml(b.hint) + '</span></span></button>'
    })
    el.innerHTML = html
    const on = el.querySelector('.ne-slash-item.on')
    if (on) on.scrollIntoView({ block: 'nearest' })
  }

  return {
    onStart(props) {
      current = props
      items = props.items
      sel = 0
      el = document.createElement('div')
      el.className = 'ne-slash'
      // Keep the caret in the note while the menu is clicked.
      el.addEventListener('mousedown', e => e.preventDefault())
      el.addEventListener('click', e => {
        const b = e.target.closest('[data-i]')
        if (b && current) current.command(items[Number(b.dataset.i)])
      })
      el.addEventListener('mousemove', e => {
        const b = e.target.closest('[data-i]')
        if (b && Number(b.dataset.i) !== sel) { sel = Number(b.dataset.i); draw() }
      })
      draw()
      unmount = props.mount(el)
    },
    onUpdate(props) {
      current = props
      items = props.items
      sel = 0
      draw()
    },
    onKeyDown({ event }) {
      if (event.key === 'Escape') return true
      if (!items.length) return false
      if (event.key === 'ArrowDown') { sel = (sel + 1) % items.length; draw(); return true }
      if (event.key === 'ArrowUp') { sel = (sel - 1 + items.length) % items.length; draw(); return true }
      if (event.key === 'Enter' || event.key === 'Tab') { current.command(items[sel]); return true }
      return false
    },
    onExit() {
      if (unmount) unmount()
      else if (el) el.remove()
      el = null; unmount = null; current = null
    }
  }
}

const Slash = Extension.create({
  name: 'slashMenu',
  addOptions() {
    return { onInsert: null }
  },
  addProseMirrorPlugins() {
    const onInsert = this.options.onInsert
    return [Suggestion({
      pluginKey: new PluginKey('slashMenu'),
      editor: this.editor,
      char: '/',
      allow: ({ state }) => !state.selection.$from.parent.type.spec.code,
      items: ({ query }) => matchBlocks(query),
      command: ({ editor, range, props: block }) => {
        const chain = editor.chain().focus().deleteRange(range)
        if (block.app) {
          chain.run()
          if (onInsert) onInsert(block.id)
          return
        }
        block.run(chain).run()
      },
      render: slashRenderer
    })]
  }
})

// ---- font size -------------------------------------------------------------

const SIZES = [10, 11, 12, 13, 14, 16, 18, 20, 24, 28, 32, 40, 48]

/** The size the next letter will be: a picked size waiting to be typed, else what is on screen. */
function currentFontSize(editor) {
  const stored = editor.state.storedMarks
  const pending = stored && stored.find(m => m.type.name === 'textStyle' && m.attrs.fontSize)
  if (pending) return Math.round(parseFloat(pending.attrs.fontSize))
  const set = editor.getAttributes('textStyle').fontSize
  if (set) return Math.round(parseFloat(set))
  try {
    const at = editor.view.domAtPos(editor.state.selection.from)
    const el = at.node.nodeType === 1 ? at.node : at.node.parentElement
    return Math.round(parseFloat(getComputedStyle(el).fontSize))
  } catch (e) {
    return 16
  }
}

function stepFontSize(editor, dir) {
  const cur = currentFontSize(editor)
  const next = dir > 0 ? SIZES.find(s => s > cur) : SIZES.slice().reverse().find(s => s < cur)
  if (!next) return false
  return editor.chain().focus().setFontSize(next + 'px').run()
}

// ---- loading old notes ---------------------------------------------------------

const FONT_SIZE_PX = { 1: '10px', 2: '13px', 3: '16px', 4: '18px', 5: '24px', 6: '32px', 7: '48px' }

/** <font> tags from the first editor become spans, which the schema understands. */
function normalize(html) {
  if (!html || html.indexOf('<font') < 0) return html || ''
  const t = document.createElement('template')
  t.innerHTML = html
  t.content.querySelectorAll('font').forEach(f => {
    const span = document.createElement('span')
    if (f.getAttribute('color')) span.style.color = f.getAttribute('color')
    if (f.getAttribute('face')) span.style.fontFamily = f.getAttribute('face')
    if (f.getAttribute('size')) span.style.fontSize = FONT_SIZE_PX[f.getAttribute('size')] || ''
    while (f.firstChild) span.appendChild(f.firstChild)
    f.parentNode.replaceChild(span, f)
  })
  return t.innerHTML
}

// ---- making one ---------------------------------------------------------------

/**
 * opts:
 *   element      where the editor mounts
 *   content      the note's stored HTML
 *   attributes   put on the editable element (id, class, data-*)
 *   bubble       an element to float over selected text, or null
 *   bubbleOn     () => boolean, checked each time the bubble would show
 *   onInsert     (kind) for blocks that need the page: math, image, embed, link
 *   renderMath   (el) draws KaTeX into an equation's element
 *   onEditMath   (tex, done) asks for new LaTeX; done(null) cancels
 */
function create(opts) {
  const extensions = [
    StarterKit.configure({
      heading: { levels: [1, 2, 3, 4] },
      link: {
        openOnClick: false,
        autolink: true,
        defaultProtocol: 'https',
        HTMLAttributes: { target: '_blank', rel: 'noopener noreferrer' }
      },
      undoRedo: { depth: 500 }
    }),
    TextStyle, Color, FontFamily, FontSize, BackgroundColor,
    Highlight.configure({ multicolor: true }),
    VTaskList.configure({ HTMLAttributes: { class: 'rte-check' } }),
    VTaskItem.configure({ nested: true }),
    Table.configure({ resizable: false }), TableRow, TableHeader, TableCell,
    Image.configure({ allowBase64: true }),
    Callout,
    MathInline.configure({ render: opts.renderMath || null, onEdit: opts.onEditMath || null }),
    FileEmbed,
    Placeholder.configure({
      placeholder: ({ node, editor }) => {
        if (node.type.name === 'heading') return 'Heading ' + node.attrs.level
        if (editor.isEmpty) return opts.placeholder || 'Start writing, or press / for blocks'
        return 'Press / for blocks'
      }
    }),
    Keys.configure({ onLink: () => opts.onInsert && opts.onInsert('link') }),
    Slash.configure({ onInsert: opts.onInsert || null })
  ]
  if (opts.bubble) {
    extensions.push(BubbleMenu.configure({
      element: opts.bubble,
      appendTo: () => document.body,
      options: { placement: 'top', offset: 8, flip: true, shift: { padding: 8 } },
      shouldShow: ({ editor, state, from, to }) => {
        if (opts.bubbleOn && !opts.bubbleOn()) return false
        if (!editor.isEditable || !editor.isFocused || from === to) return false
        if (editor.isActive('codeBlock') || state.selection.node) return false
        return state.doc.textBetween(from, to, ' ').trim().length > 0
      }
    }))
  }
  return new Editor({
    element: opts.element,
    extensions,
    content: normalize(opts.content),
    editorProps: {
      attributes: opts.attributes || {},
      // Links open with cmd or ctrl held, so a plain click still puts the caret in them.
      handleClick(view, pos, event) {
        const a = event.target && event.target.closest && event.target.closest('a[href]')
        if (!a || a.classList.contains('rte-embed')) return false
        if (!(event.metaKey || event.ctrlKey)) return false
        window.open(a.href, '_blank', 'noopener')
        return true
      }
    }
  })
}

window.VestaEditor = { create, icon, SIZES, currentFontSize, stepFontSize, BLOCKS }
