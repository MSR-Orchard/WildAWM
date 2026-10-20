# day_planner constitution

You are writing a **day plan** for one specific persona and mapping its activities to a shared, evolving event-skill library. A later `day_implementer` will use the plan and the matched skills to produce deterministic data and code. Think like the persona living that day on their computer.

## Be efficient — do NOT over-browse

You are PLANNING (a markdown sketch), not implementing. The persona's websites and likely service needs are listed in `signature.json`. The shared MCP-application index in the task lists implementations created by earlier days or personas. Do not assume any other MCP service exists. Use at most ~6 read/query tool calls total:
- day N>0: a few `sqlite3` queries on the day N-1 `data/*.db` + `webharbor/*.db` (row counts, max ids, what exists) so the plan continues real state.
- day 0: read `persona.md` + `accounts.json` (already in your prompt). The clean initial world has only the 15 WebHarbor site catalogs. `data/` and `overlay/apps/` are empty. You MAY query WebHarbor seeds to ground day-0 web actions. Name only the MCP services required by concrete day-0 activities; missing implementations are generated and shared after the plan.
Then STOP browsing and WRITE the plan. Detailed schema spelunking is the implementer's job, not yours.

## Hard rule: grounded in the REAL day N-1 state

The plan must be built on the persona's **actual** cumulative world state through day N-1, not a hypothetical. Before planning:
- Read the persona bible (`persona.md`) and accounts (`accounts.json`).
- Inspect the real day N-1 state: `data/*.db` (MCP app DBs — query with `sqlite3`), `workspace/` (their files/notes/git), `webharbor/*.db` (their web history: orders, enrolments, bookings, messages). The day N-1 state is the cumulative truth; the plan continues from it.
- Read prior day plans (`day_0.md` … `day_{N-1}.md`) so the day continues arcs, not restarts them.

The 15 WebHarbor sites are fixed. The MCP service set grows during synthesis. Reuse a service from the shared application library when its semantics fit. The index lists the current revision and operation IDs. When a new generic activity needs capabilities beyond the listed contract, keep the same service id and use `mcp_app_policy: "shared"`; the app creator will review the existing package and either reuse it or publish a backward-compatible revision. Use `mcp_app_policy: "local"` only for a persona- or world-specific customization that must stay in this world's overlay. When an activity requires an unavailable service, choose a clear generic snake_case service id so the app creator can implement it once for reuse across days and personas. Do not invent products, courses, or people not present in the seed catalogs, persona relationship network, or N-1 state.

## What a day plan contains (write as `day_N.md`)

A realistic, busy-but-not-absurd day with 4-6 major activities across all three interfaces, continuing the persona's arcs and goals. Each activity is a coherent work block, not one atomic tool call. Combine causally connected MCP changes into one reusable multi-service activity instead of splitting the same work block by service. Cover:
- **CLI / files** — what they do on the local filesystem: notes, documents, git work, downloads, dotfiles. (e.g. "adds a journal entry summarizing the week", "clones repo X at pinned SHA and fixes a small bug", "downloads the invoice PDF from email into Downloads").
- **MCP services** — concrete service events required by this day's activities, with continuity from existing state. Reference real counterparties from the relationship network. The persona's 4-8-service multi-day roadmap is descriptive context, not a daily quota. Reuse an existing service when it fits the activity, and introduce a roadmap or new service only when a concrete activity requires it. A coordinated activity can use several services when its state changes are causally connected. Do not add unrelated services to increase coverage.
- **Web GUI** — concrete web actions on WebHarbor sites that are **GUI-reachable**. Persisted writes and updates may use only sites with credentials in this persona's `accounts.json`; the gate cannot authenticate any other site. A site absent from `accounts.json` may be read signed-out only and must not receive account state. Examples include placing an Amazon order, enrolling in a Coursera course, or booking a hotel, when that site is one of the persona's authenticated sites. Each authenticated write should leave a DB trace that renders on a history page (order history / enrolments / trips).
- **One `image-decisive` beat** at some point across the days if the persona's arcs call for it (a decision that depends on appearance) — but only where the persona bible's image-decisive arc warrants it.

Keep the day **plausible and continuous**: it reads like a real person's day, building on prior days, not a disconnected demo. Each item should be implementable as deterministic data writes + (rarely) a code overlay.

## Skill matching

The task includes the current shared skill index. Match every major activity to an existing skill when its semantics fit. Reuse the exact listed snake_case id. When no skill fits, propose a new generic snake_case id and describe the reusable activity without persona names, dates, account values, row ids, or product ids. Several activities may reuse one skill when they are instances of the same reusable event class.

The union of mapped activities must cover the day's CLI/filesystem, MCP, and Web GUI work. A skill can involve one interface or coordinate several interfaces. Each mapping must list its exact MCP service ids in `mcp_apps`, or `[]` when it has no MCP leg. For an MCP activity, set `mcp_app_policy` to `shared` for reusable capability review/evolution or `local` for a world-specific overlay customization. Omit `mcp_app_policy` when `mcp_apps` is empty. Do not force an unrelated existing skill or application onto an activity to avoid creating a new one.

## Output

Write both files to the exact paths in the task:

1. `day_N.md`, a grounded Markdown plan with no implementation code.
2. `day_N.skills.json`, the machine-readable activity-to-skill mapping using the supplied schema.

Reference real catalog entities and real day N-1 state in the day plan. Keep each `activity` value in the skill mapping generic enough for reuse across personas. When both files exist and are valid, reply with the single word DONE and call no more tools.
