#!/bin/sh
# Container entrypoint. Platform volumes (Railway, many Kubernetes CSI drivers) are mounted owned by root, so the
# unprivileged `app` user could not write to DATA_DIR. When started as root we hand DATA_DIR to `app` and drop
# privileges; when already started unprivileged (e.g. runAsNonRoot) we run the command as-is.
set -e
DATA_DIR="${DATA_DIR:-/data}"
if [ "$(id -u)" = "0" ]; then
  mkdir -p "$DATA_DIR"
  # Only the top level: anything below it was written by `app` already. Avoids a slow recursive chown on big volumes.
  chown app:app "$DATA_DIR"
  exec setpriv --reuid=app --regid=app --init-groups "$@"
fi
exec "$@"
