FROM debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f AS iperf-build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential ca-certificates curl && rm -rf /var/lib/apt/lists/*
COPY deploy/call-test-endpoint/build-iperf.sh /build-iperf.sh
RUN sh /build-iperf.sh

FROM python:3.15.0-slim-trixie@sha256:ea4f1f290e071bf7d28199cfb3e09f5dbad31b50e9c32fd58f2f2d7be915f3ae
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
COPY --from=iperf-build /usr/local/bin/iperf3 /usr/local/bin/iperf3
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.txt && python -m pip check && useradd --uid 10001 --create-home harbour && mkdir /data && chown harbour:harbour /data
COPY harbour ./harbour
COPY deploy/call-test-endpoint ./harbour/endpoint_deploy
USER 10001:10001
EXPOSE 8080
CMD ["uvicorn", "harbour.app:app", "--host", "0.0.0.0", "--port", "8080", "--no-proxy-headers"]
