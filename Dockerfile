FROM kong:3.9.4
COPY --chown=kong:kong kong/plugins/rate-limiting-sliding-window /usr/local/share/lua/5.1/kong/plugins/rate-limiting-sliding-window
