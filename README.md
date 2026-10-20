# nbafantasy

Data and content pipeline behind b3basketball.com and its Instagram account: NBA/EuroLeague data collection,
fantasy cards and reels, bookmaker odds comparison and bet settlement, news posts, and the n8n posting workflows.
The website itself lives in the `b3basketball-site` repo.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the system map, every integration and the process flows.

## Setup

```
python -m venv venv
venv/Scripts/python.exe -m pip install -r requirements.txt
copy .env.example .env      # then fill it in
```

Also needed: Postgres (see `DATABASE_URL`), Google Chrome (headless rendering), ffmpeg (reels), curl, Node.js
(`npx wrangler`), Docker (n8n, `n8n Docker/docker-compose.yml`), Claude Code with the skills in `ops/claude-skills/`
copied to `~/.claude/skills/`.

Data, media and secrets are not in git (see `.gitignore`).
