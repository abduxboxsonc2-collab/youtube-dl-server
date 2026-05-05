FROM python:3-alpine
RUN apk add --no-cache ffmpeg nodejs npm git
RUN pip install yt-dlp starlette uvicorn httpx
COPY . /app

# Clone and build the PO‑Token provider
RUN git clone --single-branch --branch 1.3.1 \
    https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git \
    /app/bgutil-provider
WORKDIR /app/bgutil-provider/server
RUN npm ci && npx tsc

WORKDIR /app
CMD uvicorn server:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1
