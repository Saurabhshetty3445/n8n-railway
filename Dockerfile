FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    wget curl ca-certificates \
    fonts-liberation \
    fonts-unifont \
    fonts-dejavu \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libcups2 \
    libdbus-1-3 \
    libgdk-pixbuf-xlib-2.0-0 \
    libnspr4 \
    libnss3 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libxshmfence1 \
    libgbm1 \
    libasound2 \
    libxfixes3 \
    libxext6 \
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxss1 \
    xdg-utils \
    --no-install-recommends && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Only install browser (NO deps)
RUN playwright install chromium

COPY scraper.py .

RUN mkdir -p /data

EXPOSE 8080
CMD ["python", "scraper.py"]
