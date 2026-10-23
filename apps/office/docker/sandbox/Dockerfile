FROM mcr.microsoft.com/playwright:v1.64.1-noble

RUN groupadd --gid 10001 sandbox \
    && useradd --uid 10001 --gid sandbox --create-home --shell /usr/sbin/nologin sandbox \
    && mkdir -p /work \
    && chown sandbox:sandbox /work

USER sandbox
WORKDIR /work
ENV CI=true
ENV npm_config_update_notifier=false
