#!/usr/bin/env bash
# Teste rápido do servidor HTTP. Uso: scripts/smoke_http.sh https://mcp.seudominio/mcp TOKEN
set -euo pipefail
URL="${1:-http://127.0.0.1:8000/mcp}"; TOKEN="${2:?informe o token}"
H=(-H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream")
echo "== sem token (espera 401):"; curl -s -o /dev/null -w "%{http_code}\n" -X POST "$URL"
echo "== tools/list:"; curl -s "${H[@]}" -X POST "$URL" -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | head -c 400; echo
echo "== defime_get_pool (pool de teste):"
curl -s "${H[@]}" -X POST "$URL" -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"defime_get_pool","arguments":{"pool":"robinhood_0x9666ba19430380456bff5ac9d35a7ee2a6c6bc895bcc7ab92d3b64c8ec5cb611"}}}' | head -c 1500; echo
