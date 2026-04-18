# Agent Control Panel

Small FastAPI control panel for running commands and browsing execution logs.

## Run

The app is started by the systemd unit in `agent-cp.service`.

## Configuration

Optional environment variables:

- `CP_USER` — basic-auth username
- `CP_PASS` — basic-auth password
- `PORT` — HTTP port override

## Notes

- `venv/`, `__pycache__/`, `*.pyc`, `.env`, and `log.db` are ignored by git.
- The UI is mobile-friendly and uses responsive tables and log cards.
