# UI theme and shell

Every page extends `templates/layout.html`. The layout owns the header, navigation, flash messages, footer, the theme and
the Tailwind configuration. Styles live in `static/ui-theme.css`. There is no build step.

## Files

| File | What it holds |
| --- | --- |
| `templates/layout.html` | Shell markup, nav and its active-page logic, theme and menu scripts, the Tailwind config, `esc()` and `jsq()` |
| `static/ui-theme.css` | Design tokens (both themes), shell styles, `ui-` components, the bridge for older Tailwind classes, print rules |
| `tests/test_ui_theme.py` | Token parity between themes, Tailwind wiring, WCAG contrast of every text and control pair |
| `tests/test_ui_shell.py` | Active link for each nav entry, landmarks, flash messages, footer credit, stylesheet version |
| `tests/templates-ui.test.mjs` | Theme toggle, no-storage fallback, menu, flash dismiss (real layout scripts under Node) |
| `tests/fixtures/serve_all_pages.py` | Development server that renders every page with made-up data, to look at them |

## Add a page

```jinja
{% extends "layout.html" %}
{% block title %}Plan · Workout Manager{% endblock %}
{% block head %}<style>/* page styles, scoped under an id */</style>{% endblock %}
{% block content %}
<div class="ui-page-header">
  <div><h1 class="ui-title">Plan</h1><p class="ui-subtitle">One line of context.</p></div>
  <div class="ui-actions"><a class="ui-btn ui-btn-primary" href="/create">New plan</a></div>
</div>
<section class="ui-card">...</section>
{% endblock %}
```

1. Use only the blocks `title`, `head` and `content`. Put page scripts inside `content`.
2. Call the global `esc()` for every vendor or user string that goes into `innerHTML`. Call `jsq()` for a value inside an
   `onclick="..."` attribute. Do not declare your own `esc` (the browser rejects a second declaration, and the Insights test checks it).
3. Add the page to `ui_nav` in `layout.html` if it needs a nav entry. Entries match on an exact path or a path prefix
   (`/edit/` belongs to Create plan). Add a row to `ACTIVE` in `tests/test_ui_shell.py`.
4. Use tokens, not hex values. Never write `#rrggbb` in a page.

## Tokens

Each colour is an `R G B` triplet, so `rgb(var(--surface) / .5)` works and so does `bg-surface/50`.

| Token | Tailwind class | Use |
| --- | --- | --- |
| `--canvas` | `bg-canvas` | page background |
| `--surface` | `bg-surface` | cards, panels |
| `--raised` | `bg-raised` | buttons, chips, hover fills |
| `--inset` | `bg-inset` | inputs, tracks |
| `--line` | `border-line` | card and divider borders (decoration) |
| `--control-line` | `border-control-line` | input and empty-state borders (3:1 or better) |
| `--fg` | `text-fg` | body text |
| `--fg-strong` | `text-white` | headings. Dark in the light theme. See below |
| `--muted` | `text-muted` | secondary text |
| `--accent` | `text-accent` | links, active state (teal) |
| `--accent-solid`, `--on-accent` | `bg-accent-solid text-on-accent` | primary buttons |
| `--success`, `--warning`, `--danger` | `text-success` ... | status text |
| `--focus` | `ring-focus` | focus ring colour |
| `--radius-sm`, `--radius`, `--radius-lg` | | corner radii |
| `--space-1` ... `--space-8`, `--text-xs` ... `--text-3xl` | | spacing and type scale, for CSS written by hand |

The scales `gray`, `blue`, `green`, `red`, `yellow`, `orange` and `purple` are also tokens (`--gray-800` ...).
Tailwind's `gray-800`, `text-blue-400`, `bg-green-600` and so on point at them, so older markup follows the theme.
`blue` is the accent hue (teal). The shades 500 to 700 are solid fills that take white text in both themes.

### Why `text-white` flips

In the dark theme `text-white` is white. In the light theme it is near-black, because it means "strong text". Solid fills
(`bg-blue-600`, `bg-red-700`, `bg-black`, `.ui-btn-primary` ...) set `--fg-strong` back to white for themselves, and any
`bg-gray-*` surface or `.ui-card` inside them resets it. Write a new component with `text-fg` and `text-muted` and you
do not need this.

## Themes

* Dark is the default. Light follows `prefers-color-scheme: light` until a person chooses.
* The header button cycles Auto, Light, Dark. The choice is stored in `localStorage` under `sg-theme`.
  Auto removes the key. Every storage access is in `try/catch`. With storage blocked the page still renders and the
  button still works for that page.
* A script in `<head>` sets `data-theme` on `<html>` before the stylesheet loads, so a saved choice never flashes.
* The light tokens appear three times in the CSS (explicit choice, system preference, print). `tests/test_ui_theme.py`
  fails if the copies drift apart.

## Components

All carry the `ui-` prefix so they cannot collide with Tailwind or a page's own classes.

| Class | Use |
| --- | --- |
| `ui-page-header`, `ui-title`, `ui-subtitle`, `ui-actions` | Page title row. One per page |
| `ui-card`, `ui-section-title`, `ui-muted` | Surfaces and headings |
| `ui-grid`, `ui-stat-value`, `ui-stat-label` | Stat tiles |
| `ui-btn` + `ui-btn-primary` / `-secondary` / `-danger` | Buttons. Minimum height 40 px (44 px on touch) |
| `ui-input` | Text inputs and selects |
| `ui-chip` + `-accent` / `-success` / `-warning` / `-danger` | Status pills |
| `ui-empty`, `ui-empty-title` | Empty state. Say what is missing and what to do |
| `ui-flash-*` | Flash messages (rendered by the layout only) |
| `no-print` | Hidden when printing |

## Navigation

Train (Dashboard, Create plan, Library, Browse), Progress (History, Insights), Coach, Account (Settings).

* From 64 rem wide the groups sit in one row, separated by thin dividers. Below that a **Menu button** opens a
  full-width list with group headings. A collapsible menu fits eight links in four groups and keeps a single markup for both
  layouts. A bottom tab bar holds five items at most and would need a second "more" menu.
* The current page has `aria-current="page"`, an accent colour, a bar and a heavier weight (not colour alone).
* Without script the nav is always visible and the theme button is hidden.
* Escape closes the menu and returns focus to its button.

## Accessibility checks

* Skip link first in the page, landmarks `header`, `nav`, `main#main-content`, `footer`.
* Focus ring: 2 px outline in `--focus`, offset 2 px, stronger than Tailwind's `focus:outline-none`.
* `prefers-reduced-motion: reduce` turns animations and transitions off.
* Flash messages: errors are `role="alert"`, the rest `role="status"`, each has a close button.
* Contrast is computed from the tokens by `tests/test_ui_theme.py` and measured on rendered pages in a browser
  (walk every text node, composite its background up the ancestors). Text is 4.5:1 or better in both themes.
  Text in a day cell that is faded to 30 % opacity because it belongs to another month is exempt (inactive component).

## Change the stylesheet

1. Edit `static/ui-theme.css`. Change a token in all places it appears (dark block and the three light blocks).
2. Set `?v=` in the `<link>` in `layout.html` to the value `tests/test_ui_shell.py` asks for (the first 10 characters of the file's
   SHA-256). The version stops a browser from pairing new markup with an old cached file.
3. Run `.venv/bin/python -B -m pytest -q`.

## Look at every page

```
.venv/bin/python -B tests/fixtures/serve_all_pages.py 5099     # 127.0.0.1 only, made-up data, no vendor calls
# open http://127.0.0.1:5099/ with Basic auth: any user, password dummy-token-for-ui-fixture-00000
# http://127.0.0.1:5099/_flash-demo flashes one message of every kind
```
