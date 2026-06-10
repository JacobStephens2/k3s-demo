# Policy as code — admission control on the k3s-demo cluster

The manifests under `k8s/` already follow a hardened posture: non-root
containers, dropped capabilities, a read-only root filesystem, pinned image
tags, resource requests/limits, and liveness/readiness probes. Good *intentions*
in a YAML file are not a *guarantee* — a later edit, a copy-pasted example, or an
automated change can quietly regress any of them.

This directory makes that posture **enforced at admission**: the API server
rejects a non-compliant workload before it ever runs. Same rules the manifests
already satisfy, now mandatory.

## Two engines, on purpose

| | `gatekeeper/` — **OPA / Gatekeeper** | `vap/` — **ValidatingAdmissionPolicy** |
|---|---|---|
| Language | Rego | CEL |
| Runtime | A controller + webhook you install and operate | Built into Kubernetes (GA since 1.30); nothing to install |
| Best for | Reusable, cross-cutting policy; logic that also applies beyond K8s (API authz, agent permissioning) | Simple, single-resource rules with no extra moving parts |
| Here | The full five-rule set below | One rule (no `:latest`), as the in-tree equivalent of `K8sDisallowLatestTag` |

The `:latest` rule is deliberately implemented in **both** so the cluster
*demonstrates* the comparison rather than just asserting it. Rego was chosen as
the primary because the policy language transfers past Kubernetes — the same
default-deny, human-reviewed-exception model is how an AI agent's command surface
should be gated, not only a cluster's.

## The five rules (Gatekeeper)

| ConstraintTemplate | Enforces |
|---|---|
| `K8sRequireNonRoot` | `runAsNonRoot: true` (pod or container level) |
| `K8sRequireResourceLimits` | cpu + memory `requests` **and** `limits` on every container |
| `K8sDisallowLatestTag` | no `:latest`, no untagged images |
| `K8sRequireHardenedContainer` | `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`, drop `ALL` caps |
| `K8sRequireProbes` | a `livenessProbe` and a `readinessProbe` on every container |

Each constraint is scoped to the `k3s-demo` namespace (`match.namespaces`), so
system workloads in `kube-system` and `gatekeeper-system` are untouched. In a
real multi-tenant cluster you would instead scope cluster-wide and exempt the
system namespaces.

## Apply

Gatekeeper templates create CRDs; the constraints are instances of those CRDs, so
templates must be applied (and established) first.

```bash
# 1. Install Gatekeeper (once)
kubectl apply -f https://raw.githubusercontent.com/open-policy-agent/gatekeeper/v3.22.2/deploy/gatekeeper.yaml
kubectl -n gatekeeper-system rollout status deploy/gatekeeper-controller-manager

# 2. Templates first, then wait for their CRDs to register
kubectl apply -k gatekeeper/templates/
for k in k8srequirenonroot k8srequireresourcelimits k8sdisallowlatesttag \
         k8srequirehardenedcontainer k8srequireprobes; do
  kubectl wait --for=condition=established crd/${k}.constraints.gatekeeper.sh --timeout=60s
done

# 3. Constraints + the built-in VAP equivalent
kubectl apply -k gatekeeper/constraints/
kubectl apply -k vap/
```

Each policy directory carries a `kustomization.yaml`, so the same `apply -k`
works against a remote ref too - which is exactly how the cluster's cloud-init
applies them on first boot (see `infra/k3s/cloud-init.sh.tftpl`), keeping the
whole policy layer reproducible on a rebuild rather than hand-applied.

## Prove it denies

```bash
# Violates every rule at once -> rejected (the VAP catches the :latest tag first):
kubectl apply -f test/bad-deployment.yaml

# A pinned-but-otherwise-bad workload passes the VAP and is rejected by Gatekeeper,
# which lists each failing rule:
#   [require-probes] container 'bad' must define a livenessProbe
#   [require-non-root] container 'bad' must run as non-root ...
#   [require-resource-limits] container 'bad' must set resources.limits.cpu ...
```

The real workloads (`k8s/`) pass all six checks — `kubectl get constraints`
reports `TOTAL-VIOLATIONS 0`, and a `rollout restart` of the app still completes
zero-downtime through the same admission gate.
