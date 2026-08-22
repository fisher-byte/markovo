# Security policy

Please do not report security vulnerabilities in a public issue and do not
include API keys, customer files, private conversion results, or account data in
any report.

Report a vulnerability through GitHub's **Security** tab by selecting
**Report a vulnerability** for this repository. If private vulnerability
reporting is temporarily unavailable, use the support contact published on
[markovo.net](https://markovo.net) and clearly request a private security
channel before sharing technical details.

API keys should only be stored in environment variables or an MCP client's
secret store. If a key is exposed, revoke or rotate it immediately in the
[Developer portal](https://markovo.net/app#developer).

The public client accepts authenticated API traffic only to
`https://markovo.net` and refuses redirects for those requests. MCP tool
schemas do not accept API keys or service-origin overrides. MCP file tools also
require an explicit `MARKOVO_MCP_ROOT` and reject paths or symlinks that escape
that dedicated directory.
