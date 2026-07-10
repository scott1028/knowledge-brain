# recall-engine

`recall-engine` is a thin launcher that wraps an agent CLI (`claude`,
`codex`, `pi`, `gemini`, `opencode`, or `agy`) with a git-based Markdown
knowledge base. It prepares a knowledge repo (local path or SSH clone), starts
(or reuses) a shared MCP server that serves the repo's notes, injects a skill
that makes the agent consult those notes — its record of past processing and
notes — before replying to any message, points the agent's MCP config at the
server, launches the agent, and restores everything on exit. It also ships a
one-way sync between a Google Drive folder and the repo's `src/` directory.

The knowledge base is reached through MCP tools rather than the filesystem, so
the repo can live outside the project without a sandboxed agent needing access
to it.

## Install

From a checkout of this repository:

```bash
uv tool install .
# or
pipx install .
```

Requirements: Python >= 3.10, `git`, and at least one supported agent CLI:
Claude Code (https://claude.com/claude-code), Codex CLI
(https://developers.openai.com/codex/cli), pi
(https://github.com/earendil-works/pi), Gemini CLI
(https://github.com/google-gemini/gemini-cli), opencode, or agy. The install
provides the `recall-engine` entry point.

## Quickstart

```bash
# local knowledge repo
KNOWLEDGE_REPO_PATH=~/workspace/recall recall-engine wrap claude

# clone via SSH (default dest ./.recall)
KNOWLEDGE_REPO_SSH="git@github.com:developer/recall.git" recall-engine wrap claude

# explicit ssh key
KNOWLEDGE_REPO_SSH="git@github.com:developer/recall.git" SSH_KEY=~/.ssh/id_rsa recall-engine wrap claude

# other agent CLIs
KNOWLEDGE_REPO_PATH=~/workspace/recall recall-engine wrap codex
KNOWLEDGE_REPO_PATH=~/workspace/recall recall-engine wrap pi
KNOWLEDGE_REPO_PATH=~/workspace/recall recall-engine wrap gemini
KNOWLEDGE_REPO_PATH=~/workspace/recall recall-engine wrap opencode
KNOWLEDGE_REPO_PATH=~/workspace/recall recall-engine wrap agy
```

## How wrap works

`recall-engine wrap <agent> [args...]` runs a six-step lifecycle.
Anything after `<agent>` is forwarded verbatim to the agent, and leading
environment variables are inherited, so
`AA=1 recall-engine wrap claude foo --bar` behaves like
`AA=1 claude foo --bar`:

1. **Prepare the repo** — `KNOWLEDGE_REPO_PATH` is used as-is;
   `KNOWLEDGE_REPO_SSH` clones to `./.recall` on first run and
   `git pull --ff-only` afterwards (a failed pull warns and keeps the existing
   checkout, so wrapping still works offline). The clone dir is added to the
   host project's `.git/info/exclude`.
2. **Inject the skill** — a rendered `SKILL.md` is written to
   `.agents/skills/recall-engine/` (the Agent Skills SSOT) in the
   current project, and `.claude/skills/`, `.gemini/skills/`, `.pi/skills/`,
   and `.opencode/skills/` get relative symlinks pointing at it, so every
   supported agent discovers the same skill (`codex` and `agy` read the SSOT
   directly, so they need no symlink). Any pre-existing skill entry is backed
   up first, and a marker file tracks the session owners: multiple wrap
   sessions in the same directory attach to one injection (the marker records
   every owner pid), and the last session to exit restores the original state.
3. **Start or reuse the MCP server** — one server per machine serves every wrap
   session (see [Shared MCP server](#shared-mcp-server)).
4. **Inject the MCP config** — the server is registered in the agent's own
   config file (`.mcp.json` for claude, `.codex/config.toml` for codex,
   `.pi/mcp.json` for pi, `.gemini/settings.json` for gemini, `opencode.json`
   for opencode, `.agents/mcp_config.json` for agy). Only the `recall-engine`
   entry is added; existing content is backed up and restored on exit.
5. **Launch the agent** — as a child process with signals forwarded and
   `RECALL_REPO_PATH` set to the resolved repo path. Unknown command
   names (e.g. `claude-company`) are classified via their `--version` output
   or name tokens before launching.
6. **You work, then restore on exit** — before replying to any message, the
   skill directs the agent to call `search_knowledge` and cite the note paths
   it returns, factoring prior processing records into the answer as past
   experience. Normal exit or Ctrl+C removes the injected skill, symlinks, and
   MCP config entry, restores any backups, drops this session's claim on the
   shared server, and returns the agent's exit code.

You can run several wrap sessions concurrently in the same project directory:
the first sets up the injected skill and each later one attaches to it, so the
skill stays in place until the last session exits. A later session may omit
`KNOWLEDGE_REPO_PATH` / `KNOWLEDGE_REPO_SSH` entirely — when neither is set, the
repo is auto-detected from the running session's marker (env vars still win when
set). Sessions started from different project directories keep independent
skill injections — the marker, lock, and injected skill are all per-directory,
so they never block or attach to each other — but they do share one MCP server.

If the wrapper is killed hard (`kill -9`), the leftover state is detected and
repaired on the next `wrap`, or clean it manually with `recall-engine unwrap`.

Skill trigger reliability is higher on sonnet-tier models than on haiku
(empirical observation from real-session testing).

## Shared MCP server

The knowledge base is served over MCP by a single streamable-HTTP server per
machine, bound to `127.0.0.1` on an ephemeral port. It exposes three read-only
tools:

- `search_knowledge(query)` — matching note paths with snippets.
- `read_note(path)` — the full text of one note.
- `list_notes()` — available note file names.

The first `wrap` spawns the server; later wraps reuse it; the last session to
exit stops it. A state file (`/tmp/recall-engine-mcp-<uid>.json`, guarded by an
flock) records the server's pid, port, token, and the wrap pids that depend on
it. A stale entry left by `kill -9` is detected and replaced on the next `wrap`.

One server serves every repo. Each project's injected MCP config carries an
`X-Recall-Repo` header naming its knowledge repo, so the server routes each
connection to the right notes; an `X-Recall-Token` header rejects clients the
supervisor did not configure. Two sessions in different directories with
different repos therefore share a server yet only ever see their own notes.

Two agents need extra setup to reach the server:

- **pi** requires the `pi-mcp-adapter` extension (`pi install pi-mcp-adapter`).
  Without it pi still gets the skill, but no MCP tools.
- **codex** reads the injected `.codex/config.toml` only in trusted projects;
  the first `codex` run in a project prompts you to trust it. Until then codex
  falls back to the shared `~/.codex/config.toml` and will not see the
  recall-engine server.

Skill injection is what guarantees the agent searches before replying; the MCP
tools are how it searches. The skill is the reliable trigger because its text
enters the context directly, whereas an MCP server's `instructions` field is
injected at the client's discretion.

## Google Drive sync

### Setup

Drive access uses gcloud credentials with the Drive scope. Either login works:

```bash
# Option A: gcloud user login (token minted via `gcloud auth print-access-token`)
gcloud auth login --enable-gdrive-access

# Option B: Application Default Credentials (the default
# `gcloud auth application-default login` does NOT include the Drive scope)
gcloud auth application-default login \
  --scopes=https://www.googleapis.com/auth/drive,https://www.googleapis.com/auth/cloud-platform
```

ADC is tried first, then the user login. Some organizations also require a
quota project for ADC:
`gcloud auth application-default set-quota-project <project-id>`.

### Usage

```bash
KNOWLEDGE_DRIVE_FOLDER=Shared KNOWLEDGE_REPO_PATH=~/workspace/recall \
  recall-engine sync download   # Drive folder -> <repo>/src/

KNOWLEDGE_DRIVE_FOLDER=Shared KNOWLEDGE_REPO_PATH=~/workspace/recall \
  recall-engine sync upload     # <repo>/src/ -> Drive folder
```

`KNOWLEDGE_DRIVE_FOLDER` accepts either the Drive folder ID or the folder name.
Name matching is case-insensitive; if several folders share the name, sync
aborts and asks for the folder ID.

### Behavior notes

- Sync is **strictly one-way** per invocation; there is no merge.
- Files are **overwritten by name** (last writer wins) — the knowledge repo is
  a git repo, so `git diff` / `git checkout` are your safety net after a
  download.
- **Deletes never propagate**: removing a file on one side does not remove it
  on the other.
- Native Google Docs are **exported to `.md`** on download (verify the exported
  content quality yourself — tables, images, and formatting depend on Google's
  exporter).
- Upload never converts `.md` files back to native Google Docs; they stay
  plain Markdown files on Drive.
- Sync is **non-recursive**: only `<repo>/src/*.md` and the top level of the
  Drive folder are considered.

## Environment variables

| Variable | Purpose |
|---|---|
| `KNOWLEDGE_REPO_PATH` | Path to an existing local knowledge repo (mutually exclusive with `KNOWLEDGE_REPO_SSH`; optional for a second `wrap` in a directory that already has a running session — the repo is auto-detected) |
| `KNOWLEDGE_REPO_SSH` | SSH URL of the knowledge repo; cloned to `./.recall` |
| `SSH_KEY` | SSH private key for clone/pull; defaults to auto-detection (`id_ed25519` → `id_ecdsa` → `id_rsa` in `~/.ssh`) |
| `KNOWLEDGE_DRIVE_FOLDER` | Google Drive folder ID or name (case-insensitive) for `sync download` / `sync upload` |

## Doctor

```bash
recall-engine doctor
```

Checks git, the supported agent CLIs on PATH (at least one of
claude/codex/pi/gemini/opencode/agy must be present; missing ones print
`[skip]`), the `mcp` package, the shared MCP server, ssh key, repo
configuration, and gcloud Drive access, printing `[ok]` / `[fail]` per check
with a fix instruction for each failure. Exits non-zero if any required check
fails.

The MCP server check reports `[skip]` when no server is running (normal — `wrap`
starts one on demand), `[ok]` with its URL and owner count when it is reachable,
and `[fail]` when the state file records a server that is not, which
`recall-engine unwrap` clears.

## Manual E2E checklist

### wrap

1. In a scratch project, run `KNOWLEDGE_REPO_PATH=<sample-repo> recall-engine wrap <agent>`
   (repeat for each installed agent: claude, codex, pi, gemini, opencode, agy).
   Verify `/tmp/recall-engine-mcp-<uid>.json` appears with this wrap's pid in
   `owners`, and that the agent's config file gained a `recall-engine` entry.
2. Send a message covered by a file in `<sample-repo>/src/`; observe the
   agent calls `search_knowledge` and cites the returned `src/*.md` path.
3. Send an ordinary message not tied to any note; observe the agent still
   searches first and, finding nothing, says so before answering
   from general knowledge. A bare greeting (e.g. "hi") may skip the search.
4. From a second directory, `wrap` another agent against a *different* repo.
   Verify the state file's `owners` grows and the port is unchanged (the server
   was reused), and that queries there only match the second repo's notes.
5. Quit the first agent; the server keeps running for the second. Quit the
   second; verify the server exits and the state file is removed.
6. Verify `.agents/skills/`, `.claude/skills/`, `.gemini/skills/`,
   `.pi/skills/`, and `.opencode/skills/` contain no `recall-engine`
   leftovers, and that each agent's MCP config is back to its original content.
7. `kill -9` a wrap session, then run `recall-engine doctor` and `wrap` again;
   verify the stale state is reported and then reclaimed.

### drive sync

1. Create a Drive folder with a few `.md` files and a native Google Doc; note its folder ID.
2. Run `sync download`; verify the files landed in `<repo>/src/` and check the exported Doc's `.md` content.
3. Edit a local `.md`, run `sync upload`, and verify the change on Drive.

## Development

```bash
uv sync
uv run pytest
uv run ruff check src tests
```
