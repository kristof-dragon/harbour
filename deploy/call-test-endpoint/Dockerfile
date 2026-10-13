FROM debian:trixie-slim
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends iperf3 && rm -rf /var/lib/apt/lists/*
USER 10001:10001
ENTRYPOINT ["iperf3"]
