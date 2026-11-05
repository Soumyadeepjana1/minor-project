# Teammate laptop: setup for demo day (5 November 2026)

Your part of the demo is **the web application** (part 1 in `README.md`). Iqbal's laptop shows
the platform, the AI services and the full demo. Part A below is required; part B (the full
platform as a backup) is optional and only worth doing with enough disk and RAM.

Rules for this laptop: do not delete images, volumes or containers that belong to other
projects, do not run `docker system prune`, keep tokens and passwords out of anything you
send, and if something needs `sudo` or deleting files, the person at the laptop decides.

## 0. Get this folder

If the pull request "Demo day kit" has been merged:

```
cd <your clone of the repository>
git pull
```

Before it is merged (it lives on a branch of Iqbal's fork):

```
git clone -b demo-day https://github.com/iPatan7/minor-project.git ~/minor-project-demo
cd ~/minor-project-demo
```

(Or, in an existing clone: `git fetch https://github.com/iPatan7/minor-project.git demo-day` and
`git switch -c demo-day FETCH_HEAD`.)

## A. Show the web application (required, about 5 minutes to set up)

**The storefront.** The report's Figure 3.2 shows a storefront called "goodthings" (announcement
bar, "Find your everyday favourite.", six products, `/static/shop.css`, `/static/shop.js`). That
version is **not** in the GitHub repository: no branch has it, and the app there answers `/` with
JSON. If this laptop has it, start it the way it was started for the screenshots and show that.
Tell Iqbal which folder it is in.

**The repository version** (if there is no storefront here), in Docker, fault injection off:

```
docker build -t demo-app:0.2.0 app/
docker run -d --name demo-web -p 5000:5000 demo-app:0.2.0
```

Check it, then open it in the browser:

```
curl -s http://localhost:5000/                # {"message": "Hello from the demo app!", ...}
curl -s http://localhost:5000/api/products    # the product list
curl -s http://localhost:5000/healthz         # liveness
curl -s http://localhost:5000/readyz          # readiness
curl -s http://localhost:5000/version
curl -s http://localhost:5000/metrics | head  # Prometheus metrics the platform scrapes
```

What to show and say (about 4 minutes):

1. The home page and `/api/products`: the application the platform watches.
2. `/healthz`, `/readyz`: the probes Kubernetes uses to restart a broken container and to send
   traffic only to ready ones.
3. `/metrics`: request counts and latency that Prometheus scrapes and the alerts are built on.
4. `app/Dockerfile`: multi-stage build, small Alpine runtime, non-root user (image 84.8 MB).
5. GitHub Actions in the repository: lint, tests on Python 3.10-3.12, a configuration drift
   check, image builds and a smoke test of the whole Compose stack.
6. Hand over to Iqbal: "now the platform watches this same app on Kubernetes".

Stop it afterwards: `docker rm -f demo-web`.

## B. Full platform as a backup (optional)

Only if this laptop has about 3.5 GB free for Docker, 8 GB free RAM, and the model downloaded
or 4.7 GB more disk. From the repository root:

```
demo-day/demo-day-setup.sh check       # changes nothing; fix every FAIL line first
demo-day/demo-day-setup.sh up          # 10-15 minutes the first time
demo-day/demo-day-setup.sh rehearse    # 15-20 minutes: crash, errors, latency, security
demo-day/keep-running.sh start         # keeps it up all day
demo-day/dashboard-k3s.sh              # optional: the 9983 dashboard
docker update --restart unless-stopped k3s-demo
```

10-15 minutes before presenting: `demo-day/demo-day-setup.sh day`. At any time:
`demo-day/demo-day-setup.sh health`; after evictions or a reboot: `demo-day/demo-day-setup.sh repair`.

`demo-day-setup.sh` writes everything to `demo-day/demo-day-report.txt`: send that file to Iqbal,
with a clear note of anything that failed.

**Success** (`health` shows only `ok` lines): node Ready, no disk pressure, all 9 pods in `demo`,
`ai`, `monitoring` Running and ready, demo-app has 2 or more revisions, the five URLs answer 200,
no alerts, no incidents, `loki` and `ollama` healthy, about 1 GB or more free for Docker.

Measured on Iqbal's laptop this morning: crash analysed 133 s after the fault (root cause
"container crash loop", confidence 85); approved restart verified healthy in 10 s.

## If something goes wrong

| Symptom | Do this |
|---|---|
| Port 5000 already in use (part A) | `docker run -d --name demo-web -p 5050:5000 demo-app:0.2.0` and use http://localhost:5050 |
| `check`: Ollama runs on 127.0.0.1 only | With permission: `sudo systemctl stop ollama` (the demo script starts its own on the Docker bridge address), or set `Environment="OLLAMA_HOST=172.17.0.1:11434"` in the ollama service and restart it. Never listen on 0.0.0.0: the API has no authentication |
| `check`: not enough disk | About 3.5 GB for Docker and 4.7 GB for the model. Ask before deleting anything |
| Pods `Evicted`, `Pending`, `ErrImagePull`; DiskPressure | Free disk if possible, then `demo-day/demo-day-setup.sh repair` |
| A URL stops loading | `scripts/demo.sh forward` |
| No incident after 4 minutes | `scripts/demo.sh status`, then `kubectl -n ai logs deploy/ai-service --tail=30` |
| `approve latest` finds nothing | The alert cleared before approval: run the scenario again and approve sooner |
| Rollback says "no earlier revision" | `demo-day/demo-day-setup.sh repair`, then run `errors` again |
