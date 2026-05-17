# Security Policy

This project is local orchestration tooling. It runs user-configured commands
against local git checkouts, so treat agent CLIs, prompts, logs, and task
databases as potentially sensitive.

## Supported Versions

Security fixes target the current `master` branch.

## Reporting

If you find a vulnerability, use GitHub private vulnerability reporting if it
is available for the repository. If not, open a minimal public issue asking
for a private contact path, but do not include exploit details, secrets,
tokens, private prompts, or private repository contents in the issue.

There is no formal bug bounty or guaranteed response SLA.

## Handling Secrets

Do not commit API keys, model-provider tokens, local agent transcripts,
`kanban-orchestra.db`, `.kanban-orchestra/`, or work-repo source that is not
intended to be public.
