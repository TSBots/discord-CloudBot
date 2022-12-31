FROM python:3.6-slim-buster

RUN apt-get update && \
    apt-get install -y \
    libenchant1c2a \
    gcc && \
    rm -rf /var/lib/apt/lists/*

COPY Pipfile* /tmp/

RUN pip install pipenv && \
    cd /tmp && pipenv lock --requirements > requirements.txt && \
    pip uninstall -y pipenv && \
    pip install --no-cache-dir -r /tmp/requirements.txt && \
    python -m pip uninstall -y pip && \
    rm -rf /tmp/Pipfile*

RUN mkdir /beakerbot
WORKDIR /beakerbot
COPY . .

CMD python -m cloudbot
