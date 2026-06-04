# Push-to-deploy (CI → LKE)

Pushing to `main` runs `.github/workflows/publish-image.yml`:

1. **build-push** — builds the image, pushes `ghcr.io/shinyduo/attendee:main` + `:<sha>`.
2. **deploy** — `kubectl rollout restart deployment/attendee-web deployment/attendee-worker`
   in the `attendee` namespace, then waits for both rollouts.

## What updates, and what stays safe
- **web / worker**: rolled by the deploy job. `imagePullPolicy: Always` + the moving
  `:main` tag → they pull the freshly built image on restart.
- **bot pods**: NOT touched by CI. They are independent pods (created per meeting by
  `BotPodCreator`, `imagePullPolicy: Always`), so they pick up new code on the **next
  meeting**. In-progress recordings are unaffected by a deploy.
- **migrations**: NOT run by CI. Re-run the migrate Job manually for schema changes,
  and keep them backward-compatible (expand/contract).

## Auth
The deploy job authenticates with the **`gh-deployer`** ServiceAccount (see
`07-deploy-rbac.yaml`), stored base64 in the repo secret `KUBE_CONFIG_DATA`.
It can only get/patch deployments + read pods/replicasets in `attendee` — it cannot
read secrets, delete pods, or touch nodes. Regenerate with
`bash /tmp/make-gh-deploy-kubeconfig.sh` (or re-create from the RBAC manifest).

## Never do this during a live meeting
- "Recycle All Nodes" / drain / delete a bot node → kills the running bot pod.
