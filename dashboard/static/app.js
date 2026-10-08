"use strict";

(function () {
  const STATE = { config: null, active: null, timer: null };

  const el = {
    tabs: document.getElementById("tabs"),
    frames: document.getElementById("frames"),
    status: document.getElementById("status"),
    offline: document.getElementById("offline"),
    metrics: document.getElementById("metrics"),
    pipelineList: document.getElementById("pipeline-list"),
    pipelinesLink: document.getElementById("pipelines-link"),
    hint: document.getElementById("hint"),
    meta: document.getElementById("meta"),
    refresh: document.getElementById("refresh"),
    auto: document.getElementById("auto-refresh"),
  };

  function embedUrl(dashboard, cacheBust) {
    return cacheBust
      ? dashboard.embed_url + "&_ts=" + Date.now()
      : dashboard.embed_url;
  }

  function renderTabs() {
    el.tabs.textContent = "";
    STATE.config.dashboards.forEach(function (d, i) {
      const tab = document.createElement("button");
      tab.type = "button";
      tab.className = "tab" + (i === 0 ? " active" : "");
      tab.textContent = d.title;
      tab.dataset.uid = d.uid;
      tab.addEventListener("click", function () { select(d.uid); });
      el.tabs.appendChild(tab);
    });
  }

  function renderFrames() {
    el.frames.textContent = "";
    STATE.config.dashboards.forEach(function (d, i) {
      const frame = document.createElement("iframe");
      frame.className = "frame" + (i === 0 ? " visible" : "");
      frame.dataset.uid = d.uid;
      frame.title = d.title;
      frame.loading = "lazy";
      frame.src = embedUrl(d, false);
      el.frames.appendChild(frame);
    });
    STATE.active = STATE.config.dashboards[0].uid;
  }

  function select(uid) {
    STATE.active = uid;
    document.querySelectorAll(".tab").forEach(function (t) {
      t.classList.toggle("active", t.dataset.uid === uid);
    });
    document.querySelectorAll(".frame").forEach(function (f) {
      f.classList.toggle("visible", f.dataset.uid === uid);
    });
    updateHint();
  }

  function updateHint() {
    const d = STATE.config.dashboards.find(function (x) {
      return x.uid === STATE.active;
    });
    if (!d) { el.hint.textContent = ""; return; }
    el.hint.textContent = d.title + ": " + d.description + " ";
    const link = document.createElement("a");
    link.href = d.open_url;
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = "open in Grafana";
    el.hint.appendChild(link);
  }

  function reloadActiveFrame() {
    const frame = document.querySelector('.frame[data-uid="' + STATE.active + '"]');
    const d = STATE.config.dashboards.find(function (x) {
      return x.uid === STATE.active;
    });
    if (frame && d) { frame.src = embedUrl(d, true); }
  }

  function pill(backend) {
    const a = document.createElement("a");
    a.className = "pill " + (backend.up ? "up" : "down");
    a.href = backend.dashboard_url;
    a.target = "_blank";
    a.rel = "noopener";
    a.title = backend.up
      ? backend.label + " reachable (HTTP " + backend.status + ")"
      : backend.label + " unreachable" + (backend.error ? " (" + backend.error + ")" : "");
    const dot = document.createElement("span");
    dot.className = "dot";
    a.appendChild(dot);
    a.appendChild(document.createTextNode(backend.label));
    return a;
  }

  function metricCard(metric) {
    const card = document.createElement("div");
    card.className = "card" + (metric.ok ? "" : " stale");

    const label = document.createElement("div");
    label.className = "card-label";
    label.textContent = metric.label;
    card.appendChild(label);

    const value = document.createElement("div");
    value.className = "card-value";
    const shown = metric.ok
      ? Number(metric.value).toFixed(metric.precision)
      : "n/a";
    value.textContent = shown;
    if (metric.unit) {
      const unit = document.createElement("span");
      unit.className = "card-unit";
      unit.textContent = metric.unit;
      value.appendChild(unit);
    }
    card.appendChild(value);
    return card;
  }

  function pipelineClass(workflow) {
    if (workflow.status === "in_progress" || workflow.status === "queued" ||
        workflow.status === "requested") {
      return "running";
    }
    if (workflow.conclusion === "success") { return "up"; }
    if (workflow.conclusion) { return "down"; }  // failure, cancelled, timed_out...
    return "";  // never run
  }

  function pipelineChip(workflow) {
    const chip = document.createElement("a");
    chip.className = "pipeline " + pipelineClass(workflow);
    chip.href = workflow.url || "#";
    chip.target = "_blank";
    chip.rel = "noopener";

    const dot = document.createElement("span");
    dot.className = "dot";
    chip.appendChild(dot);
    chip.appendChild(document.createTextNode(workflow.name));

    const state = workflow.conclusion || workflow.status;
    const meta = document.createElement("span");
    meta.className = "meta";
    meta.textContent = state + (workflow.sha ? " \u00b7 " + workflow.sha : "");
    chip.appendChild(meta);

    chip.title = workflow.path + (workflow.branch ? " on " + workflow.branch : "") +
      (workflow.run_number ? " (run #" + workflow.run_number + ")" : "");
    return chip;
  }

  async function pollPipelines(force) {
    try {
      const res = await fetch("/api/pipelines" + (force ? "?force=1" : ""), {
        cache: "no-store",
      });
      if (!res.ok) { throw new Error("HTTP " + res.status); }
      const data = await res.json();

      el.pipelineList.textContent = "";
      if (data.repository_url) {
        el.pipelinesLink.href = data.repository_url;
        el.pipelinesLink.hidden = false;
      }
      if (!data.workflows || data.workflows.length === 0) {
        const note = document.createElement("span");
        note.className = "pill muted";
        note.textContent = data.configured
          ? (data.reason || "no workflow runs yet")
          : "pipeline status not configured (set GITHUB_REPOSITORY)";
        el.pipelineList.appendChild(note);
        return;
      }
      data.workflows.forEach(function (w) {
        el.pipelineList.appendChild(pipelineChip(w));
      });
    } catch (err) {
      el.pipelineList.textContent = "";
      const note = document.createElement("span");
      note.className = "pill down";
      note.textContent = "pipeline status unavailable: " + err.message;
      el.pipelineList.appendChild(note);
    }
  }

  async function pollMetrics(force) {
    try {
      const res = await fetch("/api/metrics" + (force ? "?force=1" : ""), {
        cache: "no-store",
      });
      if (!res.ok) { throw new Error("HTTP " + res.status); }
      const data = await res.json();
      el.metrics.textContent = "";
      data.metrics.forEach(function (m) { el.metrics.appendChild(metricCard(m)); });
      if (data.build && data.build.version) {
        document.querySelector(".subtitle").textContent =
          "local observability \u00b7 app v" + data.build.version +
          (data.build.environment ? " (" + data.build.environment + ")" : "");
      }
    } catch (err) {
      /* leave the previous cards in place */
    }
  }

  async function pollStatus(force) {
    try {
      const res = await fetch("/api/status" + (force ? "?force=1" : ""), {
        cache: "no-store",
      });
      if (!res.ok) { throw new Error("HTTP " + res.status); }
      const data = await res.json();

      el.status.textContent = "";
      data.backends.forEach(function (b) { el.status.appendChild(pill(b)); });

      let grafanaUp = false;
      data.backends.forEach(function (b) {
        if (b.key === "grafana") { grafanaUp = b.up; }
      });
      el.offline.hidden = grafanaUp;

      el.meta.textContent =
        data.up + "/" + data.total + " backends up | refresh " +
        STATE.config.refresh_seconds + "s | updated " +
        new Date(data.generated_at * 1000).toLocaleTimeString();
    } catch (err) {
      el.meta.textContent = "status unavailable: " + err.message;
    }
  }

  function armTimer() {
    if (STATE.timer) { clearInterval(STATE.timer); STATE.timer = null; }
    if (el.auto.checked) {
      STATE.timer = setInterval(function () {
        pollStatus(false);
        pollMetrics(false);
        pollPipelines(false);
      }, 5000);
    }
  }

  async function init() {
    try {
      const res = await fetch("/api/config", { cache: "no-store" });
      STATE.config = await res.json();
    } catch (err) {
      el.offline.hidden = false;
      el.meta.textContent = "cannot load dashboard config: " + err.message;
      return;
    }
    renderTabs();
    renderFrames();
    updateHint();
    pollStatus(false);
    pollMetrics(false);
    pollPipelines(false);
    armTimer();
  }

  el.refresh.addEventListener("click", function () {
    pollStatus(true);
    pollMetrics(true);
    pollPipelines(true);
    reloadActiveFrame();
  });

  el.auto.addEventListener("change", armTimer);

  document.addEventListener("keydown", function (e) {
    if (!STATE.config) { return; }
    const list = STATE.config.dashboards;
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      const idx = list.findIndex(function (d) { return d.uid === STATE.active; });
      const next = e.key === "ArrowRight" ? (idx + 1) % list.length
        : (idx - 1 + list.length) % list.length;
      select(list[next].uid);
    }
    if (e.key.toLowerCase() === "r" && !e.metaKey && !e.ctrlKey) {
      pollStatus(true);
      pollMetrics(true);
      pollPipelines(true);
      reloadActiveFrame();
    }
  });

  init();
})();
