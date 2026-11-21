FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bridge ./bridge

RUN mkdir -p /data/inbox

CMD ["python", "-m", "bridge.main"]
