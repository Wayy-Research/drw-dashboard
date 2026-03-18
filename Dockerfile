FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir fastapi uvicorn httpx asyncpg
COPY dashboard.py /app/mania/drw/dashboard.py
COPY db_logger.py /app/mania/drw/db_logger.py
RUN touch /app/mania/__init__.py /app/mania/drw/__init__.py
ENV PYTHONPATH=/app
EXPOSE 8080
CMD ["python", "-m", "mania.drw.dashboard"]
