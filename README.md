# Audio Relay Bot

Telegram group bot jo ek userbot account ke zariye target bot se audio episodes laata hai,
queue me serve karta hai, cache karta hai, aur prepaid wallet (Razorpay) se paisa leta hai.

> **Sirf wahi content use karo jiske resale/redistribution ka aapke paas haq ya licence hai.**
> Target bot ke terms aur Razorpay ki policy follow karna aapki zimmedari hai.

## Features
- Group me `/s naam` search, inline buttons relay, episodes: menu ko reply `360,370-399`
- FIFO queue (ek userbot, ek time par ek kaam) aur live queue number
- Episode cache (`file_id`), dobara maangne par turant, bina download/upload
- SQLite wallet: Razorpay top-up (`/add`), auto-refund, crash-safe refund on restart
- Daily limit, max episodes per request, group whitelist, admin commands

## Quick start (Ubuntu VPS)
```bash
git clone https://github.com/<you>/<repo>.git
cd <repo>
bash install.sh
```
`install.sh` packages install karta hai, `.env` khulwata hai, userbot login (phone + OTP) karwata hai,
systemd service start karta hai, aur chaho to nginx + HTTPS webhook bhi lagata hai.

### Manual setup
```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env
./venv/bin/python login.py          # ek baar
./venv/bin/python bot.py
```

## Config (`.env`)
| Key | Matlab |
|---|---|
| `API_ID`, `API_HASH` | my.telegram.org se |
| `BOT_TOKEN` | @BotFather se |
| `TARGET_BOT` | jis bot se audio aata hai, jaise `@name_bot` |
| `ALLOWED_CHATS` | group IDs (comma separated). **Zaroor bharo** |
| `ADMIN_IDS` | admin Telegram user IDs |
| `PRICE_PER_EP`, `MIN_TOPUP`, `MAX_TOPUP` | ₹ me |
| `DAILY_LIMIT`, `MAX_EPS` | per user per din / per request |
| `RZP_KEY_ID`, `RZP_KEY_SECRET`, `RZP_WEBHOOK_SECRET` | Razorpay (khali = payments band) |
| `FIRST_REPLY_TIMEOUT`, `IDLE_TIMEOUT`, `VIEW_TTL` | timing (seconds) |

## Razorpay setup
1. Dashboard → **Account & Settings → API Keys → Generate Key**
2. **Webhooks → Add New**: URL `https://your.domain/rzp-webhook`, event `payment_link.paid`, apna secret
3. Keys aur secret `.env` me daalo, `sudo systemctl restart audiobot`
4. Pehle **Test Mode** me `/add 20` try karo

## Commands
| Command | Kaam |
|---|---|
| `/s naam` | search |
| menu ko reply `360,370-399` | episodes |
| `/add 100`, `/balance`, `/terms` | wallet |
| `/credit <uid> <rs>`, `/stats` | admin |

## Operations
```bash
journalctl -u audiobot -f           # logs
sudo systemctl restart audiobot
cp data/bot.db backup-$(date +%F).db   # roz backup (isme users ka balance hai)
```

## Security
- `.env`, `data/` aur `*.session` **kabhi GitHub par mat daalo** (`.gitignore` me already hain).
  Session file se aapke Telegram account ka poora access mil jata hai.
- Agar galti se commit ho gaya, to secrets turant badlo (BotFather `/revoke`, Razorpay keys regenerate,
  Telegram me session terminate) kyunki git history me wo rehta hai.

## License
MIT, `LICENSE` dekho. Content ki zimmedari use karne wale ki hai.
