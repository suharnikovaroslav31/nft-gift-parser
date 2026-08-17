FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV DATA_DIR=/app/data
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PROXY_HOST=
ENV PROXY_PORT=0

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir "python-dotenv==1.0.1"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && python -c "import dotenv; print('python-dotenv ok')"

COPY . .
RUN mkdir -p /app/data && chmod 777 /app/data

CMD ["python", "main.py"]
