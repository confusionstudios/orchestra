# Security Policy

This project is local orchestration tooling. It runs user-configured commands
against local git checkouts, so treat agent CLIs, prompts, logs, and task
databases as potentially sensitive.

## Threat Model

Orchestra is **not** a sandbox or permission boundary. What it protects
against and what it does not:

- **Dashboard** binds to `127.0.0.1` with no authentication and assumes the
  loopback interface is trusted. Cross-origin POST requests are rejected via
  Origin header checks, but any local process can reach the dashboard.

- **Task content is untrusted.** Tasks may be created by prompt-injected
  agents, by remote-control channels (Discord, Telegram), or by any process
  that can write to the SQLite database. Task titles, descriptions, and
  branch names should be treated as attacker-controlled input by humans
  reviewing the queue and by any agent that reads them. Branch names are
  validated against a strict character whitelist before being passed to git.

- **Agent commands** run as your local user and can read or write any files
  available to that user. A malicious or prompt-injected task description
  can instruct an agent to exfiltrate data, modify files outside the work
  repo, or install persistence — the orchestrator does not constrain agent
  behavior beyond prompts.

### Recommended Deployment

Always run Orchestra under isolation. A prompt-injected or misbehaving agent
can access anything your user account can — credentials, SSH keys, other
repos, browser state.

- **Separate macOS user account** (recommended): create a dedicated user,
  install agent CLIs there, and route remote-control messages to that account.
  The primary user's files and credentials stay out of reach.
- **OrbStack / Docker container**: run the orchestrator and agents inside a
  container with only the work repo mounted.
- **`sandbox-exec` profile**: apply a macOS sandbox profile that restricts
  filesystem and network access to what the orchestrator needs.

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
