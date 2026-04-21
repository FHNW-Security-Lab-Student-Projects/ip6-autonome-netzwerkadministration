"""Incident status overview server — reads incidents.json and serves a live table.

Run independently (no import of syslog_agent):
    uv run python incident_status.py

Then open http://127.0.0.1:7933
"""

import json
import logging
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

INCIDENTS_FILE = Path(__file__).parent / 'incidents.json'

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Incident Status</title>
  <style>
    body { font-family: monospace; background: #0d1117; color: #c9d1d9; margin: 2rem; }
    h1 { color: #f0f6fc; margin-bottom: 0.25rem; }
    .meta { color: #8b949e; font-size: 0.85rem; margin-bottom: 1.5rem; }
    table { border-collapse: collapse; width: 100%; }
    th { background: #161b22; color: #8b949e; text-align: left; padding: 0.5rem 0.75rem; border-bottom: 1px solid #30363d; }
    td { padding: 0.5rem 0.75rem; border-bottom: 1px solid #21262d; vertical-align: top; }
    tr.clickable:hover td { background: #161b22; cursor: pointer; }
    tr.selected td { background: #1c2128; }
    .id { font-weight: bold; color: #79c0ff; }
    .badge { display: inline-block; padding: 0.15rem 0.5rem; border-radius: 2rem; font-size: 0.75rem; font-weight: bold; }
    .investigating { background: #1f3d5a; color: #58a6ff; }
    .waiting      { background: #2d2009; color: #e3b341; }
    .resolved     { background: #122d22; color: #3fb950; }
    .summary-cell { color: #8b949e; max-width: 40rem; }
    .empty { color: #8b949e; padding: 2rem 0; }

    /* Detail panel */
    #detail { display: none; margin-top: 2rem; border: 1px solid #30363d; border-radius: 0.5rem; padding: 1.5rem; background: #161b22; }
    #detail.open { display: block; }
    #detail h2 { color: #f0f6fc; margin: 0 0 0.25rem; font-size: 1rem; }
    #detail .close-btn { float: right; background: none; border: 1px solid #30363d; color: #8b949e;
                         padding: 0.2rem 0.6rem; border-radius: 0.3rem; cursor: pointer; font-family: monospace; }
    #detail .close-btn:hover { color: #c9d1d9; border-color: #8b949e; }
    .detail-meta { color: #8b949e; font-size: 0.85rem; margin-bottom: 1rem; }
    .detail-section { margin-top: 1.25rem; }
    .detail-section h3 { color: #8b949e; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em;
                          margin: 0 0 0.5rem; border-bottom: 1px solid #21262d; padding-bottom: 0.25rem; }
    pre { background: #0d1117; border: 1px solid #21262d; border-radius: 0.3rem; padding: 0.75rem;
          white-space: pre-wrap; word-break: break-word; margin: 0; color: #c9d1d9; font-size: 0.85rem; }
    .log-entry { margin-bottom: 0.75rem; }
    .log-step { color: #58a6ff; font-size: 0.8rem; margin-bottom: 0.25rem; }
    .copy-id-btn { background: none; border: 1px solid #30363d; color: #79c0ff; padding: 0.15rem 0.5rem;
                   border-radius: 0.3rem; cursor: pointer; font-family: monospace; font-size: 0.8rem; margin-left: 0.5rem; }
    .copy-id-btn:hover { border-color: #79c0ff; }

    .copied { position: fixed; bottom: 1.5rem; right: 1.5rem; background: #238636; color: #fff;
              padding: 0.5rem 1rem; border-radius: 0.4rem; font-size: 0.85rem; opacity: 0; transition: opacity 0.3s; }
    .copied.show { opacity: 1; }
  </style>
</head>
<body>
  <h1>Incident Status</h1>
  <div class="meta" id="meta">Loading…</div>
  <div id="content"></div>

  <div id="detail">
    <button class="close-btn" onclick="closeDetail()">✕ close</button>
    <h2 id="d-title"></h2>
    <div class="detail-meta" id="d-meta"></div>
    <div class="detail-section">
      <h3>Triggering Event</h3>
      <pre id="d-trigger"></pre>
    </div>
    <div class="detail-section">
      <h3>Summary</h3>
      <pre id="d-summary"></pre>
    </div>
    <div class="detail-section">
      <h3>Investigation Log</h3>
      <div id="d-log"></div>
    </div>
    <div class="detail-section">
      <h3 style="cursor:pointer" onclick="toggleHistory()">
        Raw Message History <span id="history-toggle" style="font-size:0.8rem;color:#8b949e">[expand]</span>
      </h3>
      <pre id="d-history" style="display:none;max-height:40rem;overflow-y:auto;font-size:0.75rem"></pre>
    </div>
  </div>

  <div class="copied" id="toast">Copied!</div>

  <script>
    let _allIncidents = {};
    let _selectedId = null;
    let _historyExpanded = false;

    function badge(status) {
      return `<span class="badge ${status}">${status}</span>`;
    }
    function esc(s) {
      return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }
    function toast(msg) {
      const t = document.getElementById('toast');
      t.textContent = msg;
      t.classList.add('show');
      setTimeout(() => t.classList.remove('show'), 1500);
    }
    function copyText(text) {
      navigator.clipboard.writeText(text);
      toast('Copied!');
    }
    function trunc(s, n) { return s.length > n ? s.slice(0, n) + '…' : s; }

    function openDetail(id) {
      if (id !== _selectedId) _historyExpanded = false;
      _selectedId = id;
      const inc = _allIncidents[id];
      if (!inc) return;
      document.querySelectorAll('tr.clickable').forEach(r => r.classList.remove('selected'));
      const row = document.getElementById('row-' + inc.incident_id.slice(0, 8));
      if (row) row.classList.add('selected');

      const short = inc.incident_id.slice(0, 8);
      document.getElementById('d-title').innerHTML =
        `Incident ${short} <button class="copy-id-btn" onclick="copyText('${inc.incident_id}')">copy full ID</button>`;
      document.getElementById('d-meta').innerHTML =
        `Device: <strong>${esc(inc.device)}</strong> &nbsp;|&nbsp; ` +
        `Status: ${badge(inc.status)} &nbsp;|&nbsp; ` +
        `Created: ${new Date(inc.created_at).toLocaleString()}`;
      document.getElementById('d-trigger').textContent = inc.triggering_event;
      document.getElementById('d-summary').textContent = inc.summary || '(investigation in progress…)';

      const logDiv = document.getElementById('d-log');
      if (!inc.investigation_log || inc.investigation_log.length === 0) {
        logDiv.innerHTML = '<span style="color:#8b949e">(no steps recorded yet)</span>';
      } else {
        logDiv.innerHTML = inc.investigation_log.map(([step, result]) => `
          <div class="log-entry">
            <div class="log-step">${esc(step)}</div>
            <pre>${esc(result)}</pre>
          </div>`).join('');
      }
      const history = inc.message_history || [];
      document.getElementById('d-history').textContent =
        JSON.stringify(history, null, 2);
      document.getElementById('d-history').style.display = _historyExpanded ? 'block' : 'none';
      document.getElementById('history-toggle').textContent = _historyExpanded ? '[collapse]' : '[expand]';
      document.getElementById('detail').classList.add('open');
    }

    function toggleHistory() {
      _historyExpanded = !_historyExpanded;
      document.getElementById('d-history').style.display = _historyExpanded ? 'block' : 'none';
      document.getElementById('history-toggle').textContent = _historyExpanded ? '[collapse]' : '[expand]';
    }

    function closeDetail() {
      _selectedId = null;
      _historyExpanded = false;
      document.getElementById('detail').classList.remove('open');
      document.querySelectorAll('tr.clickable').forEach(r => r.classList.remove('selected'));
    }

    function render(data) {
      _allIncidents = data;
      const entries = Object.values(data).sort((a, b) =>
        new Date(b.created_at) - new Date(a.created_at)
      );
      if (entries.length === 0) {
        document.getElementById('content').innerHTML = '<p class="empty">No incidents recorded yet.</p>';
        closeDetail();
        return;
      }
      let rows = entries.map(inc => {
        const short = inc.incident_id.slice(0, 8);
        const ts = new Date(inc.created_at).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
        const summary = trunc(inc.summary || '(investigating…)', 100);
        const sel = inc.incident_id === _selectedId ? ' selected' : '';
        return `<tr class="clickable${sel}" id="row-${short}" onclick="openDetail('${inc.incident_id}')">
          <td class="id">${short}</td>
          <td>${esc(inc.device)}</td>
          <td>${badge(inc.status)}</td>
          <td>${ts}</td>
          <td class="summary-cell">${esc(summary)}</td>
        </tr>`;
      }).join('');
      document.getElementById('content').innerHTML = `
        <table>
          <thead><tr>
            <th>ID (8-char)</th><th>Device</th><th>Status</th><th>Created</th><th>Summary</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>`;
      if (_selectedId && _allIncidents[_selectedId]) openDetail(_selectedId);
    }

    async function refresh() {
      try {
        const resp = await fetch('/incidents');
        if (resp.ok) {
          const data = await resp.json();
          render(data);
          document.getElementById('meta').textContent =
            `${Object.keys(data).length} incident(s) — last updated ${new Date().toLocaleTimeString()} (auto-refreshes every 5 s) · click a row for details`;
        }
      } catch (e) {
        document.getElementById('meta').textContent = 'Could not load incidents.json';
      }
    }
    refresh();
    setInterval(refresh, 5000);
  </script>
</body>
</html>"""


async def get_incidents(request: Request) -> JSONResponse:
    if not INCIDENTS_FILE.exists():
        return JSONResponse({})
    text = INCIDENTS_FILE.read_text().strip()
    return JSONResponse(json.loads(text) if text else {})


async def get_index(request: Request) -> HTMLResponse:
    return HTMLResponse(_HTML)


app = Starlette(routes=[
    Route('/', get_index),
    Route('/incidents', get_incidents),
])

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host='127.0.0.1', port=7933)
