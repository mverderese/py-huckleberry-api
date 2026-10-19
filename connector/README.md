# Huckleberry connector for Claude (remote MCP server with OAuth)

A small, stateless [MCP](https://modelcontextprotocol.io) server you host on
[Vercel](https://vercel.com). Add it to Claude as a custom connector and Claude can read and log
your Huckleberry data on the web, desktop, and mobile apps. It's built on the `huckleberry_api`
library in this repository, which it installs from the parent directory.

Tools:

- **Read:** `list_children`, `get_current_status` (running timers and the latest entries),
  `get_entries` (history for sleep, feeding, diaper, health, pump, or activity),
  `list_medicines`, `find_solid_foods`
- **Live timers:** `sleep_timer`, `nursing_timer`, `pump_timer`, `activity_timer`
  (start, pause, resume, complete, and switch side or activity), and `cancel_timer`
- **Log past entries:** `log_sleep`, `log_nursing`, `log_bottle`, `log_solids`, `log_diaper`,
  `log_potty`, `log_pump`, `log_activity`, `log_growth`, `log_temperature`, `log_medicine`
- **Set up:** `create_medicine`, `create_custom_food`

Claude asks before using tools that change something, unless you allow them in the connector's
settings. There are no delete tools, because the library can't delete entries yet: remove
mistakes in the Huckleberry app.

## How sign-in works

When you connect, Claude sends you to the server's own sign-in page, where you enter your
Huckleberry email and password. The server signs in to Huckleberry (Firebase) once and then
forgets the password. It keeps nothing on disk and has no database: the tokens it gives Claude
are encrypted with your `TOKEN_SECRET`, and they carry the Firebase login token inside them.
Anyone with a Huckleberry account can sign in, and each person's connection only reaches their
own account. It only works for accounts that sign in with an email and password.

## 1. Deploy to Vercel

1. Push this repo to your GitHub account, then in Vercel choose **Add New → Project** and import
   it. Set the **Root Directory** to `connector` and leave the framework preset as **Other**.
   Keep **Include files outside the root directory in the Build Step** turned on (the default):
   the connector installs the library from the repository root. `vercel.json`, `pyproject.toml`,
   and `uv.lock` configure the rest, including Python 3.14.
2. Under **Settings → Environment Variables**, add:

   | Variable | Value |
   | - | - |
   | `TOKEN_SECRET` | Output of `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
   | `DEFAULT_TIMEZONE` | Optional, e.g. `America/Chicago` (default `America/New_York`). Prefills the sign-in page's time zone field when the browser doesn't report one |
   | `PUBLIC_BASE_URL` | Optional. Defaults to your production URL (`https://<project>.vercel.app`). Set it if you use a custom domain |

3. Deploy, then open `https://<project>.vercel.app/` in a browser. You should see a short page
   describing the server.

Keep Vercel's Deployment Protection off for the production domain (the default "Standard
Protection" only covers preview deployments). Claude has to be able to reach the server.

## 2. Add the connector in Claude

1. In Claude (web or desktop), open **Settings → Connectors → Add custom connector**.
2. Name it "Huckleberry" and enter `https://<project>.vercel.app/mcp`. Leave the OAuth client ID
   and secret empty, since Claude registers itself.
3. Click **Connect**, sign in with your Huckleberry account, and you're sent back to Claude. The
   sign-in page also asks for your time zone, prefilled from your browser. Times like "10:30" are
   read in that zone, and entries are saved with its UTC offset. To change it, disconnect and
   connect again.

Connectors belong to your account, so it also works in the Claude mobile apps.
[docs/claude-connector.md](docs/claude-connector.md) is a step-by-step guide you can share with
anyone who'll connect to your server.
For Claude Code: `claude mcp add --transport http huckleberry https://<project>.vercel.app/mcp`.

## Disconnecting and revoking

- **One device or connection:** disconnect the connector in Claude.
- **Everything at once:** change `TOKEN_SECRET` in Vercel and redeploy. Every token the server
  has issued stops working, and you sign in again on next use. Firebase has no way for the
  server to revoke a single sign-in early.
- Changing your Huckleberry password also ends existing sign-ins.

## Run it locally

```bash
cd connector
uv sync
uv run pytest                      # OAuth flow and tools, with Huckleberry faked
uv run ty check --python-version 3.14 .
PUBLIC_BASE_URL=http://127.0.0.1:8000 TOKEN_SECRET=... uv run uvicorn api.index:app --port 8000
```

Then `claude mcp add --transport http huckleberry http://127.0.0.1:8000/mcp`.
