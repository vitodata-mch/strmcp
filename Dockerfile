FROM python:3.12-slim

WORKDIR /app

# Install system deps for torch/audio
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsndfile1 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
COPY static/ static/

EXPOSE 8003

CMD ["python", "server.py"]
