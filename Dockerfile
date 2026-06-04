FROM python:3.13-slim

WORKDIR /app

# Install dependencies
RUN pip install --no-cache-dir kubernetes grpcio protobuf

# Copy source code
COPY pydra /app/pydra

# Set pythonpath
ENV PYTHONPATH=/app

# Entrypoint for network driver by default
ENTRYPOINT ["python", "-m"]
CMD ["pydra.plugins.network.driver"]
