FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Moscow

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Xray — клиент VLESS/Trojan/VMess для VPN_SUBSCRIPTION (см. vpn.py). Скачивается с официальной страницы релизов.
ARG TARGETARCH
RUN python -c "import io, os, sys, urllib.request, zipfile; \
arch = {'amd64': '64', 'arm64': 'arm64-v8a'}.get(os.environ.get('TARGETARCH') or 'amd64', '64'); \
url = f'https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-{arch}.zip'; \
data = urllib.request.urlopen(url, timeout=120).read(); \
zipfile.ZipFile(io.BytesIO(data)).extract('xray', '/usr/local/bin')" \
    && chmod +x /usr/local/bin/xray \
    && /usr/local/bin/xray version | head -1

COPY . .

# Бот работает от непривилегированного пользователя и пишет только в /app/data.
RUN useradd --create-home --uid 1000 bot \
    && mkdir -p /app/data \
    && chown -R bot:bot /app
USER bot

CMD ["python", "-u", "bot.py"]
