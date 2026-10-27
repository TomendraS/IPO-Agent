# IPO Research Agent Team

A manager agent plus 6 specialist agents (fundamentals, market demand, sector, geopolitics/macro,
governance, news sentiment) and a skeptic. They research every open or upcoming IPO and the manager
combines everything into an APPLY / AVOID / NEUTRAL call, sent to your Telegram.

## Daily flow
1. 9:00 AM IST: bot asks "Search IPOs today?" with Yes / No buttons.
2. Tap Yes: the team researches (takes a few minutes), then one message per IPO arrives.
3. Tap "I'm interested" on an IPO: it goes on your watchlist.
4. 1 hour before it closes (4 PM IST on the closing day) you get a reminder. The morning message also lists your watchlist with days left.
5. Anytime: send `/ipo` to run research on demand, `/list` to see your watchlist.

Button taps are picked up every ~5 minutes (GitHub Actions can't listen live), so expect a short delay.

## Setup (about 15 minutes)

### 1. Telegram bot
- In Telegram, message @BotFather, send `/newbot`, copy the **bot token**.
- Open your new bot and send it any message (like "hi").
- Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser. Find `"chat":{"id":123456789` and copy that number. This is your **chat id**.

### 2. Free API keys
- Gemini (required): https://aistudio.google.com/apikey
- Groq (recommended fallback): https://console.groq.com/keys
- OpenRouter (optional fallback): https://openrouter.ai/keys

Your Gemini Pro chat subscription does not include API access, so create the key above separately. It is free.

### 3. GitHub
- Create a new **public** repository and upload everything in this folder (keep the `.github` folder).
  - Public repos get unlimited free Actions minutes. A private repo's 2,000 free minutes will run out because of the 5-minute polling.
  - The repo will show your IPO watchlist and nothing secret. Keys stay in Secrets.
- Repo > Settings > Secrets and variables > Actions > New repository secret. Add:
  `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`
- Repo > Actions tab > enable workflows. Open "ipo-agent" > Run workflow > choose `morning` to test. You should get the Telegram prompt.

## Customising
- Other market: set env `IPO_MARKET` in the workflow (default India NSE/BSE).
- Number of IPOs per run: `MAX_IPOS` (default 4, keeps you inside free rate limits).
- Models: `GEMINI_MODEL`, `GROQ_MODEL`, `OPENROUTER_MODEL`. Free model names change, so update them if a provider returns "model not found".
- Add or edit agents: the `SPECIALISTS` dictionary in `ipo_agent/team.py`.

## Honest limits
- Only Gemini can search the web live. If it hits its free limit, the other agents answer from older training data and the report warns you.
- GMP and subscription numbers come from web pages and can be wrong or stale. Verify them before bidding.
- This is research support, not financial advice. An APPLY call is not a guarantee of listing gains.
- GitHub scheduled runs can be delayed by several minutes, and GitHub pauses schedules on repos with no activity for 60 days.

## Live office page (watch the team work)

A web page shows the manager in a glass room and the six analysts plus the skeptic at their desks, seen from above.
A desk lamp turns on while that analyst works, a speech bubble says what they are doing, and a paper flies to the
manager's tray when a report is ready. Verdicts, an activity feed and your watchlist sit beside it. Anyone with the
link can watch. Only you can start a run (it needs your GitHub token).

Open `docs/index.html` in a browser first. Until Firebase is set up it plays a demo with sample data, so you can see it.

### Setup (about 10 minutes, free)
1. **Firebase:** console.firebase.google.com > Add project (turn Google Analytics off) > Build > Realtime Database > Create database (pick Singapore, start in locked mode).
2. **Rules tab:** paste this and Publish. It lets everyone read the status and nobody write it:
   ```
   { "rules": { "status": { ".read": true, ".write": false } } }
   ```
3. Copy the database address shown at the top of the Data tab. Add it as GitHub secret `FIREBASE_DB_URL`.
4. **Project settings > Service accounts > Generate new private key.** Open the downloaded file, copy all of it, and add it as GitHub secret `FIREBASE_SERVICE_ACCOUNT`. The agents use it to write status. Never put it in `docs/`.
5. Edit `docs/config.js`: set `firebaseUrl` to the same address and `githubRepo` to `your-username/your-repo`.
6. **GitHub repo > Settings > Pages:** Source "Deploy from a branch", branch `main`, folder `/docs`. Your page is at `https://your-username.github.io/your-repo/`.
7. **Start button (optional):** GitHub > Settings > Developer settings > Fine-grained tokens > new token, only this repository, permission Actions: Read and write. Open the page, press Settings, paste it. It is stored in that browser only.

Pressing Start research runs the workflow in `research` mode. GitHub takes up to a minute to start it. Runs started from Telegram or the 9 AM prompt show on the page too.

### Honest notes
- The analysts work one after another, not in parallel, to stay inside free rate limits. So you will see one desk lit at a time.
- The page shows what the agents report. If a free model fails, that desk shows a red error, never a made-up result.
- The publisher was tested against a mock of Firebase, not a live project. If the page stays at "Offline", check the database address and the rules.
- Firebase free tier is plenty for this. Do not put anything private in the status data, because the page is public.

## More free capacity: several Gemini keys and NVIDIA

**Several Gemini keys.** Put them in one GitHub secret called `GEMINI_API_KEYS`, separated by commas
(`keyA,keyB,keyC`). The agent uses the key that rested longest, so calls alternate between keys. If a key hits its
limit, the next one takes over at once, and a key that is out of daily quota is skipped for the rest of the run.
- Quota is counted per Google Cloud project, not per key. Two keys from the same project share one limit. Only keys from different projects add capacity.
- Using several accounts to get around free limits can break Google's rules. Google restricts projects it flags for this, and a flagged project can be hard to get back (see the report linked in the chat). Start with one key, add more only if you still run out, and do not use the account you depend on for Gmail or your Gemini Pro subscription.

**NVIDIA.** Create a free key at build.nvidia.com (developer account, phone verification) and add it as secret `NVIDIA_API_KEY`.
It is OpenAI compatible, so it sits in the chain after Gemini: Gemini, NVIDIA, Groq, OpenRouter. The skeptic prefers NVIDIA first.
- It cannot search the web, so it works for analysis, the skeptic and the manager, not for live research.
- The default model is `meta/llama-3.3-70b-instruct`. Change it with the `NVIDIA_MODEL` variable if NVIDIA renames or retires it.
- Reported limits are about 40 requests per minute per key. Sources disagree on whether there is a token or credit cap, so check your dashboard.
- Meant for prototyping, not production. Fine for a personal research helper.
