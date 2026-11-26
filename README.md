# Cortex Blog Studio API

Posts and images are stored in Turso. The portfolio reads published posts from `/api/posts`.

## Environment variables
- `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`: required.
- `WRITER_KEY`: the studio access key. Required.
- `SECRET_KEY`: optional, but recommended. Signs login sessions.
- `PUBLIC_URL`: this service's address, e.g. `https://cortex-blog.onrender.com`.
- `SITE_URL`: the portfolio's address, e.g. `https://my-portfolio.onrender.com`.
- `RENDER_DEPLOY_HOOK`: the portfolio's Render Deploy Hook. Publishing or editing a live post rebuilds the site.
- `REBUILD_DELAY`: optional, seconds to wait before rebuilding. Default `45`.
- `CORS_ORIGINS`: optional. Default `*`.

## Render (Web Service)
- Build command: `pip install -r requirements.txt`
- Start command: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2`

## Notes
- Thumbnails are resized to 1600 px and stored as WebP inside Turso.
- Existing tables are kept. Columns from earlier versions are ignored.
