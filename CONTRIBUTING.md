# Contributing

Orchestra is a small local-tooling project. Focused fixes, docs corrections,
and small improvements are welcome.

## Local Setup

```bash
git clone https://github.com/confusionstudios/orchestra.git
export ORCHESTRA_DIR="$PWD/orchestra"
"$ORCHESTRA_DIR/shared_scripts/bootstrap-python-env.sh"
```

Agent CLIs are not bundled. Install and authenticate whatever local model CLI
commands you choose to configure.

## Before Sending Changes

- Keep changes small and scoped.
- Do not commit local task databases, runtime logs, credentials, API keys, or
  provider tokens.
- Keep personal paths such as `/Users/Shared/orchestra` as optional examples,
  not assumptions.
- Run the test suite from this checkout:

```bash
"$ORCHESTRA_DIR/bin/ko-test"
```

## Documentation

Update README or workflow docs when behavior changes. Be explicit about which
tools are built into this repository and which tools are external CLIs that a
user must provide.
