# Recall Engine - BDD Requirements

## Overview

`recall-engine` is a Python CLI that lets an agent CLI (`claude`,
`codex`, `pi`, `gemini`, `opencode`, or `agy`) use a local Markdown
knowledge base.

The tool prepares a knowledge repo, starts (or reuses) a shared MCP server that
serves the repo's notes as read-only tools, injects a temporary Agent Skills
skill into the current project (SSOT at `.agents/skills/`, with symlinks for the
other agents' skills dirs), registers the server in the agent's own MCP config,
launches the agent, and restores the project after the session ends. It also
supports one-way sync between a Google Drive folder and the knowledge repo's
`src/` directory.

The skill is the trigger ("search before replying"); the MCP tools are the
mechanism. Because the server reads the repo directly, out-of-project repos work
without granting a sandboxed agent filesystem access to them.

## Current Codebase Facts

- The project is a Python 3.10+ package.
- The package uses a `src/` layout with import package `recall_engine`.
- The CLI entry point is `recall-engine`.
- The CLI is built with Typer.
- Google Drive access uses `google-api-python-client` and `google-auth`.
- The MCP server is built on the `mcp` SDK (FastMCP, streamable HTTP).
- The implemented commands are `wrap`, `unwrap`, `sync`, and `doctor`, plus a
  hidden `mcp-serve` used internally to spawn the shared server.
- The project uses `uv run pytest` and `uv run ruff check src tests` for checks.
- No JavaScript, TypeScript, webpack, Vite, or Next.js config is present.

## Feature: Configure a Knowledge Repo

The CLI must resolve one knowledge repo before it can wrap an agent or sync
Drive files.

Scenario: use a local repo
- Given `KNOWLEDGE_REPO_PATH` points to an existing directory
- When the user runs a command that needs the knowledge repo
- Then the CLI uses that directory as the knowledge repo
- And downstream behavior receives the resolved absolute path

Scenario: clone a repo from SSH
- Given `KNOWLEDGE_REPO_SSH` is set
- And `./.recall` does not exist
- When the user runs a command that needs the knowledge repo
- Then the CLI clones the SSH repo into `./.recall`

Scenario: update an existing SSH clone
- Given `KNOWLEDGE_REPO_SSH` is set
- And `./.recall` already exists as a git repo with the same origin
- When the user runs a command that needs the knowledge repo
- Then the CLI runs `git pull --ff-only`
- And if pull fails, it warns and continues with the existing checkout

Scenario: reject an unsafe existing clone target
- Given `KNOWLEDGE_REPO_SSH` is set
- And `./.recall` exists but is not a git repo
- When the CLI prepares the knowledge repo
- Then the command fails with a clear error

Scenario: reject an origin mismatch
- Given `./.recall` is a git repo
- And its origin does not match `KNOWLEDGE_REPO_SSH`
- When the CLI prepares the knowledge repo
- Then the command fails
- And it must not overwrite the directory

Scenario: reject ambiguous repo settings
- Given both `KNOWLEDGE_REPO_PATH` and `KNOWLEDGE_REPO_SSH` are set
- When the CLI resolves configuration
- Then the command fails with a configuration error

Scenario: inherit the repo from a live wrap session
- Given a wrap session is already running in the same project directory
- And neither `KNOWLEDGE_REPO_PATH` nor `KNOWLEDGE_REPO_SSH` is set
- When the user starts another `recall-engine wrap <agent>` there
- Then the CLI reuses the running session's knowledge repo
- And it attaches to the existing skill injection

Scenario: reject missing repo settings
- Given neither `KNOWLEDGE_REPO_PATH` nor `KNOWLEDGE_REPO_SSH` is set
- And no live wrap session exists in the current directory
- When the CLI resolves configuration
- Then the command fails with a configuration error

## Feature: Resolve SSH Access

SSH mode must use a predictable key and avoid hidden ssh-agent dependency.

Scenario: use an explicit SSH key
- Given `SSH_KEY` points to an existing file
- When the CLI runs git in SSH mode
- Then it sets `GIT_SSH_COMMAND` to use that key
- And it includes `IdentitiesOnly=yes`

Scenario: auto-detect an SSH key
- Given `SSH_KEY` is not set
- When the CLI needs an SSH key
- Then it checks `~/.ssh/id_ed25519`, `~/.ssh/id_ecdsa`, and `~/.ssh/id_rsa`
- And it uses the first existing key in that order

Scenario: fail when no SSH key exists
- Given SSH mode is configured
- And no supported key exists
- When the CLI prepares the repo
- Then the command fails
- And the error lists the searched paths

## Feature: Wrap an Agent CLI

`wrap <agent>` must launch a supported agent CLI (`claude`, `codex`, `pi`,
`gemini`, `opencode`, or `agy`) with a temporary knowledge skill.

Scenario: launch a supported agent with knowledge context
- Given a valid knowledge repo is configured
- And the agent command exists in the user's shell environment
- When the user runs `recall-engine wrap <agent>` with one of the
  six supported names
- Then the CLI injects the knowledge skill (see the skill feature below)
- And it sets `RECALL_REPO_PATH` to the resolved repo path
- And it launches the agent through the user's interactive shell
- And it exits with the agent's exit code

Scenario: classify a wrapper command by version output
- Given a command such as `claude-company` whose `--version` output contains
  a known agent signature (`Claude Code` for claude, `codex` for codex)
- When the user runs `recall-engine wrap claude-company`
- Then the CLI treats it as a wrapper of that agent family and launches it

Scenario: classify a wrapper command by name token
- Given a command such as `gemini-company` whose `--version` exits `0` but
  prints only a bare version number
- When the CLI classifies the command
- Then it splits the command name on `-`, `.`, and `_`
- And a token equal to a supported agent name selects that family
- And a name like `pip` must not classify as `pi`

Scenario: reject unsupported agents
- Given the user runs `recall-engine wrap notclaude`
- And the command matches no agent signature or name token
- When the CLI validates the agent name
- Then the command exits with code `2`
- And the output lists the supported agents

Scenario: restore after launch failure
- Given the skill was injected
- And the agent cannot be launched
- When the launcher fails
- Then the CLI restores the project skill state before exiting

Scenario: restore when the session is interrupted or the terminal closes
- Given a wrap session is running the agent
- When the user presses Ctrl-C (SIGINT) or closes the terminal (SIGHUP)
- Then the signal is forwarded to the agent and the wrapper stays alive
- And it tears down its injected skill and MCP config before exiting

## Feature: Inject and Restore the Skill

The skill injection must be reversible and safe around interrupted sessions.

Scenario: inject the rendered skill at the Agent Skills SSOT
- Given the CLI has resolved the knowledge repo path
- When the wrapper injects the skill
- Then it writes `.agents/skills/recall-engine/SKILL.md`
- And the skill directs the agent to the MCP tools, naming no repo path
- And it writes `.agents/skills/.recall-engine-marker.json`
- And the marker records the resolved repo path so a later wrap in the same
  directory can reuse it without re-specifying the repo env var

Scenario: symlink the skill into the other agents' skills dirs
- Given the skill was written to `.agents/skills/recall-engine/`
- When the wrapper finishes injecting
- Then `.claude/skills/recall-engine`,
  `.gemini/skills/recall-engine`,
  `.pi/skills/recall-engine`, and
  `.opencode/skills/recall-engine` are relative symlinks to it
- And `codex` and `agy` read `.agents/skills/` directly, so they get no symlink
- And all symlinks are removed when the session ends

Scenario: register the MCP server in the agent's config
- Given the shared MCP server is running
- When the wrapper injects the MCP config for the launched agent
- Then the agent's own config file gains a `recall-engine` server entry
  (`.mcp.json` for claude, `.codex/config.toml` for codex, `.pi/mcp.json` for
  pi, `.gemini/settings.json` for gemini, `opencode.json` for opencode, and
  `.agents/mcp_config.json` for agy)
- And the entry carries the server URL, an `X-Recall-Repo` header naming the
  resolved repo, and an `X-Recall-Token` header
- And the entry is removed when the session ends

> Note: codex reads its `.codex/config.toml` only for trusted projects; an
> untrusted project must be trusted (first-run prompt) before codex sees the
> entry. Verified: codex 0.144.1 loads the project-local file and forwards the
> custom headers.

Scenario: preserve an existing MCP config
- Given the agent's MCP config file already exists with other servers
- And it was not created by the active wrapper session
- When the wrapper injects its server entry
- Then only the `recall-engine` entry is added
- And the file's original content is restored after the session ends

Scenario: tolerate a commented or empty pre-existing config
- Given the agent's config file exists but is empty or contains JSONC comments
  (gemini/opencode accept `//` and `/* */` comments)
- When the wrapper injects its server entry
- Then injection succeeds
- And the file is restored byte-identical when the session ends

Scenario: patch an outdated entry on a new wrap
- Given an earlier version injected the `recall-engine` entry without a field
  the current version adds (e.g. pi's `lifecycle`)
- When a new wrap session registers the MCP server
- Then the entry is re-asserted so the missing field is patched in
- And no duplicate backup is created for an already-registered config

Scenario: refuse an unparseable pre-existing config
- Given the agent's config file is malformed (invalid JSON/TOML) or is not an
  object/table
- When the wrapper tries to inject its server entry
- Then it fails with a clear message naming the file and exits `1`
- And the original file is left untouched with no stray backup
- And the skill it already injected is torn down

Scenario: preserve an existing user skill
- Given `recall-engine/` already exists in the SSOT dir or in an
  agent-specific skills dir
- And it was not created by the active wrapper session
- When the wrapper injects its skill
- Then the existing entry is moved to a backup location
- And the original entry is restored after the session ends

Scenario: attach to a live wrapper session in the same directory
- Given the marker file records a live owner and the skill is injected
- When another wrap session starts in the same directory with the same repo
- Then it attaches by adding its process id to the marker
- And it reuses the existing injection instead of re-injecting
- And the last session to exit tears the injection down

Scenario: refuse attach on a different repo
- Given a live wrapper session for one repo
- When another wrap session starts in the same directory for a different repo
- Then it fails without disturbing the existing session

Note: wrap sessions in different directories are independent; the marker,
lock, and injected skill are all anchored per project directory, so sessions
started from separate directories never block or attach to each other.

Scenario: recover stale wrapper state
- Given the marker file records a dead process id
- When a new wrap session starts
- Then the CLI restores the stale skill state first
- And the new wrap session may continue

Scenario: manually clean stale wrapper state
- Given leftover wrapper state exists
- When the user runs `recall-engine unwrap`
- Then the CLI restores or removes the injected skill state
- And the output reports whether anything was cleaned

Scenario: clean leftovers from a pre-SSOT version
- Given a marker from an older version exists at
  `.claude/skills/.recall-engine-marker.json`
- And no marker exists at the SSOT location
- When the CLI restores skill state
- Then the legacy skill state is cleaned as well

## Feature: Share One MCP Server Across Sessions

One streamable-HTTP MCP server per machine serves every wrap session and every
repo. It exposes `search_knowledge(query)`, `read_note(path)`, and
`list_notes()` as read-only tools, and is started on demand rather than run as a
user-managed daemon.

Scenario: start the server on the first wrap
- Given no recall-engine MCP server is running
- When the user runs `recall-engine wrap <agent>`
- Then the CLI spawns a detached server bound to `127.0.0.1` on an ephemeral port
- And it records the pid, port, url, token, and owner pid in
  `/tmp/recall-engine-mcp-<uid>.json`
- And it waits until the server accepts connections before launching the agent

Scenario: reuse the server for a later wrap
- Given a recall-engine MCP server is already running and reachable
- When a second `wrap` starts, in any directory and against any repo
- Then no second server is spawned
- And the second session's pid is added to the state file's `owners`

Scenario: route each connection to its own repo
- Given two wrap sessions share one server with different knowledge repos
- When an agent calls `search_knowledge`
- Then the server resolves the repo from the connection's `X-Recall-Repo` header
- And results only ever come from that session's repo

Scenario: reject an unconfigured client
- Given the server was started with a token
- When a client calls a tool without the matching `X-Recall-Token` header
- Then the tool call fails with an authorization error
- And a call whose `X-Recall-Repo` header names a directory without `src/` is
  rejected the same way

Scenario: stop the server with the last session
- Given two sessions own the running server
- When the first session exits
- Then the server keeps running and only that pid leaves `owners`
- And when the last owner exits, the server is terminated
- And the state file is removed

Scenario: reclaim a stale server record
- Given the state file records a server whose process is dead or not listening
- When the user runs the next `wrap`
- Then the stale record is replaced by a freshly spawned server

Scenario: reach the server from pi
- Given `pi` is installed without the `pi-mcp-adapter` extension
- When the user runs `recall-engine wrap pi`
- Then `wrap` refuses to launch and tells the user to install the adapter first
- And `doctor` also advises installing it

## Feature: Use the Knowledge Base During Agent Sessions

The injected skill must guide the agent to consult the Markdown knowledge base
of existing processing records, notes, and prior experience before replying to
any user message — especially messages about troubleshooting, past work,
lessons learned, decisions, or terminology.

Scenario: answer with knowledge-base context
- Given an agent is running through `recall-engine wrap <agent>`
- And the user sends any message
- When the agent prepares the reply
- Then the skill instructs the agent to call `search_knowledge` (and
  `read_note` / `list_notes` as needed) for existing processing records, notes,
  or prior handling of similar problems
- And any prior experience found should be factored into the reply and cite the
  note path returned by the tools

Scenario: answer when no matching note exists
- Given the agent called `search_knowledge`
- And no relevant note was found
- When the agent answers the user
- Then it should say no relevant knowledge-base entry was found
- And it may continue with general knowledge

Scenario: skip the search for a trivial message
- Given the user sends a bare greeting or acknowledgement with no
  searchable keywords (e.g. "hi", "thanks")
- When the agent prepares the reply
- Then the skill allows a reply without searching the knowledge base

Scenario: avoid unintended edits
- Given the agent uses the knowledge repo as reference material
- When the user has not asked to edit the knowledge repo
- Then the agent must not modify knowledge repo files

## Feature: Sync from Google Drive to the Knowledge Repo

`sync download` must copy supported Drive files into `<repo>/src/`.

Scenario: download Markdown files
- Given `KNOWLEDGE_DRIVE_FOLDER` resolves to a Drive folder
- And the folder contains plain `.md` files
- When the user runs `recall-engine sync download`
- Then the CLI writes those files into `<repo>/src/`
- And local files with the same name are overwritten
- And unsupported file types are skipped

Scenario: export a native Google Doc
- Given the Drive folder contains a native Google Doc
- When the user runs `sync download`
- Then the CLI exports the document as `text/plain`
- And it saves the result as `<doc name>.md`
- And it removes a leading UTF-8 BOM
- And it normalizes CRLF line endings to LF

Scenario: handle duplicate target names
- Given multiple Drive files map to the same target filename
- When the user runs `sync download`
- Then the file with the newest `modifiedTime` wins
- And the CLI prints a warning

## Feature: Sync from the Knowledge Repo to Google Drive

`sync upload` must copy top-level Markdown files from `<repo>/src/` to Drive.

Scenario: create or update Drive Markdown files
- Given `<repo>/src/` contains top-level `.md` files
- When the user runs `recall-engine sync upload`
- Then the CLI uploads those files to the configured Drive folder
- And existing Drive files with the same name are updated
- And missing Drive files are created
- And uploads remain plain Markdown files, not native Google Docs

Scenario: reject an empty upload source
- Given `<repo>/src/` does not exist or contains no `.md` files
- When the user runs `sync upload`
- Then the command fails with a Drive sync error

## Feature: Resolve the Google Drive Folder

`KNOWLEDGE_DRIVE_FOLDER` may be either a folder ID or a folder name.

Scenario: use a folder ID
- Given `KNOWLEDGE_DRIVE_FOLDER` is a Drive folder ID
- When sync resolves the folder
- Then the CLI uses that ID directly

Scenario: use a unique folder name
- Given `KNOWLEDGE_DRIVE_FOLDER` is a folder name
- And exactly one matching folder exists
- When sync resolves the folder
- Then the CLI uses that folder's ID

Scenario: reject an ambiguous folder name
- Given several Drive folders match the configured name
- When sync resolves the folder
- Then the command fails
- And the output asks the user to set the folder ID instead

## Feature: Authenticate to Google Drive

Drive commands must use credentials that can access the Drive API.

Scenario: use Application Default Credentials
- Given Application Default Credentials are available with Drive scope
- When the CLI builds the Drive client
- Then it uses those credentials

Scenario: fall back to gcloud user credentials
- Given Application Default Credentials are unavailable
- And `gcloud auth print-access-token` returns a token
- When the CLI builds the Drive client
- Then it uses that token for the current command

Scenario: explain missing Drive credentials
- Given no usable Drive credentials exist
- When sync or doctor checks Drive access
- Then the command fails with actionable `gcloud auth` login commands
- And it does not print a Python traceback

Scenario: explain insufficient Drive scope
- Given Drive returns an insufficient-scope error
- When sync or doctor checks Drive access
- Then the command fails with actionable `gcloud auth` login commands
- And it does not print a Python traceback

## Feature: Diagnose Local Setup

`doctor` must report local readiness one check at a time.

Scenario: all required checks pass
- Given git, at least one agent CLI, SSH key, repo config, and Drive access
  are available
- When the user runs `recall-engine doctor`
- Then each required check prints `[ok]`
- And the command exits successfully

Scenario: report each agent CLI individually
- Given some of `claude`, `codex`, `pi`, `gemini`, `opencode`, and `agy` are installed
- When the user runs `doctor`
- Then each installed agent prints `[ok]`
- And each missing agent prints `[skip]`
- And the agent check passes when at least one agent is installed

Scenario: no agent CLI installed
- Given none of `claude`, `codex`, `pi`, `gemini`, `opencode`, and `agy` is on `PATH`
- When the user runs `doctor`
- Then the agent check prints `[fail]`
- And the output includes install instructions for the supported agents
- And the command exits with code `1`

Scenario: a required check fails
- Given any required dependency is missing or invalid
- When the user runs `doctor`
- Then that check prints `[fail]`
- And the output includes a concrete fix instruction
- And the command exits with code `1`

Scenario: report a reachable MCP server
- Given a wrap session has started the shared MCP server
- When the user runs `doctor`
- Then the MCP server check prints `[ok]` with the server URL and owner count

Scenario: no MCP server is running
- Given no wrap session is active
- When the user runs `doctor`
- Then the MCP server check prints `[skip]`
- And that skip does not make doctor fail

Scenario: report a stale MCP server record
- Given the state file records a server that is not reachable
- When the user runs `doctor`
- Then the MCP server check prints `[fail]`
- And the fix instruction names `recall-engine unwrap`
- And the command exits with code `1`

Scenario: Drive folder is not configured
- Given `KNOWLEDGE_DRIVE_FOLDER` is unset
- When the user runs `doctor`
- Then the Drive folder check prints `[skip]`
- And that skip does not make doctor fail

## Out of Scope for v1

- LLM proxying or traffic interception
- A user-managed daemon lifecycle (`start` / `stop` commands, autostart on
  boot). The shared MCP server is in scope, but it is spawned on demand by
  `wrap` and stops with the last session.
- Two-way Drive merge
- Delete propagation between Drive and the repo
- Recursive Drive sync
- Converting local `.md` uploads into native Google Docs
- CLI aliases such as `recall` or `rengine`
- Agent support beyond `claude`, `codex`, `pi`, `gemini`, `opencode`, and `agy`

## Acceptance Checks

Automated:
- `uv run pytest`
- `uv run ruff check src tests`

Manual:
- `recall-engine --help` lists `wrap`, `unwrap`, `sync`, and `doctor`
- A wrapped agent session reads and cites a relevant file from `<repo>/src/`
- Drive download writes plain `.md` files and exported Google Docs into
  `<repo>/src/`
- Drive upload creates or updates plain Markdown files in the configured Drive
  folder
