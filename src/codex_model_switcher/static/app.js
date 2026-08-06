(function () {
  "use strict";

  let csrfToken = "";
  let providers = [];
  const API_TIMEOUT_MS = 30000;
  const ACTIONS = {
    "/api/config/apply": { label: "应用 Codex 配置", busy: "应用中…" },
    "/api/config/restore": { label: "恢复原配置", busy: "恢复中…" },
    "/api/router/start": { label: "启动 Router", busy: "启动中…" },
    "/api/router/stop": { label: "停止 Router", busy: "停止中…" },
  };

  const byId = (id) => document.getElementById(id);
  const text = (value, fallback) => typeof value === "string" && value ? value : fallback;
  const errorText = (error) => error && error.message ? error.message : "请求失败";

  function setFeedback(id, message, state) {
    const node = byId(id);
    if (!node) return;
    node.textContent = message || "";
    node.classList.remove("is-pending", "is-success", "is-error");
    if (state) node.classList.add(`is-${state}`);
    node.dataset.state = state || "idle";
    node.setAttribute("aria-live", "polite");
  }

  function setBusy(button, busyLabel) {
    if (!button) return () => {};
    const originalLabel = button.dataset.originalLabel || button.textContent;
    button.dataset.originalLabel = originalLabel;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = busyLabel;
    return () => {
      button.disabled = false;
      button.removeAttribute("aria-busy");
      button.textContent = originalLabel;
    };
  }

  async function api(path, options) {
    const config = options || {};
    const timeoutMs = config.timeoutMs || API_TIMEOUT_MS;
    const requestConfig = Object.assign({}, config);
    delete requestConfig.timeoutMs;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    const headers = Object.assign({ "Content-Type": "application/json" }, requestConfig.headers || {});
    if (csrfToken) headers["X-Codex-CSRF"] = csrfToken;
    try {
      const result = await fetch(path, Object.assign({}, requestConfig, {
        headers,
        signal: controller.signal,
      }));
      const nextToken = result.headers.get("X-Codex-CSRF");
      if (nextToken) csrfToken = nextToken;
      const raw = await result.text();
      let payload = {};
      try {
        payload = raw ? JSON.parse(raw) : {};
      } catch (_error) {
        payload = { error: { message: "服务器返回了无效响应" } };
      }
      if (!result.ok) {
        const issue = payload && payload.error && typeof payload.error.message === "string"
          ? payload.error.message
          : text(payload && payload.message, text(payload && payload.reason, text(
            payload && payload.status,
            "操作未完成"
          )));
        throw new Error(issue);
      }
      return payload;
    } catch (error) {
      if (error && error.name === "AbortError") {
        throw new Error("请求超时，请检查本地控制中心是否仍在运行");
      }
      if (error instanceof TypeError) {
        throw new Error("无法连接本地控制中心，请确认页面来自 127.0.0.1");
      }
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }

  function capabilitySummary(capabilities) {
    const items = [];
    if (capabilities && capabilities.supports_streaming) items.push("stream");
    if (capabilities && capabilities.supports_tools) items.push("tools");
    if (capabilities && capabilities.supports_images) items.push("images");
    if (capabilities && capabilities.supports_files) items.push("files");
    return items.length ? items.join(" · ") : "text only";
  }

  function renderProviders(items) {
    providers = Array.isArray(items) ? items : [];
    const rows = byId("provider-rows");
    const selector = byId("credential-provider");
    rows.replaceChildren();
    selector.replaceChildren();
    providers.forEach((provider) => {
      const row = document.createElement("tr");
      const lane = provider.id === "official" ? "Official" : "API";
      row.innerHTML = `<td><strong>${text(provider.name, provider.id)}</strong><small>${text(provider.base_url, "—")}</small></td>` +
        `<td><span class="mono">${text(provider.protocol, "—")}</span></td>` +
        `<td><strong>${text(provider.model, "—")}</strong><small>${lane}</small></td>` +
        `<td>${capabilitySummary(provider.capabilities)}</td>` +
        `<td><span class="state-pill ${provider.credential_configured ? "is-ready" : "is-muted"}">${provider.credential_configured ? "已配置" : "未配置"}</span></td>` +
        `<td><span class="probe-state">${text(provider.recent_probe && provider.recent_probe.status, "unknown")}</span>` +
        `<button class="inline-action" data-probe="${provider.id}">探测</button></td>`;
      rows.appendChild(row);
      if (provider.id !== "official") {
        const option = document.createElement("option");
        option.value = provider.id;
        option.textContent = provider.name || provider.id;
        selector.appendChild(option);
      }
    });
    if (!rows.children.length) rows.innerHTML = '<tr><td colspan="6" class="empty-cell">暂无 provider</td></tr>';
  }

  function renderStatus(status) {
    const router = status.router || {};
    const config = status.codex_config || {};
    const identity = status.official_identity || {};
    const models = Array.isArray(status.models) ? status.models : [];
    byId("router-status").textContent = text(router.status, "unknown");
    byId("router-detail").textContent = router.running
      ? text(router.address, "本地 Router 正在运行")
      : "等待明确启动动作";
    byId("config-status").textContent = config.applied ? "已应用" : "未应用";
    byId("config-detail").textContent = text(config.message, "尚未应用真实 Codex 配置");
    byId("identity-status").textContent = identity.available ? "可用" : "不可用";
    byId("identity-detail").textContent = "官方身份只读，不在此输入或导出";
    byId("routes-status").textContent = String(models.length);
    byId("routes-detail").textContent = "条模型路由已载入";
    byId("last-updated").textContent = `最后更新 ${new Date().toLocaleTimeString()}`;
  }

  async function refresh() {
    try {
      const status = await api("/api/status");
      renderStatus(status);
      renderProviders(status.providers);
      return { ok: true };
    } catch (error) {
      return { ok: false, error };
    }
  }

  document.addEventListener("click", async (event) => {
    const target = event.target;
    if (!target || typeof target.closest !== "function") return;
    const probeButton = target.closest("[data-probe]");
    const actionButton = target.closest("[data-action]");
    if (probeButton) {
      const providerId = probeButton.getAttribute("data-probe");
      const release = setBusy(probeButton, "探测中…");
      setFeedback("credential-result", `正在探测 ${providerId}，请稍候…`, "pending");
      try {
        const result = await api(
          `/api/providers/${encodeURIComponent(providerId)}/probe`,
          { method: "POST", body: "{}" }
        );
        const refreshed = await refresh();
        if (!refreshed.ok) {
          setFeedback("credential-result", `探测已返回，但状态刷新失败：${errorText(refreshed.error)}`, "error");
        } else if (result.status === "ok") {
          const latency = typeof result.latency_ms === "number" ? `（${result.latency_ms} ms）` : "";
          setFeedback("credential-result", `探测成功${latency}`, "success");
        } else {
          setFeedback("credential-result", `探测结果：${text(result.status, "unknown")}`, "error");
        }
      } catch (error) {
        setFeedback("credential-result", `探测失败：${errorText(error)}`, "error");
      } finally {
        release();
      }
      return;
    }
    if (actionButton) {
      const path = actionButton.getAttribute("data-action");
      const action = ACTIONS[path] || { label: "控制面操作", busy: "处理中…" };
      const release = setBusy(actionButton, action.busy);
      setFeedback("operation-result", `${action.label}，请稍候…`, "pending");
      try {
        const result = await api(path, { method: "POST", body: "{}" });
        const refreshed = await refresh();
        if (!refreshed.ok) {
          setFeedback("operation-result", `${action.label}已返回，但状态刷新失败：${errorText(refreshed.error)}`, "error");
        } else {
          const detail = text(result.message, text(result.address, ""));
          setFeedback(
            "operation-result",
            detail ? `${action.label}成功：${detail}` : `${action.label}成功`,
            "success"
          );
        }
      } catch (error) {
        setFeedback("operation-result", `${action.label}失败：${errorText(error)}`, "error");
      } finally {
        release();
      }
    }
  });

  const refreshButton = byId("refresh-button");
  refreshButton.addEventListener("click", async () => {
    const release = setBusy(refreshButton, "刷新中…");
    setFeedback("operation-result", "正在刷新状态，请稍候…", "pending");
    const result = await refresh();
    if (result.ok) {
      setFeedback("operation-result", "状态已刷新", "success");
    } else {
      setFeedback("operation-result", `刷新失败：${errorText(result.error)}`, "error");
    }
    release();
  });

  const credentialForm = byId("credential-form");
  credentialForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const providerId = byId("credential-provider").value;
    const keyField = byId("provider-key");
    const submitButton = event.submitter || credentialForm.querySelector("button[type=submit]");
    const release = setBusy(submitButton, "保存中…");
    setFeedback("credential-result", "正在保存凭据，请稍候…", "pending");
    try {
      if (!keyField.value.trim()) throw new Error("请输入 API key");
      const result = await api(`/api/providers/${encodeURIComponent(providerId)}/credential`, {
        method: "POST",
        body: JSON.stringify({ credential: keyField.value })
      });
      keyField.value = "";
      const refreshed = await refresh();
      if (!refreshed.ok) {
        setFeedback("credential-result", `凭据已返回，但状态刷新失败：${errorText(refreshed.error)}`, "error");
      } else {
        setFeedback(
          "credential-result",
          result.configured ? "凭据已保存并配置成功" : "凭据未配置",
          result.configured ? "success" : "error"
        );
      }
    } catch (error) {
      keyField.value = "";
      setFeedback("credential-result", `保存失败：${errorText(error)}`, "error");
    } finally {
      release();
    }
  });

  refresh().then((result) => {
    if (!result.ok) setFeedback("operation-result", `初始状态读取失败：${errorText(result.error)}`, "error");
  });
}());
