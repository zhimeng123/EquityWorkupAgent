FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY mlc_agent ./mlc_agent
COPY configs ./configs
COPY Workup_template_260617-外测版.docx ./

RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install .

ENTRYPOINT ["mlc-agent"]
CMD ["--help"]
