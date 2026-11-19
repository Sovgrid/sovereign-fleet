# RFC: a lean, mobile-first fleet UI (UI-REDESIGN)

Status: look approved by the operator on 2026-11-18 (prototype `fff00dc`, `/static/preview/index.html`); next the real shell (ARCHITECTURE.md phase 4); part of `ARCHITECTURE.md` (FLEET-ARCH, decided 2026-11-18: headless core, open-standard surfaces; this dashboard becomes the slim reference surface and an MCP Apps host). The operator decides the look and every name; this document is the brief for
the clickable prototype and, once the operator has chosen, for the real build.

## Why

- The dashboard is one page of about 8000 lines. Every feature added a panel below the last one:
  KPIs, Signer, Privateers, Routines, New task, Tasks, Models, Lane health, Coordinator, Budgets,
  Issue sources and Settings, all on one scroll. On a phone the operator cannot find what needs him.
- Phone-signer setup on 2026-11-18 needed a coordinator at every step:
  - a QR code the phone cannot scan when the dashboard is open on that same phone;
  - two pairing ways side by side with no advice;
  - a long `sudo` command;
  - protocol names on the operator's path.
  The operator's verdict: not production ready.
- The model is Pi Pocket (MIT, `TannerMidd/pi-pocket`), whose UX the operator calls state of the art.
  What it gets right, and what we adopt:
  - a lean shell where everything else is a module;
  - a layout each person arranges (show, hide, reorder);
  - extensions that bring their own views and commands;
  - templates as one-tap starts;
  - themes as data;
  - a launcher that searches everything;
  - bottom sheets for settings on phones;
  - install to the home screen, with push.

## Principles

1. **Home answers two questions:** "What needs me?" (approvals, failures, a signature) and "What is
   running?". Nothing else is on it by default.
2. **Everything else is a module.** A module has an id, a title, an icon and a place:
   - `home` (a tile on Home),
   - `menu` (a tile in the menu),
   - `settings` (a row in Settings),
   - `task` (a section in a task's detail).
   Today's panels become modules. No function is dropped; it moves to where it is looked for.
3. **The person arranges it.** Settings, Layout lists every module per place with a switch and
   up/down. Reset restores the defaults.
4. **Plugins bring their own module.** A fleet plugin (Privateer, Matrix, Buzz, a capability) declares
   a module, and the shell shows it. The dashboard stops growing with every feature.
5. **Templates.** "New task" starts from a template: name, project, prompt with blanks, gate, allowed
   paths, lane policy. Routines use the same templates. Templates are files, global and per project.
6. **Plain words on the operator's path.**
   - Protocol names (NIP-46, kind 4911, bunker, anchor) appear only under "Details" or in docs.
   - The operator sees "Connect phone", "Approve on your phone", "Confirm once on the computer".
7. **Phone first, desktop richer.** On a phone a module opens as a bottom sheet with a grip, and
   swipe or back closes it. On a wide screen it opens beside the list. One code path, two layouts.
8. **Themes are data:** colour tokens on `:root`, dark and light, selectable in Settings, Appearance.

## Lightness: essentials first, the rest on a tap (operator, 2026-11-18)

The operator: "still over-engineered and complex; I miss Pi Pocket's lightness; advanced options collapsed."

Measured on UI-PROTO-5 at 390 px, with nothing opened:

| Screen | Visible words | Controls | Length |
|---|---|---|---|
| Schedule | 356 | 32 | 3.1 screens |
| Appearance | 320 | 46 | 4 screens |
| Task | 149 | 8 cards | 1.8 screens |
| Approval | 132 | 4 cards | 1.2 screens |

Pi Pocket is light because it shows one line per thing and opens the rest on demand (progressive disclosure).

**Rules, binding for every screen:**
1. **One line per item:**
   - a quiet status mark, the title, and one short meta at the right ("in 13 h", "needs you", "2 min");
   - no second line by default, and no buttons inside rows;
   - the row is the button, and it opens a sheet.
2. **One primary action per screen at most.** Other actions sit in the sheet or behind a quiet `...` menu.
3. **Everything advanced is folded** (`<details data-advanced>`, closed by default) and labelled plainly:
   - filters, search, legends, spend, tables, technical details;
   - every appearance setting beyond Theme and Colors;
   - provider limits;
   - the why and the details of an approval.
4. **Short words on the default view:**
   - no explanatory sentences; at most one hint line per screen;
   - meta in fragments ("in 13 h"), not sentences ("runs tomorrow at 03:32, which is in 13 hours").
5. **Fewer boxes:**
   - no card inside a card;
   - sections are plain lists on hairlines with a small quiet header;
   - cards only for what floats or what matters (the approval question).
6. **Quieter type:**
   - body 15 px and meta 13 px at the default text size; compact density is the default;
   - one accent; status as small marks, not filled boxes.

**Targets at 390 px with nothing opened** (the coordinator measures every round):

| Screen | Words | Controls | Length |
|---|---|---|---|
| Home | 60 | 8 | 1 screen |
| Schedule | 80 | 10 | 1.3 screens |
| Appearance | 60 | 16 | 1.5 screens |
| Task | 60 | 4 | 1.2 screens |
| Approval | 70 | 4 | 1 screen |
| Every other screen | 60 | 10 | 1.2 screens |

A folded row (`<details class="fold">` whose summary is one line: a mark, a title and the meta) counts as a list item, not as a control.

**The approval keeps its substance in fewer words.** Shown: the question, the three chips, the recommendation in one
line, and "Yes: ..." / "No: ..." in one line each, then the buttons. Folded: "Why am I asked?" and the details.

## Glance: visual at a glance, inside the lightness (operator, 2026-11-18)

The operator on UI-PROTO-6: "the premium icons were removed radically; it looks simpler, but best in class does
both, visual engagement and state-of-the-art UX with everything at a glance; now parts are only text."

Lightness stays: the word, control and length targets still hold. What carries information moves from words into
shape, colour and icons. Pi Pocket does the same: a session's square takes its folder's colour, and its shape says
the state.

1. **State glyphs instead of plain dots.**
   - A small square in the project's colour; its shape says the state:
     - working: quadrants stepping;
     - waiting for you: two bars;
     - failed: a cross;
     - finished and unseen: solid;
     - finished and seen: hollow;
     - scheduled: a ring (once) or a diamond (repeats).
   - Every list, Home, Tasks, Schedule and Search use the same glyph (`data-glyph`), with the text state in its
     aria-label.
2. **A leading icon per row for what it is:**
   - task kinds from the template (bug, docs, package, check, link);
   - routine (repeat), signature (pen), approval (shield);
   - provider and model (chip), setting (tinted square, as in iOS Settings).
   One stroke icon set, one sprite.
3. **Glance tiles on Home** (`data-glance`):
   - 3 or 4 compact tiles above the lists, each with an icon, a large number and a two-word label;
   - for example "3 need you", "2 running", "next in 13 h", and today's spend as a small ring against the budget;
   - each tile opens its list.
4. **Inline visuals instead of second lines:**
   - running rows: a thin progress line under the title and a lane chip (a monogram in the lane's colour, e.g.
     Q for local_qwen, C for claude_code; `data-lane-chip`);
   - Next up: the time as a small clock badge.
5. **Schedule: a mini timeline is visible by default** (`data-mini-timeline`):
   - one slim strip of the next 24 h with the marks and a now tick, above Next up;
   - the full timeline stays folded.
6. **Task detail rows and approval chips get icons:**
   - task rows: progress, pen, diff, test flask, receipt, retry;
   - approval chips: matters (clock), risk (shield) and undo (arrow), and the recommendation shows a check or an
     alert mark.
7. **Menu and settings look like Pi Pocket's:**
   - the menu as a grid of rounded tiles, each with an icon and a count badge;
   - settings as grouped inset rows, each with an icon in a tinted square.
8. **Empty states** get a quiet icon and four words, never a blank area.
9. **Motion with meaning only:**
   - the working glyph steps, and a new item fades in;
   - all motion holds still under reduced motion.

## Appearance: flexibility first (operator, 2026-11-18)

The operator's requirement is flexibility, not a copy of Pi Pocket: "Pi Pocket lets you adjust design parameters
individually." The fleet UI is built on a documented **design-token surface**, and every visual decision is a
token, never a literal in a component. Pi Pocket's model, which we follow, has three layers, chosen per browser:

1. **Theme**: the look and the layout. It is a folder `<fleet.root>/themes/<id>/` holding `theme.json`
   (`themeApi`, `name`, `description`, `author`, `version`) and `theme.css` that sets tokens and may restyle parts.
   - Fonts and images are allowed; **no code**.
   - Built in: one calm default and one more. The operator adds his own.
2. **Colors**: a palette, separate from the theme, so a layout keeps when colors change.
   - Format: Omarchy's `colors.toml` (`background`, `foreground`, `accent`, `red`, `green`, `yellow`, ...,
     `mode = "light"`), so any Omarchy theme folder works as it is.
   - A theme may bring a dark/light pair that follows the system.
3. **Personal settings**, per browser and saved there. Each one changes tokens live, with no reload:
   - text size;
   - density (compact, regular, comfortable: `--space-x`, `--space-y`);
   - corners (square to round: the `--r-*` scale);
   - tiling on desktop (gaps, rings) on or off;
   - motion (system, reduced, fluid);
   - fonts (`--sans`, `--mono` from the bundled set);
   - accent override;
   - layout (see Principle 3);
   - "Use these for new browsers", and Reset.

**Currency is a setting too** (operator, 2026-11-18). Budgets and costs are stored in the currency the providers
bill in, US dollars: one source of truth, as the ledger does today.

- Settings, Region, Currency chooses how amounts show:
  - any ISO currency, listed from the browser (`Intl.supportedValuesOf("currency")`) and formatted with
    `Intl.NumberFormat` in the person's locale;
  - or **sats**, for a Bitcoin-native setup.
- **The conversion rate:**
  - entered by the operator (a fixed rate with its date shown);
  - optionally fetched from a source he names, for example a self-hosted price feed. The fleet ships no default
    source, so nothing phones home.
- Without a rate, amounts stay in dollars and say so.
- The package ships no country's currency as a default (`test_the_package_ships_no_currency_or_host_specifics`).
  Codes come from the browser, not from a list in the code.

Owner actions:
- **Duplicate** a theme or palette into a folder of his own.
- **Edit with the fleet**: queue a fleet task scoped to that theme folder, the way Pi Pocket's "Edit with Pi" works.

**The Appearance sheet, after Pi Pocket's** (operator: "copy best practice"):
- **Theme cards:**
  - name, a quiet tag (`built in`, `own colors`) and one sentence on what the theme is for;
  - the chosen card has an accent ring;
  - **Duplicate** under them ("to make a theme of your own").
- **Fonts for the chosen theme:**
  - text font, code font and ligatures (Off, Contextual, Normal);
  - a live preview line under them, a sentence with *emphasis* and one code line with a comment;
  - a built-in theme's fonts are changed through **Duplicate to change**.
- **Colors as mini-desktop cards:** each card is a tiny window pair drawn in that palette's own colors, with its
  name and colour dots.
  - Hover or keyboard focus previews the palette live; leaving without choosing restores the old one.
  - The bundled palettes are Omarchy's (Catppuccin, Everforest, Gruvbox, Tokyo Night, Rose Pine, Nord, Kanagawa,
    the light ones), with their `colors.toml` as they are and the licence noted.
  - Any Omarchy theme folder dropped into `themes/` shows here.
- **Under them:**
  - text size, density, corners, tiling, motion, accent override;
  - "Use these for new browsers" and Reset.

The token surface is documented in `docs/THEMES.md` (to write with the real build), in the spirit of Pi Pocket's
`theme-surface.md`:
- palette `--o-*`;
- surfaces `--bg --panel --card --card-2 --scrim`;
- lines `--hair --line`;
- text `--text --body --muted --faint`;
- accent `--accent --accent-soft --accent-ink --focus-ring`;
- status `--ok --warn --err` with `-soft`;
- type `--sans --mono --text-size`;
- corners `--r-control --r-card --r-sheet --r-pill`;
- space `--space-x --space-y`;
- shadows `--shadow --pop`;
- motion `--ease-out --t-in --t-out`.

Plugins and extensions use the same tokens, so a theme reaches them too.

## Information architecture

| Place | Modules (default order) |
|---|---|
| Home | Needs you (approvals, signature requests, failed tasks), Running now, Recent results |
| Bottom bar (phone) / rail (desktop) | Home, Tasks, New (+), Search, Menu |
| Menu (tiles) | Tasks, Routines, Templates, Models, Budgets & spend, Issue sources, Privateers, Coordinator |
| Settings (rows) | Connect phone (signer), Autonomy & approvals, Lanes & health, Capabilities, Appearance, Layout, Access (token, devices), About |
| Task detail | Status & timeline, Approve/reject, Drafts awaiting signature, Changes (diff), Gate output, Receipt, Attempts |

The KPI strip ("Agent success rate" and the rest) moves into Models or Budgets & spend; Home shows a
single health line ("All good" or "2 lanes resting").

## Key flows the prototype must show

1. **Approve from the phone.**
   1. A push opens Home with the item on top.
   2. The approval sheet shows what will run, where, and what it may reach.
   3. The operator taps Approve or Reject.
   4. For signed approvals the sheet says "Approve in Amber" and opens Amber.
2. **New task from a template.**
   1. Tap +, pick a template (chips), fill the one or two blanks, send.
   2. Advanced (lane, harness, paths, gate) is folded but present.
3. **Connect phone** (Settings, Connect phone). Three steps, automatic where possible:
   1. *On the phone:* one button, "Open Amber", which is a `nostrconnect://` link that hands over to
      Amber on the same device.
      *On a computer:* the QR code. The prototype shows both and marks which device each is for.
   2. "Approve in Amber". The screen waits by itself, shows the short key (`npub1abc...xyz`) and asks
      "Does Amber show the same?".
   3. "Confirm once on the computer": one short command with a copy button. It is needed because an
      agent on this computer must never be able to swap the key. A line under Details says why.
   4. Done screen with "Send a test approval".
   - The bunker link is under Advanced.
4. **Search** (Ctrl/Cmd+K, or the search tab). One field for tasks, templates, modules and settings,
   with fuzzy match and grouped results.

## Approval brief: decide in ten seconds (operator, 2026-11-18)

The operator is not a coder. To approve or reject competently he needs, with every request:
- why he is asked;
- what follows from yes and from no;
- how much the decision matters.

The pattern follows decision memos (bottom line first), change-advisory practice (impact, risk, rollback) and
permission prompts (purpose stated with the request).

**The card, top to bottom:**
1. **The question in one plain sentence**, a verb and an object.
   Example: "May the fleet fix the broken link check in the blog and keep it on a branch?"
2. **Three chips:**
   - **Matters:**
     - "Blocks 2 tasks";
     - "Routine misses its Monday run";
     - "Can wait";
     - "Expires in 2 h".
   - **Risk:** low, medium or high.
   - **Undo:**
     - "Reversible: only a branch";
     - "Not reversible: publishes" (signed, sent, pushed or paid).
3. **The fleet's recommendation:**
   - **Approve**, **Look closely** or **Your call**, with one reason.
   - Example: "Approve: low risk, nothing leaves this machine, the test proves it."
4. **Why you are asked:** the rule that held it, in words.
   - "It came through OpenClaw; tasks from there always wait for you."
   - "It wants to reach example.com."
   - "It may cost up to $0.40."
   - "Its text mentions deleting files."
5. **If you approve:** concrete consequences.
   - Which project and how many files.
   - Which model runs it, and the cost (free / estimate).
   - Where the result lands: a branch, a pull request, or main.
   - Whether anything leaves the machine.
   - How to undo it.
6. **If you reject:** what happens instead.
   - "Nothing changes; OpenClaw is told."
   - "The two tasks after it are skipped."
   - "The routine skips this week."
7. **Buttons:** Approve, Reject, Later (snooze until a time). Signed approvals say "Approve in Amber".
8. **Details, folded:** the full prompt, the files, the diff so far, the lane, the technical reason.

**Facts come from the daemon; a model only summarises.** Today's risk check (`core/risk.py`) rates the task *text*
with a model. A text that calls itself harmless can talk it down, and it says nothing about consequences.

- **The daemon builds the brief from facts** (`approval_brief(task)`):
  - the source and the rule that held it;
  - project, allowed paths and landing mode;
  - allowed domains and capabilities;
  - the lane and its cost from the budget data;
  - the tasks that depend on it;
  - any expiry.
- **Risk, undo and the recommendation are rules over those facts:**
  - a branch-only landing with no network and no cost is low risk and reversible;
  - publish, push, payment or a signature is irreversible and "look closely";
  - a model cannot change them.
- **The model writes only the question sentence and a one-line summary,** marked as a summary. The task text is
  given to it as data, never as instructions.
- **Every surface shows the same brief:**
  - the dashboard sheet;
  - the MCP App card in goose;
  - the Matrix message;
  - the push notification, which carries the headline and the chips.

## Running and scheduled (operator, 2026-11-18)

The operator wants a view like Pi Pocket's "Running now" for managing routines. It is one module
(`schedule-board`, on Home or in the Menu as the layout says), filled from data the daemon already has:
- **Coming up:** a timeline with a 24h / 3d / 7d switch and a "now" line.
  - Marks: routine runs from `next_runs`; a ring for once, a diamond for repeats.
  - Held: routines waiting for approval, or with a pending change.
  - Missed: slots past `max_lateness`, from routine history. The fleet has these and Pi Pocket does not.
  - Tasks waiting on `depends_on`.
  - Bars for what is at work, with lane and runtime.
- **Filters with counts:** All, Running, Scheduled, Needs approval, Held, Paused.
- **Table:** task or routine, what it is doing now (lane, step, runtime), next run or schedule. A row opens the
  routine's sheet: schedule, the next 5 runs, history, and Pause / Resume / Approve with review id.
- Optional: the day's spend against the budget as a thin line under the timeline.
- **Revision after the operator's review (2026-11-18: "I still do not get the schedule intuitively").** Compared with
  Pi Pocket's Running now it lacked an hour ruler, what-and-when on a mark, the repeat in words, Needs you / Next
  up beside it, and a detail sheet. Its "Now 14:20" also contradicted an axis starting at 11:56. Plain words come
  first, the picture second:
  1. **Needs you:** held routines and ones waiting for approval, in one sentence each, with the button.
  2. **Next up:** the next 3 to 5 runs as sentences: name, the time in his zone, how far ahead ("in 13 h"), and
     the repeat in words ("every day at 03:30").
  3. **The timeline:**
     - an hour ruler on top, with ticks every 3 h (24h), 12 h (3d) or 1 day (7d);
     - "Now 14:20" on the line; the window starts a little before now, so recent misses show;
     - it scrolls sideways on a phone;
     - each mark is a button whose label says what and when;
     - tapping one opens the routine sheet: the repeat in words, the next 5 runs, how often it ran, who set it
       up, and Pause / Resume / Cancel;
     - an empty row says "No run in this window".
  4. **The table:** a routine, what it is doing now, and the next run with the repeat in words.
- Search: "Find a task, routine or schedule".

## Coordinator chat (operator, 2026-11-18)

Modelled on Pi Pocket's PiP: an assistant over every screen. On desktop it is a floating window bottom right; on a
phone it is a bottom sheet. Alt+A or a rail button opens it. It does in the app what a coordinator session does
today, so the operator can steer the fleet without one. It replaces today's Coordinator panel.

- **Answers:**
  - "What needs me?";
  - "Why did task X fail?", from the receipt, the autopsy and the gate output;
  - "What runs tonight?", from routines and `next_runs`.
- **Proposes:**
  - a task spec from plain words (project, allowed paths, gate, criteria), shown as a card;
  - a routine;
  - a retry with a better spec;
  - a watch on a repository or issue source.
- **Never acts by itself.** It reads untrusted text (issue bodies, agent output, web pages), so it holds front-door
  rights: dispatch only with `require_approval`, no approve, reject, cancel, budget or signer actions. Every state
  change is a confirm card the operator taps; signed approvals stay on the phone. This is the front-door rule
  (`mcp_server.py --mode front_door`), applied to the in-app chat.
- **Model:** local by default (`local_qwen`: free, private), switchable per conversation like PiP. It shows the
  context fill and the cost.
- **Conversation:** kept per operator, short. Durable facts go to the knowledge base, not into chat memory.

## Providers and models (operator, 2026-11-18)

Modelled on Pi Pocket's Providers sheet (Settings, Providers). Today the same facts sit in `fleet.toml`, in
secrets, and on the Models and Lane health panels. They go into one sheet with two columns (on phones, two tabs).

- **Providers:**
  - Search, then "Signed in" and "Not configured" groups.
  - Per provider: the model count, health (from lane health), and "resting until 14:00" when a quota stopped it.
  - Actions: **API key** (enter, replace, remove) and a subscription sign-in where the harness has one.
  - A key is written by the daemon into the secrets directory (0600) and never shown or sent back. The UI only
    learns "set" or "not set". Operator only; never from the front door or the coordinator chat.
- **Models:**
  - Search, then the filters All, Allowed and Favorites; grouped by provider; a row is a model with its harness
    chips.
  - **Checkbox, allowed:** the router may pick it. It is per installation, and per project a project can narrow it
    (private repos: local only).
  - **Star, favorite:** shown first in New task's picker. It grants nothing.
  - **"For hard cases":** the frontier list the escalation uses after repeated failures (`escalate_to`).
  - The router's pass rate per category and attempts show beside each model. Pi Pocket has no such data.
- **When a limit hits** (segmented control):
  - switch lane at once (today's failover);
  - wait up to 1, 6, 12 or 24 h, then go on by itself;
  - wait, then switch.
  - A per-task override is possible for work only one model does well.

## Prototype scope (fleet task UI-PROTO-1)

- One self-contained file: `src/fleet/api/static/preview/index.html`, served at
  `/static/preview/index.html`.
- Inline CSS and JS. No external requests, no `fetch`, no build step. Sample data only, with a banner
  "Preview with sample data".
- Every screen is a `<section data-screen="...">`. Every module carries `data-module="..."` with the
  ids in the gate test. Navigation between screens happens in JS (hash routing is fine).
- Appearance shows the flexibility live: at least two themes (layouts), at least four palettes (dark and
  light, one pair following the system), and the personal settings above. Every change re-renders at once through
  tokens, and the choice is kept in `localStorage` with try/catch, so it survives a reload. Themes and palettes
  are plain data objects (token to value) in the page, the shape `theme.css` and `colors.toml` take later.
  "Duplicate" and "Edit with the fleet" are present as sample actions.
- No emoji anywhere. Icons are one inline SVG sprite.
- It works at 360 px width with no horizontal scroll, with tap targets of at least 44 px. Desktop at
  1280 px shows the list beside the detail.
- It is accessible:
  - `button` elements for actions;
  - labels on inputs;
  - visible focus;
  - `aria-live` for "waiting for Amber".

The gate is `tests/test_ui_preview.py`, written by the coordinator. The agent does not edit it.

## Later (not in the prototype)

- the real shell, with the module registry fed by `/api/fleet/ui/modules`;
- plugin-declared modules;
- template files and their API;
- the routines-from-templates link;
- the `fleet-pin` wrapper;
- the "Send test approval" endpoint;
- web push.

Each is its own task once the operator has picked a look.


## The real shell, slice 1 (operator 2026-11-18: "build it", lane antigravity_gemini)

The approved prototype becomes the real UI next to the old dashboard, slice by slice; the old one stays at `/`
until the operator switches.

- **Where:** `src/fleet/api/static/app/index.html`, served at `/static/app/index.html`, one self-contained file
  like the prototype. It starts as a copy of the prototype's look: tokens, appearance, glyphs, icons, lightness.
- **Sign-in:**
  - It uses the dashboard's session cookie. A 401 shows a sign-in screen with a token field.
  - A `#token=...` fragment (the `fleet-token` link) is exchanged once through `POST /api/auth/session` and removed
    from the URL with `history.replaceState`.
  - The token is never written to `localStorage`.
- **Slice 1, real data:**
  - **Home:** glance tiles, Needs you, Running now, Recent.
    - From `GET /api/fleet/tasks` (polled with `If-None-Match`, about 5 s while visible, paused when hidden).
    - Spend from `GET /api/fleet/budgets`; the next run from `GET /api/fleet/routines`.
  - **Tasks:** a list with filters.
  - **Task detail:** the fold rows, from the task, `GET .../receipt` and `result`.
  - **Approval sheet:**
    - It renders `task.brief` (below). Without a brief it shows the task text and the plain Approve / Reject.
    - Approve, Reject, Cancel and Retry call the existing endpoints, only after a tap, and show the result.
  - Screens not in slice 1 (schedule, settings, providers, appearance stays local) are visible as rows. They open
    the old dashboard until their slice lands.
- **Untrusted text:**
  - Task text, issue bodies and agent output are rendered with `textContent` only; there is no `innerHTML` with
    data.
  - Links in data are never made clickable automatically.

**`task.brief`** (daemon, `core/brief.py`, written by the coordinator; present on tasks awaiting approval):

```json
{ "question": "May the fleet ...?",
  "summary": "one line, model-written, marked as such",
  "matters": {"label": "Blocks 2 tasks", "level": "high|normal|low"},
  "risk": {"level": "low|medium|high", "reasons": ["..."]},
  "undo": {"reversible": true, "label": "Branch only"},
  "recommendation": {"action": "approve|look_closely|your_call", "reason": "..."},
  "why": ["It came in through OpenClaw; tasks from there always wait for you."],
  "if_approve": ["..."], "if_reject": ["..."],
  "facts": {"project": "", "files": [], "lane": "", "cost": "", "landing": "", "network": []} }
```

## The real shell, slices 2 and 3 (built 2026-11-18, coordinator in-session)

Every screen of the shell now reads the daemon; no sample data is left in `static/app/index.html` (a gate checks it).
Each action calls an existing endpoint after a tap, and the daemon checks the scope; the page decides nothing.

- **Schedule** (`GET /api/fleet/routines`):
  - Routines map to the board's states: `awaiting_approval` is *needs approval*, a `pending_change` is *held*, an
    open run is *running*, `paused`, else *scheduled*.
  - Next runs come from `replay_next_runs`. Misses from the last two days come from `history`.
  - The routine sheet says the repeat in words (cron and interval turned into a sentence; an unknown cron shows the
    expression) and lists the next 5 runs, the history and the prompt.
  - Approve / Reject (sending the `review_id` of the version shown), Run now, Pause / Resume and Remove. Removing
    and rejecting need a second tap.
- **Providers and models** (`GET /api/fleet/budgets`, `/lanes`, `/models`):
  - Each provider shows its lanes, its health and its policy. A CLI lane with its own sign-in is not probed, so it
    counts as usable.
  - The provider sheet sets the policy (`PUT /api/fleet/budgets/{provider}`). The policy names come from the API,
    not from a list in the page.
  - Lanes switch on and off (`POST /api/fleet/roster/toggle`). **Look again** runs roster discovery.
  - Keys are never typed into the page: they are set on the computer.
  - The Models tab lists the catalogue with a harness-compatibility fold.
  - The "when a limit is reached" control is out until the daemon has it (FLEET-LANE-POLICY).
- **Settings sheets:**
  - **Autonomy** (`POST /api/fleet/settings` with `autonomy_level`).
  - **Lanes and health**, with a check-now button.
  - **Coordinator** (`POST /api/fleet/coordinator`).
  - **Issue sources** (pause, check now).
  - **Privateers** (list).
  - **Budgets.**
  - **About** (version, a link to the old dashboard).
  - **Access:** the tokens with their scope and last use. Create a token: it is shown once, with Copy token and Copy
    sign-in link, and kept nowhere in the page. Remove takes a second tap.
- **Connect phone** (`/api/fleet/signer*`):
  - **Step 1:** `POST /signer/pair` gives the `nostrconnect://` link behind **Open Amber** (the computer gets the
    daemon's QR code as an `<img>` data URI, never as markup). The page polls until the phone answers. A pasted
    bunker link goes to `POST /signer/bunker`.
  - **Step 2:** check the key.
  - **Step 3:** the `pin_command` to run once with sudo; the page polls until the anchor is pinned.
  - **Step 4:** Done, with Disconnect (two taps).
- **New task:**
  - The fields are project (from `/api/fleet/projects`, the last one remembered), a template, and Advanced: lane
    and harness, both from the daemon's catalogue.
  - `POST /api/fleet/tasks` with an `Idempotency-Key`.
- **Task detail folds**, from the task record:
  - Progress: queued, each try with lane and time, landing, and the agent's note folded.
  - Changes: files, branch, commit.
  - Test: the last 15 output lines, folded.
  - Attempts, and a link to the receipt.
  - Drafts awaiting a signature (`GET .../drafts`): **Send to Amber** or Reject.
  - Your own rejection shows as *rejected*, not *failed*, and does not count as "needs you".
- **Hidden until there is a daemon side:** the coordinator chat, Capabilities, and theme duplication.
- **Checked:**
  - `tests/test_ui_app.py` (16 tests).
  - The overflow sweep at 360 and 390 px (13 screens x 5 palettes x 3 text sizes, all folds open): 0 overflows.
  - Lighthouse mobile in navigation mode: accessibility, SEO and agentic browsing 100; best practices 96 only
    because the read-only review token gets a 403 on `/tokens`.

### Templates as files

Templates live as Markdown files instead of a fixed list in the shell (`core/templates.py`), in three override levels keyed by `id` (the file stem, matching `[a-z0-9-]{1,64}`):

1. Built-in defaults shipped as package data in `src/fleet/templates/*.md` (`built-in`).
2. Operator templates in `<fleet root>/templates/*.md` (`global`).
3. Per-project templates in `<project path>/.fleet/templates/*.md` (`project`).

Later levels override earlier ones with the same `id`. Each file starts with a `---` front-matter block (`name: ...`, `description: ...`, and one `blank.<id>: <label> | <placeholder>` line per blank, matching `[a-z][a-z0-9_]{0,31}`), followed by the body with `{<id>}` placeholders.

Safety limits: files over 16 KiB, directories beyond 100 `.md` files, symlinks, paths that resolve outside their directory, and invalid front matter are skipped with a logged warning. Template bodies are treated as untrusted text (rendered via `textContent` only). `GET /api/fleet/templates?project=<key>` (`read` scope) serves them, the MCP server exposes them via `prompts/list` and `prompts/get` in both modes without queueing a task, and the live shell loads `/api/fleet/templates` for the selected project and displays each template's `scope` (`built-in`, `global`, `project`) in the Templates sheet.

**Parity, before the old dashboard goes** (on the operator's word): Privateer create and commission, the ledger
view, the plugin list, a routine create form (today: CLI / MCP / coordinator), the archive actions, and voice
transcription. Until then the About sheet links to `/`.

