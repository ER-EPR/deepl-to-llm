FROM python:3.11-slim
WORKDIR /app

# 设置时区和环境变量
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码
COPY main.py bridge.py ./

# 暴露端口
EXPOSE 1188

# 启动服务
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "1188"]
