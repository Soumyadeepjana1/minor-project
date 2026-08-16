#!/usr/bin/env python3
"""Extract raw configs from the Kubernetes ConfigMap manifests.

The observability configs live inside ConfigMaps (e.g. monitoring/
prometheus/prometheus-config.yaml) so `kubectl apply -f monitoring/` works.
Docker Compose needs the *raw* config files, so this script pulls the
`data.*` values out into a local directory and rewrites service hostnames
for the Compose network.

It also regenerates `monitoring/grafana/dashboards-configmap.yaml` from the
standalone dashboard JSON files in `monitoring/grafana/dashboards/`.

Usage:
    python scripts/extract-configs.py [--out .local]
"""

import argparse
import json
import os
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_configmap(path: pathlib.Path, name: str) -> dict:
    """Return the `data` of the named ConfigMap inside a (possibly multi-doc) file."""
    docs = [d for d in yaml.safe_load_all(path.read_text()) if d is not None]
    for doc in docs:
        if doc.get("kind") == "ConfigMap" and doc.get("metadata", {}).get("name") == name:
            return doc["data"]
    raise SystemExit(f"{path}: ConfigMap '{name}' not found")


def write(name: str, content: str, out: pathlib.Path) -> None:
    target = out / name
    target.write_text(content)
    print(f"  wrote {target.relative_to(ROOT)}")


def rewrite_hosts(text: str) -> str:
    """Rewrite k8s in-cluster service DNS to Compose service names."""
    return (
        text.replace("loki.monitoring.svc.cluster.local", "loki")
        .replace("alertmanager.monitoring.svc.cluster.local", "alertmanager")
        .replace("prometheus.monitoring.svc.cluster.local", "prometheus")
    )


def substitute_slack_webhook(text: str) -> str:
    """Replace the placeholder Slack webhook URL with SLACK_WEBHOOK_URL if set."""
    url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not url:
        return text
    return text.replace("https://hooks.slack.com/services/REPLACE/ME", url)


def prometheus_for_compose(raw: str) -> str:
    """Swap k8s service discovery for static Compose targets."""
    cfg = yaml.safe_load(raw)
    for job in cfg["scrape_configs"]:
        if job["job_name"] == "demo-app":
            job.pop("kubernetes_sd_configs", None)
            job.pop("relabel_configs", None)
            job["static_configs"] = [{"targets": ["app:5000"]}]
    # Existing hosts are already rewritten by rewrite_hosts().
    return yaml.safe_dump(cfg, sort_keys=False)


def regenerate_dashboards_configmap() -> None:
    """Recreate monitoring/grafana/dashboards-configmap.yaml from dashboards/*.json."""
    dash_dir = ROOT / "monitoring/grafana/dashboards"
    out_path = ROOT / "monitoring/grafana/dashboards-configmap.yaml"
    lines = [
        "# Generated from monitoring/grafana/dashboards/*.json - do not edit by hand.",
        "# Regenerate with: python scripts/extract-configs.py",
        "apiVersion: v1",
        "kind: ConfigMap",
        "metadata:",
        "  name: grafana-dashboards",
        "  namespace: monitoring",
        "  labels:",
        "    app: grafana",
        "data:",
    ]
    for f in sorted(dash_dir.glob("*.json")):
        pretty = json.dumps(json.loads(f.read_text()), indent=2)
        lines.append(f"  {f.name}: |")
        lines += ["    " + line for line in pretty.splitlines()]
    out_path.write_text("\n".join(lines) + "\n")
    print(f"  regenerated {out_path.relative_to(ROOT)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=".local", help="output directory for raw configs")
    args = parser.parse_args()

    out = ROOT / args.out
    (out / "dashboards").mkdir(parents=True, exist_ok=True)

    prom_raw = load_configmap(ROOT / "monitoring/prometheus/prometheus-config.yaml", "prometheus-config")["prometheus.yml"]
    write("prometheus.yml", prometheus_for_compose(rewrite_hosts(prom_raw)), out)

    rules = load_configmap(ROOT / "monitoring/alerts/rules.yaml", "prometheus-alert-rules")["rules.yaml"]
    write("rules.yaml", rules, out)

    am = load_configmap(ROOT / "monitoring/alertmanager/alertmanager-config.yaml", "alertmanager-config")["alertmanager.yml"]
    write("alertmanager.yml", substitute_slack_webhook(rewrite_hosts(am)), out)

    loki = load_configmap(ROOT / "monitoring/loki/loki-config.yaml", "loki-config")["loki-config.yaml"]
    write("loki-config.yaml", rewrite_hosts(loki), out)

    promtail = load_configmap(ROOT / "monitoring/promtail/promtail-config.yaml", "promtail-config")["promtail.yaml"]
    write("promtail.yaml", rewrite_hosts(promtail), out)

    datasources = load_configmap(ROOT / "monitoring/grafana/grafana-deployment.yaml", "grafana-datasources")
    write("datasources.yaml", rewrite_hosts(datasources["datasources.yaml"]), out)
    provider = load_configmap(ROOT / "monitoring/grafana/grafana-deployment.yaml", "grafana-dashboards-provider")
    write("dashboards-provider.yaml", provider["dashboards.yaml"], out)

    for f in sorted((ROOT / "monitoring/grafana/dashboards").glob("*.json")):
        target = out / "dashboards" / f.name
        target.write_text(f.read_text())
        print(f"  copied {target.relative_to(ROOT)}")

    regenerate_dashboards_configmap()
    print(f"Done. Raw configs are in {out.relative_to(ROOT)}/ for docker-compose.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
