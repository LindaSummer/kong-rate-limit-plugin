FROM python:3.12-slim-trixie
COPY --from=ghcr.io/astral-sh/uv:0.9.4 /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/test-venv UV_PYTHON_DOWNLOADS=never
WORKDIR /test
COPY test/pyproject.toml test/uv.lock ./
RUN uv sync --locked --no-dev
CMD ["uv", "run", "--frozen", "--no-sync", "pytest", "-v", "-m", "not restart"]
