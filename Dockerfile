FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV DATA_DIR=/app/data
ENV PYTHONUNBUFFERED=1
ENV PROXY_HOST=
ENV PROXY_PORT=0
RUN mkdir -p /app/data && chmod 777 /app/data

CMD ["python", "main.py"]
