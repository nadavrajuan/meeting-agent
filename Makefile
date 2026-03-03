VENV := /tmp/google-auth-venv
PYTHON := $(VENV)/bin/python

.PHONY: auth up down

auth:
	@if [ ! -f $(PYTHON) ]; then \
		python3 -m venv $(VENV) && $(VENV)/bin/pip install --quiet google-auth-oauthlib google-auth google-api-python-client python-dotenv; \
	fi
	PYTHONUNBUFFERED=1 $(PYTHON) scripts/auth_google.py

up:
	docker compose up -d

down:
	docker compose down
