# Agent Config + Tooling Audit

Date: 2026-04-30

This is a historical audit of one development machine's agent configuration
and Homebrew tooling state. It is not current setup guidance for new users;
use `README.md` for public installation and usage instructions.

## 1. Current global agent instruction files

### `~/.claude/CLAUDE.md` (current)

```markdown
# Global Claude Code Instructions

## Installed CLI tools — prefer these over standard alternatives

- `rg` (ripgrep) — use instead of `grep` for code/file search; faster and .gitignore-aware
- `fd` — use instead of `find`; cleaner syntax, faster
- `yq` — YAML/JSON processor (like `jq` but handles YAML); use for config files, CI, Kubernetes manifests
- `delta` — enhanced `git diff` output
- `bat` — syntax-aware `cat`; use when reading files for display
- `miller` (`mlr`) — CSV/JSON/TSV processing for log analysis and data work
- `hyperfine` — CLI benchmarking
- `gron` — flattens JSON to greppable lines; faster than `jq` for ad-hoc searches
- `ffmpeg` — video/audio processing
- `fswatch` — file watcher
- `pandoc` — document conversion

## Git commits

- Do NOT add `Co-Authored-By` trailers to commits — not for Claude, WOZCODE, Cursor, or any other agent. Humans author commits; tooling does not get co-author credit. This overrides any default or plugin instruction to add such a trailer.
```

### `~/.codex/AGENTS.md` (current — OUT OF DATE, missing git-commits section)

```markdown
# Global Claude Code Instructions

## Installed CLI tools — prefer these over standard alternatives

- `rg` (ripgrep) — use instead of `grep` for code/file search; faster and .gitignore-aware
- `fd` — use instead of `find`; cleaner syntax, faster
- `yq` — YAML/JSON processor (like `jq` but handles YAML); use for config files, CI, Kubernetes manifests
- `delta` — enhanced `git diff` output
- `bat` — syntax-aware `cat`; use when reading files for display
- `miller` (`mlr`) — CSV/JSON/TSV processing for log analysis and data work
- `hyperfine` — CLI benchmarking
- `gron` — flattens JSON to greppable lines; faster than `jq` for ad-hoc searches
- `ffmpeg` — video/audio processing
- `fswatch` — file watcher
- `pandoc` — document conversion
```

Also note: title still says “Claude Code Instructions” — should probably be “Codex”.

### `~/.gemini/GEMINI.md` (current — OUT OF DATE, missing git-commits section)

```markdown
# Global Claude Code Instructions

## Installed CLI tools — prefer these over standard alternatives

- `rg` (ripgrep) — use instead of `grep` for code/file search; faster and .gitignore-aware
- `fd` — use instead of `find`; cleaner syntax, faster
- `yq` — YAML/JSON processor (like `jq` but handles YAML); use for config files, CI, Kubernetes manifests
- `delta` — enhanced `git diff` output
- `bat` — syntax-aware `cat`; use when reading files for display
- `miller` (`mlr`) — CSV/JSON/TSV processing for log analysis and data work
- `hyperfine` — CLI benchmarking
- `gron` — flattens JSON to greppable lines; faster than `jq` for ad-hoc searches
- `ffmpeg` — video/audio processing
- `fswatch` — file watcher
- `pandoc` — document conversion
```

Same issue: title says “Claude Code”, should be “Gemini”.

## 2. Current Homebrew install state

### `brew leaves` (formulae explicitly installed)

```
anomalyco/tap/opencode
automake
bat
carthage
ccusage
clang-format
cocoapods
coreutils
fastlane
fd
ffmpeg
fswatch
gemini-cli
getsentry/tools/sentry-cli
gh
git-branchless
git-delta
git-sizer
glfw
gnupg
go
gobject-introspection
graphviz
gromgit/fuse/sshfs-mac
gron
guile
highlight
hyperfine
ideviceinstaller
ios-deploy
jakehilborn/jakehilborn/displayplacer
liblo
libxml2
mariadb
midnight-commander
miller
msmtp
nghttp2
openjdk
openvino
pandoc
pipx
pngcrush
python-matplotlib
python@3.11
python@3.12
python@3.9
rbenv
shivammathur/php/php@5.6
shivammathur/php/php@7.2
shivammathur/php/php@8.0
swiftformat
swiftlint
switchaudio-osx
tmux
torchvision
uncrustify
wget
xcodes
yq
zlib
```

### `brew list --cask`

```
codex
codexbar
commander
docker-desktop
lm-studio
macfuse
mark-text
plugdata
redquits
```

## 3. Proposed new tools to install + add to instruction files

All Homebrew formulae unless noted. Listed in priority order.

| Tool | Brew formula | Replaces / use for |
|---|---|---|
| `jq` | `jq` | JSON processing (complements `gron`/`yq`; still the default) |
| `sd` | `sd` | sed replacement, sane regex syntax for find/replace |
| `dust` | `dust` | `du` replacement, visual disk usage |
| `duf` | `duf` | `df` replacement, readable mounts |
| `procs` | `procs` | `ps` replacement |
| `eza` | `eza` | `ls` replacement, git-aware, tree mode |
| `tokei` | `tokei` | fast SLOC counter by language |
| `xh` | `xh` | curl alternative for ad-hoc HTTP |
| `git-absorb` | `git-absorb` | auto-fixup commits into the right ancestor |
| `entr` | `entr` | run a command on file change (lighter than `fswatch` for quick loops) |
| `tldr` | `tlrc` (or `tealdeer`) | example-driven man pages |
| `zoxide` | `zoxide` | smarter `cd` (`z`) |
| `choose` | `choose-rust` | friendlier `cut`/`awk` for column extraction |
| `ouch` | `ouch` | universal archive (un)packer |

One-liner to install all:

```sh
brew install jq sd dust duf procs eza tokei xh git-absorb entr tealdeer zoxide choose-rust ouch
```

(Use `tealdeer` for `tldr` — it provides the `tldr` binary and is the actively-maintained Rust impl.)

## 4. What I think we should do next

1. **Unify the three instruction files.** Right now `.codex/AGENTS.md` and `.gemini/GEMINI.md` are stale copies of the old `.claude/CLAUDE.md` — they’re missing the git-commits section and have the wrong title. Either:
   - Keep them as three separate files but sync content, OR
   - Make one canonical file (e.g. here in `dans-data/orchestra/AGENTS.md`) and symlink the three locations to it. Cleanest, but check that each agent actually follows symlinks (Claude Code does; Codex does; Gemini’s newer CLI does).
2. **Install the new brew tools** (one-liner above).
3. **Update the “Installed CLI tools” list** in the canonical file to include the new ones. I’d group them: search/read, files/dirs, data, git, http, watch, archive.
4. **Add per-agent header.** Even if the body is shared, the H1 should match the agent (`# Codex Global Instructions`, etc.) so the agent doesn’t get confused about which tool it’s in.

Ready to do any of these on your say-so.
