FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mcai ./mcai
COPY scripts ./scripts

RUN useradd --uid 10001 --create-home promptcraft && mkdir -p /data/scripts && chown -R promptcraft /data
USER 10001

ENV PYTHONUNBUFFERED=1 \
    SCRIPTS_DIR=/data/scripts \
    BRIDGE_PORT=8765
EXPOSE 8765
CMD ["python", "-m", "mcai.ws_bridge"]
