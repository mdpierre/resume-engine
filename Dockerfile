FROM python:3.12-slim

# LibreOffice renders the PDF, Poppler measures it, Liberation Sans is metric-compatible with Arial.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libreoffice-writer-nogui poppler-utils fonts-liberation fontconfig \
 && rm -rf /var/lib/apt/lists/* \
 && fc-cache -f

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 SOFFICE=/usr/bin/soffice HOME=/home/app
WORKDIR /app
COPY requirements.txt requirements-bot.txt ./
RUN pip install --no-cache-dir -r requirements-bot.txt
COPY . .

RUN useradd --create-home --uid 10001 app && chown -R app /app
USER app
CMD ["python", "telegram_bot.py"]
