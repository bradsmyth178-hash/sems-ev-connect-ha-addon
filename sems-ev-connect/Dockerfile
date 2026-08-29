ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.20
FROM ${BUILD_FROM}
WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt
COPY bridge ./bridge
ENV BRIDGE_CONFIG=/data/bridge.yaml
CMD ["python3", "-m", "bridge.main"]
