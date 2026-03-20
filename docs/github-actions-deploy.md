# GitHub Actions Auto Deploy

This repository is set up to deploy from the `prod` branch using GitHub Actions.

## Branch Model

- `upstream/main`: official `HKUDS/nanobot`
- `origin/dev`: day-to-day development
- `origin/prod`: deployment branch
- `wip/local-custom-20260319`: preserved local migration branch

Recommended flow:

```bash
git checkout dev
# hack, test, commit
git push origin dev

git checkout prod
git merge --ff-only dev
git push origin prod
```

Pushing to `prod` triggers `.github/workflows/deploy.yml`.

## Automated Server Setup

Run this once on the server:

```bash
wget https://raw.githubusercontent.com/dufangshi/nanobot/prod/deploy_setup.sh
chmod +x deploy_setup.sh
sudo ./deploy_setup.sh
```

The script installs Docker/Git, generates both SSH key pairs, initializes the deployment directory, creates `~/.nanobot`, and prints everything you need to paste into GitHub.

## GitHub Secrets

Add these repository secrets in GitHub:

- `DEPLOY_HOST`: server IP or hostname
- `DEPLOY_PORT`: SSH port, for example `22`
- `DEPLOY_USER`: SSH user
- `DEPLOY_PATH`: deployment path on server, for example `/opt/nanobot`
- `DEPLOY_SSH_KEY`: private key used by GitHub Actions to SSH into the server

The setup script also prints a GitHub deploy key for server-side `git fetch` / `git pull`.

## First Deployment

After adding the deploy key and Actions secrets, trigger the workflow by pushing to `prod` or running it manually from GitHub Actions.

On first deployment, the workflow will:

- fetch the repo into the prepared server path
- auto-run `nanobot onboard` if `~/.nanobot/config.json` does not exist
- build and start `nanobot-gateway`

Then review and edit the server-side config:

```bash
vim ~/.nanobot/config.json
docker compose -f /opt/nanobot/docker-compose.yml restart nanobot-gateway
```

If you use an external `orchestration-mcp` sidecar with `claude_code` from the Dockerized gateway, add `CLAUDE_CODE_BUBBLEWRAP=1` to the server `.env` and also pass the same variable in `~/.nanobot/config.json` under `tools.mcpServers.orchestration-mcp.env`. This keeps Claude Code permission bypass available when the gateway container runs as `root`.

## Notes

- Runtime secrets stay on the server in `~/.nanobot/config.json`; do not commit them.
- The deploy workflow assumes Docker and Docker Compose are already installed on the server.
- If you also want to deploy custom sidecars like `orchestration-mcp`, add them to the server compose stack before enabling auto deploy.
- The server setup script is [`deploy_setup.sh`](/Users/fonsh/PycharmProjects/Treer/nanobot/deploy_setup.sh).
