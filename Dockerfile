# Image for the minimal API (api/app.py): the three AUDIT section 2 flows only (CLAUDE.md D38, D39).
# No model key is set here, and none may be set on the host: the service refuses to start if one is.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# dependencies first, so a code change does not reinstall them
COPY api/requirements.txt api/requirements.txt
RUN pip install -r api/requirements.txt

# exactly what the service imports or reads: the API, the demo functions it calls, and the library, servers, specs and duties those use
COPY api/ api/
COPY demo/ demo/
COPY agent/ agent/
COPY servers/ servers/
COPY learning/ learning/
COPY generators/ generators/
COPY duties/ duties/
COPY specs/ specs/

# the service writes only to a temporary directory; it needs no other writable path
RUN useradd --create-home --uid 10001 app
USER app

# the host supplies PORT (Railway does); 8000 is the default for running the image by hand
ENV PORT=8000
EXPOSE 8000

# shell form through `sh -c` so that $PORT is expanded; `exec` makes uvicorn PID 1 so the host's stop signal reaches it
CMD ["sh", "-c", "exec uvicorn api.app:app --host 0.0.0.0 --port ${PORT}"]
