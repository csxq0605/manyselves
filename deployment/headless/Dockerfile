ARG BASE_IMAGE
FROM ${BASE_IMAGE}

ARG SOURCE_COMMIT=unknown
ARG BUILD_EPOCH=0
LABEL org.opencontainers.image.revision=$SOURCE_COMMIT \
      org.opencontainers.image.created=$BUILD_EPOCH

RUN useradd --create-home --uid 10001 manyselves
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir '.[server]'

USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["manyselves-headless"]
CMD ["api", "--host", "0.0.0.0", "--port", "8080"]
