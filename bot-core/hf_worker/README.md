---
title: Cat-Bi Worker
emoji: 🤖
colorFrom: blue
colorTo: indigo
sdk: docker
pinned: false
---

# Cat-Bi Worker Space
This is a worker for the Cat-Bi Discord Bot. It handles heavy operations like manga downloading and image stitching.

## Configuration
Set the following secrets in your Space Settings:
- `HF_WORKER_KEY`: Secure key for communication (must match the bot's config)
- `WEB_PANEL_SECRET`: Fallback secret
- `GOOGLE_SERVICE_ACCOUNT_JSON`: (Optional) For Google Drive support
- `GOFILE_TOKEN`: (Optional) For Gofile uploads
