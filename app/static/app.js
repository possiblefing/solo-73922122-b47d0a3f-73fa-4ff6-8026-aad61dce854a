/* Nightly batch acceleration planner — page behaviour. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const els = {
    fileInput: $("file-input"),
    fileName: $("file-name"),
    dropzone: $("dropzone"),
    jsonInput: $("json-input"),
    parsePasted: $("parse-pasted"),
    loadSample: $("load-sample"),
    uploadErrors: $("upload-errors"),
    uploadOk: $("upload-ok"),
    configCard: $("config-card"),
    configHint: $("config-hint"),
    jobSelect: $("job-select"),
    secondsInput: $("seconds-input"),
    secondsHint: $("seconds-hint"),
    quickRow: $("quick-row"),
    configErrors: $("config-errors"),
    analyzeBtn: $("analyze-btn"),
    results: $("results"),
    targetPill: $("target-pill"),
    originalCompletion: $("original-completion"),
    originalCompletionSub: $("original-completion-sub"),
    predictedCompletion: $("predicted-completion"),
    predictedCompletionSub: $("predicted-completion-sub"),
    savedSeconds: $("saved-seconds"),
    savedSub: $("saved-sub"),
    resultNotes: $("result-notes"),
    chain: $("chain"),
    chainHint: $("chain-hint"),
    timelineAxis: $("timeline-axis"),
    timeline: $("timeline"),
    jobsBody: $("jobs-body"),
  };

  const state = {
    run: null,          // the validated record as sent to the API
    jobs: [],           // [{id, duration, depends_on, start, end, observed_wait}]
    busy: false,
  };

  const SAMPLE = {
    run_id: "2026-11-14 nightly",
    jobs: [
      { id: "extract_orders", depends_on: [], start: 0, end: 600 },
      { id: "extract_users", depends_on: [], start: 0, end: 420 },
      { id: "extract_goods", depends_on: [], start: 0, end: 300 },
      { id: "clean_orders", depends_on: ["extract_orders"], start: 660, end: 1100 },
      { id: "clean_users", depends_on: ["extract_users"], start: 480, end: 720 },
      { id: "clean_goods", depends_on: ["extract_goods"], start: 360, end: 540 },
      { id: "join_sales", depends_on: ["clean_orders", "clean_users"], start: 1160, end: 1520 },
      { id: "dim_goods", depends_on: ["clean_goods"], start: 600, end: 880 },
      { id: "enrich", depends_on: ["join_sales", "dim_goods"], start: 1580, end: 1820 },
      { id: "agg_daily", depends_on: ["enrich"], start: 1880, end: 2200 },
      { id: "agg_region", depends_on: ["enrich"], start: 1880, end: 2100 },
      { id: "quality_check", depends_on: ["agg_daily", "agg_region"], start: 2260, end: 2400 },
      { id: "export_bi", depends_on: ["quality_check"], start: 2460, end: 2700 },
      { id: "export_ads", depends_on: ["agg_region"], start: 2160, end: 2350 },
      { id: "notify", depends_on: ["export_bi", "export_ads"], start: 2760, end: 2800 }
    ]
  };

  /* ---------------------------------------------------------------- format */

  function round1(value) {
    return Math.round(value * 10) / 10;
  }

  function formatNumber(value) {
    const rounded = round1(value);
    return Number.isInteger(rounded)
      ? String(rounded)
      : rounded.toFixed(1).replace(/\.0$/, "");
  }

  function formatSpan(seconds) {
    const total = Math.round(seconds);
    const sign = total < 0 ? "−" : "";
    let rest = Math.abs(total);
    const h = Math.floor(rest / 3600);
    rest -= h * 3600;
    const m = Math.floor(rest / 60);
    const s = rest - m * 60;
    const parts = [];
    if (h) parts.push(h + " 小时");
    if (m) parts.push(m + " 分");
    if (s || parts.length === 0) parts.push(s + " 秒");
    return sign + parts.join(" ");
  }

  function clock(seconds) {
    const total = Math.max(0, Math.round(seconds));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    return [h, m, s].map((n) => String(n).padStart(2, "0")).join(":");
  }

  function deltaText(shift) {
    if (Math.abs(shift) < 1e-6) return "—";
    return (shift < 0 ? "−" : "+") + formatSpan(Math.abs(shift));
  }

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function setAlerts(container, messages, kind) {
    if (!messages || messages.length === 0) {
      container.hidden = true;
      container.innerHTML = "";
      return;
    }
    container.hidden = false;
    const items = messages
      .map((item) => {
        if (typeof item === "string") return "<li>" + escapeHtml(item) + "</li>";
        const field = item.field ? "<code>" + escapeHtml(item.field) + "</code> " : "";
        return "<li>" + field + escapeHtml(item.message) + "</li>";
      })
      .join("");
    container.innerHTML = (messages.length > 1 ? "<ul>" + items + "</ul>" : items);
  }

  /* ------------------------------------------------------------- uploading */

  function parseRecord(text) {
    let parsed;
    try {
      parsed = JSON.parse(text);
    } catch (error) {
      return { error: "JSON 解析失败：" + error.message };
    }
    if (Array.isArray(parsed)) parsed = { jobs: parsed };
    return { record: parsed };
  }

  async function validateAndLoad(record, sourceLabel) {
    setAlerts(els.uploadErrors, null);
    setAlerts(els.uploadOk, null);
    els.uploadOk.hidden = true;

    let response;
    try {
      response = await fetch("/api/inspect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ run: record }),
      });
    } catch (error) {
      resetConfig();
      state.run = null;
      state.jobs = [];
      setAlerts(els.uploadErrors, [
        { field: "network", message: "无法连接校验服务：" + error.message },
      ]);
      els.fileName.textContent = sourceLabel + " · 校验失败";
      return false;
    }
    const data = await response.json().catch(() => null);

    if (!response.ok || !data || data.ok !== true) {
      resetConfig();
      state.run = null;
      state.jobs = [];
      const errors = (data && data.errors) || [
        { field: "body", message: "服务返回了无法解析的响应。" },
      ];
      setAlerts(els.uploadErrors, errors);
      els.fileName.textContent = sourceLabel + " · 校验未通过";
      return false;
    }

    state.run = record;
    state.jobs = data.jobs;
    els.fileName.textContent = sourceLabel + " · 已校验";
    els.uploadOk.hidden = false;
    els.uploadOk.textContent =
      "记录有效：" +
      data.job_count +
      " 个作业，原本整批完成于 " +
      clock(data.original_completion) +
      "（" +
      formatSpan(data.original_completion) +
      "）。";
    populateJobSelect();
    return true;
  }

  function resetConfig() {
    els.jobSelect.innerHTML = '<option value="">— 请先上传记录 —</option>';
    els.jobSelect.disabled = true;
    els.secondsInput.value = "";
    els.secondsInput.disabled = true;
    els.secondsInput.removeAttribute("max");
    els.secondsHint.textContent = "上传记录后显示该作业的原耗时上限。";
    els.secondsHint.classList.remove("is-error");
    els.quickRow.hidden = true;
    els.analyzeBtn.disabled = true;
    els.configCard.classList.add("is-disabled");
    els.configCard.setAttribute("aria-disabled", "true");
    els.configHint.textContent = "请先上传一份有效的运行记录。";
    setAlerts(els.configErrors, null);
    els.results.hidden = true;
  }

  function populateJobSelect() {
    const options = ['<option value="">— 选择一项作业 —</option>'];
    for (const job of state.jobs) {
      const deps = job.depends_on.length
        ? "依赖 " + job.depends_on.join("、")
        : "无上游依赖";
      options.push(
        '<option value="' + escapeHtml(job.id) + '">' +
          escapeHtml(job.id) + " · 耗时 " + formatSpan(job.duration) + " · " +
          escapeHtml(deps) +
        "</option>"
      );
    }
    els.jobSelect.innerHTML = options.join("");
    els.jobSelect.disabled = false;
    els.configCard.classList.remove("is-disabled");
    els.configCard.setAttribute("aria-disabled", "false");
    els.configHint.textContent =
      "共 " + state.jobs.length + " 个作业。选择一项并填写拟缩短的秒数。";
    els.secondsInput.disabled = false;
    els.analyzeBtn.disabled = true;
    els.quickRow.hidden = true;
    updateSecondsHint();
  }

  function selectedJob() {
    const id = els.jobSelect.value;
    return state.jobs.find((job) => job.id === id) || null;
  }

  function updateSecondsHint() {
    const job = selectedJob();
    els.secondsHint.classList.remove("is-error");
    if (!job) {
      els.secondsInput.removeAttribute("max");
      els.secondsHint.textContent = "请先选择一项作业。";
      els.quickRow.hidden = true;
      els.analyzeBtn.disabled = true;
      return;
    }
    els.secondsInput.max = String(job.duration);
    els.secondsHint.textContent =
      "“" + job.id + "” 原耗时 " + formatSpan(job.duration) +
      "（" + formatNumber(job.duration) + " 秒），缩短量不得超过该值。";
    els.quickRow.hidden = false;
    validateRequest();
  }

  function validateRequest() {
    const job = selectedJob();
    const raw = els.secondsInput.value.trim();
    const seconds = Number(raw);
    let message = null;

    if (!job) {
      els.analyzeBtn.disabled = true;
      return;
    }
    if (raw === "" || !Number.isFinite(seconds)) {
      message = "请输入拟缩短的秒数。";
    } else if (seconds <= 0) {
      message = "缩短秒数必须大于 0。";
    } else if (seconds > job.duration + 1e-6) {
      message = "缩短量不能超过该作业的原耗时 " + formatSpan(job.duration) + "。";
    }

    if (message) {
      els.secondsHint.textContent = message;
      els.secondsHint.classList.add("is-error");
      els.analyzeBtn.disabled = true;
    } else {
      els.secondsHint.textContent =
        "“" + job.id + "” 原耗时 " + formatSpan(job.duration) +
        "（" + formatNumber(job.duration) + " 秒），缩短后为 " +
        formatSpan(job.duration - seconds) + "。";
      els.secondsHint.classList.remove("is-error");
      els.analyzeBtn.disabled = false;
    }
  }

  async function handleFile(file) {
    if (!file) return;
    let text;
    try {
      text = await file.text();
    } catch (error) {
      resetConfig();
      setAlerts(els.uploadErrors, [
        { field: "file", message: "无法读取文件：" + error.message },
      ]);
      return;
    }
    const parsed = parseRecord(text);
    if (parsed.error) {
      resetConfig();
      setAlerts(els.uploadErrors, [parsed.error]);
      els.fileName.textContent = file.name + " · 解析失败";
      return;
    }
    els.jsonInput.value = JSON.stringify(parsed.record, null, 2);
    await validateAndLoad(parsed.record, file.name);
  }

  /* ------------------------------------------------------------- analysing */

  async function analyze() {
    if (state.busy || !state.run) return;
    const job = selectedJob();
    const seconds = Number(els.secondsInput.value);
    if (!job || !Number.isFinite(seconds) || seconds <= 0) return;

    state.busy = true;
    els.analyzeBtn.disabled = true;
    els.analyzeBtn.classList.add("is-loading");
    setAlerts(els.configErrors, null);
    try {
      const response = await fetch("/api/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ run: state.run, job_id: job.id, seconds }),
      });
      const data = await response.json().catch(() => null);
      if (!response.ok || !data || data.ok !== true) {
        els.results.hidden = true;
        const errors = (data && data.errors) || [
          { field: "body", message: "服务返回了无法解析的响应。" },
        ];
        setAlerts(els.configErrors, errors);
        return;
      }
      render(data);
    } catch (error) {
      els.results.hidden = true;
      setAlerts(els.configErrors, [
        { field: "network", message: "无法连接测算服务：" + error.message },
      ]);
    } finally {
      state.busy = false;
      els.analyzeBtn.classList.remove("is-loading");
      els.analyzeBtn.disabled = false;
      validateRequest();
    }
  }

  function render(data) {
    const summary = data.summary;
    els.results.hidden = false;

    els.targetPill.textContent = "目标作业：" + summary.target_job;
    els.originalCompletion.textContent = formatSpan(summary.original_completion);
    els.originalCompletionSub.textContent = "T+" + clock(summary.original_completion);
    els.predictedCompletion.textContent = formatSpan(summary.predicted_completion);
    els.predictedCompletionSub.textContent = "T+" + clock(summary.predicted_completion);
    els.savedSeconds.textContent = formatSpan(summary.saved_seconds);
    els.savedSub.textContent =
      "拟缩短 " + formatSpan(summary.requested_seconds) +
      " · 整批重算后实际提前 " + formatNumber(summary.saved_seconds) + " 秒";

    renderNotes(summary);
    renderChain(data);
    renderTimeline(data);
    renderTable(data);
    els.results.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function renderNotes(summary) {
    const notes = [];
    const capped = summary.saving_capped_at;

    if (capped) {
      notes.push({
        kind: "warn",
        icon: "!",
        text:
          "并行分支接替：缩短后的 “" + summary.target_job + "” 一路传导，到 “" +
          capped.job + "” 时被上游 “" + capped.limiting_dependency +
          "”（完成于 " + clock(capped.limiting_end) + "）挡住——该处只提前了 " +
          formatSpan(capped.reduction) + "，而不是请求的 " +
          formatSpan(summary.requested_seconds) + "。",
      });
    } else if (summary.bottleneck_changed) {
      notes.push({
        kind: "warn",
        icon: "!",
        text:
          "整批瓶颈转移：原本由 “" + summary.original_bottleneck +
          "” 决定整批完成，缩短后改由 “" + summary.predicted_bottleneck +
          "” 决定；收益已按全批重新计算。",
      });
    }

    if (!summary.fully_realised) {
      notes.push({
        kind: "warn",
        icon: "!",
        text:
          "要求缩短 " + formatSpan(summary.requested_seconds) +
          "，整批实际只提前 " + formatSpan(summary.saved_seconds) +
          "，少 " + formatSpan(summary.requested_seconds - summary.saved_seconds) +
          "——差额被并行分支的耗时占据。",
      });
    } else {
      notes.push({
        kind: "info",
        icon: "✓",
        text:
          "缩短量全部转化为整批提前交付：目标作业每省 1 秒，整批完成时刻同步提前 1 秒。",
      });
    }

    notes.push({
      kind: "info",
      icon: "i",
      text:
        "本次共 " + summary.changed_job_count + " 个作业的时刻发生变化；" +
        "整批作业数 " + summary.job_count + "，原本完成于 " +
        clock(summary.original_completion) + "。",
    });

    if (summary.chain_includes_target) {
      notes.push({
        kind: "info",
        icon: "i",
        text: "目标作业位于决定预计完成时刻的依赖链上。",
      });
    } else if (!capped) {
      notes.push({
        kind: "warn",
        icon: "!",
        text:
          "目标作业不在决定预计完成时刻的依赖链上，缩短它没有改变整批完成时刻，" +
          "整批提前量为 0。",
      });
    }

    els.resultNotes.innerHTML = notes
      .map(
        (note) =>
          '<div class="note is-' + note.kind + '">' +
          '<span class="note-icon" aria-hidden="true">' + note.icon + "</span>" +
          "<span>" + escapeHtml(note.text) + "</span></div>"
      )
      .join("");
  }

  function renderChain(data) {
    const chain = data.critical_chain;
    const bottleneck = data.summary.predicted_bottleneck;
    els.chainHint.textContent =
      "从批次起点沿依赖关系走到决定整批完成时刻的作业 “" +
      bottleneck + "”。链上每一步都真正推迟了下游的开始，因此这条链决定整批何时结束。";
    const parts = [];
    chain.forEach((row, index) => {
      const classes = ["chain-node"];
      if (row.is_target) classes.push("is-target");
      if (row.id === bottleneck) classes.push("is-bottleneck");
      parts.push(
        '<div class="' + classes.join(" ") + '">' +
          '<span class="chain-name">' + escapeHtml(row.id) + "</span>" +
          '<span class="chain-meta">' + clock(row.predicted_start) + " → " +
            clock(row.predicted_end) + " · " + formatSpan(row.predicted_duration) + "</span>" +
        "</div>"
      );
      if (index < chain.length - 1) {
        parts.push('<span class="chain-arrow" aria-hidden="true">→</span>');
      }
    });
    els.chain.innerHTML = parts.join("");
  }

  function renderTimeline(data) {
    const rows = data.jobs;
    const maxEnd = Math.max(
      data.summary.original_completion,
      data.summary.predicted_completion,
      1
    );
    const axis = [];
    for (let i = 0; i <= 4; i += 1) {
      const value = (maxEnd * i) / 4;
      const cls = i === 0 ? " is-first" : i === 4 ? " is-last" : "";
      axis.push(
        '<span class="timeline-tick' + cls + '" style="left:' + (i * 25) + '%">' +
          clock(value) + "</span>"
      );
    }
    els.timelineAxis.innerHTML = axis.join("");

    const html = rows.map((row) => {
      const pct = (value) => Math.min(100, Math.max(0, (value / maxEnd) * 100));
      const origLeft = pct(row.original_start);
      const origWidth = Math.max(0.4, pct(row.original_end) - origLeft);
      const predLeft = pct(row.predicted_start);
      const predWidth = Math.max(0.4, pct(row.predicted_end) - predLeft);
      const classes = ["timeline-row"];
      if (row.is_target) classes.push("is-target");
      else if (row.on_critical_chain) classes.push("is-chain");

      const bars = [];
      if (row.predicted_end < row.original_end - 1e-6) {
        bars.push(
          '<span class="bar is-original" style="left:' + origLeft + "%;width:" +
            origWidth + '%" title="原本 ' + clock(row.original_start) + " → " +
            clock(row.original_end) + '"></span>'
        );
      }
      bars.push(
        '<span class="bar is-predicted' + (row.on_critical_chain && !row.is_target ? " is-chain" : "") +
          '" style="left:' + predLeft + "%;width:" + predWidth +
          '%" title="预计 ' + clock(row.predicted_start) + " → " + clock(row.predicted_end) +
          '"></span>'
      );
      return (
        '<div class="' + classes.join(" ") + '">' +
          '<span class="timeline-label" title="' + escapeHtml(row.id) + '">' +
            escapeHtml(row.id) + "</span>" +
          '<span class="timeline-track">' + bars.join("") + "</span>" +
        "</div>"
      );
    });
    els.timeline.innerHTML = html.join("");
  }

  function renderTable(data) {
    const html = data.jobs.map((row) => {
      const classes = [];
      if (row.is_target) classes.push("is-target");
      const tags = [];
      if (row.is_target) tags.push('<span class="job-tag target">目标</span>');
      if (row.on_critical_chain) tags.push('<span class="job-tag chain">关键链</span>');
      const deps = row.depends_on.length
        ? row.depends_on.map((dep) => "<span>" + escapeHtml(dep) + "</span>").join("")
        : "—";
      let deltaClass = "delta";
      if (Math.abs(row.end_shift) < 1e-6) deltaClass += " is-zero";
      else if (row.end_shift < 0) deltaClass += " is-early";

      return (
        '<tr' + (classes.length ? ' class="' + classes.join(" ") + '"' : "") + ">" +
          '<td><span class="job-name">' + escapeHtml(row.id) + "</span>" +
            tags.join("") + "</td>" +
          '<td class="dep-list">' + deps + "</td>" +
          '<td class="num">' + clock(row.original_start) + "</td>" +
          '<td class="num">' + clock(row.original_end) + "</td>" +
          '<td class="num">' + formatNumber(row.observed_wait) + " 秒</td>" +
          '<td class="num">' + clock(row.predicted_start) + "</td>" +
          '<td class="num">' + clock(row.predicted_end) + "</td>" +
          '<td class="num ' + deltaClass + '">' + deltaText(row.end_shift) + "</td>" +
        "</tr>"
      );
    });
    els.jobsBody.innerHTML = html.join("");
  }

  /* ---------------------------------------------------------------- events */

  els.fileInput.addEventListener("change", (event) => {
    handleFile(event.target.files && event.target.files[0]);
  });

  ["dragenter", "dragover"].forEach((type) => {
    els.dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      els.dropzone.classList.add("dragging");
    });
  });
  ["dragleave", "drop"].forEach((type) => {
    els.dropzone.addEventListener(type, (event) => {
      event.preventDefault();
      els.dropzone.classList.remove("dragging");
    });
  });
  els.dropzone.addEventListener("drop", (event) => {
    const file = event.dataTransfer && event.dataTransfer.files[0];
    if (file) handleFile(file);
  });

  els.parsePasted.addEventListener("click", async () => {
    const text = els.jsonInput.value.trim();
    if (!text) {
      setAlerts(els.uploadErrors, [{ field: "json", message: "请先粘贴 JSON 内容。" }]);
      return;
    }
    const parsed = parseRecord(text);
    if (parsed.error) {
      resetConfig();
      setAlerts(els.uploadErrors, [parsed.error]);
      return;
    }
    await validateAndLoad(parsed.record, "粘贴内容");
  });

  els.loadSample.addEventListener("click", async () => {
    els.jsonInput.value = JSON.stringify(SAMPLE, null, 2);
    els.fileInput.value = "";
    await validateAndLoad(SAMPLE, "示例批次");
  });

  els.jobSelect.addEventListener("change", updateSecondsHint);
  els.secondsInput.addEventListener("input", validateRequest);
  els.secondsInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !els.analyzeBtn.disabled) analyze();
  });
  els.analyzeBtn.addEventListener("click", analyze);

  els.quickRow.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-fraction]");
    if (!button) return;
    const job = selectedJob();
    if (!job) return;
    const fraction = Number(button.dataset.fraction);
    const seconds = round1(job.duration * fraction);
    els.secondsInput.value = String(seconds);
    validateRequest();
  });

  resetConfig();
})();
