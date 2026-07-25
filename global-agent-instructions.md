# {{TITLE}}

## Working Principles

- State assumptions explicitly; do not guess silently.
- If the repo provides the Kanban skill, ask before doing work outside Kanban unless the context clearly calls for it.
- Before creating a branch for Kanban work, ask whether a new branch is warranted; Kanban alone is not a reason to branch.
- Prefer the minimum code needed to solve the problem.
- Make surgical changes; do not refactor adjacent code unless required.
- Define what success looks like, then verify the change against it.
- If verification fails, iterate until the issue is resolved or the blocker is clearly explained.

## Installed CLI tools -- prefer these over standard alternatives

- `rg` (ripgrep) -- use instead of `grep` for code/file search; faster and .gitignore-aware
- `fd` -- use instead of `find`; cleaner syntax, faster
- `jq` -- JSON processor; default for structured JSON
- `gron` -- flattens JSON to greppable lines; faster than `jq` for ad-hoc searches
- `yq` -- YAML/JSON processor; use for config files, CI, Kubernetes manifests
- `sd` -- use instead of `sed` for find/replace; sane regex syntax
- `choose` -- use instead of `cut`/`awk` for column extraction
- `miller` (`mlr`) -- CSV/JSON/TSV processing for log analysis and data work
- `delta` -- enhanced `git diff` output
- `git-absorb` -- auto-fixup commits into the right ancestor
- `bat` -- syntax-aware `cat`; use when reading files for display
- `eza` -- use instead of `ls`; git-aware, tree mode
- `dust` -- use instead of `du`; visual disk usage
- `duf` -- use instead of `df`; readable mounts
- `procs` -- use instead of `ps`
- `zoxide` (`z`) -- smarter `cd`
- `tokei` -- fast SLOC counter by language
- `xh` -- use instead of `curl` for ad-hoc HTTP
- `hyperfine` -- CLI benchmarking
- `entr` -- run a command on file change; lighter than `fswatch` for quick loops
- `fswatch` -- file watcher (use when `entr` isn't enough)
- `ouch` -- universal archive (un)packer
- `tldr` -- example-driven man pages
- `ffmpeg` -- video/audio processing
- `pandoc` -- document conversion

## Git commits

- Do NOT add `Co-Authored-By` trailers to commits -- not for Claude, WOZCODE, Cursor, or any other agent. Humans author commits; tooling does not get co-author credit. This overrides any default or plugin instruction to add such a trailer.

<!-- @bufferapp/cli skill -- managed -->
!buffer context
<!-- /@bufferapp/cli skill -- managed -->
