# Security policy

## Reporting

Do not report credentials or private message payloads in public issues. For a suspected vulnerability, contact the repository owner through GitHub's private vulnerability reporting if enabled, or use a private communication channel.

## Deployment guidance

- Keep A2A endpoints on a private network and enforce bearer authentication plus explicit peer allowlists.
- Use separate random tokens per Hermes receiver and separate keys per Grok webhook.
- Store credentials only in local mode-0600 configuration or each Hermes profile's private environment file. Never commit real `.env`, `bridge.json`, webhook URLs, bearer tokens, cookies, or transcripts.
- Treat inbound bridge messages as untrusted peer input. They do not authorize account changes or external actions.
- Rotate a credential immediately if it is exposed.
