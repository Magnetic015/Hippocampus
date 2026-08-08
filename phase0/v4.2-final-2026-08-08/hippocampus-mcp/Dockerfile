# Hippocampus MCP image (plan 06 §6.4). Build args pin everything; the
# production stage removes the failpoint module entirely (09 §10.2).
FROM python:3.12-slim AS base
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
COPY vendor ./vendor
COPY scanner-pkg ./scanner
RUN pip install --no-cache-dir "tiktoken>=0.7,<0.8" && pip install --no-cache-dir -e .
ENV PYTHONPATH=/app/scanner \
    HIPPOCAMPUS_TIKTOKEN_CACHE=/app/vendor/tiktoken-cache

FROM base AS test
ENV HIPPOCAMPUS_TEST_BUILD=1

FROM base AS production
# failpoint code path must not exist in the production image
RUN rm -f src/hippocampus/failpoints.py \
    && printf 'def enabled():\n    return False\n\ndef arm(*a, **k):\n    raise RuntimeError("failpoints not built")\n\ndef clear(*a, **k):\n    pass\n\ndef hit(name):\n    pass\n' \
       > src/hippocampus/failpoints.py
USER 1000:1000
ENTRYPOINT ["python", "-m", "hippocampus.server"]
