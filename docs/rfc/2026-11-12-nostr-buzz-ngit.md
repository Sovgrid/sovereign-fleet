# RFC: the fleet on Nostr (Buzz and ngit)

Status: draft, 2026-11-12. Nothing here is built yet; step S0 decides the open questions.

## Why

Today the fleet takes work from GitHub and Gitea and lands it there. Two Nostr-based places for code
work are now usable, and both speak the same git protocol, NIP-34:

- **ngit** (git over Nostr): repositories are announced as events, patches and pull requests are
  signed events, `nostr://` remotes push through `git-remote-nostr`.
- **Buzz** (Block, Apache-2.0, self-hostable): a workspace on a Nostr relay where humans and agents
  are members with their own keypairs; every message, approval and git event is a signed event in one
  log, and patches land as NIP-34 events. Its agents run in harnesses (goose, Codex, Claude Code, Pi)
  on the user's machine, without a sandbox, gates or a landing check.

The two fit together: Buzz is where work is asked for, discussed and approved; the fleet is where it
runs, confined by Landlock and verified by gates, before anything lands. Nostr adds what the fleet
lacks today: an identity of its own, so every intake reply, receipt and patch is signed by the fleet
and checkable by anyone, on any relay.

## Principles

- One protocol, two places: build against NIP-34 and NIP-01, not against one product, so ngit and Buzz
  are served by the same code.
- No new crypto in Python. Sign, publish and query through existing CLIs (`nak`, `ngit`, `buzz`),
  called like `gh` is today, with fakes in the tests. S0 picks the CLIs.
- The fleet's key is its own (a key file in the secrets dir, like provider keys), never the
  operator's. A relay that is closed admits it as a member.
- Same rules as every intake and landing: tests never touch the network, a failed call falls through
  or reports, never blocks the queue.

## Steps

**S0, spike (coordinator, no fleet code).** Self-hosted Buzz relay, a goose agent in a channel with
the fleet's MCP server as an extension (dispatch from a channel without new code), then answer:
1. Does the Buzz relay accept and serve NIP-34 issues (kind 1621), patches (1617) and pull requests
   (1618), and where does its git hosting live (URL scheme, auth)?
2. Which CLI signs and publishes for the fleet: `nak`, `ngit`, `buzz` (NIP-98 for Buzz REST)? Are
   there linux/arm64 builds?
3. Can `ngit` push a branch as a pull request non-interactively (flags, exit codes, output)?

**F1, identity.** `[nostr]` in fleet.toml: `key_file` (secret key, secrets dir), `relays`. The fleet
derives and shows its npub (`fleet config show`, dashboard), never logs the secret. One module,
tests with a fixed test key.

**F2, intake from NIP-34 issues.** A `NostrTracker` behind the existing `IssueTracker` interface
(`list_open`, `comment`, `close`): open issues of an announced repo (`30617:<pubkey>:<d>`) become
tasks exactly as Gitea issues do (the same `fleet` block, the same author allowlist, here by pubkey);
replies are NIP-22 comments, a done issue gets status 1631 (resolved), a rejected one 1632 (closed). Serves ngit repos and Buzz repos.

**F3, landing as a NIP-34 pull request.** A `nostr` pull request provider beside GitHub and Gitea:
for a `nostr://` remote, push the task branch and publish a pull request event; reuse an open one
for the same branch (as v0.11.1 does for GitHub and Gitea). Merge stays a human decision.

**F4, Buzz channels.** Intake from a channel: a message that mentions the fleet and carries a `fleet`
block becomes a task; the fleet answers in the thread with the task id, then the result. Through the
`buzz` CLI (JSON in and out), polling like the issue intake.

**F5, signed receipts.** When a task finishes, the fleet publishes its receipt (status, gates, commit,
lane, cost class; no prompt text unless the project allows it) as a signed event in the thread or on
the issue, so a reviewer can check who did what without trusting the dashboard.

Each F step is one module plus its tests and goes through the fleet like any other task. F2 and F3
together make ngit work end to end; F4 and F5 make Buzz a front end for the fleet.

## Open questions

- Private projects: a closed, self-hosted relay keeps events on the operator's machine; public
  relays must never see a private project's issues or receipts (the `private` project flag decides).
- Spam and authority: on open relays, only allowlisted pubkeys may create tasks (as with issue
  authors today).
