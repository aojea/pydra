FROM python:3.13-slim
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir kubernetes grpcio protobuf
ENV PYTHONPATH=/app
ENTRYPOINT ["python", "-m"]
CMD ["pydra.plugins.tpu.driver"]
