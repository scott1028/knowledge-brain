---
name: recall-engine
description: Use at the start of every conversation. Invoke this skill before any response, including clarifying questions, whenever there is any possibility that it may apply.
---

# Recall Engine

The knowledge base is served by the **recall-engine MCP server**, which exposes
these tools:

- `search_knowledge(query)` — search the knowledge base and return matching
  note paths with snippets.
- `read_note(path)` — read the full text of a matching note.
- `list_notes()` — list available note file names (to discover relevant files).

Treat the knowledge base as the authoritative record of past processing, notes,
decisions, and prior experience handling similar problems. The server reads the
knowledge repo directly, so you never need filesystem access to it.

## Before replying

Before replying to ANY user message — whether you recognize it or not —
search the knowledge base for existing processing records, notes, or prior handling
of similar problems. Never reply "I don't know" or answer from general knowledge
before searching:

1. Extract keywords from the message.
2. Call `search_knowledge` with those keywords (call `list_notes` to discover
   relevant file names when a keyword search is inconclusive).
3. Call `read_note` on the matches and factor any prior experience into your reply.

Treat these as strong signals to search — but never limit the search to them:

- Troubleshooting: errors, bugs, incidents, root-cause analysis, workarounds.
- Past experience: past work, prior experience, lessons learned, past learning
  experience, retrospectives, how something was done before.
- Decisions: decisions, trade-offs, and their rationale.
- Terminology: terms, names, concepts, definitions, acronyms, jargon.
- Processes: procedures, how-tos, runbooks, setup, configuration, guides.
- Conventions: standards, best practices, guidelines, policies.
- Technical entities: any specific protocol, system, tool, library, framework,
  API, service, or technology, and internal team knowledge.

Skip the search only for trivial messages that carry no searchable keywords
— bare greetings, acknowledgements, or thanks (e.g. "hi", "thanks", "ok").
When in doubt, search.

## Reply rules

- Prefer knowledge-base content (past experience) over general knowledge
  whenever they overlap.
- Cite the source: quote the note path returned by the tools (e.g. the `path`
  field of a `search_knowledge` result) in the reply.
- If nothing relevant is found, say so explicitly, then reply from
  general knowledge.

## Constraints

- These tools are read-only. Do not attempt to modify the knowledge repo.
