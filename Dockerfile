FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir fastapi "uvicorn[standard]" httpx selectolax curl_cffi pyyaml

COPY app ./app
RUN mkdir -p /app/data

EXPOSE 8787
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8787", "--workers", "1"]
