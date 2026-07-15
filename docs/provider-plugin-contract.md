# Provider and plugin contract

Phase 7 publishes a reviewable **provider contract**, not a general executable-plugin loader.
Third-party executable plugins remain deferred because installation alone must never grant filesystem,
mailbox, credential, or commit authority.

The v1 manifest schema is [`schemas/provider-plugin-v1.schema.json`](schemas/provider-plugin-v1.schema.json).
The matching Python model is `ProviderPluginManifestV1`. A provider may estimate a compiled prompt and
return findings matching AIOrganizer's strict structured output. It must declare whether it uses the
network, whether it is a cloud provider, and the content classes it can receive.

Provider implementations:

- receive a compiled prompt and bounded evidence selected by the desktop application;
- return data only—never callbacks, scripts, commands, paths, or approval state;
- do not read the workspace database, OS keyring, arbitrary files, or environment variables directly;
- do not implement apply, approve, delete, send, shell, or filesystem operations;
- use the application's credential and provider-request preview boundaries;
- preserve stable schema behavior within API version 1.

Built-in transports remain separate adapters:

- OpenAI uses the native Responses API and strict JSON Schema output.
- Anthropic uses the native Messages API and its structured-output configuration.
- Codex uses the local app-server/MCP integration and never requires an inbound listener.
- DeepSeek V4 uses OpenAI-compatible Chat Completions with thinking explicitly disabled for
  `tool_choice` rounds.
- OpenRouter uses its OpenAI-compatible Chat endpoint and model-qualified IDs. AIOrganizer supplies
  inventory and bounded web tools itself; OpenRouter's optional server-side web plugin is not required.

Tool-driven views check adapter capabilities (`audit_inventory`, `plan_folders`, and
`research_updates`) instead of branching on provider names. The shared OpenAI-Chat-compatible loop is a
transport base used by sibling DeepSeek and OpenRouter adapters; native OpenAI, Anthropic, and Codex
adapters do not inherit provider-specific compatibility flags.

The runtime does not dynamically discover or import third-party manifests yet. Adding a loader requires
separate threat modeling, provenance/signature policy, isolation decisions, dependency-conflict handling,
disable/recovery UX, and adversarial conformance tests. The published contract lets providers be reviewed
and developed without prematurely granting that authority.
