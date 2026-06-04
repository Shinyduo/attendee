# attendee on Linode LKE — pod-per-bot POC (+ IP-rotation)

Goal of this POC:
1. **Pod-per-bot** (`LAUNCH_BOT_METHOD=kubernetes`) — fixes manual scaling toil + missed
   meetings (each bot = its own pod, dies on leave, autoscaler adds/removes nodes).
2. **IP rotation, in two phases** — see bottom.

Image: `ghcr.io/shinyduo/attendee:main` (your fork). Reuses the proven GKE manifests in
`../` adapted for Linode (no ManagedCertificate/BackendConfig, no gke-nodepool selectors,
in-cluster Postgres+Redis, NodeBalancer LB, `attendee.settings.production`).

---

## 0. Create the cluster (Linode UI or CLI), region **US East (Newark)**

> New Linode accounts have a node/Linode limit (often < 20). To autoscale to **30** bot nodes,
> open a support ticket to raise the limit to ≥32 right after signup (else scaling silently
> stops at the cap and bots queue).

Two node pools:
- **system pool** — **Dedicated 4 GB (2 vCPU/4 GB)**, fixed **1 node** (no autoscale). Runs web + worker.
- **bots pool** — **Dedicated 8 GB (4 vCPU/8 GB)**, **Autoscale min 2 / max 30**. One bot per node.
  - Dedicated 4 vCPU + `BOT_CPU_REQUEST=3` ⇒ exactly **1 bot per node** → each bot egresses via
    its own node's public IP (free Phase-1 IP rotation). Supports your 16–20 parallel meetings.
  - Before the evening rush (~14 starts/slot), raise bots-pool **min to ~8** so warm nodes absorb
    the burst (a cold node takes ~1–3 min vs the ~60s join window).

(Optional: label the system pool and add a `nodeSelector` to web/worker in 05-app.yaml so they
never land on a bot node. For the POC the scheduler usually places them fine without it.)

- Download the kubeconfig and point kubectl at it:
  ```bash
  export KUBECONFIG=~/Downloads/<cluster>-kubeconfig.yaml
  kubectl get nodes        # confirm nodes are Ready
  ```

## 1. Fill secrets
```bash
cd k8s/linode
cp 03-secrets.template.yaml 03-secrets.yaml
# edit 03-secrets.yaml: DJANGO_SECRET_KEY, CREDENTIALS_ENCRYPTION_KEY (REUSE Railway v2's),
# DB password (same in DATABASE_URL + postgres-secret), AWS/R2, DEEPGRAM.
```
> Reuse the **same `CREDENTIALS_ENCRYPTION_KEY`** as Railway v2 so existing stored credentials
> (Google Meet login group key.pem/cert.pem) decrypt. Otherwise you re-add the login group.

## 2. Point at Railway DB + apply base
Postgres + Redis are on **Railway** — so **DO NOT apply 04-data.yaml**. Put Railway's
**public** URLs (`DATABASE_PUBLIC_URL` / `REDIS_PUBLIC_URL`) into 03-secrets.yaml.

> 🚩 Before testing, **scale Railway v2 `web` + `worker` to 0 replicas.** Two worker fleets on
> one Redis fight over the same Celery queue and double-launch bots. Reusing Railway v2's
> Postgres also means your existing Project / API key / "Test" login group / superuser carry
> over — so you can skip the createsuperuser step below.

```bash
kubectl apply -f 01-namespace-rbac.yaml
kubectl apply -f 02-configmap.yaml
kubectl apply -f 03-secrets.yaml
# (04-data.yaml intentionally skipped — DB/Redis are on Railway)
```

## 3. Migrate, then app
```bash
kubectl apply -f 05-app.yaml
kubectl -n attendee wait --for=condition=complete job/attendee-migrate --timeout=300s
kubectl -n attendee logs job/attendee-migrate          # confirm migrations incl. 0082_botlogin*
```

## 4. Get the public IP and set SITE_DOMAIN (CRITICAL for signed-in bots)
```bash
kubectl -n attendee get svc attendee-web-lb -w        # wait for EXTERNAL-IP
IP=<external-ip>
# Set SITE_DOMAIN to that IP (or <ip>.nip.io / a real domain), then restart:
kubectl -n attendee patch configmap env --type merge -p "{\"data\":{\"SITE_DOMAIN\":\"$IP\"}}"
kubectl -n attendee rollout restart deploy/attendee-web deploy/attendee-worker
```
> Without correct `SITE_DOMAIN`, signed-in Google Meet bots fail SAML ("Login timed out").
> This was the exact Railway v2 bug.

## 5. Bootstrap an admin + API key
```bash
POD=$(kubectl -n attendee get pod -l app=attendee-web -o jsonpath='{.items[0].metadata.name}')
kubectl -n attendee exec -it $POD -- python manage.py createsuperuser
# then log in at http://$IP/  -> create/confirm a Project -> copy its API key.
```
(If you reused the DB dump from Railway, the existing project + login group + API key carry over.)

## 6. Fire a test bot (Phase 1 — bare Linode IP, no proxy)
```bash
curl -X POST "http://$IP/api/v1/bots" \
  -H "Authorization: Token <PROJECT_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "meeting_url": "https://meet.google.com/xxx-xxxx-xxx",
    "bot_name": "Arali Agent",
    "google_meet_settings": {"use_login": true, "login_mode": "always", "login_group_name": "Test"}
  }'
```
Watch it:
```bash
kubectl -n attendee get pods -w                  # a bot-<id> pod should appear (1 per node)
kubectl -n attendee logs -f <bot-pod>            # join progress
```

---

## IP-rotation POC — two phases

**Phase 1 — free, per-node IP (do this first).**
With 1 bot/node, each bot already egresses from its **own Linode node public IP**. Fire 2–3
concurrent bots → they land on different nodes → different IPs. This answers: *does a Linode
datacenter IP get admitted to Meet at all, and does spreading across nodes help?* No code, no
proxy, no cost beyond nodes. If Linode IPs join reliably, **you may not need the proxy.**

Check each bot's egress IP:
```bash
kubectl -n attendee exec <bot-pod> -- curl -s https://api.ipify.org   # should differ per pod
```

**Phase 2 — residential / ISP proxy (only if Phase 1 IPs get blocked).**
The proxy feature (`bots/residential_proxy.py` + `web_bot_adapter.py` edit) is drafted but **not
yet in the fork image** (you paused it: "Lets not push, lets discuss it first"). To POC it:
1. Merge the proxy PR into `Shinyduo/attendee` main → GitHub Actions rebuilds
   `ghcr.io/shinyduo/attendee:main`.
2. Pick a proxy. Recommended for authorized recording: an **ISP / static-residential** pool
   (flat per-IP/month, **unlimited bandwidth** → no per-GB bill; sticky per meeting). Set in
   `02-configmap.yaml`: `RESIDENTIAL_PROXY_ENABLED=true` + HOST/PORT/USERNAME_TEMPLATE
   (+ `RESIDENTIAL_PROXY_PASSWORD` in the Secret). Leave `INCLUDE_MEDIA=false` (signaling-only
   is enough to pass the join gate; cheaper).
3. Re-apply configmap + `rollout restart`. Re-run step 6; the egress IP check should now show
   the **proxy's residential IP**, not the node IP.

> With the proxy on, the node's IP reputation is irrelevant — so the same setup works unchanged
> (and cheaper) on Hetzner/Civo later.

---

## Notes / gotchas
- **Image pull**: GHCR package is public, so no `regcred` needed. If a bot pod shows
  `ImagePullBackOff`, set `DISABLE_BOT_POD_IMAGE_PULL_SECRET=false` in the configmap and create
  `regcred` (`kubectl -n attendee create secret docker-registry regcred --docker-server=ghcr.io
  --docker-username=<gh-user> --docker-password=<PAT-with-read:packages>`).
- **Autoscaler cold start**: a new node takes ~1–3 min; the join window is ~60s. Keep node-pool
  **min ≥ 2** warm so the first concurrent bots don't miss. Raise min before known busy slots.
- **Beat (scheduled bots)**: not needed for adhoc `POST /api/v1/bots`. Add a `celery beat`
  deployment later if you want calendar-scheduled auto-join.
- **Teardown**: `kubectl delete ns attendee attendee-webpage-streamer` + delete the LKE cluster.
  `linode-block-storage-retain` keeps the PG volume; delete it manually in the UI to stop billing.
