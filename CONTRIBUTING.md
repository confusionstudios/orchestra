# Contributing

The most useful contribution is using Orchestra and describing your experience.
Reports about confusing docs, rough edges, surprising behavior, and ideas that
come from real use shape the project more than anything else. Code pull
requests are welcome too, but they are not the expected first step.

## Reporting Experience And Suggestions

Open a GitHub issue describing what you tried and what you would improve. A
useful report includes:

- your OS and version
- the Orchestra commit (`git -C "$ORCHESTRA_DIR" rev-parse --short HEAD`)
- the agent CLI and its version (for example, `codex --version`)
- what you expected and what actually happened
- relevant evidence, such as `ko-get-update` output, task comments, or log
  excerpts, with paths, tokens, and private code removed

Report security issues as described in [SECURITY.md](SECURITY.md), not in a
public issue.

## Local Setup

```bash
git clone https://github.com/confusionstudios/orchestra.git
export ORCHESTRA_DIR="$PWD/orchestra"
"$ORCHESTRA_DIR/shared_scripts/bootstrap-python-env.sh"
```

Agent CLIs are not bundled. Install and authenticate whatever local model CLI
commands you choose to configure.

## Sending Code Changes

- Keep changes small and scoped.
- Do not commit local task databases, runtime logs, credentials, API keys, or
  provider tokens.
- Keep personal install paths as examples, not assumptions.
- For changes that affect behavior, run the test suite from this checkout:

```bash
"$ORCHESTRA_DIR/bin/ko-test"
```

Documentation-only changes do not need a full test run.

## Documentation

Update README or workflow docs when behavior changes. Be explicit about which
tools are built into this repository and which tools are external CLIs that a
user must provide.
