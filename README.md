# telegram-kicker

A Telegram bot that removes new members of a group or channel when their bio contains a Telegram link (`t.me/…`, `telegram.me/…`, `telegram.dog/…`, `tg://…`).

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`) and copy the token.
2. Install and configure:
   ```sh
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   cp .env.example .env   # then paste your token into .env
   ```
3. Run it:
   ```sh
   .venv/bin/python bot.py
   ```
4. Add the bot to your group or channel and **make it an administrator** with the **Ban users** and **Delete messages** permissions. Without **Delete messages**, the bot still removes spammers but leaves their posts behind. In a channel, that permission is listed under the admin rights for adding and removing subscribers.

## Deploy to Fly.io

Set `app` in `fly.toml` to a unique name first. Then run:

```sh
fly apps create <your-app-name>
fly volumes create kicker_data --size 1 --region ams
fly secrets set BOT_TOKEN=123456:ABC-your-token
fly deploy --ha=false
fly logs
```

Run exactly one machine. If two copies poll Telegram with the same token, Telegram returns conflict errors. A second machine also wouldn't see the join dates stored on the first machine's volume. `--ha=false` keeps it to one machine.

## How it works

- **New members**: when someone joins, the bot fetches their bio. If the bio has a Telegram link, the bot removes them.
- **Recheck after joining**: six minutes after someone joins, the bot checks their bio a second time.
- **Recent members who post**: the bot records when each member joins. For the first 14 days after someone joins, the bot checks their bio each time they send a message. If a link has appeared, it deletes the message and removes them. In a channel, this covers comments in the linked discussion group. The bot must be an admin there too.
- **Sweep of recent members**: while a chat is active, the bot also re-checks the bios of everyone who joined it in the last 14 days, at most once every 10 minutes. Any message in the chat (or in a channel's discussion group) triggers the sweep, so a quiet chat isn't swept.
- **Join requests**: if the chat requires admin approval, the bot declines requests from users with a link in their bio. It leaves every other request for a human admin to handle.

## Configuration (`.env`)

| Variable         | Default | Meaning                                                      |
|------------------|---------|--------------------------------------------------------------|
| `BOT_TOKEN`      | —       | Token from BotFather                                         |
| `ACTION`         | `kick`  | `kick` removes the user but lets them rejoin; `ban` blocks them permanently |
| `MATCH_MENTIONS` | `false` | Also treat bare `@username` mentions as links                |
| `NEW_MEMBER_DAYS`| `14`    | How long after joining a member's bio is still re-checked     |
| `DB_PATH`        | `members.db` | SQLite file where join dates are stored                 |

## Limitations

- Users can hide their bio with Telegram's privacy settings. The bot can't see a hidden bio, so it lets that user stay.
- Telegram doesn't tell bots when someone joined, so the bot records join dates itself. It won't recheck people who joined before it started running.
- After the recent-member window ends, the bot stops checking that person.
- A bio is cached for 5 minutes, so the bot might not catch a link that's added in the middle of a conversation right away.
- The bot must be an admin. Otherwise Telegram doesn't send it member updates.

## License

[MIT](LICENSE)
