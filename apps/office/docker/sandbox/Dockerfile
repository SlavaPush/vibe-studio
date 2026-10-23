FROM mcr.microsoft.com/playwright:v1.64.1-noble

RUN npm install -g @playwright/test@1.64.1 \
    && ln -sfn "$(npm root -g)" /opt/node_modules \
    && groupadd --gid 10001 sandbox \
    && useradd --uid 10001 --gid sandbox --create-home --shell /usr/sbin/nologin sandbox \
    && mkdir -p /work \
    && chown sandbox:sandbox /work

COPY smoke /smoke
COPY smoke.sh /usr/local/bin/sandbox-smoke

ENV NODE_PATH=/opt/node_modules
ENV CI=true
ENV npm_config_update_notifier=false

USER sandbox
WORKDIR /work
