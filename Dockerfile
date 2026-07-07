FROM python:3.11-slim AS builder

WORKDIR /app
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir .

FROM python:3.11-slim

WORKDIR /app
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin/unifai-mcp /usr/local/bin/unifai-mcp
COPY src/ ./src/

EXPOSE 13456

USER nobody

CMD ["unifai-mcp"]
