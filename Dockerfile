FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends stockfish espeak-ng libsndfile1 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY server/requirements.txt server/requirements.txt
RUN pip install --no-cache-dir -r server/requirements.txt
# Uncomment for local TTS / STT (adds ~2 GB):
# RUN pip install --no-cache-dir kokoro misaki[en] faster-whisper
COPY server server
COPY client client
COPY lessons lessons
ENV STOCKFISH_PATH=/usr/games/stockfish OLLAMA_URL=http://ollama:11434 LLM_MODEL=qwen3:8b TTS_ENGINE=kokoro
EXPOSE 8000
CMD ["uvicorn", "app:app", "--app-dir", "server", "--host", "0.0.0.0", "--port", "8000"]
