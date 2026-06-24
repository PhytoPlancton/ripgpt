FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libglib2.0-0 libnss3 libnspr4 libdbus-1-3 libatk1.0-0 \
        libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libatspi2.0-0 \
        libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
        libcairo2 libasound2 libx11-xcb1 libgtk-3-0 libx11-6 libxcb1 \
        libxext6 fonts-liberation xvfb && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    playwright install firefox && \
    playwright install-deps firefox && \
    python -c "import tiktoken; tiktoken.get_encoding('o200k_base')"   # pre-cache vocab in the image

COPY app ./app

ENV DISPLAY=:99
ENV HEADLESS=true

EXPOSE 8850

ENTRYPOINT ["sh", "-c", "rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null; Xvfb :99 -screen 0 1280x800x24 &>/dev/null & exec python -m app"]
