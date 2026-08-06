FROM python:3.11-slim AS base

ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONFAULTHANDLER=1
ENV PYTHONUNBUFFERED=1

FROM base AS python-deps

# pipenv plus the toolchain the wheels occasionally need to build from source.
# apt lists are fetched in the stage that uses them; inheriting them from a
# parent stage worked by accident and breaks the moment the base is rebuilt.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir pipenv

COPY Pipfile Pipfile.lock ./
RUN PIPENV_VENV_IN_PROJECT=1 pipenv install --deploy

FROM base AS runtime

# ffmpeg is needed to send audio in voice channels
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=python-deps /.venv /.venv
ENV PATH="/.venv/bin:$PATH"

RUN useradd --create-home --uid 1000 smitele
WORKDIR /home/smitele

# Both entrypoints live here: the bot (default) and the match data collector,
# which the deployment runs on a schedule. They share HirezAPI, so the image is
# one artifact and the command selects the role.
#
# The previous version also copied matchDetails/*.json and config.json, neither
# of which is in the repo — .gitignore excludes both — so the build could not
# succeed from a clean checkout.
COPY src/HirezAPI/*.py src/HirezAPI/
# A separate line because the glob above matches files, not directories — the
# Smite 2 package was silently absent from the image while every import of it
# passed locally, and the bot crash-looped on ModuleNotFoundError.
COPY src/HirezAPI/smite2/*.py src/HirezAPI/smite2/
COPY src/SmiteBot/*.py src/SmiteBot/
COPY src/match_data_collector/*.py src/match_data_collector/
# The tracker.gg crawl. Its entrypoint lives in Dockerfile.s2collector, which
# layers a browser on top of this image, but the source belongs here so the two
# cannot drift apart.
COPY src/smite2_collector/*.py src/smite2_collector/
# The bot scores candidate builds with a numpy copy of the trained model, so it
# needs this package but not torch. Training runs from Dockerfile.train, which
# layers torch on top of this image.
COPY src/ml/*.py src/ml/

# Adding HirezAPI and ml to PYTHONPATH
ENV PYTHONPATH="/home/smitele/src/HirezAPI:/home/smitele/src/ml"

# Fail the build, not the deployment, when a module is missing from the image.
# The bot's imports all resolve from the source tree whether or not the
# Dockerfile copies them, so nothing before this caught a missed COPY.
RUN python -c "import sys; sys.path[:0] = ['src/HirezAPI', 'src/ml', 'src/SmiteBot']; \
import smite2.players, smite2.provider, smite2.tracker_client, smite2.voicelines, smite2.wikitext; \
import providers, game, guild_settings; \
print('image imports ok')"

# Two volumes, with very different shapes. /data is small and private to one
# replica — session token, patch marker, gods/items caches, downloaded art.
# /matchdata is the big shared match-detail corpus: the collector writes it and
# the bot reads it, so in the cluster it's network storage.
#
# Credentials come from the environment (SMITELE_DISCORD_TOKEN,
# SMITELE_HIREZ_DEV_ID, SMITELE_HIREZ_AUTH_KEY). SMITELE_CONFIG_FILE stays
# pointed at a path that normally doesn't exist so a config.json can still be
# mounted in instead; missing is not an error when the environment has the keys.
ENV SMITELE_DATA_DIR=/data \
    SMITELE_CACHE_DIR=/data/cache \
    SMITELE_MATCH_DATA_DIR=/matchdata/output \
    SMITELE_MATCH_ARCHIVE_DIR=/matchdata/archive \
    SMITELE_CONFIG_FILE=/config/config.json

RUN mkdir -p /data /matchdata/output /matchdata/archive \
    && chown -R smitele:smitele /data /matchdata

USER smitele

# Run Smitele! Override the command to run the collector instead:
#   python src/match_data_collector/match_data_collector.py [YYYY-MM-DD]
ENTRYPOINT ["python", "./src/SmiteBot/smitele_bot.py"]
