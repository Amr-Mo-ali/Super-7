FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

WORKDIR /app

RUN groupadd --system app && useradd --system --gid app --create-home --home-dir /home/app app

COPY --chown=app:app pyproject.toml README.md ./
COPY --chown=app:app src ./src

RUN pip install --no-cache-dir . \
    && pip uninstall --yes opencv-python \
    && pip install --no-cache-dir --force-reinstall --no-deps "opencv-python-headless>=4.10,<5" \
    && mkdir --parents /app/debug /models \
    && chown --recursive app:app /app /models

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "from urllib.request import urlopen; response = urlopen('http://127.0.0.1:8000/openapi.json', timeout=3); assert response.status == 200" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
