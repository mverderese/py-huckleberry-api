# Using the Huckleberry connector in Claude

The connector lets Claude read and log your Huckleberry baby tracking: sleep, feeds, diapers,
pumping, growth, temperature, medicine, and activities, including the app's live timers. It works
in Claude on the web, on desktop, and in the iPhone and Android apps.

You need the connector URL of a deployed server, `https://<project>.vercel.app/mcp` (see
[the connector README](../README.md) to deploy your own), and the email and password you use to
sign in to the Huckleberry app. It only works for accounts that sign in with an email and
password.

## Add it to Claude

1. In the Claude app (iPhone, Android, or desktop) or at [claude.ai](https://claude.ai), open
   **Settings → Connectors → Add custom connector**. Once added, it's available on all your
   devices.
2. Name it **Huckleberry** and paste the connector URL. Leave the OAuth client ID and secret
   empty.
3. Click **Connect**. On the page that opens:
   - Enter your Huckleberry email and password.
   - Check the **Time zone** field. It's filled in from your browser (e.g. `America/New_York`).
     Times you give Claude, like "10:30", are read in this zone.
   - Click **Sign in and allow**. You're sent back to Claude.

Your password is sent to Huckleberry once to sign in. It isn't stored, and Claude never sees it.

**Claude Code:** connectors added at claude.ai also appear in Claude Code. To add it to Claude
Code only, run `claude mcp add --transport http huckleberry <connector URL>`, then `/mcp` to
sign in.

## Use it

Turn on the Huckleberry connector in a chat and ask things like:

- "Start a nap." / "She's awake, stop the sleep timer."
- "Start nursing on the left." / "Switch sides." / "Done nursing."
- "Log a wet and dirty diaper at 3:10, yellow and runny."
- "Log a 4 oz formula bottle 20 minutes ago."
- "How did she sleep last night?" / "When did she last eat?"
- "Log 1 ml of Vitamin D."
- "Her temperature is 100.4 F."

Claude asks before it changes anything. If a child's name is needed (more than one child on the
account), Claude asks. Claude can also delete an entry logged by mistake ("delete that last
diaper"); it confirms which one first, and deleting can't be undone.

## Disconnecting

- **Remove it from Claude:** Settings → Connectors → Huckleberry → Disconnect.
- **Change your time zone:** disconnect and connect again.
- **If Claude asks you to reconnect:** your sign-in expired. Connect again and sign in.
