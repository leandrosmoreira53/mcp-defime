FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . && useradd --create-home --uid 10001 app && mkdir -p /app/cache && chown app /app/cache

USER app
ENV MCP_TRANSPORT=http MCP_HOST=0.0.0.0 MCP_PORT=8000 CACHE_PATH=/app/cache/defime.db
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=3)" || exit 1
CMD ["mcp-defime"]
