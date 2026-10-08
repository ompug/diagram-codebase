# Rate limits

What is known about Figma MCP limits, what this skill enforces, and how to talk
about it honestly.

## Figma's documented limits

Figma documents per-seat limits for MCP tool calls on paid plans; for
Professional plans with a Dev or Full seat these are 10 calls per minute and 200
per day. Education plans get the same limits as Professional Dev/Full. Other
plans and seat types have different limits; check Figma's current MCP
documentation for the user's plan rather than quoting numbers from memory.
Limits are per account, not per file or repository.

Documented as exempt: `whoami`, `create_new_file`, `add_code_connect_map`, and
the authentication tools. Whether `generate_diagram` and `use_figma` are
counted is not stated explicitly, so this skill counts them. Treat any statement
about their exemption as unverified.

## What this skill enforces

Defaults (`config.budget`), deliberately below the documented Professional
numbers:

| Setting | Default | Meaning |
|---|---|---|
| `per_minute` | 8 | counted calls in any rolling 60 s |
| `per_day` | 160 | counted calls in any rolling 24 h |
| `exempt_tools` | whoami, create_new_file, add_code_connect_map, authenticate, complete_authentication | never counted |
| `max_hook_sleep_seconds` | 65 | longest the pre-hook waits for the minute window before denying |
| `backoff_base_seconds` / `backoff_max_seconds` | 30 / 900 | backoff after Figma reports a rate limit |

Override per repository in `<out>/config.json`, e.g. for a plan with a lower
allowance:

```json
{"budget": {"per_minute": 4, "per_day": 40}}
```

Raising them above Figma's documented limit for the user's plan only produces
rate-limit errors and backoff.

## How enforcement works

- **Ledger.** Every counted call is appended to the global
  `~/.cache/diagram-codebase/ledger.jsonl` (or `$XDG_CACHE_HOME/...`), shared by
  all repositories and runs, because the quota is per account.
- **Hook.** `figma_gate.py` runs on PreToolUse/PostToolUse/PostToolUseFailure for
  `mcp__.*figma.*`, only while a run is active (`active.json` fresh). Pre: if the
  minute window is full, sleep up to `max_hook_sleep_seconds`, else deny; if the
  day window is full, deny. Post: record the outcome; rate-limit text in the
  result starts a backoff.
- **Driver.** `dc.py next` checks the same ledger before emitting a counted
  action and emits `stop` (budget or backoff) instead of exceeding it. If the
  hooks never fired, it estimates from its own records.
- Calls made outside this skill (other sessions, the Figma UI) are invisible to
  the ledger. Figma may therefore rate-limit earlier than the ledger predicts.

## Talking about it

- Ledger and `dc.py budget` numbers are **local estimates** of this skill's own
  usage. Say "estimated N of the 160/day budget this skill allows itself", never
  "N of your Figma quota".
- The `review` estimate of calls per publish is a plan, not a measurement.
- When stopped for budget: say how many diagrams are done, how many remain, the
  estimated calls they need, and that `/diagram-codebase --resume` continues
  after the window clears (minute window: about a minute; day window: up to 24 h
  from the oldest counted call).
