FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8501

WORKDIR /app

# Dependencies first so code changes don't rebuild the wheel layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Fonts ship in the repo (assets/fonts), so the PNG renders identically to a
# local run without installing system fonts.

EXPOSE 8501

# 2 workers x 4 threads is plenty for an internal tool; report generation is
# CPU-light and mostly waits on the Jira/Zoho APIs.
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT} --workers ${WEB_CONCURRENCY:-2} --threads 4 --timeout 180 wsgi:app"]
