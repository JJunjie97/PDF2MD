(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const controls = {
    source: $("#source-path"),
    output: $("#output-path"),
    pages: $("#pages"),
    profile: $("#profile"),
    method: $("#method"),
    language: $("#language"),
    timeout: $("#timeout"),
    force: $("#force"),
    choosePdf: $("#choose-pdf"),
    chooseOutput: $("#choose-output"),
    convert: $("#convert"),
    cancel: $("#cancel"),
    openOutput: $("#open-output"),
    status: $("#status-text"),
    percent: $("#progress-percent"),
    fill: $("#progress-fill"),
    error: $("#error-banner"),
    errorText: $("#error-text"),
  };

  let ready = false;
  let running = false;
  let progress = 0;
  let outputIsCustom = false;
  let hasResult = false;

  controls.convert.disabled = true;

  function setStatus(message) {
    const clean = String(message || "").replace(/[。；\r\n].*$/s, "").trim();
    controls.status.textContent = clean || (running ? "转换中" : "就绪");
  }

  function setProgress(value, message, reset = false) {
    const parsed = Number(value);
    const bounded = Number.isFinite(parsed) ? Math.min(100, Math.max(0, parsed)) : progress;
    progress = reset ? bounded : Math.max(progress, bounded);
    const rounded = Math.round(progress);
    controls.fill.style.width = `${progress}%`;
    controls.percent.textContent = `${rounded}%`;
    if (message) setStatus(message);
  }

  function showError(message) {
    controls.errorText.textContent = String(message || "发生未知错误");
    controls.error.hidden = false;
  }

  function hideError() {
    controls.error.hidden = true;
    controls.errorText.textContent = "";
  }

  function setRunning(value) {
    running = Boolean(value);
    document.body.dataset.state = running ? "running" : document.body.dataset.state || "idle";
    [
      controls.source,
      controls.output,
      controls.pages,
      controls.profile,
      controls.method,
      controls.language,
      controls.timeout,
      controls.force,
      controls.choosePdf,
      controls.chooseOutput,
    ].forEach((control) => {
      control.disabled = running;
    });
    controls.convert.disabled = running || !ready;
    controls.cancel.disabled = !running;
    controls.openOutput.disabled = running || !hasResult;
  }

  async function callApi(name, ...args) {
    if (!ready || !window.pywebview?.api?.[name]) {
      throw new Error("本地界面尚未就绪");
    }
    return window.pywebview.api[name](...args);
  }

  async function runAction(action) {
    hideError();
    try {
      return await action();
    } catch (error) {
      showError(error?.message || error);
      return null;
    }
  }

  function applyApiError(result) {
    if (result && result.ok === false) {
      showError(result.message || "操作失败");
      return true;
    }
    return false;
  }

  controls.choosePdf.addEventListener("click", () => runAction(async () => {
    const result = await callApi("choose_pdf", controls.source.value, outputIsCustom);
    if (!result || applyApiError(result) || !result.path) return;
    controls.source.value = result.path;
    if (!outputIsCustom && result.output) controls.output.value = result.output;
    hasResult = false;
    document.body.dataset.state = "idle";
    setProgress(0, "就绪", true);
    setRunning(false);
  }));

  controls.chooseOutput.addEventListener("click", () => runAction(async () => {
    const result = await callApi("choose_output", controls.output.value, controls.source.value);
    if (!result || applyApiError(result) || !result.path) return;
    controls.output.value = result.path;
    outputIsCustom = true;
    hasResult = false;
    setRunning(false);
  }));

  controls.source.addEventListener("change", async () => {
    if (outputIsCustom || !controls.source.value.trim()) return;
    const result = await runAction(() => callApi("default_output", controls.source.value));
    if (result?.ok && result.output) controls.output.value = result.output;
  });

  controls.output.addEventListener("input", () => {
    outputIsCustom = Boolean(controls.output.value.trim());
    hasResult = false;
    controls.openOutput.disabled = true;
  });

  controls.pages.addEventListener("focus", () => {
    if (controls.pages.value.trim() === "全文") controls.pages.select();
  });

  controls.convert.addEventListener("click", () => runAction(async () => {
    hideError();
    hasResult = false;
    setProgress(1, "准备", true);
    document.body.dataset.state = "running";
    setRunning(true);
    let result;
    try {
      result = await callApi("start_conversion", {
        source: controls.source.value,
        output: controls.output.value,
        pages: controls.pages.value,
        profile: controls.profile.value,
        method: controls.method.value,
        language: controls.language.value,
        timeout: controls.timeout.value,
        force: controls.force.checked,
      });
    } catch (error) {
      document.body.dataset.state = "error";
      setStatus("无法开始");
      setRunning(false);
      throw error;
    }
    if (applyApiError(result)) {
      document.body.dataset.state = "error";
      setStatus("无法开始");
      setRunning(false);
      return;
    }
    if (result?.output) controls.output.value = result.output;
  }));

  controls.cancel.addEventListener("click", () => runAction(async () => {
    controls.cancel.disabled = true;
    setStatus("正在取消");
    const result = await callApi("cancel");
    applyApiError(result);
  }));

  controls.openOutput.addEventListener("click", () => runAction(async () => {
    const result = await callApi("open_output");
    applyApiError(result);
  }));

  $("#dismiss-error").addEventListener("click", hideError);
  $("#minimize-button").addEventListener("click", () => runAction(() => callApi("minimize")));
  $("#maximize-button").addEventListener("click", () => runAction(() => callApi("toggle_maximize")));
  $("#close-button").addEventListener("click", () => runAction(() => callApi("close_window")));
  $("#drag-region").addEventListener("dblclick", () => runAction(() => callApi("toggle_maximize")));

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      if (!controls.error.hidden) hideError();
      else if (running) controls.cancel.click();
    }
    if (event.ctrlKey && event.key === "Enter" && !controls.convert.disabled) {
      controls.convert.click();
    }
  });

  window.PDF2MD = {
    receive(type, payload = {}) {
      switch (type) {
        case "progress":
          setProgress(payload.percent, payload.message);
          break;
        case "message":
          setStatus(payload.message || payload);
          break;
        case "complete": {
          hasResult = true;
          document.body.dataset.state = "complete";
          setProgress(100, "完成");
          const elapsed = Number(payload.elapsed_seconds);
          if (Number.isFinite(elapsed) && elapsed > 0) setStatus(`完成 · ${elapsed.toFixed(1)} 秒`);
          if (payload.output_dir) controls.output.value = payload.output_dir;
          setRunning(false);
          break;
        }
        case "cancelled":
          document.body.dataset.state = "idle";
          setStatus("已取消");
          setRunning(false);
          break;
        case "error":
          document.body.dataset.state = "error";
          setStatus("转换失败");
          showError(payload.message || payload);
          setRunning(false);
          break;
        default:
          break;
      }
    },
  };

  window.addEventListener("pywebviewready", () => {
    ready = true;
    document.body.dataset.state = "idle";
    setStatus("就绪");
    setRunning(false);
  });
})();
