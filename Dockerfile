FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

# tzdata:让 ONLINE_HOURS 按 TZ 指定的本地时间计算
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# 登录 session 的存放目录(docker-compose 会把命名卷挂到这里)
RUN mkdir -p /data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py auto_checkin.py .

CMD ["python", "main.py"]
