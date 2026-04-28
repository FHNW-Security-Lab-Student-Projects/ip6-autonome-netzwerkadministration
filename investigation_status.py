"""Investigation status overview server — reads investigations.json and serves a live table.

Run independently (no import of syslog_agent):
    uv run python investigation_status.py

Then open http://127.0.0.1:7933
"""

import json
import logging
import os
from pathlib import Path

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

INVESTIGATIONS_FILE = Path(__file__).parent / 'investigations.json'
AGENT_API_URL = f"http://127.0.0.1:{os.getenv('AGENT_API_PORT', '7934')}"

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Investigation Status</title>
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
    @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.45; } }
    .investigating { background: #1f3d5a; color: #58a6ff; animation: pulse 1.6s ease-in-out infinite; }
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
    #detail .resolve-btn { float: right; margin-right: 0.5rem; background: none; border: 1px solid #238636;
                           color: #3fb950; padding: 0.2rem 0.6rem; border-radius: 0.3rem; cursor: pointer; font-family: monospace; }
    #detail .resolve-btn:hover { background: #122d22; }
    #detail .resolve-btn:disabled { opacity: 0.4; cursor: default; }
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
  <h1>Investigation Status</h1>
  <div class="meta" id="meta">Loading…</div>
  <div id="content"></div>

  <div id="detail">
    <button class="close-btn" onclick="closeDetail()">✕ close</button>
    <button class="resolve-btn" id="d-resolve-btn" onclick="resolveInvestigation()">✓ mark resolved</button>
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
    let _allInvestigations = {};
    let _selectedId = new URLSearchParams(window.location.search).get('id');
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
      const inv = _allInvestigations[id];
      if (!inv) return;
      document.querySelectorAll('tr.clickable').forEach(r => r.classList.remove('selected'));
      const row = document.getElementById('row-' + inv.investigation_id.slice(0, 8));
      if (row) row.classList.add('selected');

      const short = inv.investigation_id.slice(0, 8);
      document.getElementById('d-title').innerHTML =
        `Investigation ${short} <button class="copy-id-btn" onclick="copyText('${inv.investigation_id}')">copy full ID</button>`;
      document.getElementById('d-meta').innerHTML =
        `Device: <strong>${esc(inv.device)}</strong> &nbsp;|&nbsp; ` +
        `Status: ${badge(inv.status)} &nbsp;|&nbsp; ` +
        `Created: ${new Date(inv.created_at).toLocaleString()}`;
      const resolveBtn = document.getElementById('d-resolve-btn');
      resolveBtn.style.display = inv.status === 'resolved' ? 'none' : '';
      resolveBtn.disabled = false;
      resolveBtn.textContent = '✓ mark resolved';
      document.getElementById('d-trigger').textContent = inv.triggering_event;
      document.getElementById('d-summary').textContent = inv.summary || '(investigation in progress…)';

      const logDiv = document.getElementById('d-log');
      if (!inv.investigation_log || inv.investigation_log.length === 0) {
        logDiv.innerHTML = '<span style="color:#8b949e">(no steps recorded yet)</span>';
      } else {
        logDiv.innerHTML = inv.investigation_log.map(([step, result]) => `
          <div class="log-entry">
            <div class="log-step">${esc(step)}</div>
            <pre>${esc(result)}</pre>
          </div>`).join('');
      }
      const history = inv.message_history || [];
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

    async function resolveInvestigation() {
      if (!_selectedId) return;
      const btn = document.getElementById('d-resolve-btn');
      btn.disabled = true;
      btn.textContent = 'resolving…';
      try {
        const resp = await fetch(`/investigations/${_selectedId}/resolve`, { method: 'POST' });
        if (resp.ok) {
          await refresh();
        } else {
          const body = await resp.text();
          toast(`Error: ${body}`);
          btn.disabled = false;
          btn.textContent = '✓ mark resolved';
        }
      } catch (e) {
        toast(`Error: ${e}`);
        btn.disabled = false;
        btn.textContent = '✓ mark resolved';
      }
    }

    function render(data) {
      _allInvestigations = data;
      const entries = Object.values(data).sort((a, b) =>
        new Date(b.created_at) - new Date(a.created_at)
      );
      if (entries.length === 0) {
        document.getElementById('content').innerHTML = '<p class="empty">No investigations recorded yet.</p>';
        closeDetail();
        return;
      }
      let rows = entries.map(inv => {
        const short = inv.investigation_id.slice(0, 8);
        const ts = new Date(inv.created_at).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
        const summary = trunc(inv.summary || '(investigating…)', 100);
        const sel = inv.investigation_id === _selectedId ? ' selected' : '';
        return `<tr class="clickable${sel}" id="row-${short}" onclick="openDetail('${inv.investigation_id}')">
          <td class="id">${short}</td>
          <td>${esc(inv.device)}</td>
          <td>${badge(inv.status)}</td>
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
      if (_selectedId && _allInvestigations[_selectedId]) openDetail(_selectedId);
    }

    async function refresh() {
      try {
        const resp = await fetch('/investigations');
        if (resp.ok) {
          const data = await resp.json();
          render(data);
          document.getElementById('meta').textContent =
            `${Object.keys(data).length} investigation(s) — last updated ${new Date().toLocaleTimeString()} (auto-refreshes every 5 s) · click a row for details`;
        } else {
          const body = await resp.text();
          document.getElementById('meta').textContent = `Server error ${resp.status}: ${body}`;
        }
      } catch (e) {
        document.getElementById('meta').textContent = `Could not reach server: ${e}`;
      }
    }
    refresh();
    setInterval(refresh, 5000);
  </script>
</body>
</html>"""


async def get_investigations(request: Request) -> JSONResponse:
    if not INVESTIGATIONS_FILE.exists():
        return JSONResponse({})
    try:
        text = INVESTIGATIONS_FILE.read_text().strip()
        return JSONResponse(json.loads(text) if text else {})
    except Exception as exc:
        logging.exception('Failed to read investigations.json')
        return JSONResponse({'error': str(exc)}, status_code=500)


async def resolve_investigation(request: Request) -> JSONResponse:
    inv_id = request.path_params['investigation_id']
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.post(f'{AGENT_API_URL}/investigations/{inv_id}/resolve')
        if resp.status_code == 409:
            return JSONResponse({'error': 'Investigation is still running'}, status_code=409)
        return JSONResponse(resp.json(), status_code=resp.status_code)
    except httpx.ConnectError:
        return JSONResponse(
            {'error': 'Agent is not running — start the agent first, then retry.'},
            status_code=503,
        )
    except Exception:
        logging.exception('Failed to contact agent API')
        return JSONResponse({'error': 'Internal error'}, status_code=500)


async def get_index(request: Request) -> HTMLResponse:
    return HTMLResponse(_HTML)


app = Starlette(routes=[
    Route('/', get_index),
    Route('/investigations', get_investigations),
    Route('/investigations/{investigation_id}/resolve', resolve_investigation, methods=['POST']),
])

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    port = int(os.getenv('INVESTIGATION_STATUS_PORT', '7933'))
    uvicorn.run(app, host='127.0.0.1', port=port)
