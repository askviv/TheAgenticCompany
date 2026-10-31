FROM node:24-bookworm-slim AS frontend
WORKDIR /frontend
COPY web/package*.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM ghcr.io/theagentcompany/servers-api-server:1.0.0
# Use the YAML parser already declared and pinned by the benchmark's Poetry lock.
RUN pip install --no-cache-dir PyYAML==6.0.3
COPY app.py coordinator.py runner.py inputs.json retail-catalog.json benchmark-compatibility.json briefs-*.json /opt/experiments/
COPY --from=frontend /static/app /opt/experiments/static/app
ENV PYTHONPATH=/opt
WORKDIR /opt
USER 1000:1000
EXPOSE 8110
HEALTHCHECK --interval=15s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8110/api/health',timeout=3)"
ENTRYPOINT ["python", "-m", "experiments.app"]
