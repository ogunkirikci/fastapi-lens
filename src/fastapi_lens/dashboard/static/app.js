"use strict";

const dashboard = document.querySelector("#dashboard");
const requestRows = document.querySelector("#request-rows");
const routeRows = document.querySelector("#route-rows");
const requestCount = document.querySelector("#request-count");
const traceDetail = document.querySelector("#trace-detail");
const detailTemplate = document.querySelector("#detail-template");
const notice = document.querySelector("#notice");
const refreshButton = document.querySelector("#refresh-button");
const clearButton = document.querySelector("#clear-button");
const tabs = [...document.querySelectorAll(".tab")];
const views = {
  requests: document.querySelector("#requests-view"),
  routes: document.querySelector("#routes-view"),
};

const state = {
  selectedTraceId: null,
  activeView: "requests",
};

function element(tagName, className, text) {
  const node = document.createElement(tagName);
  if (className) {
    node.className = className;
  }
  if (text !== undefined && text !== null) {
    node.textContent = String(text);
  }
  return node;
}

function svgElement(tagName, className) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tagName);
  if (className) {
    node.setAttribute("class", className);
  }
  return node;
}

function showNotice(message, kind = "") {
  notice.textContent = message;
  notice.className = kind ? `notice ${kind}` : "notice";
}

function formatMilliseconds(value) {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "—";
  }
  if (value < 1) {
    return `${value.toFixed(3)} ms`;
  }
  if (value < 100) {
    return `${value.toFixed(2)} ms`;
  }
  return `${value.toFixed(1)} ms`;
}

function formatStartedAt(value) {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return "Unknown";
  }
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    fractionalSecondDigits: 3,
  }).format(parsed);
}

function statusBadge(statusCode, complete = true) {
  let label = statusCode === null || statusCode === undefined ? "Pending" : statusCode;
  let kind = "pending";
  if (statusCode !== null && statusCode !== undefined) {
    kind = statusCode >= 500 ? "error" : "ok";
  }
  if (!complete) {
    label = statusCode === null || statusCode === undefined ? "Incomplete" : label;
    kind = "pending";
  }
  return element("span", `status-badge ${kind}`, label);
}

async function requestJson(url, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  const response = await fetch(url, {
    ...options,
    credentials: "same-origin",
    headers,
  });
  if (!response.ok) {
    let detail = `Request failed with status ${response.status}.`;
    try {
      const payload = await response.json();
      if (typeof payload.detail === "string") {
        detail = payload.detail;
      }
    } catch {
      // Preserve the generic error when the response is not JSON.
    }
    throw new Error(detail);
  }
  return response.json();
}

function emptyTable(tableBody, columns, message) {
  const row = element("tr");
  const cell = element("td", "empty-state", message);
  cell.colSpan = columns;
  row.append(cell);
  tableBody.replaceChildren(row);
}

function requestRow(item) {
  const row = element("tr", "request-row");
  row.tabIndex = 0;
  row.dataset.traceId = item.trace_id;
  if (item.trace_id === state.selectedTraceId) {
    row.classList.add("selected");
  }

  const started = element("td");
  started.append(
    element("span", "", formatStartedAt(item.started_at)),
    element("span", "secondary-line", item.complete ? "Complete" : "Incomplete"),
  );

  const request = element("td", "request-cell");
  const route = element("span", "request-route");
  route.append(
    element("span", "request-method", item.method),
    document.createTextNode(item.route || item.path),
  );
  request.append(route, element("span", "secondary-line", item.path));

  const status = element("td");
  status.append(statusBadge(item.status_code, item.complete));

  const response = element(
    "td",
    "",
    formatMilliseconds(item.response_complete_duration_ms),
  );
  const postResponse = element(
    "td",
    "",
    formatMilliseconds(item.post_response_duration_ms),
  );
  const sql = element("td", "", item.sql_query_count);
  const dependencies = element("td", "", item.dependency_count);
  const diagnostics = element("td", "", item.diagnostic_count);
  row.append(
    started,
    request,
    status,
    response,
    postResponse,
    sql,
    dependencies,
    diagnostics,
  );

  const select = () => loadTrace(item.trace_id);
  row.addEventListener("click", select);
  row.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      select();
    }
  });
  return row;
}

async function loadTraces() {
  try {
    const url = new URL(dashboard.dataset.tracesUrl);
    url.searchParams.set("limit", "100");
    url.searchParams.set("offset", "0");
    const payload = await requestJson(url);
    requestCount.textContent = `${payload.items.length} ${
      payload.items.length === 1 ? "trace" : "traces"
    }`;
    if (payload.items.length === 0) {
      emptyTable(requestRows, 8, "No traces have been captured yet.");
      return;
    }
    requestRows.replaceChildren(...payload.items.map(requestRow));
  } catch (error) {
    emptyTable(requestRows, 8, "Trace data could not be loaded.");
    showNotice(error.message, "error");
  }
}

function routeRow(item) {
  const row = element("tr");
  const values = [
    item.route,
    item.request_count,
    item.complete_count,
    formatMilliseconds(item.average_ms),
    formatMilliseconds(item.minimum_ms),
    formatMilliseconds(item.maximum_ms),
    formatMilliseconds(item.p50_ms),
    formatMilliseconds(item.p95_ms),
    formatMilliseconds(item.p99_ms),
    item.error_count,
  ];
  for (const value of values) {
    row.append(element("td", "", value));
  }
  return row;
}

async function loadRoutes() {
  try {
    const payload = await requestJson(dashboard.dataset.routesUrl);
    if (payload.items.length === 0) {
      emptyTable(routeRows, 10, "No route statistics are available yet.");
      return;
    }
    routeRows.replaceChildren(...payload.items.map(routeRow));
  } catch (error) {
    emptyTable(routeRows, 10, "Route statistics could not be loaded.");
    showNotice(error.message, "error");
  }
}

function metric(label, value) {
  const container = element("div", "metric");
  container.append(
    element("span", "metric-label", label),
    element("span", "metric-value", value),
  );
  return container;
}

function setDetailField(root, name, value) {
  const target = root.querySelector(`[data-field="${name}"]`);
  target.textContent = String(value);
}

function validInterval(start, end, boundsStart, boundsEnd) {
  return (
    Number.isFinite(start) &&
    Number.isFinite(end) &&
    end >= start &&
    start >= boundsStart &&
    end <= boundsEnd
  );
}

function mergedDuration(intervals) {
  if (intervals.length === 0) {
    return 0;
  }
  const ordered = intervals
    .map(([start, end]) => [start, end])
    .sort((left, right) => left[0] - right[0] || left[1] - right[1]);
  const merged = [ordered[0]];
  for (const interval of ordered.slice(1)) {
    const previous = merged[merged.length - 1];
    if (interval[0] > previous[1]) {
      merged.push(interval);
    } else {
      previous[1] = Math.max(previous[1], interval[1]);
    }
  }
  return merged.reduce((total, [start, end]) => total + end - start, 0);
}

function segmentSelfMilliseconds(segment, segments) {
  if (!Number.isFinite(segment.end_ns) || segment.end_ns < segment.start_ns) {
    return null;
  }
  const childIntervals = segments
    .filter((candidate) => candidate.parent_id === segment.id)
    .filter((candidate) =>
      validInterval(
        candidate.start_ns,
        candidate.end_ns,
        segment.start_ns,
        segment.end_ns,
      ),
    )
    .map((candidate) => [candidate.start_ns, candidate.end_ns]);
  return (
    (segment.end_ns - segment.start_ns - mergedDuration(childIntervals)) /
    1000000
  );
}

function unattributedMilliseconds(trace, lifecycleEnd) {
  if (!Number.isFinite(lifecycleEnd) || lifecycleEnd < trace.request_received_ns) {
    return null;
  }
  const topLevelIntervals = trace.segments
    .filter((segment) => segment.parent_id === null)
    .filter((segment) =>
      validInterval(
        segment.start_ns,
        segment.end_ns,
        trace.request_received_ns,
        lifecycleEnd,
      ),
    )
    .map((segment) => [segment.start_ns, segment.end_ns]);
  return (
    (lifecycleEnd -
      trace.request_received_ns -
      mergedDuration(topLevelIntervals)) /
    1000000
  );
}

function renderOverview(root, trace) {
  const overview = root.querySelector('[data-section="overview"]');
  overview.replaceChildren(
    metric("Method", trace.method),
    metric("Path", trace.path),
    metric("Started", formatStartedAt(trace.started_at)),
    metric("Complete", trace.complete ? "Yes" : "No"),
    metric("Application", formatMilliseconds(trace.application_duration_ms)),
    metric("Post-response", formatMilliseconds(trace.post_response_duration_ms)),
  );
}

function checkpointOffset(trace, checkpoint) {
  if (!Number.isFinite(checkpoint)) {
    return "Not observed";
  }
  return formatMilliseconds((checkpoint - trace.request_received_ns) / 1000000);
}

function renderLifecycle(root, trace) {
  const lifecycle = root.querySelector('[data-section="lifecycle"]');
  lifecycle.replaceChildren(
    metric("Request received", "0.000 ms"),
    metric(
      "Response started",
      checkpointOffset(trace, trace.response_started_ns),
    ),
    metric(
      "Response body complete",
      checkpointOffset(trace, trace.response_body_completed_ns),
    ),
    metric(
      "Application complete",
      checkpointOffset(trace, trace.application_completed_ns),
    ),
    metric("Response send", formatMilliseconds(trace.response_send_duration_ms)),
    metric(
      "Response complete",
      formatMilliseconds(trace.response_complete_duration_ms),
    ),
  );
}

function renderWaterfall(root, trace) {
  const container = root.querySelector('[data-section="waterfall"]');
  const lifecycleEnd =
    trace.application_completed_ns ||
    Math.max(
      trace.request_received_ns,
      ...trace.segments
        .map((segment) => segment.end_ns)
        .filter((value) => Number.isFinite(value)),
    );
  const lifecycleDuration = Math.max(
    1,
    lifecycleEnd - trace.request_received_ns,
  );
  const rows = [];

  for (const segment of trace.segments) {
    const row = element("div", "waterfall-row");
    const label = element("span", "waterfall-label", segment.name);
    label.title = `${segment.type} · ${segment.status}`;
    const track = svgElement("svg", "waterfall-track");
    track.setAttribute("viewBox", "0 0 100 20");
    track.setAttribute("preserveAspectRatio", "none");
    track.setAttribute("aria-hidden", "true");
    const bar = svgElement(
      "rect",
      `waterfall-bar ${segment.type.replaceAll("_", "-")}`,
    );
    const startRatio = Math.max(
      0,
      (segment.start_ns - trace.request_received_ns) / lifecycleDuration,
    );
    const end = Number.isFinite(segment.end_ns)
      ? segment.end_ns
      : lifecycleEnd;
    const widthRatio = Math.max(0, (end - segment.start_ns) / lifecycleDuration);
    const barStart = Math.min(100, startRatio * 100);
    const barWidth = Math.max(
      0,
      Math.min(100 - barStart, widthRatio * 100),
    );
    bar.setAttribute("x", String(barStart));
    bar.setAttribute("y", "3");
    bar.setAttribute("width", String(Math.max(0.35, barWidth)));
    bar.setAttribute("height", "14");
    bar.setAttribute("rx", "2");
    track.append(bar);
    const selfTime = segmentSelfMilliseconds(segment, trace.segments);
    const timing = element(
      "span",
      "waterfall-time",
      `${formatMilliseconds(segment.duration_ms)} / ${formatMilliseconds(
        selfTime,
      )} self`,
    );
    row.append(label, track, timing);
    rows.push(row);
  }

  if (rows.length === 0) {
    container.replaceChildren(
      element("p", "muted", "No execution segments were captured."),
    );
  } else {
    container.replaceChildren(...rows);
  }
  setDetailField(
    root,
    "unattributed",
    `${formatMilliseconds(
      unattributedMilliseconds(trace, lifecycleEnd),
    )} unattributed`,
  );
}

function dependencyDepth(dependency, byId) {
  let depth = 0;
  let parentId = dependency.parent_id;
  const visited = new Set([dependency.id]);
  while (parentId && byId.has(parentId) && !visited.has(parentId)) {
    visited.add(parentId);
    depth += 1;
    parentId = byId.get(parentId).parent_id;
  }
  return depth;
}

function renderDependencies(root, trace) {
  const container = root.querySelector('[data-section="dependencies"]');
  if (trace.logical_dependencies.length === 0) {
    container.replaceChildren(
      element("p", "muted", "No FastAPI dependencies were captured."),
    );
    return;
  }
  const tree = element("ol", "dependency-tree");
  const byId = new Map(
    trace.logical_dependencies.map((dependency) => [
      dependency.id,
      dependency,
    ]),
  );
  for (const dependency of trace.logical_dependencies) {
    const depth = Math.min(6, dependencyDepth(dependency, byId));
    const item = element(
      "li",
      `dependency-node dependency-depth-${depth}`,
      dependency.name,
    );
    const details = [
      dependency.cache_status,
      dependency.executed ? "executed" : "reused",
      dependency.scope ? `${dependency.scope} scope` : null,
    ].filter(Boolean);
    item.append(element("span", "dependency-meta", details.join(" · ")));
    tree.append(item);
  }
  container.replaceChildren(tree);
}

function renderSql(root, trace) {
  const container = root.querySelector('[data-section="sql"]');
  const queries = trace.segments.filter((segment) => segment.type === "sql");
  if (queries.length === 0) {
    container.replaceChildren(
      element("p", "muted", "No SQL queries were captured."),
    );
    return;
  }

  const table = element("table", "data-table");
  const head = element("thead");
  const headerRow = element("tr");
  for (const label of ["Operation", "Statement", "Duration", "Rows", "Status"]) {
    headerRow.append(element("th", "", label));
  }
  head.append(headerRow);
  const body = element("tbody");
  for (const query of queries) {
    const row = element("tr");
    const statementCell = element("td");
    statementCell.append(
      element(
        "code",
        "sql-statement",
        query.attributes.statement || "Statement unavailable",
      ),
    );
    row.append(
      element("td", "", query.attributes.operation || "SQL"),
      statementCell,
      element("td", "", formatMilliseconds(query.duration_ms)),
      element("td", "", query.attributes.row_count ?? "—"),
      element("td", "", query.status),
    );
    body.append(row);
  }
  table.append(head, body);
  container.replaceChildren(table);
}

function renderDiagnostics(root, trace) {
  const container = root.querySelector('[data-section="diagnostics"]');
  if (trace.diagnostics.length === 0) {
    container.replaceChildren(
      element("p", "muted", "No diagnostics were emitted."),
    );
    return;
  }
  const findings = trace.diagnostics.map((diagnostic) => {
    const card = element("article", "diagnostic");
    card.append(
      element(
        "strong",
        "",
        `${diagnostic.severity} · ${diagnostic.code}`,
      ),
      element("span", "", diagnostic.message),
    );
    return card;
  });
  container.replaceChildren(...findings);
}

function renderError(root, trace) {
  const section = root.querySelector('[data-section="error-section"]');
  const container = root.querySelector('[data-section="error"]');
  if (!trace.error) {
    section.hidden = true;
    return;
  }
  section.hidden = false;
  const parts = [`${trace.error.type}: ${trace.error.message}`];
  if (trace.error.stack) {
    parts.push(trace.error.stack);
  }
  container.replaceChildren(element("pre", "error-block", parts.join("\n\n")));
}

function renderTraceDetail(trace) {
  const fragment = detailTemplate.content.cloneNode(true);
  const root = fragment.querySelector(".detail-content");
  setDetailField(root, "route", trace.route || trace.path);
  setDetailField(root, "trace-id", trace.trace_id);
  const statusTarget = root.querySelector('[data-field="status"]');
  statusTarget.replaceWith(statusBadge(trace.status_code, trace.complete));
  renderOverview(root, trace);
  renderLifecycle(root, trace);
  renderWaterfall(root, trace);
  renderDependencies(root, trace);
  renderSql(root, trace);
  renderDiagnostics(root, trace);
  renderError(root, trace);
  traceDetail.replaceChildren(fragment);
}

async function loadTrace(traceId) {
  state.selectedTraceId = traceId;
  for (const row of requestRows.querySelectorAll(".request-row")) {
    row.classList.toggle("selected", row.dataset.traceId === traceId);
  }
  try {
    const base = dashboard.dataset.tracesUrl.replace(/\/$/, "");
    const payload = await requestJson(`${base}/${encodeURIComponent(traceId)}`);
    renderTraceDetail(payload.trace);
  } catch (error) {
    traceDetail.replaceChildren(
      element("div", "empty-detail", error.message),
    );
    showNotice(error.message, "error");
  }
}

function csrfHeaders() {
  if (dashboard.dataset.cookieAuthenticated !== "true") {
    return {};
  }
  const cookieName = dashboard.dataset.csrfCookieName;
  const cookie = document.cookie
    .split(";")
    .map((entry) => entry.trim())
    .find((entry) => entry.startsWith(`${cookieName}=`));
  if (!cookie) {
    return {};
  }
  const token = decodeURIComponent(cookie.slice(cookieName.length + 1));
  return { [dashboard.dataset.csrfHeaderName]: token };
}

async function clearTraces() {
  clearButton.disabled = true;
  try {
    const payload = await requestJson(dashboard.dataset.clearUrl, {
      method: "DELETE",
      headers: csrfHeaders(),
    });
    state.selectedTraceId = null;
    showNotice(
      `${payload.cleared_count} ${
        payload.cleared_count === 1 ? "trace was" : "traces were"
      } cleared from this store.`,
      "success",
    );
    await Promise.all([loadTraces(), loadRoutes()]);
    traceDetail.replaceChildren(
      element("div", "empty-detail", "Select a request to inspect its trace."),
    );
  } catch (error) {
    showNotice(error.message, "error");
  } finally {
    clearButton.disabled = false;
  }
}

function activateView(viewName) {
  state.activeView = viewName;
  for (const tab of tabs) {
    const active = tab.dataset.view === viewName;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  }
  for (const [name, view] of Object.entries(views)) {
    view.hidden = name !== viewName;
  }
  if (viewName === "routes") {
    loadRoutes();
  }
}

for (const tab of tabs) {
  tab.addEventListener("click", () => activateView(tab.dataset.view));
}

refreshButton.addEventListener("click", async () => {
  refreshButton.disabled = true;
  try {
    await Promise.all([loadTraces(), loadRoutes()]);
    if (state.selectedTraceId) {
      await loadTrace(state.selectedTraceId);
    }
    showNotice("Dashboard data refreshed.", "success");
  } finally {
    refreshButton.disabled = false;
  }
});

clearButton.addEventListener("click", clearTraces);
loadTraces();
loadRoutes();
