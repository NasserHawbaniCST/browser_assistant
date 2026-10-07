# Chromium and its system libraries come with the official Playwright image (same version as requirements.txt)
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

# Arabic fonts: the pages and their screenshots show Arabic text correctly
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-noto-core fonts-kacst fonts-hosny-amiri \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# The system Python of the image: --break-system-packages is expected there
RUN python3 -m pip install --no-cache-dir --break-system-packages -r requirements.txt
COPY app.py .

USER pwuser
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=20s CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["python3", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
