Tool declarations captured from the current local sibling repositories on 2026-10-20:

* Remnawave `9b54b795037b992ebe5d0c654ad8e0df47329b24`, support mode:
  `tools.json` with the `support-hwid.json` overrides, 17 declarations.
* Bedolaga `c3ddbf2fae9fa87e9b5f9c6dff1f6a2dfd684c75`, 8 declarations.

The optional external-MCP-binary acceptance test was removed. Frozen tool
schemas remain for isolated router/identity unit tests; no sibling executable
or real backend is required.

The bot's owner profile exposes 10 Remnawave tools (9 in read-only mode) and 8
Bedolaga tools. Foreign internal Remnawave user IDs require a fresh lookup by
the trusted Telegram caller; interrupted HWID writes are never replayed. These
are documented safety changes from the Python baseline.
