# A Buzz agent that hands work to the fleet

Buzz starts its own agent runtime per agent and loads none of your MCP servers into it, so an
agent in Buzz has no fleet tools. It does not need them: the fleet already reads a Buzz channel
(see [docs/NOSTR.md](../../docs/NOSTR.md)). This example is the missing half, an agent that turns
a loosely worded request into a well-formed fleet task and posts it there.

```
you ──"fix the date bug in the importer"──▶ Fleet Dispatcher (Buzz agent)
                                              │ reads the repo, names files + test
                                              ▼
                               fleet channel: sentence + ```fleet {...}``` block
                                              │ signed by the agent's own key
                                              ▼
                          Sovereign Fleet: holds it for your approval, then
                          sandbox → gates → landing, replies in the thread
```

## Why the task waits for approval

The fleet runs a Buzz message unattended only when its author is listed in the project's
`authors` and its signature verifies. The dispatcher signs with its own agent key, which is not in
that list, so every task it posts stops at **awaiting approval** in the dashboard. Keep it that way:
an agent in Buzz runs unconfined on your machine, and the approval is the point where a person
reads what is about to run. Do not add an agent's key to `authors`.

## Set it up

1. The fleet side: a project with `provider = "buzz"` intake on a channel
   ([docs/NOSTR.md](../../docs/NOSTR.md), "Buzz channel intake").
2. In Buzz: **Create agent**, pick a runtime, and paste
   [`fleet-dispatcher.instructions.md`](fleet-dispatcher.instructions.md) into **Instructions**.
3. Add the agent to the fleet's channel, so it may post there.
4. Ask it for a change in any conversation.

## Status

Written against Buzz 1.0 (`send`, `channel_list`) and sovereign-fleet 0.12.1. The pieces it relies
on are in use (the fleet's Buzz intake, the approval rule for unlisted authors); the agent
instructions themselves have not been through a recorded end-to-end run yet.
