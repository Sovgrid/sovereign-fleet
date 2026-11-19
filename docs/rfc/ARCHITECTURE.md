# RFC: fleet architecture, a headless core with open-standard surfaces (FLEET-ARCH)

Status: decided by the operator on 2026-11-18 ("ok, double-check, optimise, record the plan"). This page records the
decision, the reasons, and the plan with exit criteria. `UI-REDESIGN.md` is the UI part of it.

## The question

The dashboard had become one ~8000-line page, and phone-signer setup needed a coordinator at every step. Three ways
were weighed on five criteria: long-term performance, robustness, security, independence, and fit with the goose /
Buzz / Spiral grant (pitch: "sovereign-fleet as a safe, long-running background mode for goose").

| | A: own large UI | B: Pi Pocket as the primary UI | **C: headless core, open-standard surfaces** |
|---|---|---|---|
| Independence | yes; we rebuild everything | no: experimental Pi Pocket / Pi Durable APIs | **yes: no single front end carries load** |
| Robustness | one large page to maintain | an extra Node service; upstream changes break us | **one surface failing leaves the others** |
| Security | good | a shell-as-you agent host next to the security core | **best: the daemon alone decides; each surface gets only its scope** |
| Performance | fine | fine | fine; live events instead of polling |
| Grant fit | neutral | weakens it (Pi is not Block) | **strengthens it: the fleet runs *inside* goose and Buzz** |

**Decision: C.** Pi Pocket stays the reference for UX (themes, appearance, schedule board, PiP, providers) and can
become an optional adapter.

## Principles

1. **One authority.** The daemon owns state and policy:
   - tasks, routines, approvals, signatures, budgets, routing, sandbox, gates, landing.
   - No surface decides anything. A surface asks, the daemon checks.
2. **Least privilege per surface.** Every client has its own revocable token with a scope; nobody shares the
   operator's token.
3. **Open standards at the edges:**
   - HTTP + OpenAPI for programs;
   - MCP (tools, prompts, resources) for agents;
   - MCP Apps for interactive UI inside agent hosts;
   - Nostr (NIP-46, NIP-34) for identity, signatures and code collaboration.
4. **Write a UI part once, render it everywhere.** The same component (an approval card, a task card, the schedule
   board) is an MCP App resource. Goose, Claude and VS Code render it, and so does our own dashboard, which is itself
   an MCP Apps host.
5. **Every visual is a token.** Themes, palettes and personal settings change tokens, never components (see
   `UI-REDESIGN.md`).
6. **Durability before features.** Write-ahead intent, idempotent requests, attempt records (FLEET-DURABILITY).

## Layers

```
 Surfaces (replaceable)
   own dashboard (PWA)   goose / Claude / VS Code   Buzz   OpenClaw / Matrix   Pi Pocket (optional)
        |  MCP Apps host       |  MCP Apps + tools     |  plugin  |  front door     |  extension
        v                      v                       v          v                 v
 Contracts (stable, versioned)
   /api/v1 (OpenAPI)  +  /api/v1/events (SSE, resumable)  +  MCP server (tools, prompts, resources, apps)
        |
 Authorization:  scoped tokens  ->  step-up for high risk (the operator's phone signature)
        |
 Core (one process, the only writer)
   tasks · routines · approvals · signer · budgets · router · engine (sandbox, gates, landing) · receipts
   state: SQLite (WAL) + receipts + hash-chained call logs
```

## Three improvements found in the second pass

1. **An MCP App can show an approval, but it never grants one by itself.**
   - Why: in an MCP Apps host the UI calls tools through the same host as the model. A model talked round by an
     issue body or a web page could press the button too.
   - So host-rendered cards are read-and-route only: "Approve in Amber" (the phone signature) or "Open in fleet"
     (the dashboard with operator scope).
   - The daemon refuses an approve call from any non-operator scope. This also closes the front-door gap
     (FLEET-FRONT-DOOR-AUTHZ).
2. **One event log, not polling.**
   - `/api/v1/events` is server-sent events with event ids and `Last-Event-ID` resume, read from an append-only log.
   - The same log serves:
     - the dashboard's live view;
     - adapters (Buzz, Matrix, push);
     - the coordinator chat's "what changed";
     - the audit.
   - Today's 2-60 s polling and the ETag workaround go away.
3. **Plugin UI is sandboxed by design.**
   - A plugin's view is an MCP App resource, rendered in a sandboxed iframe that talks to the host only through
     `postMessage`.
   - It gets no operator token and no DOM access to the dashboard.
   - Plugins add views without being able to act as the operator. This is safer than Pi Pocket's model, where
     extension code runs in the page.

Further alignment with the standards:
- **Templates are MCP prompts.** `.fleet/templates/*.md` (per project and global) appear as slash commands in
  goose and other hosts, and as one-tap starts in the dashboard.
- **Routines and receipts are MCP resources,** so any host can read them.

## Scopes (FLEET-FRONT-DOOR-AUTHZ)

| Scope | May | Typical holder |
|---|---|---|
| `read` | list and read tasks, receipts, routines, health, events | status widgets, push relay |
| `propose` | `read`, plus dispatch with `require_approval`, propose routines, read templates | OpenClaw, Buzz agents, the coordinator chat, MCP hosts, Pi Pocket |
| `operate` | `propose`, plus approve or reject plain tasks, cancel, retry, pause or resume routines | the operator's dashboard session |
| `admin` | `operate`, plus provider keys, budgets, autonomy tier, signer pairing | the operator's dashboard session, CLI on the host |
| step-up | a signed operator event (kind 4910 / 4911) | needed for drafts, Privateers, and whatever the policy marks as high risk |

Token handling:
- Tokens are stored hashed. Each one has a name, a scope, an expiry and a last-used time, and can be revoked in
  Settings, Access.
- Today's single token becomes `admin` for the host CLI only; the dashboard signs in to a session cookie with
  `operate`/`admin`.

## Plan with exit criteria

| Phase | What | Done when | Who |
|---|---|---|---|
| 0 | Record this decision; finish the UI prototype (UI-PROTO-4: review findings, mini-desktop palette cards, font preview, schedule board, providers sheet, coordinator chat as sample) | the operator approves the look on his phone | fleet builds, coordinator reviews |
| 1 | Scoped tokens + step-up rule in the daemon; OpenClaw and the MCP server move to `propose` | a `propose` token gets 403 on approve/cancel/budget; hostile tests pass; operator flows unchanged | coordinator writes the core (security code), fleet does wiring and docs |
| 2 | `/api/v1` with generated OpenAPI, contract tests; `/api/v1/events` SSE with resume; `approval_brief(task)` from facts (UI-REDESIGN, Approval brief) on every waiting task | the dashboard and one adapter run on v1 and events only, with no polling | fleet, coordinator-written contract tests |
| 3 | MCP Apps spike: approval card and task card rendered in goose desktop on this machine; templates as MCP prompts | the operator sees a task card in goose and opens Amber from it; the card cannot approve by itself (test) | fleet, coordinator review |
| 4 | Slim dashboard shell on the token system (from the prototype): module registry, MCP Apps host for its own and plugin views, templates, schedule board, providers sheet, appearance | parity with today's dashboard, which is then removed; the operator sets up phone signing alone from a fresh state | fleet, coordinator review per module |
| 5 | Adapters: Buzz plugin, Pi Pocket extension (optional), Matrix/OpenClaw on `propose` | each adapter runs on its own token; removing it changes nothing else | fleet |

### Phase 2 status: `events` done (FLEET-EVENTS-1); the rest of `/api/v1` stays open

- Done (`src/fleet/core/events.py`, `GET /api/v1/events`, `tests/test_events.py`): one append-only log,
  `<fleet root>/events/events.jsonl`.
- Record: `{id, at, type, task_id?, routine_id?, lane?, data}`.
  - `id` is an integer that strictly increases, across restarts and rotations (the last id is kept in the sidecar
    `last_id`).
  - `type` is `task.<status>` (queued, in_progress, awaiting_approval, completed, failed, cancelled, skipped),
    `task.approved`, `task.rejected`, `routine.<created|approved|rejected|paused|resumed|run|deleted>`,
    `lane.blocked`, `lane.unblocked`.
  - `data` holds only `id`, `status`, `title` (at most 120 characters, from `intent.title`), `lane`, `category`.
    Task text, prompt, result, gate output, tokens and file paths never enter it.
- Writes are flock-serialised; the file rotates at 20 MB and the last two files are kept. A failed write is logged
  and never fails the action.
- Stream: SSE with `id:`, `event:` and `data:` lines. A connection without a position gets only new events; resume
  with the `Last-Event-ID` header or `?since=<id>` (the header wins). A `: ping` comment every 15 s; the stream ends
  after 30 min and the client reconnects. At most 4 open streams per token or session, the fifth gets 429.
  `?format=json&since=<id>` returns `{events, last_id}` (at most 500) for polling clients. Scope `read`.
- The new shell uses it: task events call `refresh()`, routine events `refreshRoutines()`, lane events
  `refreshSettings()`; polling is 60 s while the stream is open and 5 s otherwise.
- Not yet: the grid backend emits no approve or reject event; adapters and the dashboard still poll as a fallback;
  the OpenAPI generation and contract tests of the other `/api/v1` routes are open.

### Phase 3 status: MCP Apps verified live in Goose Desktop `1.46.0` (2026-11-19)

- Built (`src/fleet/api/mcp_server.py`, `src/fleet/api/mcp_apps/`): `resources/list` and `resources/read` serve
  `ui://fleet/task-card` and `ui://fleet/approval-card` as `text/html;profile=mcp-app`. The tools `fleet_show_task`
  and `fleet_show_approval` (both modes, read-only, through the daemon API with the server's token) name a card in
  `_meta.ui.resourceUri` and return `structuredContent`. The cards speak the host bridge (`ui/initialize`, then
  `ui/notifications/tool-result` and `ui/notifications/size-changed` so hosts expand past the default 200 px height),
  write text only through `textContent` and load nothing from the network.
- Read-only by design: the cards have no approve, reject, cancel or retry control and never send `tools/call`.
  Their links are "Open in fleet" (`<base>/static/app/#/t/<task_id>` when `task_id` matches the safe id pattern) and,
  only when the daemon puts a `nostrsigner:` link in `brief.links.amber`, "Approve in Amber". For plain tasks the daemon
  intentionally omits `brief.links.amber` (kind 4910 phone approvals bind a daemon-held pending draft and nonce), so
  the card offers "Open in fleet" only. `tests/test_mcp_apps.py` holds these properties.
- Live Goose verification (2026-11-19, Goose Desktop `1.46.0` / Electron `43.3.0`, Goose CLI `1.60.1`):
  - Configured Goose (`~/.config/goose/config.yaml`) with `python3 -m fleet.api.mcp_server --mode front_door` and a
    dedicated `propose`-scoped token (`goose`). Direct HTTP `POST .../approve` with that token returned `403`, and
    `tools/list` exposed only the 11 front-door tools.
  - Both `ui://fleet/approval-card` (`fleet_show_approval`) and `ui://fleet/task-card` (`fleet_show_task`) rendered live
    inside Goose Desktop's sandboxed `/mcp-app-proxy` + `/mcp-app-guest` iframes after the host sent
    `ui/notifications/tool-result`, showing the rendered brief/task facts, deep-linked `Open in fleet` button
    (`https://.../static/app/#/t/<task_id>`), and zero interactive buttons/forms.
  - Initial 200 px iframe clipping observed in Goose Desktop `1.46.0` was resolved by emitting
    `ui/notifications/size-changed` from both cards.

FLEET-DURABILITY steps 3 to 6 run alongside phases 1 and 2; they share the event log and attempt records.

## Risks

- **MCP Apps in goose is experimental** (draft spec, minimal implementation). Phase 3 is a spike first, and the
  dashboard is the fallback host.
- **More moving parts:** tokens and events are new contracts. Contract tests, version them under `/api/v1`, and keep
  the old endpoints until every surface has moved.
- **UI taste is not testable:** every UI step is screenshot-reviewed at phone and desktop width before the operator
  sees it (coordinator playbook).

## Sources

- MCP Apps, official extension (2026-01-26): <https://blog.modelcontextprotocol.io/posts/2026-01-26-mcp-apps/>
- goose and MCP Apps: <https://goose-docs.ai/blog/2026/01/06/mcp-apps>, tutorial
  <https://goose-docs.ai/docs/tutorials/building-mcp-apps>
- Pi Pocket extension API (routes, browser modules, tools, events): `docs/rfc/ui-reference/pi-pocket/` and
  <https://github.com/TannerMidd/pi-pocket/tree/main/docs/extensions>
- Grant: the goose grant program, now administered by Spiral: <https://goose-docs.ai/grants/>
