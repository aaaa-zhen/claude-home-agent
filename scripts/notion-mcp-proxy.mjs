// Local refreshing reverse-proxy for Notion's remote MCP.
// Claude Code points its "notion" HTTP MCP at http://127.0.0.1:8790/mcp; this
// process injects a always-fresh OAuth Bearer token, transparently refreshing it
// via the stored refresh_token before it expires. Keeps Notion connected forever
// without restarting the agent. Managed by launchd (com.zhen.notion-mcp-proxy).
import http from 'node:http';
import fs from 'node:fs';
import { Readable } from 'node:stream';

const ROOT = '/Users/zhen/home-agent/weixin-agent';
const AS = 'https://mcp.notion.com';
const UPSTREAM = 'https://mcp.notion.com/mcp';
const PORT = Number(process.env.NOTION_PROXY_PORT || 8790);
const STATE = `${ROOT}/tmp/notion-oauth.json`;
const SKEW_MS = 120_000; // refresh 2 min before expiry

function load() { return JSON.parse(fs.readFileSync(STATE, 'utf8')); }
function save(s) { fs.writeFileSync(STATE, JSON.stringify(s, null, 2)); fs.chmodSync(STATE, 0o600); }

let refreshing = null;
async function refresh(state) {
  const body = new URLSearchParams({
    grant_type: 'refresh_token',
    refresh_token: state.refresh_token,
    client_id: state.client_id,
    resource: AS,
  });
  const res = await fetch(`${AS}/token`, {
    method: 'POST',
    headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body,
  });
  if (!res.ok) throw new Error(`refresh failed ${res.status}: ${await res.text()}`);
  const tok = await res.json();
  const next = { ...state, ...tok };
  next.obtained_at = Date.now();
  next.expires_at = Date.now() + (tok.expires_in || 28800) * 1000;
  if (!tok.refresh_token) next.refresh_token = state.refresh_token; // reuse if not rotated
  save(next);
  console.log(`[notion-proxy] token refreshed, valid ${Math.round((next.expires_at - Date.now())/60000)}min`);
  return next;
}

async function ensureToken() {
  let state = load();
  if (Date.now() < (state.expires_at || 0) - SKEW_MS) return state.access_token;
  if (!refreshing) refreshing = refresh(state).finally(() => { refreshing = null; });
  state = await refreshing;
  return state.access_token;
}

const server = http.createServer(async (req, res) => {
  try {
    const token = await ensureToken();
    // collect request body
    const chunks = [];
    for await (const c of req) chunks.push(c);
    const reqBody = Buffer.concat(chunks);

    const headers = { ...req.headers };
    delete headers.host; delete headers.authorization; delete headers['content-length'];
    headers.authorization = `Bearer ${token}`;

    const upstream = await fetch(UPSTREAM, {
      method: req.method,
      headers,
      body: ['GET', 'HEAD'].includes(req.method) ? undefined : reqBody,
    });

    const outHeaders = {};
    upstream.headers.forEach((v, k) => {
      if (['content-encoding', 'content-length', 'transfer-encoding', 'connection'].includes(k)) return;
      outHeaders[k] = v;
    });
    res.writeHead(upstream.status, outHeaders);
    if (upstream.body) Readable.fromWeb(upstream.body).pipe(res);
    else res.end();
  } catch (e) {
    console.error(`[notion-proxy] error: ${e.message}`);
    if (!res.headersSent) res.writeHead(502, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ error: 'proxy_error', message: e.message }));
  }
});

server.listen(PORT, '127.0.0.1', () => console.log(`[notion-proxy] listening on http://127.0.0.1:${PORT}/mcp`));
