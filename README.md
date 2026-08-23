# Markovo CLI and MCP server

[Markovo](https://markovo.net) turns supported files and explicitly
authorized public HTTPS pages into clean, structured Markdown. For the
dedicated web experience and product details, visit the
[PDF to Markdown Converter](https://markovo.net/pdf-to-markdown).

This public repository is the distribution source for the customer-side
`markovo` CLI and the `markovo-mcp` stdio MCP server. It contains no local
conversion engine, backend service, deployment configuration, production
credentials, or billing implementation. Conversion runs through the
account-metered service at `https://markovo.net`.

## Install

```bash
pip install markovo
```

Or run it without a permanent installation:

```bash
uvx markovo --help
uvx --from markovo markovo-mcp
```

## API key and Credits

1. Create an API key in the
   [Developer portal](https://markovo.net/app#developer).
2. Store it in your shell or MCP host secret store; do not paste it into
   prompts or commit it to source control.
3. Verify the setup:

```bash
export MARKOVO_API_KEY="your-key"
markovo doctor --json
```

Every conversion requires an explicit Credit ceiling. When a key is
missing, the client points to the Developer portal. When Credits are
insufficient, the service response points to the
[billing page](https://markovo.net/app#billing) so the account can be
topped up before retrying.

## MCP configuration

### Remote MCP (recommended for hosted agents)

Connect the Streamable HTTP endpoint:

```text
https://markovo.net/mcp
```

The MCP host discovers Markovo's OAuth 2.1 metadata, opens the Markovo
account sign-in and consent screen, and stores its own scoped token. You
do not create, paste, or share an API key for this remote connection.
Remote tools can inspect capabilities, Credits, and owned jobs; convert
one explicitly supplied public HTTPS page with a required
`max_credit_units` ceiling; and create or revoke short-lived result-image
links. Remote MCP cannot read local files. Use the stdio package below or
REST multipart upload when a local file must be converted.

Markovo never tops up Credits or changes a plan automatically. A low
balance response points to https://markovo.net/app#billing and waits for
the account owner to act.

### Local stdio MCP

```json
{
  "mcpServers": {
    "markovo": {
      "command": "uvx",
      "args": ["--from", "markovo", "markovo-mcp"],
      "env": {
        "MARKOVO_API_KEY": "${MARKOVO_API_KEY}",
        "MARKOVO_MCP_ROOT": "/path/to/safe/project"
      }
    }
  }
}
```

MCP clients should inject `MARKOVO_API_KEY` through their secret or
environment-variable settings. The key and service origin are never MCP
tool arguments, so prompts cannot redirect the credential. The public
client accepts only `https://markovo.net` and does not follow authenticated
redirects.

The MCP server can read and write only within the required
`MARKOVO_MCP_ROOT`. Set it to the smallest dedicated project directory
your MCP host needs. If it is missing, the server refuses all file tools;
parent-directory and symlink escapes are rejected.

```bash
export MARKOVO_MCP_ROOT="/path/to/safe/project"
```

Structured errors point to the Developer portal when a key is missing and
to Billing when Credits are insufficient.

## Links

- [Markovo home](https://markovo.net)
- [PDF to Markdown](https://markovo.net/pdf-to-markdown)
- [Documentation](https://markovo.net/docs)
- [Remote MCP guide](https://markovo.net/docs/mcp)
- [Pricing and Credits](https://markovo.net/pricing)
- [Developer portal and API keys](https://markovo.net/app#developer)
- [Billing and top-up](https://markovo.net/app#billing)

## Security

Report security issues privately using the instructions in
[SECURITY.md](https://github.com/fisher-byte/markovo/blob/main/SECURITY.md).
Do not include API keys, customer documents,
or private conversion results in a public issue.

<!-- mcp-name: io.github.fisher-byte/markovo -->
