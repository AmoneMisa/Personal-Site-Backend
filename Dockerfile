FROM python:3.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
  build-essential \
  libffi-dev \
  python3-dev \
  ghostscript \
  fonts-dejavu-core \
  && rm -rf /var/lib/apt/lists/*


COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
