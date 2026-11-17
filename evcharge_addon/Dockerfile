# Home Assistant add-on: builds the app from this repository at a fixed commit, so a
# version of the add-on always means the same code. To update: push the code, put its
# commit here and raise `version` in config.yaml.
FROM python:3.12-alpine

ARG EVCHARGE_REF=07cfce2ea000882715949311b43718282cc2e55b

WORKDIR /srv
RUN wget -qO /tmp/src.tar.gz "https://github.com/nevs089/community-ev-charging/archive/${EVCHARGE_REF}.tar.gz" \
 && tar -xzf /tmp/src.tar.gz --strip-components=1 \
 && rm -rf /tmp/src.tar.gz tests tests_ha docs deploy custom_components evcharge_addon \
 && pip install --no-cache-dir -r requirements.txt

COPY start.py /start.py

ENV PYTHONUNBUFFERED=1 DB_PATH=/data/ev.sqlite3 BRAND_DIR=/share/evcharge
EXPOSE 8080 9000
CMD ["python3", "/start.py"]
