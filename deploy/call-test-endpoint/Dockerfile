FROM debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f AS iperf-build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential ca-certificates curl && rm -rf /var/lib/apt/lists/*
COPY build-iperf.sh /build-iperf.sh
RUN sh /build-iperf.sh

FROM python:3.15.0-slim-trixie@sha256:ea4f1f290e071bf7d28199cfb3e09f5dbad31b50e9c32fd58f2f2d7be915f3ae
COPY --from=iperf-build /usr/local/bin/iperf3 /usr/local/bin/iperf3
COPY --chmod=0555 call_endpoint_service.py /app/service.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
ENTRYPOINT ["python", "-I", "/app/service.py"]
