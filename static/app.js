"use strict";

const view = document.getElementById("view");

/* ---------- helpers ---------- */

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined) continue;
    node.append(child.nodeType ? child : document.createTextNode(child));
  }
  return node;
}

async function api(path) {
  const resp = await fetch("/api/" + path);
  if (!resp.ok) throw new Error(`API returned ${resp.status} for /api/${path}`);
  return resp.json();
}

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

function fmtDate(iso) {
  return iso ? iso.slice(0, 16).replace("T", " ") : "—";
}

function statusBadge(status) {
  const cls = { ACTIVE: "good", MISSING: "critical", DECOMMISSIONED: "muted" }[status] || "muted";
  return el("span", { class: `badge badge-${cls}` }, status);
}

function verLabel(v) {
  return `${v.version}-${v.release}`;
}

function table(headers, rows, emptyText) {
  if (!rows.length) return el("p", { class: "muted" }, emptyText || "Nothing to show.");
  return el("div", { class: "table-wrap" },
    el("table", { class: "data" },
      el("thead", {}, el("tr", {}, headers.map(h =>
        typeof h === "string" || h.nodeType ? el("th", {}, h) : h))),
      el("tbody", {}, rows)));
}

function goto(hash) { location.hash = hash; }

let filterOpts = null;
async function getFilterOpts() {
  if (!filterOpts) {
    const s = await api("stats");
    filterOpts = {
      os: s.os_counts.map(r => r.os).filter(Boolean),
      beheergroep: s.beheergroep_counts.map(r => r.beheergroep).filter(Boolean),
    };
  }
  return filterOpts;
}

function select(options, value, allLabel, onchange) {
  return el("select", { onchange: e => onchange(e.target.value) },
    el("option", { value: "" }, allLabel),
    options.map(o => {
      const opt = el("option", { value: o }, o);
      if (o === value) opt.selected = true;
      return opt;
    }));
}

/* ---------- dashboard ---------- */

function tile(label, value, href) {
  return el("a", { class: "tile", href },
    el("div", { class: "tile-value" }, String(value)),
    el("div", { class: "tile-label" }, label));
}

function barList(title, items, labelKey) {
  const max = Math.max(1, ...items.map(i => Number(i.n)));
  return el("section", { class: "card" },
    el("h3", {}, title),
    items.length
      ? items.map(item =>
          el("div", { class: "bar-row", title: `${item[labelKey]}: ${item.n}` },
            el("span", { class: "bar-label" }, item[labelKey] ?? "—"),
            el("div", { class: "bar-track" },
              el("div", { class: "bar-fill", style: `width:${(Number(item.n) / max) * 100}%` })),
            el("span", { class: "bar-value" }, String(item.n))))
      : el("p", { class: "muted" }, "No data."));
}

async function dashboardView() {
  const s = await api("stats");
  const status = Object.fromEntries(s.status_counts.map(r => [r.inventory_status, Number(r.n)]));
  view.replaceChildren(
    el("h2", {}, "Dashboard"),
    el("div", { class: "tiles" },
      tile("Active servers", status.ACTIVE || 0, "#/servers"),
      tile("Missing servers", status.MISSING || 0, "#/servers"),
      tile("Tracked packages", s.package_count, "#/packages"),
      tile("Packages with drift", s.drifting_packages, "#/drift"),
      tile("Servers behind", s.servers_behind, "#/drift"),
      tile("Last import", s.last_run ? fmtDate(s.last_run.completed_at || s.last_run.started_at) : "never", "#/runs")),
    el("div", { class: "cards" },
      barList("Active servers per beheergroep", s.beheergroep_counts, "beheergroep"),
      barList("Active servers per OS", s.os_counts, "os"),
      barList("Most installed extra packages", s.top_packages, "name")));
}

/* ---------- drift ---------- */

const driftState = { q: "", os: "", beheergroep: "" };

async function driftView() {
  const opts = await getFilterOpts();
  const results = el("div", {}, el("p", { class: "muted" }, "Loading…"));

  async function refresh() {
    const params = new URLSearchParams();
    for (const [k, v] of Object.entries(driftState)) if (v) params.set(k, v);
    const groups = await api("drift?" + params);
    results.replaceChildren(table(
      ["Package", "OS", "Version spread (newest first)", el("th", { class: "num" }, "Servers behind")],
      groups.map(g =>
        el("tr", { class: "clickable", onclick: () => goto(`#/packages/${g.package_id}`) },
          el("td", {}, g.name),
          el("td", {}, g.os),
          el("td", {}, el("div", { class: "chips" }, g.versions.map(v =>
            el("span", { class: "chip " + (v.is_latest ? "chip-latest" : "chip-behind") },
              `${v.is_latest ? "✓" : "↓"} ${verLabel(v)} × ${v.server_count}`)))),
          el("td", { class: "num" }, String(g.behind_count)))),
      "No version drift found — every package is at one version per OS."));
  }

  const search = el("input", {
    type: "search", placeholder: "Filter packages…", value: driftState.q,
    oninput: debounce(e => { driftState.q = e.target.value; refresh(); }, 250),
  });

  view.replaceChildren(
    el("h2", {}, "Package drift"),
    el("div", { class: "filters" },
      search,
      select(opts.os, driftState.os, "All OS", v => { driftState.os = v; refresh(); }),
      select(opts.beheergroep, driftState.beheergroep, "All beheergroepen",
        v => { driftState.beheergroep = v; refresh(); })),
    results);
  refresh();
}

/* ---------- servers ---------- */

const serverState = { q: "", beheergroep: "", os: "", status: "", sort: "hostname", dir: "asc" };

async function serversView() {
  const opts = await getFilterOpts();
  const results = el("div", {}, el("p", { class: "muted" }, "Loading…"));

  function sortableTh(label, key, extraClass) {
    const active = serverState.sort === key;
    const arrow = active ? (serverState.dir === "asc" ? " ▲" : " ▼") : "";
    return el("th", {
      class: "sortable" + (extraClass ? ` ${extraClass}` : ""),
      onclick: () => {
        if (serverState.sort === key) {
          serverState.dir = serverState.dir === "asc" ? "desc" : "asc";
        } else {
          serverState.sort = key;
          serverState.dir = "asc";
        }
        refresh();
      },
    }, label + arrow);
  }

  async function refresh() {
    const params = new URLSearchParams();
    for (const [k, v] of Object.entries(serverState)) if (v) params.set(k, v);
    const servers = await api("servers?" + params);
    results.replaceChildren(table(
      [
        sortableTh("Hostname", "hostname"),
        sortableTh("Beheergroep", "beheergroep"),
        sortableTh("Owner", "owner"),
        sortableTh("OS", "os"),
        sortableTh("SL", "servicelevel"),
        sortableTh("Status", "inventory_status"),
        sortableTh("Packages", "package_count", "num"),
        el("th", { class: "num" }, "Behind"),
        sortableTh("Last seen", "last_seen"),
      ],
      servers.map(s =>
        el("tr", { class: "clickable", onclick: () => goto(`#/servers/${s.id}`) },
          el("td", {}, s.hostname),
          el("td", {}, s.beheergroep ?? "—"),
          el("td", {}, s.owner ?? "—"),
          el("td", {}, `${s.os} ${s.osversie}`),
          el("td", {}, s.servicelevel ?? "—"),
          el("td", {}, statusBadge(s.inventory_status)),
          el("td", { class: "num" }, String(s.package_count)),
          el("td", { class: "num" }, s.behind_count
            ? el("span", { class: "badge badge-warn" }, String(s.behind_count))
            : "0"),
          el("td", {}, fmtDate(s.last_seen)))),
      "No servers match these filters."));
  }

  view.replaceChildren(
    el("h2", {}, "Servers"),
    el("div", { class: "filters" },
      el("input", {
        type: "search", placeholder: "Search hostname…", value: serverState.q,
        oninput: debounce(e => { serverState.q = e.target.value; refresh(); }, 250),
      }),
      select(opts.beheergroep, serverState.beheergroep, "All beheergroepen",
        v => { serverState.beheergroep = v; refresh(); }),
      select(opts.os, serverState.os, "All OS", v => { serverState.os = v; refresh(); }),
      select(["ACTIVE", "MISSING", "DECOMMISSIONED"], serverState.status, "All statuses",
        v => { serverState.status = v; refresh(); })),
    results);
  refresh();
}

async function serverDetailView(id) {
  const { server, packages } = await api("servers/" + id);
  let onlyOutdated = false;
  const results = el("div");

  function refresh() {
    const rows = packages.filter(p => !onlyOutdated || !p.is_latest);
    results.replaceChildren(table(
      ["Package", "Version", "Release", "Arch", "Installed", "Drift"],
      rows.map(p =>
        el("tr", { class: "clickable", onclick: () => goto(`#/packages/${p.package_id}`) },
          el("td", {}, p.name),
          el("td", {}, p.version),
          el("td", {}, p.release),
          el("td", {}, p.arch),
          el("td", {}, fmtDate(p.install_time)),
          el("td", {}, p.is_latest
            ? el("span", { class: "badge badge-good" }, "latest")
            : el("span", { class: "badge badge-warn" },
                `behind → ${p.latest_version}-${p.latest_release}`)))),
      onlyOutdated ? "No outdated packages on this server." : "No extra packages recorded."));
  }

  const dt = (label, value) =>
    el("div", {}, el("dt", {}, label), el("dd", {}, value));
  view.replaceChildren(
    el("h2", {}, server.hostname),
    el("dl", { class: "meta" },
      dt("Status", statusBadge(server.inventory_status)),
      dt("Beheergroep", server.beheergroep ?? "—"),
      dt("Beheer email", server.beheeremail ?? "—"),
      dt("Owner", server.owner ?? "—"),
      dt("Service level", server.servicelevel ?? "—"),
      dt("OS", `${server.os} ${server.osversie}`),
      dt("SUMA API", server.apiversie || "—"),
      dt("Last seen", fmtDate(server.last_seen)),
      dt("Extra packages", String(packages.length)),
      dt("Packages behind", String(server.behind_count))),
    el("div", { class: "filters" },
      el("label", { class: "check" },
        el("input", {
          type: "checkbox",
          onchange: e => { onlyOutdated = e.target.checked; refresh(); },
        }),
        "Show only outdated packages")),
    results);
  refresh();
}

/* ---------- packages ---------- */

const packageState = { q: "" };

async function packagesView() {
  const results = el("div", {}, el("p", { class: "muted" }, "Loading…"));

  async function refresh() {
    const params = new URLSearchParams();
    if (packageState.q) params.set("q", packageState.q);
    const pkgs = await api("packages?" + params);
    results.replaceChildren(table(
      ["Package", el("th", { class: "num" }, "Versions"),
       el("th", { class: "num" }, "Servers"), "Drift"],
      pkgs.map(p =>
        el("tr", { class: "clickable", onclick: () => goto(`#/packages/${p.id}`) },
          el("td", {}, p.name),
          el("td", { class: "num" }, String(p.version_count)),
          el("td", { class: "num" }, String(p.server_count)),
          el("td", {}, p.has_drift
            ? el("span", { class: "badge badge-warn" }, "drift")
            : el("span", { class: "muted" }, "—")))),
      "No packages match."));
  }

  view.replaceChildren(
    el("h2", {}, "Packages"),
    el("div", { class: "filters" },
      el("input", {
        type: "search", placeholder: "Search packages…", value: packageState.q,
        oninput: debounce(e => { packageState.q = e.target.value; refresh(); }, 250),
      })),
    results);
  refresh();
}

async function packageDetailView(id) {
  const pkg = await api("packages/" + id);
  view.replaceChildren(
    el("h2", {}, pkg.name),
    el("div", {}, pkg.os_groups.length
      ? pkg.os_groups.map(group =>
          el("section", { class: "os-group" },
            el("h3", {},
              `${group.os} `,
              group.drifting
                ? el("span", { class: "badge badge-warn" }, "drift")
                : el("span", { class: "badge badge-good" }, "in sync")),
            group.versions.map(v =>
              el("div", { class: "version-block" },
                el("div", { class: "version-head" },
                  el("span", { class: "chip " + (v.is_latest ? "chip-latest" : "chip-behind") },
                    `${v.is_latest ? "✓ newest" : "↓ behind"} ${verLabel(v)}`),
                  el("span", { class: "muted" }, v.arch)),
                table(["Server", "Beheergroep", "Status", "Installed"],
                  v.servers.map(s =>
                    el("tr", { class: "clickable", onclick: () => goto(`#/servers/${s.id}`) },
                      el("td", {}, s.hostname),
                      el("td", {}, s.beheergroep ?? "—"),
                      el("td", {}, statusBadge(s.inventory_status)),
                      el("td", {}, fmtDate(s.install_time)))))))))
      : el("p", { class: "muted" }, "This package is not installed on any tracked server.")));
}

/* ---------- runs ---------- */

async function runsView() {
  const runs = await api("runs");
  view.replaceChildren(
    el("h2", {}, "Inventory runs"),
    table(
      [el("th", { class: "num" }, "ID"), "Source", "Started", "Completed",
       el("th", { class: "num" }, "Servers in file")],
      runs.map(r =>
        el("tr", {},
          el("td", { class: "num" }, String(r.id)),
          el("td", {}, r.source ?? "—"),
          el("td", {}, fmtDate(r.started_at)),
          el("td", {}, fmtDate(r.completed_at)),
          el("td", { class: "num" }, String(r.server_count)))),
      "No imports have run yet."));
}

/* ---------- router ---------- */

const routes = [
  [/^#\/$/, dashboardView],
  [/^#\/drift$/, driftView],
  [/^#\/servers$/, serversView],
  [/^#\/servers\/([0-9a-fA-F-]+)$/, serverDetailView],
  [/^#\/packages$/, packagesView],
  [/^#\/packages\/(\d+)$/, packageDetailView],
  [/^#\/runs$/, runsView],
];

function setActiveNav(hash) {
  for (const a of document.querySelectorAll("#nav a")) {
    const route = a.dataset.route;
    a.classList.toggle("active",
      route === "#/" ? hash === "#/" : hash.startsWith(route));
  }
}

function router() {
  const hash = location.hash || "#/";
  setActiveNav(hash);
  for (const [re, fn] of routes) {
    const m = hash.match(re);
    if (m) {
      Promise.resolve(fn(...m.slice(1))).catch(err => {
        view.replaceChildren(el("p", { class: "error" }, "Failed to load: " + err.message));
      });
      return;
    }
  }
  view.replaceChildren(el("p", { class: "error" }, "Unknown page."));
}

window.addEventListener("hashchange", router);
router();
