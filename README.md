# Big-Red-Photon
## Photon spike (Stage 0b)

Bridge: `cd bridge && cp .env.example .env` (fill in Photon creds), `bun install`, `bun run src/index.ts`.
SDK: `spectrum-ts` 12.10.1. Findings below come from the SDK's type definitions and Photon's docs. Items marked **LIVE?** still need to be confirmed with real phones.

| Question | Answer |
|---|---|
| Same handler for group + DM? How to tell apart? | Yes. Both arrive on `app.messages` as `[space, message]`. `imessage(space).type` is `"dm"` or `"group"`. **LIVE?** |
| Can the bot initiate a DM to a handle that has only spoken in a group? | The API exists (`imessage(app).space.create(handle)`), but shared (free) lines only message recipients registered as **users** in the Photon project, and only after that number has texted the line first. Keep the "DM me start" flow. **LIVE?** |
| Native polls in groups? Do votes arrive as events? | SDK supports `space.send(poll(title, options))` (iMessage, not as a reply). Votes arrive on `app.messages` as `content.type === "poll_option"` with `selected` and `option.title`; the bridge forwards selected votes to `/webhooks/photon/poll_vote`. **LIVE?** |
| Shared location payload? | The SDK has no location content type. If anything arrives, it will be an `attachment` (the bridge logs its mime type). Default to typed landmarks. **LIVE?** |
| Rate / allowlist limits? | Free/Pro = **shared line pool**: each user is routed through a number that may differ per person (confirmed: two teammates see different bot numbers). ~5,000 msgs/day, ~50 new conversations per line per day. Recipients must be registered as users. |
| Do group chats work on the free tier, or does HACKWITHPHOTON unlock them? | Per Photon docs, shared pool: **no group creation and no group-change events**; only the Business plan (dedicated line, everyone texts one number) fully supports groups. A human-created group that includes one bot number *may* still deliver messages. Also, the bot can only send to a group it has received a message from since the bridge started. Ask Photon whether HACKWITHPHOTON gives a dedicated line. **LIVE?** |
| Sender display name? | Not provided. `sender.id` is the handle (E.164 phone or email). Onboarding must ask for a name. |

## Web signup

People can sign up on a web page instead of answering setup questions by text, then
finish by texting the bot a short code. The site never sends the first text: Photon's
free shared lines can't message a number that hasn't texted them, and texting the code
proves the person owns the number. Details: ARCHITECTURE.md §7.5.

**Settings** (in `.env`):

| Variable | What it does |
|---|---|
| `APP_NAME` | Product name shown on the pages |
| `PHOTON_PROJECT_ID`, `PHOTON_PROJECT_SECRET` | Same values as `bridge/.env`. The backend registers each signup as a user of your Photon project and reads the bot number Photon assigns them |
| `BOT_PHONE_NUMBER` | Fallback number to show if Photon hasn't assigned one yet, e.g. `+16075550000` |
| `PHOTON_CAN_INITIATE` | Leave `false` on the free plan. If `true`, the bot also texts the code right after signup |

**Try it locally:** run the backend (`uv run uvicorn app.main:app --port 8000`) and the
bridge, then:

1. Open http://localhost:8000/signup, enter your first and last name, phone, email, and
   a demo bank. With `PHOTON_TOKEN` set (run `npx @photon-ai/cli login`, then put the
   token in `.env`), Photon emails you its opt-in invite: accept it.
   Your number is registered with the Photon project (you'll see it in the dashboard's
   Users tab), and the page shows the bot number Photon assigned you.
2. Tap **Text the bot to finish** (or scan the QR code with your phone). It's Photon's
   opt-in link: it opens Messages to your number with `start <CODE>` filled in, and
   sending it is what opts you in so the bot can text you back. Typing the number by
   hand may not opt you in.
3. Reply `yes` to the suggested budget (or type a number). You get the intro text.
4. Check Photon's dashboard → **Users**: your number is listed with its **TEXTS ON** line.

**Share it with people off your laptop** using a tunnel, both free:

```bash
cloudflared tunnel --url http://localhost:8000   # prints https://<random>.trycloudflare.com
# or
ngrok http 8000                                  # prints https://<random>.ngrok-free.app
```

Share `https://<tunnel-host>/signup`. Through a tunnel only `/signup` and `/static` are
reachable. The bot's webhook and
the simulator answer "not found", so nobody on the internet can fake a text to the bot.

**Photon free-plan notes:** the bot only talks to phones registered as users in the
Photon project, and each person must text their bot line first. Signup handles both:
it registers the number, then shows the line Photon assigned that person (on the shared
pool, people can get different numbers). If Photon is unreachable, the page shows
`BOT_PHONE_NUMBER` instead, or asks the person to refresh.
