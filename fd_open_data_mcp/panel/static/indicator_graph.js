/* Indicator observatory relation graph (panel-indicator-observatory 2.2).
 * Thin glue over the vendored vis-network bundle: fetches the bounded
 * neighborhood JSON from the same-origin endpoint, renders it with pan/zoom,
 * and fills the node-detail panel on select. Theme tokens are read from the
 * panel's CSS custom properties and re-read when the theme toggle flips
 * data-theme, so the graph follows the active theme like every other page
 * (panel-ui presentation contract). No build step, no dependencies beyond
 * the vendored vis-network UMD. */
(function () {
  "use strict";

  var wrap = document.getElementById("graph-wrap");
  if (!wrap || typeof vis === "undefined") return;

  var canvas = document.getElementById("graph-canvas");
  var detailBody = document.getElementById("graph-detail-body");
  var note = document.getElementById("graph-note");
  var src = wrap.getAttribute("data-graph-src");

  var KIND_LABELS = {
    domain: "域 domain", family: "族 family", concept: "概念 concept",
    indicator: "指标 indicator", column: "绑定列 binding", term: "词表词条 term"
  };
  var EDGE_LABELS = {
    member: "成员 member", binding: "绑定 binding",
    mapping: "映射 mapping", domain: "属于 in-domain"
  };

  function tokens() {
    var cs = getComputedStyle(document.documentElement);
    function v(name, fallback) {
      var val = (cs.getPropertyValue(name) || "").trim();
      return val || fallback;
    }
    return {
      surface: v("--surface", "#ffffff"),
      border: v("--border", "#dde5ee"),
      text: v("--text", "#0f172a"),
      muted: v("--muted", "#64748b"),
      accent: v("--accent", "#2563eb"),
      ok: v("--chart-ok", "#16a34a"),
      warn: v("--heat-aging", "#f59e0b")
    };
  }

  function nodeColor(node, t) {
    if (node.kind === "family") return { background: t.accent, border: t.accent };
    if (node.kind === "concept") return { background: t.surface, border: t.accent };
    if (node.kind === "indicator") {
      return { background: t.surface, border: node.verified ? t.ok : t.warn };
    }
    return { background: t.surface, border: t.border };
  }

  function options() {
    var t = tokens();
    return {
      autoResize: true,
      nodes: {
        shape: "dot", size: 10, borderWidth: 2,
        font: { color: t.text, face: "inherit", size: 12 },
        color: { highlight: { border: t.accent } }
      },
      edges: {
        color: { color: t.border, highlight: t.accent },
        font: { color: t.muted, size: 9, strokeWidth: 0 },
        arrows: { to: { enabled: true, scaleFactor: 0.4 } }
      },
      interaction: { hover: true, zoomView: true, dragView: true }
    };
  }

  function toVis(data, t) {
    var nodes = data.nodes.map(function (n) {
      var c = nodeColor(n, t);
      return {
        id: n.id, label: n.label, title: n.label,
        group: n.kind, color: c,
        shape: (n.kind === "family" || n.kind === "domain") ? "diamond" : "dot",
        size: (n.kind === "family" || n.kind === "domain") ? 14 : 9,
        _detail: n
      };
    });
    var edges = data.edges.map(function (e, i) {
      return {
        id: "e" + i, from: e.from, to: e.to,
        label: EDGE_LABELS[e.kind] || e.kind
      };
    });
    return { nodes: nodes, edges: edges };
  }

  function renderDetail(n) {
    if (!n) return;
    var d = n._detail || n;
    var rows = [];
    rows.push(KIND_LABELS[d.kind] || d.kind);
    if (d.code) rows.push("code: " + d.code);
    if (d.semantic_code) rows.push("semantic_code: " + d.semantic_code);
    if (d.name_zh) rows.push("zh: " + d.name_zh);
    if (d.name_en) rows.push("en: " + d.name_en);
    if (d.source) rows.push("source: " + d.source + " · " + (d.column || ""));
    if (d.vocabulary) rows.push("mapping: " + d.vocabulary + " / " + d.term +
                                " (" + d.relation + ")");
    if (typeof d.verified === "boolean") {
      rows.push("verified: " + (d.verified ? "✓" : "✗ unverified"));
    }
    if (typeof d.binding_count === "number") rows.push("bindings: " + d.binding_count);
    if (typeof d.mapping_count === "number") rows.push("mappings: " + d.mapping_count);
    if (typeof d.anchors === "number") rows.push("anchors: " + d.anchors);
    if (detailBody) {
      detailBody.innerHTML = "";
      var ul = document.createElement("ul");
      rows.forEach(function (r) {
        var li = document.createElement("li");
        li.textContent = r;
        ul.appendChild(li);
      });
      detailBody.appendChild(ul);
    }
  }

  fetch(src, { headers: { accept: "application/json" } })
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (data.error) {
        if (note) note.textContent = data.error;
        return;
      }
      if (note) {
        note.textContent = data.truncated
          ? ("视图已达服务端节点上限（" + data.max_nodes + "），已截断 — 改用更小的范围或深度 " +
             "view hit the server-side node cap and was truncated")
          : (data.nodes.length + " nodes · depth " + data.depth);
      }
      var t = tokens();
      var visData = toVis(data, t);
      var network = new vis.Network(canvas, new vis.DataSet(visData.nodes),
                                    new vis.DataSet(visData.edges), options());
      network.on("selectNode", function (params) {
        var id = params.nodes[0];
        var node = visData.nodes.filter(function (n) { return n.id === id; })[0];
        renderDetail(node);
      });
      // Theme contract: re-read the CSS tokens when the theme toggle flips
      // data-theme, so the graph follows the active theme.
      var mo = new MutationObserver(function () {
        network.setOptions(options());
      });
      mo.observe(document.documentElement,
                 { attributes: true, attributeFilter: ["data-theme"] });
    })
    .catch(function (err) {
      if (note) {
        note.textContent = "图加载失败 graph load failed: " + err;
      }
    });
})();
