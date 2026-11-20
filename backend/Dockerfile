FROM python:3.11-slim
WORKDIR /service
RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 libgl1 tesseract-ocr && rm -rf /var/lib/apt/lists/*
COPY backend/requirements.txt /service/backend/requirements.txt
RUN pip install --no-cache-dir -r /service/backend/requirements.txt
COPY backend/app /service/backend/app
COPY frontend /service/frontend
RUN mkdir -p /service/backend/model_cache /service/backend/data
EXPOSE 8000
CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
