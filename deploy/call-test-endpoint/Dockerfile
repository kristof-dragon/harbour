FROM debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f AS iperf-build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential ca-certificates curl && rm -rf /var/lib/apt/lists/*
COPY build-iperf.sh /build-iperf.sh
RUN sh /build-iperf.sh

FROM debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f
COPY --from=iperf-build /usr/local/bin/iperf3 /usr/local/bin/iperf3
USER 10001:10001
ENTRYPOINT ["iperf3"]
