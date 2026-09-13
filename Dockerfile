FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml ./
# The live extra only adds order signing; paper mode never imports it.
RUN pip install --no-cache-dir ".[live]"
COPY app ./app

CMD ["python", "-m", "app"]
