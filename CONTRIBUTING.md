# Contributing

Orchestra is a small local-tooling project. Focused fixes, docs corrections,
and small improvements are welcome.

## Local Setup

```bash
git clone https://github.com/dr2050/orchestra.git
cd orchestra
./shared_scripts/bootstrap-python-env.sh
export ORCHESTRA_DIR="$PWD"
export PATH="$ORCHESTRA_DIR/bin:$PATH"
```

Agent CLIs are not bundled. Install and authenticate any `codex`, `claude`,
`gemini`, `kilo`, or other model CLI you choose to configure locally.

## Before Sending Changes

- Keep changes small and scoped.
- Do not commit local task databases, runtime logs, credentials, API keys, or
  provider tokens.
- Keep personal paths such as `/Users/Shared/orchestra` as optional examples,
  not assumptions.
- Run the test suite from this checkout:

```bash
bin/ko-test
```

## Documentation

Update README or workflow docs when behavior changes. Be explicit about which
tools are built into this repository and which tools are external CLIs that a
user must provide.
