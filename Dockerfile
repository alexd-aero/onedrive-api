FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
ENV PORT=8080
# App serves itself over TCP (Werkzeug, threaded). No gunicorn: it needs AF_UNIX, which
# Wasmer's WASIX Python lacks — so we use one code path that works everywhere.
CMD ["python", "app.py"]
