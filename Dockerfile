FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd -r -u 10001 -m monitor \
    && mkdir /data && chown monitor:monitor /data
COPY monitor.py .
USER monitor
CMD ["python", "/app/monitor.py"]
