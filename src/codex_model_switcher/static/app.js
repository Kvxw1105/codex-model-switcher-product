(function () {
  "use strict";

  let csrfToken = "";
  let providers = [];

  const byId = (id) => document.getElementById(id);
  const text = (value, fallback) => typeof value === "string" && value ? value : fallback;

  async function api(path, options) {
    const config = options || {};
    const headers = Object.assign({ "Content-Type": "application/json" }, config.headers || {});
    if (csrfToken) headers["X-Codex-CSRF"] = csrfToken;
    const result = await fetch(path, Object.assign({}, config, { headers }));
    const nextToken = result.headers.get("X-Codex-CSRF");
    if (nextToken) csrfToken = nextToken;
    const payload = await result.json();
    if (!result.ok) {
      const issue = payload && payload.error
        ? payload.error.message
        : text(payload && payload.message, text(payload && payload.reason, "操作未完成"));
      throw new Error(issue);
    }
    return payload;
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
    } catch (error) {
      byId("operation-result").textContent = error.message;
    }
  }

  document.addEventListener("click", async (event) => {
    const probeButton = event.target.closest("[data-probe]");
    const actionButton = event.target.closest("[data-action]");
    try {
      if (probeButton) {
        const providerId = probeButton.getAttribute("data-probe");
        await api(`/api/providers/${encodeURIComponent(providerId)}/probe`, { method: "POST", body: "{}" });
        await refresh();
      }
      if (actionButton) {
        const path = actionButton.getAttribute("data-action");
        const result = await api(path, { method: "POST", body: "{}" });
        byId("operation-result").textContent = text(
          result.message,
          text(result.address, text(result.status, "操作完成"))
        );
        await refresh();
      }
    } catch (error) {
      byId("operation-result").textContent = error.message;
    }
  });

  byId("refresh-button").addEventListener("click", refresh);
  byId("credential-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const providerId = byId("credential-provider").value;
    const keyField = byId("provider-key");
    try {
      const result = await api(`/api/providers/${encodeURIComponent(providerId)}/credential`, {
        method: "POST",
        body: JSON.stringify({ credential: keyField.value })
      });
      keyField.value = "";
      byId("credential-result").textContent = result.configured ? "凭据已配置" : "凭据未配置";
      await refresh();
    } catch (error) {
      keyField.value = "";
      byId("credential-result").textContent = error.message;
    }
  });

  refresh();
}());
