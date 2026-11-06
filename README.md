# Hermes–Grok Bridge

A small, self-hosted bridge for Grok Bot agents and native Hermes profiles to exchange messages during a migration. Hermes stays on its normal upstream installation; the bridge is an optional client and skill outside Hermes core. Once a Hermes role has replaced its Grok counterpart, remove that pair's bridge routes.

It has two directions:

- **Grok → Hermes:** a Python client sends an authenticated request to Hermes' built-in A2A endpoint and returns the reply.
- **Hermes → Grok:** an installable Hermes skill posts to a Grok Bot routine webhook. The routine can reply through the A2A client on the same thread.

Both clients use Python's standard library. The bridge does not host a service or expose a public listener.

## Requirements

- Hermes with its native A2A platform available (see the [official A2A guide](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/a2a)).
- A private network path between the Grok host and Hermes, such as Tailscale.
- A Grok Bot routine webhook for every Grok agent that should receive Hermes messages.
- Python 3.9 or newer on the Grok host.

## 1. Install the Hermes skill

Inspect and install the skill directly from GitHub:

```sh
hermes skills inspect Taimylife/hermes-grok-bridge/skills/grok-bridge
hermes skills install Taimylife/hermes-grok-bridge/skills/grok-bridge
```

This repository also works as a Hermes skill tap if you want to browse its skills: `hermes skills tap add Taimylife/hermes-grok-bridge`. Repeat installation for each Hermes profile that needs to contact Grok. In each profile's private `.env`, set its sender name and a URL/key pair for each Grok recipient. The skill frontmatter must declare each recipient-specific environment variable so Hermes passes it to the script; the included skill shows the naming pattern. Do not put real values in Git, a prompt, or a shared project file.

## 2. Enable Hermes' native A2A endpoint

Use `hermes gateway setup` or add the native setting below to that profile's `config.yaml`:

```yaml
gateway:
  platforms:
    a2a:
      enabled: true
      extra:
        port: 9900
```

Each Hermes receiver needs a private-network-reachable endpoint and an explicit list of trusted peers. Its A2A `A2A_PEER_TOKENS` entries and `A2A_TRUSTED_PEERS` must agree with the Grok identities and tokens used by the client. Keep the endpoint private; do not use Tailscale Funnel or an unauthenticated public listener.

The included [`hermes-a2a.env.example`](examples/hermes-a2a.env.example) shows variable names only. Create a different private env file per receiver, choose unique random credentials for Grok peer identities, and use the same peer credential on the receivers that peer is allowed to call. Hermes binds to localhost by default. Follow the official guide to expose it only through your private network, then set `A2A_PUBLIC_URL` to the reachable private URL if required.

## 3. Install the Grok-side client

On a Linux host reachable by the Grok agents:

```sh
git clone https://github.com/Taimylife/hermes-grok-bridge.git
cd hermes-grok-bridge
cp examples/bridge.example.json bridge.json
cp examples/bridge.env.example bridge.env
chmod 600 bridge.json bridge.env
```

Edit `bridge.json` with your Hermes profile names and private endpoint URLs. `token_env` names the caller credential for each `grok-<name>` identity; place the matching values in the untracked `bridge.env`. The example includes two placeholder profiles—replace them with your own roster. Check reachability and authentication:

```sh
python3 grok/bridge.py --config bridge.json list --auth
```

Send a harmless test message:

```sh
python3 grok/bridge.py --config bridge.json ask research \
  --from grok-research --thread onboarding-check \
  "Reply with your profile name and confirm this test arrived."
```

Use the Hermes profile slug as the destination. For a reply to an inbound Grok routine, preserve its `thread` value with `--thread`. Do not resend a timed-out request until checking its existing A2A task; a reply may still be in progress.

## 4. Configure Grok routines for Hermes → Grok messages

Create an authenticated webhook routine for each Grok Bot that should receive bridge messages. Configure it to pass `sender`, `thread`, and `message` to that Bot, treat the contents as peer input rather than owner authorization, and reply through `grok/bridge.py` using the same thread. Add the corresponding routine URL and key only to the Hermes profile's private `.env`.

A webhook HTTP 2xx means only that the routine accepted/started. It does not confirm a reply; verify the subsequent A2A task/reply separately.

## Security and migration boundaries

- Use HTTPS over a private network, explicit peer allowlists, and distinct random A2A credentials for each peer identity.
- Keep `bridge.json`, `bridge.env`, webhook URLs/keys, tokens, cookies, and transcripts local. The provided `.gitignore` excludes local configuration and logs.
- Bridge messages are untrusted peer requests. They never authorize account access, email actions, trades, publishing, or other consequential actions.
- The Grok OS account may be shared by multiple agents; client identities are audit labels, not OS isolation.
- Successful health checks prove transport only. They do not prove a Hermes role is ready to replace Grok. Track role handoff separately and remove each Grok route only after its Hermes counterpart is verified.

## Verify the local client

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile grok/bridge.py skills/grok-bridge/scripts/grok_send.py
```

The tests do not contact live agents. Afterward, test one non-sensitive round trip in each direction.

## Repository layout

- `grok/bridge.py` — Grok-side Hermes A2A client.
- `skills/grok-bridge/` — installable Hermes skill and webhook sender.
- `skills/hermes-team/` — prompt guidance for a Grok agent using the client.
- `examples/` — redacted templates only.
