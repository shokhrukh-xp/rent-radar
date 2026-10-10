FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/* && pip install --no-cache-dir requests
WORKDIR /opt/rano
# сам цикл — из репозитория (/opt/rano/app), чтобы и он обновлялся через git pull
CMD ["sh", "/opt/rano/app/deploy/server/run.sh"]
