FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium + all its system dependencies in one layer
RUN playwright install-deps chromium 2>/dev/null || \
    apt-get update && apt-get install -y --no-install-recommends \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libxcomposite1 \
    libxdamage1 libxrandr2 libgbm1 libxkbcommon0 libpango-1.0-0 \
    libpangocairo-1.0-0 libasound2 libx11-xcb1 libxfixes3 libxi6 \
    libxcursor1 libgtk-3-0 fonts-liberation \
    && rm -rf /var/lib/apt/lists/*
RUN playwright install chromium

COPY . .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
