FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    ITMAM_PATHWAYS_DIR=/app/api/pathways

WORKDIR /app
COPY api ./api
RUN pip install --no-cache-dir ./api \
    && useradd --create-home itmam
USER itmam

EXPOSE 8080
CMD ["sh", "-c", "uvicorn itmam.app:app --host 0.0.0.0 --port ${PORT}"]
