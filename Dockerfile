# P1Pro 控制台 · 飞牛版
# 只用标准库，所以镜像可以做得非常小（约 55 MB）
FROM python:3.12-alpine

LABEL org.opencontainers.image.title="sunlogin-p1pro-nas" \
      org.opencontainers.image.description="向日葵 P1Pro 智能插线板控制台（飞牛 fnOS 版）" \
      org.opencontainers.image.source="https://github.com/52sanmao/sunlogin-p1pro-nas"

WORKDIR /app

# 时区：曲线、定时、电费统计全按本地自然日/自然月切分，
# 容器默认 UTC 会让"今天"错 8 小时。
RUN apk add --no-cache tzdata \
    && cp /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo "Asia/Shanghai" > /etc/timezone

COPY app/server/app.py ./
COPY app/server/p1pro ./p1pro
COPY app/server/static ./static

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    P1PRO_CONFIG=/app/data/config.json \
    P1PRO_DB=/app/data/p1pro.db

# 数据（SQLite + 配置）都在 /app/data，挂出来就不会随容器重建丢失
RUN mkdir -p /app/data
VOLUME ["/app/data"]

EXPOSE 9787

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:%s/api/health'%os.environ.get('P1PRO_PORT','9787'),timeout=3).read()" || exit 1

CMD ["python", "app.py", "-c", "/app/data/config.json"]
