# Fine-tuning Pipeline — Cloud Run Job for domain-specific fine-tuning
# Pinned by digest; Dependabot proposes the tag's new digest for review.
FROM python:3.11-slim@sha256:673719e071ecf27434fd2635bbd8d00f49f4fdd81f243143438a42ced8bedf60

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy pipeline code
COPY pipeline.py .

# Run as non-root
USER 1000

ENTRYPOINT ["python", "pipeline.py"]
