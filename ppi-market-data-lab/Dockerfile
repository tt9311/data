FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 OPTIONS_DATA_DIR=/data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py schema.sql ./
RUN mkdir -p /data && chown -R 10001:10001 /data /app
USER 10001:10001
CMD ["python", "capture.py"]
