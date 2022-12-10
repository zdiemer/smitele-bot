FROM python:3.11-slim as base

ENV LANG C.UTF-8
ENV LC_ALL C.UTF-8
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONFAULTHANDLER 1
ENV PYTHONUNBUFFERED 1
RUN apt-get update

FROM base AS python-deps

# Install pipenv and compilation dependencies
RUN pip install pipenv
RUN apt-get install -y --no-install-recommends gcc && apt-get install -y libc6-dev

# Install python dependencies in /.venv
COPY Pipfile .
COPY Pipfile.lock .
RUN PIPENV_VENV_IN_PROJECT=1 pipenv install --deploy

FROM base AS runtime

# ffmpeg is needed to send audio in voice channels
RUN apt-get install -y --no-install-recommends ffmpeg

# Copy virtual env from python-deps stage
COPY --from=python-deps /.venv /.venv
ENV PATH="/.venv/bin:$PATH"

# Create and switch to smitele
RUN useradd --create-home smitele
WORKDIR /home/smitele
USER smitele

# Copying Smitele code to container
COPY src/HirezAPI/*.py src/HirezAPI/
COPY src/SmiteBot/*.py src/SmiteBot/
COPY config.json config.json

# Adding HirezAPI to PYTHONPATH
ENV PYTHONPATH="$PYTHONPATH:/home/smitele/src/HirezAPI"

# Run Smitele!
ENTRYPOINT ["python", "./src/SmiteBot/smitele_bot.py"]