# OpenTripPlanner runtime for Railway (or any PaaS): downloads a prebuilt graph on first boot and serves it.
# Build the graph elsewhere (scripts/build-graph.sh or scripts/otp-native.sh build) and publish graph.obj +
# router-config.json as release assets; point GRAPH_URL / ROUTER_CONFIG_URL at them.
FROM opentripplanner/opentripplanner:2.9.0
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY deploy/otp/entrypoint.sh /otp-entrypoint.sh
RUN chmod +x /otp-entrypoint.sh
ENV JAVA_OPTS="-Xmx5G" OTP_DATA_DIR=/var/opentripplanner PORT=8080
EXPOSE 8080
ENTRYPOINT ["/otp-entrypoint.sh"]
