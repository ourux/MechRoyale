# Stdlib-only bot: nothing is downloaded at build time except the base image.
# Base image pinned by digest so the final rebuild matches what was scored.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /bot
COPY mechfight.py bot.py ./
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]
