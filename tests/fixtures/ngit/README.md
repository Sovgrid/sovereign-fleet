# ngit CLI answers (recorded 2026-11-12)

Real `--json` output of ngit 3.0.5 (aarch64 release) against a self-hosted Buzz relay (sha-0e6037c),
for a throwaway repository announced only to that relay. The relay's host and the signing key are
replaced: every npub is the test key `0x…03`'s, every nevent was re-encoded with the same event id
and the relay hint `wss://relay.example`. Used by tests/test_intake_ngit.py (RFC
docs/rfc/2026-11-12-nostr-buzz-ngit.md, F2) so they never touch the network.

ngit runs only inside a git repository; `--repo <nostr://…|naddr>` picks the target from anywhere.

- `issue_list.json`: `ngit issue list --repo <url> --nsec-file <key> --json`. Authors are npubs,
  ids are nevents, the issue text is not included. `issue_list_empty.json`: the same after the
  only issue was resolved (the default filter is `--status open`).
- `issue_view.json`: `ngit issue view <hex id> --repo <url> --nsec-file <key> --json` (`--offline`
  gives the same answer once a list call filled the cache). The text is `description`.
- `issue_resolved.json`: `ngit issue resolved <hex id> --reason <text> …` (kind 1631 status).
- `issue_comment_refused.json`: `ngit issue comment <hex id> --body <text> …`, exit code 1. The
  Buzz relay accepts issues (kind 1621) and statuses but refuses NIP-22 comments (kind 1111):
  `restricted: unknown event kind`.
- `issue_not_found.json`: `ngit issue comment` with an id that is not an issue of the repository,
  exit code 1.
- `repo_info.json`: `ngit repo --repo <url> --nsec-file <key> --json` for a repository hosted on the
  Buzz relay's git server (`git_servers`), announced to that relay only. Used by
  tests/test_nostr_pr.py (RFC F3).
- `pr_list.json`: `ngit pr list --status open,draft …` in a fresh helper repository, after the fleet
  published its pull requests (kind 1618) with nak. ngit shows a pull request's branch as
  `pr/<branch-name>(<first 8 hex of its id>)`. In a helper repository whose cache was filled
  before, the same call missed them ("no updates"); a fresh one lists all of them.
