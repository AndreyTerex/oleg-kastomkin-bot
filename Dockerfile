FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Moscow

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Бот работает от непривилегированного пользователя и пишет только в /app/data.
RUN useradd --create-home --uid 1000 bot \
    && mkdir -p /app/data \
    && chown -R bot:bot /app
USER bot

CMD ["python", "-u", "bot.py"]
