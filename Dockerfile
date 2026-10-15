FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 先安装依赖，业务代码变更时可以复用依赖层缓存。
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY lab/ ./lab/
USER 10001:10001

CMD ["python", "-m", "uvicorn", "lab.orders.main:app", "--host", "0.0.0.0", "--port", "8000"]
