FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STORAGE_DIR=/data

WORKDIR /app
COPY requirements.txt ./
# MediaPipe depends on opencv-contrib-python, which installs a second, non-headless cv2
# over the one requested here; the reinstall puts the headless build back on top, which is
# the only one that imports without a display server.
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir --force-reinstall opencv-python-headless

COPY . ./
# /data holds submissions and the SQLite file: outside /app, so the public static
# handler of the signature tool can never reach identity photographs.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data
USER appuser

EXPOSE 8000
CMD ["uvicorn", "service.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
