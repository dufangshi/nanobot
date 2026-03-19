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

## GitHub Secrets

Add these repository secrets in GitHub:

- `DEPLOY_HOST`: server IP or hostname
- `DEPLOY_PORT`: SSH port, for example `22`
- `DEPLOY_USER`: SSH user
- `DEPLOY_PATH`: deployment path on server, for example `/opt/nanobot`
- `DEPLOY_SSH_KEY`: private key used by GitHub Actions to SSH into the server

## Server Bootstrap

Run once on the server:

```bash
mkdir -p /opt/nanobot
git clone -b prod https://github.com/dufangshi/nanobot.git /opt/nanobot
cd /opt/nanobot

mkdir -p ~/.nanobot
docker compose run --rm nanobot-cli onboard
```

Then edit the server-side config:

```bash
vim ~/.nanobot/config.json
```

After that, the workflow can update and restart the gateway automatically.

## Notes

- Runtime secrets stay on the server in `~/.nanobot/config.json`; do not commit them.
- The deploy workflow assumes Docker and Docker Compose are already installed on the server.
- If you also want to deploy custom sidecars like `orchestration-mcp`, add them to the server compose stack before enabling auto deploy.
