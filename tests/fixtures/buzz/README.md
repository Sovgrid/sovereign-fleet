# Buzz CLI answers (recorded 2026-11-12)

Real output of the `buzz` CLI (Buzz desktop 1.0.0, relay sha-0e6037c) against a self-hosted relay,
recorded with throwaway keys that were removed from the relay afterwards. Used by the tests of
the Buzz intake (RFC docs/rfc/2026-11-12-nostr-buzz-ngit.md, F4) so they never touch the network.

- `messages_get.json`: `buzz messages get --channel <uuid> --limit 5`. Channel messages are kind 9
  events with the channel in an `h` tag; a thread reply also has `["e", <root id>, "", "reply"]`.
- `messages_send.json`, `messages_reply.json`: `buzz messages send` (with `--reply-to <id>`).
- `reactions_add.json`: `buzz reactions add --event <id> --emoji <e>`.
- `error_mention.json`: a send whose `@name` matches no channel member (exit code 1).
Exit codes: 0 ok, 1 bad input, 2 relay/network, 3 auth, 4 other, 5 write conflict.
