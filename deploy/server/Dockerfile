FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/* && pip install --no-cache-dir requests
WORKDIR /opt/rano
COPY run.sh /run.sh
RUN chmod +x /run.sh
CMD ["/run.sh"]
