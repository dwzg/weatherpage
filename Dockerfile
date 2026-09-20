FROM python:3.12-slim

# Unbuffered output so container logs appear immediately; no .pyc files to
# write into a layer that is thrown away anyway.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data

WORKDIR /code

# Dependencies first: this layer is cached until requirements.txt changes.
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

VOLUME /data
EXPOSE 8080

# The app serves this from its own database, so an unhealthy container means
# the database is unreachable rather than just the port being open.
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"

# --proxy-headers so request logs show the real client behind the nginx proxy.
# Asset URLs in the page are root-relative and do not depend on this.
#
# Runs as root because the deployment bind-mounts a host directory to /data.
# To run unprivileged, add a user to this image, `USER` it here, and chown the
# host directory to the same uid — otherwise the app cannot write its database.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
