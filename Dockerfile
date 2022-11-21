FROM python:3.11-slim as base

# Setup env
ENV LANG C.UTF-8
ENV LC_ALL C.UTF-8
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONFAULTHANDLER 1

FROM base AS python-deps

# Install pipenv and compilation dependencies
RUN pip install pipenv
RUN apt-get update && apt-get install -y --no-install-recommends gcc && apt-get install -y libc6-dev

# Install python dependencies in /.venv
COPY Pipfile .
COPY Pipfile.lock .
RUN PIPENV_VENV_IN_PROJECT=1 pipenv install --deploy

FROM base AS runtime

# Application dependency
RUN apt-get update && apt-get install -y ffmpeg

# Copy virtual env from python-deps stage
COPY --from=python-deps /.venv /.venv
ENV PATH="/.venv/bin:$PATH"

# Create and switch to a new user
RUN useradd --create-home smitele
WORKDIR /home/smitele
USER smitele

# Install application into container
COPY src/HirezAPI/*.py src/HirezAPI/
COPY src/SmiteBot/*.py src/SmiteBot/
COPY config.json config.json
ENV PYTHONPATH="$PYTHONPATH:/home/smitele/src/HirezAPI"

# Run the application
ENTRYPOINT ["python", "./src/SmiteBot/smitele_bot.py"]