# Kubernetes commands for the demo and for Docker Unit 3

Every command here was run against the live demo cluster on 5 November 2026 (k3s v1.33.4 in
Docker). Section C follows the Kubernetes topics of the Containerization with Docker course
(Unit 3: architecture, components, scaling, rolling updates, stateful applications, Helm), so
each topic can be shown on a real cluster.

| Section | Safe during the demo? |
|---|---|
| A. Setup | yes |
| B. Health checks | yes, read-only |
| C. Unit 3 topics on the live cluster | yes, read-only |
| D. Practice: self-healing, scaling, rolling update | **not during the demo**; after it, each has an undo |
| E. Not available on this cluster | (explains why) |
| F. Never run before or during the demo | no |

## A. Setup (each new terminal)

```
cd <repository root>
export KUBECONFIG="$PWD/.local/demo/kubeconfig"
alias k=kubectl          # optional
```

`scripts/demo.sh` and `demo-day-setup.sh` set the kubeconfig themselves; plain `kubectl` needs the export.

## B. Health checks (read-only, safe any time)

One command for everything: `demo-day/demo-day-setup.sh health` (from the repository root).

| Check | Command | Healthy result |
|---|---|---|
| Node up | `kubectl get nodes -o wide` | `k3s-demo  Ready  control-plane,master  v1.33.4+k3s1` |
| Node conditions | `kubectl describe node k3s-demo \| grep -A6 Conditions` | MemoryPressure, DiskPressure, PIDPressure `False`; Ready `True` |
| API server | `kubectl get --raw /readyz` | `ok` |
| All demo pods | `kubectl get pods -A -o wide` | demo 2, ai 1, monitoring 6: all `1/1 Running` |
| Only problem pods | `kubectl get pods -A \| grep -v -E "Running\|NAME"` | nothing printed |
| Restarts of the app | `kubectl get pods -n demo -l app=demo-app -o jsonpath='{.items[*].status.containerStatuses[0].restartCount}'` | `0 0` (rises after the crash scenario; that is expected) |
| Deployments rolled out | `kubectl get deploy -A` | READY equals desired everywhere |
| One rollout | `kubectl rollout status deploy/demo-app -n demo` | `successfully rolled out` |
| Recent warnings | `kubectl get events -A --field-selector type=Warning --sort-by=.lastTimestamp` | none recent (probe warnings from start-up are normal) |
| Service has pods behind it | `kubectl get endpointslices -n demo -l kubernetes.io/service-name=demo-app` | two pod IPs on port 5000 |
| App logs | `kubectl logs -n demo deploy/demo-app --tail=20` | gunicorn access lines with 200 |
| AI service logs | `kubectl logs -n ai deploy/ai-service --tail=30` | no tracebacks |
| Logs of a crashed container | `kubectl logs -n demo <pod> --previous` | the output before the crash (after the crash scenario) |
| AI service checks | `scripts/demo.sh status` | loki healthy, ollama healthy, k8s enabled for `demo` |
| Images kept in k3s | `docker exec k3s-demo crictl images` | 8 demo images + 3 k3s system images |
| Image pinned | `docker exec k3s-demo crictl inspecti -o json docker.io/grafana/grafana:11.2.0 \| grep pinned` | `"pinned": true` |
| Disk for Docker | `df -h /` | above about 1 GB free |
| URLs | `curl -s -o /dev/null -w '%{http_code}\n' http://localhost:18080/healthz` (app port 8080 or 18080; also :8000/health, :3000/api/health, :9090/-/ready, :9093/-/ready) | `200` |

## C. Docker Unit 3 topics on the live cluster (read-only)

### Architecture and components

```
kubectl cluster-info                       # API server address, CoreDNS
kubectl get nodes -o wide                  # one node: control plane and worker together
kubectl get pods -n kube-system            # coredns, local-path-provisioner
kubectl api-resources | head -20           # object types the API server knows
kubectl explain deployment.spec.strategy   # built-in documentation of any field
docker exec k3s-demo crictl ps             # containers seen by the runtime (containerd)
```

Say: in k3s the API server, scheduler and controller manager run inside one `k3s server` process,
and a single-server k3s stores cluster state in SQLite instead of etcd, so they do not appear as
separate pods. kubelet and kube-proxy are also in that process; containerd is the container runtime.

### Objects: Pod, ReplicaSet, Deployment, Service, Namespace

```
kubectl get ns
kubectl get deploy,rs,pods,svc -n demo
kubectl describe deploy demo-app -n demo
kubectl get pods -n demo --show-labels     # app=demo-app, pod-template-hash=...
kubectl get svc -A                         # ClusterIP services: demo-app 80, ai-service 8000, grafana 80, prometheus 9090 ...
```

### Rolling update strategy and probes

```
kubectl get deploy demo-app -n demo -o jsonpath='{.spec.strategy}'
#  {"rollingUpdate":{"maxSurge":"25%","maxUnavailable":"25%"},"type":"RollingUpdate"}
kubectl get deploy demo-app -n demo -o jsonpath='{.spec.template.spec.containers[0].readinessProbe.httpGet.path} {.spec.template.spec.containers[0].livenessProbe.httpGet.path}'
#  /readyz /healthz
kubectl rollout history deploy/demo-app -n demo      # needs 2+ revisions for the rollback scenario
```

### Networking and DNS

```
kubectl get endpointslices -n demo -l kubernetes.io/service-name=demo-app
kubectl exec -n demo deploy/demo-app -- cat /etc/resolv.conf
#  search demo.svc.cluster.local svc.cluster.local cluster.local
#  nameserver 10.43.0.10        (CoreDNS; compare Docker's embedded DNS 127.0.0.11)
kubectl exec -n demo deploy/demo-app -- python -c "import socket; print(socket.gethostbyname('prometheus.monitoring.svc.cluster.local'))"
#  10.43.94.195                 (the Service's ClusterIP)
kubectl exec -n demo deploy/demo-app -- python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:5000/healthz').status)"
#  200
```

### Storage: stateful data

```
kubectl get pvc,pv -A          # monitoring/loki-storage Bound to a 5Gi local-path volume
kubectl describe pvc loki-storage -n monitoring
```

### Security: NetworkPolicy, RBAC, Secret (report section 2.3)

```
kubectl describe networkpolicy ai-service-ingress -n ai     # only Alertmanager and Prometheus may connect
kubectl get role,rolebinding -n demo                        # ai-service-remediation
kubectl auth can-i delete pods -n demo --as=system:serviceaccount:ai:ai-service         # yes
kubectl auth can-i patch deployments -n demo --as=system:serviceaccount:ai:ai-service   # yes
kubectl auth can-i delete pods -n monitoring --as=system:serviceaccount:ai:ai-service   # no  (least privilege)
kubectl get secret ai-service-token -n ai                   # exists; do not print its value
```

### Configuration

```
kubectl get cm app-config -n demo -o yaml      # FAULT_INJECTION: "true" and the other app settings
```

### Docker side of the cluster (volumes and networks)

```
docker ps --filter name=k3s-demo               # the whole cluster is one privileged container
docker system df                                # images, containers, volumes, build cache
docker volume ls                                # k3s keeps its data in an anonymous volume
docker network inspect bridge -f '{{(index .IPAM.Config 0).Gateway}}'   # 172.17.0.1: how pods reach Ollama
```

### Helm

```
helm version --short
helm list -A          # no releases: this project deploys with plain manifests (kubectl apply)
```

Helm prints a warning that the kubeconfig is world-readable; that comes from the drive's file permissions and is harmless here.

## D. Practice commands (after the demo only; each has an undo)

| Topic | Try | Undo / check |
|---|---|---|
| Self-healing | `kubectl delete pod -n demo -l app=demo-app --wait=false` then `kubectl get pods -n demo -w` | nothing to undo: the ReplicaSet recreates both pods in seconds |
| Manual scaling | `kubectl scale deploy/demo-app -n demo --replicas=3` | `kubectl scale deploy/demo-app -n demo --replicas=2` |
| Rolling update | `kubectl rollout restart deploy/demo-app -n demo` then `kubectl rollout status deploy/demo-app -n demo` | not needed (same image); it adds a revision |
| History and rollback | `kubectl rollout history deploy/demo-app -n demo` then `kubectl rollout undo deploy/demo-app -n demo` | `kubectl rollout status ...`; history keeps 2+ revisions |
| Shell in a pod | `kubectl exec -it -n demo deploy/demo-app -- sh` | `exit` |
| Port-forward by hand | `kubectl port-forward -n demo svc/demo-app 18081:80` | Ctrl+C |

After any practice: `demo-day/demo-day-setup.sh health` must show only `ok` lines. If the
app port stops answering, run `scripts/demo.sh forward`.

## E. Course topics not available on this cluster

| Command | Why not here |
|---|---|
| `kubectl top nodes`, `kubectl top pods` | k3s runs with `--disable metrics-server` to save resources: "Metrics API not available" |
| `kubectl autoscale ...` (HPA) | needs the metrics server; the demo scales with the AI service's approved `scale_deployment` instead |
| Ingress | Traefik is disabled; the demo uses port-forwards |
| `kubeadm init/join`, `minikube start` | this cluster is k3s in Docker (Minikube is supported by `CLUSTER=minikube`, but needs more disk) |

## F. Never run before or during the demo

- `kubectl set image ...` on demo-app or ai-service (breaks the app or the AI service)
- `kubectl delete deploy|ns|-f ...`, `kubectl scale ... --replicas=0`
- `docker stop k3s-demo`, `docker system prune`, `docker volume prune`, `docker image prune`
- `rm -rf .local` in the repository (holds the kubeconfig and port-forward state)
