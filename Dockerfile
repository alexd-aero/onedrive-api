FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
ENV PORT=8080
# Single worker (in-memory token + device-code poller must live in one process), many threads.
CMD gunicorn -w 1 --threads 8 --timeout 600 -b 0.0.0.0:${PORT} app:app
