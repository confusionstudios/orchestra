# Orchestra — Project History

A concise retrospective of how Kanban Orchestra evolved from an extraction
out of a larger codebase into a standalone AI-agent orchestration harness.

## Origin (March 2026)

Orchestra began as an orchestration workflow embedded inside a larger
application repository. The first commit extracted it into its own repo,
carrying over:

- A Python-based orchestrator that drove AI agents through plan/build/review
  cycles.
- Prompt templates and skill stubs for Claude, Gemini, and Codex.
- A browser dashboard for monitoring progress.

The initial system was organized around a rigid, linear project workflow:
larger efforts were broken into ordered units, and each unit moved through
planning, implementation, review, and pull-request creation. The orchestrator
ran one unit at a time, handing off between agents at each step.

## The Kanban Rewrite (Late March — Early April 2026)

Within two weeks, the linear workflow was replaced by a **Kanban task queue**
backed by SQLite. This was the single largest architectural shift in the
project. Key motivations:

- The first workflow was rigid — it assumed a linear sequence of work that
  didn't match how tasks actually arrived.
- The Kanban model gave each task its own lifecycle
  (`commit-plan → commit-plan-review → commit-make → commit-review → finalize`)
  while letting the queue hold unrelated tasks in parallel.
- SQLite made state durable and inspectable without external infrastructure.

The transition happened fast. The Kanban spec, orchestrator, dashboard, and
CLI were all landed in a burst of roughly 80 commits across one week. Early
commits show rapid iteration — "first run of kanban orch," "early
improvements," agent fallback handling — indicating that the system was
being tested live against real agent runs from the start.

## Stabilization and Hardening (April 2026)

April was the highest-volume month (155 commits). Work fell into several
recurring themes:

**Agent reliability.** Agents fail in surprising ways: they narrate CLI
commands instead of running them, leave partial work behind, or silently
produce no output. The orchestrator grew retry logic, coder fallback chains,
stuck-task recovery, stash-based WIP preservation, and an explicit
`DONE_WITHOUT_COMMIT` path for tasks that finish without a commit.

**Prompt engineering.** Prompt files were repeatedly tightened, renumbered,
and restructured to reduce token usage and improve agent compliance. A
recurring lesson: agents follow instructions more reliably when prompts are
short, declarative, and structured as state machines rather than prose
narratives.

**Review workflow.** The review cycle matured from a simple approve/reject
gate into a multi-round system with rejection feedback loops, review-round
tracking, and configurable skippability. The decision to use a separate
reviewer agent (rather than self-review) was tested with multiple model
combinations.

**Dashboard polish.** The web dashboard went through continuous refinement —
timestamp formatting, supertask badges, inline editing, focus views,
sidebar controls, terminal-inspired styling, and fallback port handling.
Dashboard work was interspersed throughout; it was never a distinct phase.

## Supertasks (Mid-April 2026)

Supertasks added hierarchical decomposition: a parent task that breaks down
into ordered child tasks, each following the normal commit lifecycle. The
feature went through two planning rounds ("plan for supertasks redux 2")
before landing, reflecting the difficulty of fitting hierarchical work into
a flat Kanban queue. The final design keeps supertasks as planning
containers — they never land commits themselves.

## Operational Infrastructure (April — May 2026)

Several operational concerns were addressed:

- **Singleton locking** to prevent concurrent orchestrator runs.
- **Graceful shutdown** via sentinel file.
- **Database backup and restore** tooling.
- **Agent ACK ping gates** to verify agents are responsive before dispatching.
- **Dirty-worktree guards** to refuse orchestration when uncommitted changes
  exist.
- **Deferred build validation** (`SKIP_BUILD_UNTIL_APPROVED`) to avoid
  running expensive builds until a reviewer approves the change.

## Multi-Agent Model Exploration

The commit history shows extensive experimentation with agent assignments:

- Early commits used a single agent (Claude) for both coding and review.
- Codex was tested as both coder and reviewer ("switch to codex," "trying
  all codex"), then settled into the default reviewer role.
- Gemini appeared in shared config but was primarily used for review.
- Haiku was tried for coding as a cost optimization.
- The system evolved from hardcoded agent assignments to per-task
  `coder_agent` and `reviewer_agent` fields, with configurable defaults
  and environment-variable overrides.

A consistent finding: different models have different failure modes, and
the orchestrator's reliability depends more on how well the prompt constrains
behavior than on which model runs it.

## Skill and Tooling Layer (April — May 2026)

Orchestra developed a skill-wrapper system to give agents portable
capabilities:

- Skills are Markdown prompt files installed into each agent's configuration
  directory (`.claude/`, `.codex/`, etc.).
- A sync script keeps skill wrappers consistent across agents.
- Skills cover git operations, PR creation, kanban status queries, code
  review, and screenshot inspection.

## Architecture at a Glance

By May 2026, the system consisted of:

| Component | Implementation |
|---|---|
| Orchestrator | Python, ~3,500 lines |
| Task database | SQLite |
| Dashboard | Python HTTP server, HTML/JS |
| Task CLI | Python (`ko-task`) |
| Prompts | Markdown templates, ~3,600 lines |
| Skills | Markdown wrappers synced to agent config dirs |
| Tests | Python unittest, ~60 test cases |
| Orchestra UI | Shell wrapper around the dashboard |

Total: roughly 17,000 lines of Python, 3,600 lines of Markdown prompts,
and 500 lines of shell scripts.

## Recurring Patterns

Looking across 262 commits and two months of development, several patterns
stand out:

1. **Spec-first, then iterate.** Major features (Kanban, supertasks) started
   with spec documents, but the specs were revised repeatedly as
   implementation revealed gaps. The specs were living documents, not
   contracts.

2. **Live testing over unit testing.** The system was tested primarily by
   running real agent sessions. Unit tests were added in bursts (the "Test
   Repo" series) but lagged behind the orchestrator's actual behavior.
   Several bug fixes addressed issues that only surfaced during live runs.

3. **Dashboard as feedback loop.** The dashboard wasn't an afterthought — it
   was how the operator monitored agent behavior in real time and spotted
   problems. Dashboard improvements often immediately followed orchestrator
   changes because operational visibility drove the next fix.

4. **Prompt tightening never ends.** Prompt refinement commits appear
   throughout the entire history. Each round of agent testing revealed new
   ways that agents could misinterpret or ignore instructions, leading to
   more precise prompt language.

5. **Rollbacks happen.** The `commit_hashes` array feature was built, merged,
   and then backed out within a few days when it proved more complex than
   valuable. The willingness to revert kept the codebase from accumulating
   speculative complexity.

## Timeline Summary

| Period | Focus |
|---|---|
| Mar 15–17 | Extraction from parent repo, initial linear orchestrator |
| Mar 18–28 | Dashboard, agent config, prompt refinement |
| Mar 29–31 | Kanban spec and initial implementation |
| Apr 1–7 | Kanban stabilization, retry logic, dashboard rebuild |
| Apr 8–15 | Supertask design and implementation |
| Apr 16–23 | Prompt tightening, config consolidation, test infrastructure |
| Apr 24–30 | Operational hardening, deferred builds, commit footers |
| May 1–17 | Skill sync, multi-agent config, UI polish, open-source prep |
