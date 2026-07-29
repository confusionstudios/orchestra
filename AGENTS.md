# Repository Instructions

## Working Principles

- State assumptions explicitly; do not guess silently.
- Prefer the minimum code needed to solve the problem.
- Make surgical changes; do not refactor adjacent code unless required.
- Define what success looks like, then verify the change against it.
- If verification fails, iterate until the issue is resolved or the blocker is clearly explained.

## Local Test Policy

Only generate tests when the code contains actual logic.

- Skip tests for pure data: constants, config, DTOs, type definitions, simple mappings with no branching, no calculations, no side effects, and no business rules.
- Write tests only for real behavior: conditionals, loops, calculations, state changes, error handling, and algorithms.

There is no build step for this repo.

Do not queue or run Kanban Orchestra tasks on `master` or `main` in this repo.
Use a feature branch for orchestrated work.

Before staging changes that could affect executable behavior, run the unit tests
from this checkout:

```bash
bin/ko-test
```

Pure documentation, instruction text, screenshots, images, and other static
assets do not require a test run unless they are part of a behavior change.

Use the repo-local paths above directly.
