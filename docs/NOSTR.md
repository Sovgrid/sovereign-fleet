# Nostr — operator guide

The fleet can take work from Nostr through two providers: **Buzz** (channel messages as issues) and **ngit** (NIP-34 issues). Every event the fleet publishes on Nostr — replies, reactions, and issue comments — is signed with the fleet's own key.

The fleet can also open pull requests on Nostr for a project whose git remote is a `nostr://` URL (section 4).

---

## 1. The fleet's identity

The fleet signs all Nostr events with a keypair it owns. The secret never appears in logs, error messages or process arguments.

```toml
[nostr]
key_file = "fleet-nsec"    # nsec file (mode 0600); absolute path or a name in [secrets] dir
nak = "nak"                # the nak CLI used to derive the public key (default: "nak")
```

- `key_file` points to an nsec file containing the fleet's secret key. Mode **0600** is required; `fleet config check` refuses a missing file or wrong mode.
- The file is either an absolute path, or a name looked up in the secrets directory (`[secrets] dir`, default `<config dir>/secrets`).
- The fleet's public key (and npub) is derived through the `nak` CLI (`nak decode` → `nak key public` → `nak encode npub`); `fleet config show` prints `nostr.npub`.
- This is the fleet's identity. The fleet **never** uses the operator's key.

**Example** (all placeholders):

```toml
[secrets]
dir = "/etc/fleet/secrets"

[nostr]
key_file = "/etc/fleet/secrets/fleet-nsec"
nak = "nak"
```

---

## 2. Buzz channel intake

A Buzz channel on a relay acts as an issue tracker: top-level channel messages become issues, and the fleet replies in the message's thread.

```toml
[buzz]
relay_url = "https://relay.example:8447"   # the relay's HTTPS URL
cli = "buzz"                               # the buzz CLI (default: "buzz")
```

Per project:

```toml
[projects.app.intake]
enabled = true
provider = "buzz"
channel = "00000000-0000-0000-0000-000000000000"
authors = ["64hexpubkey…", "64hexpubkey…"]
```

- `relay_url`: the HTTPS URL of the Buzz relay.
- `cli`: path or name of the buzz CLI (default `"buzz"`).
- `channel`: a 36-character channel UUID.
- `authors`: list of the author's 64-character hex public keys.
- Top-level messages (kind 9) of the channel are the issues; thread replies are ignored.
- The fleet answers in the message's thread and marks a landed task with a ✅ reaction.
- `label` is **ignored** for Buzz; the `fleet` block and author allowlist are the only gates.
- The secret key reaches the buzz CLI only through its environment (`BUZZ_PRIVATE_KEY`), never on the command line.

---

## 3. ngit intake (NIP-34 issues)

ngit (git over Nostr, version 3) reads open NIP-34 issues from a repository announced on Nostr.

```toml
[ngit]
cli = "ngit"   # the ngit CLI (default: "ngit")
```

Per project — either explicitly:

```toml
[projects.example.intake]
enabled = true
provider = "ngit"
repo = "nostr://npub1…/relay.example/app"   # nostr://<npub>/<relay>/<identifier>
authors = ["64hexpubkey…"]
label = "fleet"   # default: "fleet"; "" reads every open issue
interval_min = 5  # poll every 5 minutes (default: 5; max: 1440)
```

Or from the project's git remote: with `provider = "auto"` (or `"ngit"` and no `repo`), a project whose
remote (`[projects.<name>] remote`, default `origin`) is a `nostr://` URL takes its issues from that
repository, for example after `git remote add origin nostr://npub1…/relay.example/app`.

- Requires **ngit 3** (`ngit` and `git-remote-nostr` on `$PATH`).
- `repo`: a `nostr://` URL or an `naddr`; empty: the project's git remote, when it is a `nostr://` URL.
- `authors`: 64-hex pubkeys. ngit shows authors as npubs on `issue list`; the fleet decodes them internally.
- **`label` is applied** (default `"fleet"`): only open issues with that label are read. Label the issues you want worked on (`ngit issue create --label fleet`), or set `label = ""` for every open issue.
- The fleet runs ngit in a fresh helper git repository under `<fleet root>/ngit` for each call (section 4), passing the key as `--nsec-file <path>`.
- A landed issue is marked resolved (NIP-34 kind 1631 status).

**Known limit with Buzz relays.** A Buzz 1.0 relay accepts NIP-34 issues (kind 1621) and status events (kind 1631) but **refuses NIP-22 comments** (kind 1111) with the error `restricted: unknown event kind`. So on a Buzz relay the fleet cannot post its replies (task queued, result); the comment is retried on the next four reports, and after the last try a landed issue is still marked resolved. The result text stays in the dashboard. A relay that accepts kind 1111 gets the replies as usual.

---

## 4. Pull requests on Nostr (NIP-34)

A project with `landing = "pull_request"` whose git remote is a `nostr://` URL gets its pull
requests as NIP-34 events, signed with the fleet's key:

```toml
[projects.app]
path = "/srv/app"
landing = "pull_request"
remote = "origin"          # a git remote whose URL is nostr://<npub>/<relay>/<identifier>
default_branch = "main"
```

- The fleet reads the repository announcement (`ngit repo --json`): its git server, relays and
  maintainers.
- It fetches the default branch from that git server, rebases the task branch onto it and pushes
  it there as `pr/<branch>` (`pr/fleet/<task-id>`). Nothing is pushed to the default branch.
- It publishes a pull request event (kind 1618) to the repository's relays: subject, description,
  tip commit, clone URL, branch name and target branch, as ngit writes them. A maintainer sees it
  with `ngit pr list` and merges with `ngit pr merge`.
- A rerun of the same task updates the fleet's open pull request of that branch (kind 1619)
  instead of opening a second one.
- A git server that asks for Nostr HTTP auth (a Buzz relay's git server answers 401 with
  `WWW-Authenticate: Nostr`) gets a NIP-98 token signed with the fleet's key for each git call.
  The token reaches git through its environment, never its command line. No credential helper
  and no newer git are needed.
- On a Buzz relay the repository must be bound to a channel (`buzz repos bind`) and the fleet
  must be a member of that channel; the relay's branch protection still applies.

ngit's own `git push` to a `nostr://` remote is not used: it needs the key in ngit's key store
and cannot answer a Buzz git server's auth.

**Known limit (ngit 3.0.5).** ngit's incremental sync of a reused event cache misses events that
other clients published later (an issue written in the Buzz app). The fleet therefore runs every
ngit call in a fresh, empty helper repository under `<fleet root>/ngit`, which reads the
repository's events in full.

---

## 5. Privacy

Events on a private project's relay must never reach public relays. Use the `--repo-relay-only` flag when initializing a ngit repository:

```bash
ngit init --private --repo-relay-only
```

or equivalently:

```bash
git config nostr.repo-relay-only true
```

The fleet has no relay of its own to fall back to: Buzz intake talks only to `[buzz] relay_url`, ngit intake to the repository's relays. ngit itself may look up the signing key's profile on its default relays (a read; nothing is published there with `--repo-relay-only`).

---

## 6. Trust (same rule as Gitea / GitHub)

The intake trust policy is identical for all providers:

- An issue from an author in `authors` that carries a valid ```` ```fleet ```` block runs **unattended**.
- Everything else — no block, or an author not in `authors` — is queued for **manual approval** in the dashboard.
- A block that does not parse gets one reply (per issue revision) and nothing is queued.
- Without any `authors` nothing runs unattended, which is the right default for public repositories.

**Who vouches for the author.** The author's signature does. For Buzz intake the fleet checks the
id and signature of every channel message with `nak verify` before it reads the author (since
0.12.1): a message the relay altered, or one posted under someone else's key, is skipped. If `nak`
cannot run, the poll fails instead of accepting unverified messages. ngit verifies signatures itself.

The ```` ```fleet ```` block (JSON) sets the task contract: `allowed_paths`, `gate` (or `gate_cmd`), `category`, `lane`, and `acceptance_criteria`.

---

## Quick checklist

| What | Where |
| :--- | :--- |
| Fleet identity | `[nostr] key_file`, `nak` |
| Buzz relay | `[buzz] relay_url`, `cli` |
| ngit CLI | `[ngit] cli` |
| Per-project intake | `[projects.<name>.intake] provider`, `channel` / `repo` |
| Author allowlist | `[projects.<name>.intake] authors` |
| Issue label | `[projects.<name>.intake] label` |
| Pull requests on Nostr | `[projects.<name>] landing = "pull_request"` and a `nostr://` remote |
| Private repo flag | `ngit init --repo-relay-only` (or `git config nostr.repo-relay-only`) |
| Config validation | `fleet config check` (refuses missing or improperly permissioned key file) |
| Show npub | `fleet config show` prints `nostr.npub` |
