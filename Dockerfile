FROM python:3.11-slim

WORKDIR /app

# LibreOffice converts Word invoices to PDF so the rest of the pipeline only
# ever handles PDFs. Writer alone, not the full suite - that is the difference
# between roughly 600MB and 1.5GB, which matters on a disk that has been at
# 95%. --no-install-recommends and clearing the apt lists in the same layer
# keep the cache out of the image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-writer \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency requirements first to leverage Docker layer caching
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the remaining project files
COPY . .

# Expose the application port
EXPOSE 8002

# Command to run the background service and FastAPI
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8002"]
