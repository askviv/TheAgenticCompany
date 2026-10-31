FROM ghcr.io/mailu/webmail:2024.06.58@sha256:bb438c63dc4e54204e9bd57f139368f98d58f3bc287a75bdb5eb4d18c9bdfc6c

RUN mv /start.py /start-mailu.py
COPY --chmod=755 start.py /start.py
COPY tac_correlation/ /var/www/roundcube/plugins/tac_correlation/
