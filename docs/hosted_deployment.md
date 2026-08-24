# Hosted Deployment (AWS EC2)

A concrete, end-to-end walkthrough for running this proxy as a hosted HTTPS endpoint on a single AWS EC2 instance, fronted by Caddy for TLS. This is the worked example behind the README's [Production ingress checklist](../README.md#production-ingress-checklist).

For the general security model (localhost binding, bearer auth, no public proxy port), see that checklist — it is not repeated in full here.

---

## Architecture

```
Claude Code client → HTTPS :443 → Caddy (TLS) → 127.0.0.1:4000 → LiteLLM → OpenRouter (primary)
                                                                        └→ GitHub Copilot (fallback)

```

- One small EC2 instance runs two containers: the LiteLLM proxy (bound to localhost only) and Caddy (terminates TLS, forwards to the proxy).
- Auth is enforced by LiteLLM via `LITELLM_MASTER_KEY`. Caddy does **not** add auth — the proxy is the gatekeeper.
- The proxy is **dual-provider**: OpenRouter is the primary upstream (`OPENROUTER_API_KEY` in `.env`), and GitHub Copilot is the automatic fallback (OAuth token cached on the host and mounted read-write).

---

## Prerequisites

- An AWS account, a registered domain, a GitHub Copilot subscription, and an [OpenRouter](https://openrouter.ai) account with an API key.
- A subdomain you can point at the instance (e.g. `proxy.example.com`).
- Familiarity with the README's [Production ingress checklist](../README.md#production-ingress-checklist).

---

## 1. Provision the instance

- **EC2:** a small instance (e.g. `t3.small`) running Ubuntu LTS. Attach an **encrypted** root volume of **at least 20 GiB** — Docker image builds are disk-heavy and an 8 GiB volume fills quickly across rebuilds.
- **Elastic IP:** allocate and associate one so the public IP is stable across stop/start. Point your subdomain's DNS `A` record at it, and confirm it resolves before requesting a certificate (Caddy's ACME challenge depends on it).
- **Security group:** follow the README ingress checklist — allow `443/tcp` (and `80/tcp` for ACME issuance / HTTP→HTTPS redirects); do **not** expose the proxy port (`4000`); prefer **no inbound SSH**.
- **IAM instance role:** attach a role granting:
  - `AmazonSSMManagedInstanceCore` — for Session Manager shell access (so you never open SSH).
  - Scoped **read** of your proxy secret in Parameter Store, e.g. `ssm:GetParameter` / `ssm:GetParameters` on `arn:aws:ssm:<region>:<account>:parameter/<your-prefix>/*`.
  - *(Optional)* scoped ECR push/pull if you back up the image (see §8).

Use AWS Systems Manager **Session Manager** for all shell access instead of SSH. A freshly launched instance can take a few minutes (and sometimes a reboot) to register with SSM before the **Connect** button is available.

---

## 2. Install runtime and clone

On the instance (via Session Manager, as root — `sudo su -`):

```bash
apt-get update && apt-get install -y ca-certificates curl gnupg git make
# Install Docker Engine + Compose plugin using the official Docker apt repo
# (see docs.docker.com for the current steps for your Ubuntu release).

cd /opt
git clone https://github.com/arndvs/llm-gateway
cd llm-gateway

```

The AWS CLI is **not** installed on Ubuntu by default. If you want to read Parameter Store from the box, install AWS CLI v2 and verify the instance role:

```bash
cd /tmp
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o awscliv2.zip
unzip -o awscliv2.zip && ./aws/install      # use sudo if not root
aws sts get-caller-identity --region <region>   # expect the instance role ARN

```

---

## 3. Generate secrets

```bash
cd /opt/llm-gateway
umask 077
make setup            # generates .env with a random LITELLM_MASTER_KEY (sk-…)
chmod 600 .env

```

> `make` **on minimal Ubuntu + dash:** some Make recipes assume `bash`. If `make setup` errors with a `dash` syntax error, generate `.env` by hand:
>
> ```bash
> umask 077
> python3 -c "import uuid; open('.env','w').write('LITELLM_MASTER_KEY=sk-'+str(uuid.uuid4())+'\nLITELLM_PORT=4000\nLITELLM_LOCAL_MODEL_COST_MAP=true\n')"
> chmod 600 .env
> curl -LsSf https://astral.sh/uv/install.sh | sh
> export PATH="$HOME/.local/bin:$PATH"
>
> ```

Add your OpenRouter API key to `.env`:

```bash
echo 'OPENROUTER_API_KEY=sk-or-…' >> .env
chmod 600 .env
```

Store the master key in a managed secret store rather than leaving it only on disk. With AWS Parameter Store (run from **CloudShell** or anywhere with write access — the instance role is intentionally read-only):

```bash
aws ssm put-parameter \
  --name "/<your-prefix>/auth-key" \
  --value "sk-…" \
  --type SecureString --overwrite --region <region>

```

The instance role can then read it back; the on-disk `.env` stays `chmod 600`.

---

## 4. Authenticate Copilot once, on the host

The proxy is dual-provider: **OpenRouter is the primary upstream**, so it needs only the API key in `.env`. **GitHub Copilot is the automatic fallback**, so its OAuth token must be obtained once on the host and mounted into the container.

**Copilot OAuth (fallback).** The device-code flow needs a browser and writes a token cache the container later mounts:

```bash
set -a && . ./.env && set +a
UV_NATIVE_TLS=true uv run --with "litellm[proxy]" \
  litellm --config litellm_config.yaml --port 4000

```

LiteLLM prints a GitHub device URL and code. Open the URL in a browser, enter the code, approve access. Once it starts serving, stop it with **Ctrl-C**. Confirm the token cached:

```bash
ls -la ~/.config/litellm/github_copilot   # expect access-token and api-key.json

```

If the token ever expires, delete that directory and repeat this step:

```bash
rm -rf ~/.config/litellm/github_copilot
# then re-run the uv run command above

```

**OpenRouter key (fallback).** Add it to `.env` and verify it works:

```bash
echo 'OPENROUTER_API_KEY=sk-or-…' >> .env
chmod 600 .env
set -a && . ./.env && set +a
curl -s https://openrouter.ai/api/v1/models \
  -H "Authorization: Bearer $OPENROUTER_API_KEY" | head -c 200

```

If either credential is rotated, update `.env` / redo the OAuth and recreate the container (see §7 — a restart reuses the old environment).

---

## 5. Run the proxy container

> **Mount the Copilot token read-write (**`:rw`**).** LiteLLM refreshes the Copilot token periodically and must write the new value back to `api-key.json`. A read-only (`:ro`) mount causes requests to start failing with a `Read-only file system … api-key.json` error once the token needs refreshing.

```bash
docker build \
  --build-arg BUILD_SHA=$(git rev-parse --short HEAD) \
  --build-arg BUILD_TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ) \
  -t llm-gateway-proxy:latest .

docker run -d --name proxy --restart unless-stopped \
  --env-file .env \
  -v "$HOME/.config/litellm/github_copilot:/root/.config/litellm/github_copilot:rw" \
  -p "127.0.0.1:4000:4000" \
  llm-gateway-proxy:latest

```

Notes:

- The `--build-arg` flags bake the git commit SHA and build timestamp into the image, exposed via `/health/version`.
- `-p 127.0.0.1:4000:4000` binds to **localhost only** — the proxy is never directly internet-reachable. Verify with `ss -tlnp | grep 4000` (expect `127.0.0.1:4000`, not `0.0.0.0:4000`).
- `--restart unless-stopped` brings the container back automatically after a reboot or crash.
- **Changing env vars (e.g. rotating the key) requires** `docker rm -f` **+ a fresh** `docker run`**, not** `docker restart` — a restart reuses the original environment and will keep serving the old key. See §7.
- This single-container run is **DB-less**, and so is the default `docker compose up` — the proxy needs no database for master-key auth and static model routing. To enable the optional spend-tracking Postgres, layer the overlay: `docker compose -f docker-compose.yml -f docker-compose.db.yml up --build`. Never set `DATABASE_URL` without starting that `db` service, or LiteLLM enters DB mode with no reachable database and returns `400 "No connected db"` on every request. (The image already includes `prisma` for the DB-mode path.)

---

## 6. Put Caddy in front for TLS

```bash
mkdir -p /opt/caddy/data /opt/caddy/config
cat > /opt/caddy/Caddyfile <<'EOF'
proxy.example.com {
    reverse_proxy 127.0.0.1:4000
}
EOF

docker run -d --name caddy --restart unless-stopped --network host \
  -v /opt/caddy/Caddyfile:/etc/caddy/Caddyfile:ro \
  -v /opt/caddy/data:/data \
  -v /opt/caddy/config:/config \
  caddy:latest

```

- `--network host` lets Caddy reach `127.0.0.1:4000` directly and bind `80/443` on the host.
- The `/data` volume **persists the issued certificate** across restarts, which avoids re-issuance and Let's Encrypt rate limits.
- With DNS pointing at the Elastic IP and `80/443` open, Caddy obtains a certificate automatically within seconds. Watch it with `docker logs caddy 2>&1 | tail -n 30` — look for `certificate obtained successfully`.

---

## 7. Verify

Run the README ingress checks, plus end-to-end auth from an **external** machine (not the box):

```bash
# A) valid key → 200 with a completion
curl -s -X POST https://proxy.example.com/v1/messages \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <LITELLM_MASTER_KEY>" \
  -d '{"model":"claude-sonnet-4-6","max_tokens":20,"messages":[{"role":"user","content":"hi"}]}'

# B) no key → 401 (auth enforced)
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://proxy.example.com/v1/messages \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4-6","max_tokens":20,"messages":[{"role":"user","content":"hi"}]}'

# C) raw proxy port from outside → connection refused / timeout (never exposed)
curl -s -o /dev/null -w "%{http_code}\n" --max-time 8 http://proxy.example.com:4000/v1/messages

# D) version endpoint — confirm which commit is running (no auth required)
curl -s https://proxy.example.com/health/version
# expect {"sha":"<7-char>","built_at":"<ISO 8601 timestamp>"}

```

Expected: (A) a completion, (B) `401`, (C) a timeout/`000`, (D) JSON with `sha` and `built_at`.

**Reboot-survival test:** reboot the instance, wait a few minutes, and re-run check (A) from your external machine. If it returns a completion with no manual intervention, the auto-restart chain (Docker → both containers → persisted cert) is sound.

### Confirm deployed version

After a redeploy, confirm which code is running without SSH/SSM access:

```bash
# D) check deployed version — no auth required
curl -s https://proxy.example.com/health/version
```

Expected output:

```json
{"sha": "abc1234", "built_at": "2024-06-01T12:00:00Z"}
```

- `sha` — the 7-character git commit baked in at `docker build` time (via `--build-arg BUILD_SHA`).
- `built_at` — ISO 8601 UTC timestamp of the build (via `--build-arg BUILD_TIMESTAMP`).

> **This replaces the need to SSH/SSM into the instance and run `git log` to check
> which version is deployed.** After any redeploy (§8), hit `/health/version` from
> your workstation to confirm the new SHA matches the commit you pushed.

---

## 8. Operations

### Rotate the master key

```bash
cd /opt/llm-gateway
umask 077
python3 -c "import uuid; open('.env','w').write('LITELLM_MASTER_KEY=sk-'+str(uuid.uuid4())+'\nLITELLM_PORT=4000\nLITELLM_LOCAL_MODEL_COST_MAP=true\n')"
chmod 600 .env

# Recreate (NOT restart) so the new key is picked up:
docker rm -f proxy 2>/dev/null || true
docker run -d --name proxy --restart unless-stopped \
  --env-file .env \
  -v "$HOME/.config/litellm/github_copilot:/root/.config/litellm/github_copilot:rw" \
  -p "127.0.0.1:4000:4000" \
  llm-gateway-proxy:latest

```

Then update Parameter Store (from CloudShell) with the new value and update any clients' `ANTHROPIC_AUTH_TOKEN`.

### Rotate the OpenRouter API key

```bash
cd /opt/llm-gateway
# update OPENROUTER_API_KEY in .env, then recreate (NOT restart):
docker rm -f proxy 2>/dev/null || true
docker run -d --name proxy --restart unless-stopped \
  --env-file .env \
  -v "$HOME/.config/litellm/github_copilot:/root/.config/litellm/github_copilot:rw" \
  -p "127.0.0.1:4000:4000" \
  llm-gateway-proxy:latest

```

This box has two deploy models. **Build-on-box** (below) pulls the repo and
rebuilds the image on the instance — simplest, and less prone to dependency drift
now that the `Dockerfile` pins the LiteLLM + Prisma versions (the base image and
OS still track upstream). **Immutable ECR image** (further below) is recommended
for production: build once, push to ECR, then pull a frozen image — no on-box
build, no dependency drift, instant rollback.

### Redeploy after a repo update (build on the box)

```bash
cd /opt/llm-gateway
git fetch origin && git reset --hard origin/main   # or your deploy branch
docker build \
  --build-arg BUILD_SHA="$(git rev-parse --short HEAD)" \
  --build-arg BUILD_TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  -t llm-gateway-proxy:latest .
docker rm -f proxy 2>/dev/null || true
docker run -d --name proxy --restart unless-stopped \
  --env-file .env \
  -v "$HOME/.config/litellm/github_copilot:/root/.config/litellm/github_copilot:rw" \
  -p "127.0.0.1:4000:4000" \
  llm-gateway-proxy:latest

```

The `.env` and the Copilot OAuth token are outside git and are untouched by a redeploy.

### Deploy via ECR (immutable image — recommended for production)

The build-on-box redeploy rebuilds the image on the instance, re-resolving
dependencies each time. The pinned `Dockerfile` makes that reproducible, but the
most robust path is to build the image **once** (on a build host or in CI), push
it to ECR, and have the box **pull a frozen image** — no on-box build, no
dependency resolution, and instant rollback. The image bakes in
`litellm_config.yaml`, so an ECR-deployed box needs only `.env` and the Copilot
OAuth token mount — not a git checkout.

**1. One-time — create the registry:**

```bash
# CloudShell (write access)
aws ecr create-repository --repository-name llm-gateway-proxy --region <region>
```

Grant the **instance role** pull access — `ecr:GetAuthorizationToken` (resource
`*`) plus, scoped to the repo ARN: `ecr:BatchCheckLayerAvailability`,
`ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer`. A **build host / CI** that
pushes additionally needs `ecr:PutImage`, `ecr:InitiateLayerUpload`,
`ecr:UploadLayerPart`, `ecr:CompleteLayerUpload`. (`BatchGetImage` is required —
the push performs a manifest existence check that fails with `403 Forbidden`
without it.)

**2. Build and push (build host or CI):**

```bash
ACCOUNT=<account>; REGION=<region>; REPO=llm-gateway-proxy
ECR_URI=$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/$REPO
SHA=$(git rev-parse --short HEAD)

aws ecr get-login-password --region $REGION | \
  docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$REGION.amazonaws.com

docker build \
  --build-arg BUILD_SHA="$SHA" \
  --build-arg BUILD_TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  -t llm-gateway-proxy:$SHA .
# Tag with the commit SHA (immutable — enables rollback) AND a moving latest:
docker tag llm-gateway-proxy:$SHA $ECR_URI:$SHA
docker tag llm-gateway-proxy:$SHA $ECR_URI:latest
docker push $ECR_URI:$SHA
docker push $ECR_URI:latest
```

**3. Deploy on the box (pull, don't build):**

```bash
ACCOUNT=<account>; REGION=<region>; REPO=llm-gateway-proxy
ECR_URI=$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/$REPO
TAG=<image-tag-from-step-2>   # the exact tag you pushed (e.g. the short git SHA); avoid 'latest' in prod

aws ecr get-login-password --region $REGION | \
  docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$REGION.amazonaws.com
docker pull $ECR_URI:$TAG

# Recreate the container against the pulled image (env + token are box-local):
docker rm -f proxy 2>/dev/null || true
docker run -d --name proxy --restart unless-stopped \
  --env-file .env \
  -v "$HOME/.config/litellm/github_copilot:/root/.config/litellm/github_copilot:rw" \
  -p "127.0.0.1:4000:4000" \
  $ECR_URI:$TAG
```

**4. Roll back** — pull a previous SHA tag and recreate:

```bash
docker pull $ECR_URI:<previous-sha>
docker rm -f proxy 2>/dev/null || true
docker run -d --name proxy --restart unless-stopped \
  --env-file .env \
  -v "$HOME/.config/litellm/github_copilot:/root/.config/litellm/github_copilot:rw" \
  -p "127.0.0.1:4000:4000" \
  $ECR_URI:<previous-sha>
```

**5. Verify** after any deploy:

```bash
curl -s http://localhost:4000/health/readiness          # expect 200
curl -s http://localhost:4000/health/version            # expect {"sha":"<7-char>","built_at":"<ISO>"}
# then run check (A) from §7 with a valid key, from an external machine
```

> The `.env` lives outside git and the image, and survives every deploy. Because
> the image is immutable and self-contained, rolling forward or back is just a
> `docker pull` + recreate — the proxy is never rebuilt on the box.

### Grow the disk (no downtime)

EBS volumes resize live. Modify the volume in the console (e.g. to 20 GiB), wait for "optimizing" to finish, then on the box:

```bash
lsblk                          # confirm the device, usually nvme0n1 / nvme0n1p1
growpart /dev/nvme0n1 1
resize2fs /dev/nvme0n1p1
df -h /

```

Prune Docker before a rebuild if space is tight: `docker builder prune -af && docker image prune -af`.

### Observability & debugging empty completions

The image ships a lightweight logging callback (`litellm_logger.py`, enabled via
`litellm_settings.callbacks` in `litellm_config.yaml`) plus `json_logs: true`. For
**every** completion it prints one metadata-only line — never message content — to
stdout:

```bash
docker logs sandcastle-proxy 2>&1 | grep PROXY_LOG | tail
# PROXY_LOG {"t":"proxy_log","status":"success","model":"claude-sonnet-4-6",
#            "finish":"end_turn","content_len":4,"completion_tokens":4,"upstream_empty":false}
```

`upstream_empty: true` flags a `200` whose upstream completion returned no
content — the signal to watch for.

**Known issue — empty `/v1/messages` responses.** The upstream can intermittently
return a `200` with empty content through the Anthropic `/v1/messages` endpoint.
It has been **localized to LiteLLM's Anthropic-translation adapter**, not the
upstream or the router: on the same server, the OpenAI `/v1/chat/completions`
path (no translation) is reliable while `/v1/messages` occasionally empties.
Reproduce by alternating the two endpoints:

```bash
# from a host with the master key — compare the two endpoints back to back
PROXY_HOST="proxy.example.com"; KEY="<LITELLM_MASTER_KEY>"
for i in $(seq 1 6); do
  M=$(curl -s -X POST "https://$PROXY_HOST/v1/messages" \
      -H "Authorization: Bearer $KEY" -H "anthropic-version: 2023-06-01" \
      -H "content-type: application/json" \
      -d '{"model":"claude-sonnet-4-6","max_tokens":64,"messages":[{"role":"user","content":"pong"}]}' \
      | jq -r '[.content[]?.text] | add // "<EMPTY>"')
  C=$(curl -s -X POST "https://$PROXY_HOST/v1/chat/completions" \
      -H "Authorization: Bearer $KEY" \
      -H "content-type: application/json" \
      -d '{"model":"claude-sonnet-4-6","max_tokens":64,"messages":[{"role":"user","content":"pong"}]}' \
      | jq -r '.choices[0].message.content // "<EMPTY>"')
  echo "round $i: messages=[$M] chat=[$C]"
done
```

Clients that can use the OpenAI endpoint are unaffected; for Anthropic clients
(Claude Code) the mitigation is a client/agent-side retry — the CI canary in this
repo treats a transient empty as a warning, not an outage.

**Deep trace (temporary, on the box).** For a one-off, recreate the container
with debug logging, capture an empty, then restore:

```bash
docker rm -f sandcastle-proxy 2>/dev/null || true
docker run -d --name sandcastle-proxy --restart unless-stopped \
  --env-file .env -e LITELLM_LOG=DEBUG \
  -v "$HOME/.config/litellm/github_copilot:/root/.config/litellm/github_copilot:rw" \
  -p "127.0.0.1:4000:4000" llm-gateway-proxy:latest
# reproduce, read `docker logs sandcastle-proxy`, then re-run WITHOUT -e LITELLM_LOG=DEBUG
```

**Caddy access logs (host-local `Caddyfile`).** Add request-level visibility at
the TLS front door:

```caddyfile
proxy.example.com {
    log {
        output file /var/log/caddy/access.log
        format json
    }
    reverse_proxy 127.0.0.1:4000
}
```

---

## Model selection note

The repo ships honest model mappings — each alias has a **primary** (GitHub Copilot) and a **fallback** (OpenRouter) deployment. To change the fallback model, remap the fallback entry to any OpenRouter model ID your key can access by editing `litellm_config.yaml`, e.g.:

```yaml
  - model_name: "claude-sonnet-4-6-fallback"
    litellm_params:
      model: "openrouter/anthropic/claude-sonnet-4-5"   # remapped fallback
      api_key: "os.environ/OPENROUTER_API_KEY"
      stream: true

```

Use `curl https://openrouter.ai/api/v1/models -H "Authorization: Bearer $OPENROUTER_API_KEY"` to see which models your key can access.

---

## What lives outside git

A repo checkout does **not** capture the full running state. When rebuilding a host from scratch, these must be recreated manually:

- `.env` — generated, git-ignored; holds `LITELLM_MASTER_KEY`, `OPENROUTER_API_KEY` (and Postgres password if using Compose).
- **The Copilot OAuth token** — `~/.config/litellm/github_copilot/`; created by the §4 device-code flow, cannot be regenerated without redoing it.
- **Caddy config and certificate** — `/opt/caddy/Caddyfile` and `/opt/caddy/data/`.
- **AWS resources** — the instance, IAM role and policies, security group, Elastic IP, EBS volume, Parameter Store secret, DNS record, ECR repo.

Keep this in mind for disaster recovery: the image (in ECR) plus this document plus the secret (in Parameter Store) are what make a rebuild reproducible.