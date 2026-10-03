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
