FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    STORAGE_DIR=/data \
    REQUIRE_FACE_MESH=true \
    FORWARDED_ALLOW_IPS=*

WORKDIR /app
COPY requirements.txt constraints.txt ./
# Every version comes from constraints.txt, so a rebuild installs exactly what was tested.
# MediaPipe pulls opencv-contrib-python, whose cv2 needs a display server; the headless
# build is laid back on top — pinned and --no-deps.  The previous unpinned
# `--force-reinstall opencv-python-headless` pulled OpenCV 5 and NumPy 2, which breaks
# MediaPipe 0.10.21 (built against NumPy 1) and silently left the service without any
# face detector.  The last line fails the build instead of shipping that state again.
RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt \
    && pip install --no-cache-dir --force-reinstall --no-deps -c constraints.txt opencv-python-headless \
    && python -c "import cv2, numpy, mediapipe as mp; \
assert cv2.__version__.startswith('4.') and hasattr(cv2, 'CascadeClassifier'), cv2.__version__; \
assert numpy.__version__.startswith('1.'), numpy.__version__; \
mp.solutions.face_mesh.FaceMesh(static_image_mode=True).close(); \
mp.solutions.selfie_segmentation.SelfieSegmentation(model_selection=0).close(); \
print('vision stack OK', cv2.__version__, numpy.__version__, mp.__version__)"

COPY . ./
# /data holds submissions and the SQLite file.  The code stays root-owned and read-only
# for the runtime user, who can write nowhere but /data and /tmp.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"
# One worker on purpose: the processing pool and the login limiter live in this process.
# Access logs come from the service itself, which never logs query strings.
CMD ["uvicorn", "service.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--proxy-headers", "--no-access-log", "--timeout-graceful-shutdown", "30"]
