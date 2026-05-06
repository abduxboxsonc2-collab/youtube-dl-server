FROM python:3-alpine
RUN apk add --no-cache ffmpeg deno   # ✅ deno added here
RUN pip install yt-dlp starlette uvicorn httpx
COPY . /app
WORKDIR /app
CMD uvicorn server:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1
