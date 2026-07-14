# ADR-007: Provider abstraction and embedded Codex

Status: accepted

Provider adapters consume one provider-neutral compiled prompt and return schema-
validated proposals. OpenAI Responses and Anthropic Messages use API keys held in
the OS credential store. Codex subscription access uses a compatible installed
runtime first and the Python SDK's pinned runtime as fallback. Codex runs in an
empty working directory, read-only sandbox, deny-all approval mode, disabled web
search and shell network, with only AIOrganizer's proposal MCP configured.
