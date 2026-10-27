# GitHub MCP compatibility fixture

`github_mcp_v2_0_3.json` contains the original input schemas discovered over MCP
stdio from the official `github/github-mcp-server` v2.0.3 Windows x86_64 release.
It contains no user credentials, repository content or API responses.

Source: https://github.com/github/github-mcp-server/releases/tag/v2.0.3

Archive SHA-256: `f0e1b9880a1b88730d1250d1c25cfee39617ec3e800a7a252f2279cd0c8394e0`.

The captured schemas retain upstream parameter definitions and descriptions.
The upstream MIT license is reproduced in `github-mcp-server.LICENSE`.
Tests use this fixture offline; they do not download or launch the server.
