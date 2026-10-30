import asyncio, os, re, json, time, hashlib, hmac, uuid, sqlite3, logging
import httpx
from aiohttp import web
from dotenv import load_dotenv
from telethon import TelegramClient, events, Button
from telethon.errors import BotResponseTimeoutError

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("audiobot")

# ======================= CONFIG =======================
E = os.environ.get
API_ID, API_HASH = int(E("API_ID")), E("API_HASH")
BOT_TOKEN, TARGET_BOT = E("BOT_TOKEN"), E("TARGET_BOT")
ALLOWED_CHATS = [int(x) for x in E("ALLOWED_CHATS", "").split(",") if x.strip()]
ADMIN_IDS = [int(x) for x in E("ADMIN_IDS", "").split(",") if x.strip()]
PRICE_PER_EP = int(E("PRICE_PER_EP", "10"))          # Rs
MIN_TOPUP, MAX_TOPUP = int(E("MIN_TOPUP", "20")), int(E("MAX_TOPUP", "2000"))
DAILY_LIMIT = int(E("DAILY_LIMIT", "30"))
MAX_EPS = int(E("MAX_EPS", "20"))
RZP_KEY_ID, RZP_KEY_SECRET = E("RZP_KEY_ID", ""), E("RZP_KEY_SECRET", "")
RZP_WEBHOOK_SECRET = E("RZP_WEBHOOK_SECRET", "")
WEBHOOK_PORT = int(E("WEBHOOK_PORT", "8080"))
FIRST_REPLY_TIMEOUT = int(E("FIRST_REPLY_TIMEOUT", "30"))
IDLE_TIMEOUT = int(E("IDLE_TIMEOUT", "10"))
VIEW_TTL = int(E("VIEW_TTL", "21600"))
LINK_VALID_MIN = 30

TERMS = (
    "📜 **Terms**\n"
    "• Wallet balance sirf is bot me use hota hai, withdraw/transfer nahi hota.\n"
    "• Episode na mile to uska paisa wallet me wapas aa jata hai.\n"
    "• Payment Razorpay se hota hai. Help ke liye admin se sampark karo."
)

os.makedirs("data", exist_ok=True)
os.makedirs("downloads", exist_ok=True)
userbot = TelegramClient("data/userbot", API_ID, API_HASH)
bot = TelegramClient("data/mybot", API_ID, API_HASH)

# ======================= DATABASE =======================
db = sqlite3.connect("data/bot.db", check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL")
db.executescript("""
CREATE TABLE IF NOT EXISTS wallet(uid INTEGER PRIMARY KEY, paise INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY, uid INTEGER, ts INTEGER, amt INTEGER, why TEXT);
CREATE TABLE IF NOT EXISTS orders(ref TEXT PRIMARY KEY, uid INTEGER, chat INTEGER, amount INTEGER,
                                  reply_to INTEGER, paid INTEGER DEFAULT 0, payment_id TEXT);
CREATE TABLE IF NOT EXISTS paid_ids(pid TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS views(hid TEXT PRIMARY KEY, path TEXT, items TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS menus(key TEXT PRIMARY KEY, hid TEXT);
CREATE TABLE IF NOT EXISTS eps(key TEXT PRIMARY KEY, fids TEXT);
CREATE TABLE IF NOT EXISTS daily(uid INTEGER, day TEXT, n INTEGER, PRIMARY KEY(uid, day));
CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY, uid INTEGER, chat INTEGER, reply_to INTEGER,
                                cost INTEGER, status TEXT, ts INTEGER);
""")
db.commit()


def _credit(uid, paise, why):          # transaction ke andar call karo
    db.execute("INSERT INTO wallet(uid,paise) VALUES(?,?) ON CONFLICT(uid) DO UPDATE SET paise=paise+?",
               (uid, paise, paise))
    db.execute("INSERT INTO ledger(uid,ts,amt,why) VALUES(?,?,?,?)", (uid, int(time.time()), paise, why))


def bal(uid):
    r = db.execute("SELECT paise FROM wallet WHERE uid=?", (uid,)).fetchone()
    return r[0] if r else 0


def rs(p):
    return f"₹{p / 100:g}"


def today():
    return time.strftime("%Y-%m-%d")


def used_today(uid):
    r = db.execute("SELECT n FROM daily WHERE uid=? AND day=?", (uid, today())).fetchone()
    return r[0] if r else 0


def add_used(uid, n):
    db.execute("INSERT INTO daily(uid,day,n) VALUES(?,?,?) ON CONFLICT(uid,day) DO UPDATE SET n=n+?",
               (uid, today(), n, n))


def start_job(uid, chat, reply_to, cost):
    """Balance kaato + job row banao, ek hi transaction me. Fail ho to None."""
    with db:
        cur = db.execute("UPDATE wallet SET paise=paise-? WHERE uid=? AND paise>=?", (cost, uid, cost))
        if cur.rowcount == 0:
            return None
        db.execute("INSERT INTO ledger(uid,ts,amt,why) VALUES(?,?,?,?)",
                   (uid, int(time.time()), -cost, "episodes"))
        cur = db.execute("INSERT INTO jobs(uid,chat,reply_to,cost,status,ts) VALUES(?,?,?,?,'pending',?)",
                         (uid, chat, reply_to, cost, int(time.time())))
        return cur.lastrowid


def finish_job(job_id, uid, ok, total):
    failed = total - ok
    with db:
        db.execute("UPDATE jobs SET status='done' WHERE id=?", (job_id,))
        add_used(uid, ok)
        if failed > 0:
            _credit(uid, failed * PRICE_PER_EP * 100, f"refund {failed} ep")


def hid_of(path):
    return hashlib.md5(json.dumps(path).encode()).hexdigest()[:10]


def get_view(hid):
    r = db.execute("SELECT path,items,ts FROM views WHERE hid=?", (hid,)).fetchone()
    return {"path": json.loads(r[0]), "items": json.loads(r[1]), "ts": r[2]} if r else None


def fresh(hid):
    v = get_view(hid)
    return bool(v) and time.time() - v["ts"] < VIEW_TTL


# ======================= QUEUE =======================
jobs_q = asyncio.Queue()
pending = []
active_users = set()


async def enqueue(chat, uid, reply_to, fn):
    active_users.add(uid)
    status = await bot.send_message(chat, f"⏳ Queue me aapka number: {len(pending) + 1}", reply_to=reply_to)
    job = {"fn": fn, "status": status, "chat": chat, "uid": uid}
    pending.append(job)
    jobs_q.put_nowait(job)


async def worker_loop():
    while True:
        job = await jobs_q.get()
        pending.remove(job)
        try:
            await job["status"].edit("🔄 Aapka number aa gaya, processing...")
        except Exception:
            pass
        try:
            await job["fn"]()
        except Exception as e:
            log.exception("job failed")
            await bot.send_message(job["chat"], f"Error: {e}")
        finally:
            active_users.discard(job["uid"])
            try:
                await job["status"].delete()
            except Exception:
                pass
        for n, j in enumerate(pending, 1):
            try:
                await j["status"].edit(f"⏳ Queue me aapka number: {n}")
            except Exception:
                pass


# ======================= USERBOT <-> TARGET BOT =======================
incoming = None
target_id = None
g_path, g_msgs = [], []


async def collect(event):
    if incoming is not None:
        await incoming.put(event.message)


async def run_step(action, on_new=None):
    global incoming
    incoming = asyncio.Queue()
    order, byid = [], {}
    try:
        try:
            await action()
        except BotResponseTimeoutError:
            pass
        timeout = FIRST_REPLY_TIMEOUT
        while True:
            try:
                msg = await asyncio.wait_for(incoming.get(), timeout)
            except asyncio.TimeoutError:
                break
            timeout = IDLE_TIMEOUT
            is_new = msg.id not in byid
            if is_new:
                order.append(msg.id)
            byid[msg.id] = msg
            if is_new and on_new:
                await on_new(msg)
    finally:
        incoming = None
    return [byid[i] for i in order]


async def goto(path):
    """Target bot ko path par le jao (zaroorat ho to replay)."""
    global g_path, g_msgs
    if g_path == path:
        return g_msgs
    if g_path and path[:len(g_path)] == g_path:
        start = len(g_path)
    else:
        start, g_msgs = 0, []
    for n in range(start, len(path)):
        step = path[n]
        if step[0] == "t":
            g_msgs = await run_step(lambda: userbot.send_message(TARGET_BOT, step[1]))
        else:
            _, k, i, j, btn_text = step
            m = g_msgs[k]
            ci, cj = i, j
            for ii, row in enumerate(m.buttons or []):
                for jj, b in enumerate(row):
                    if b.text == btn_text:
                        ci, cj = ii, jj
            g_msgs = await run_step(lambda: m.click(ci, cj))
        g_path = path[:n + 1]
    return g_msgs


# ======================= VIEWS (search / buttons) =======================
def make_items(msgs):
    return [{"text": m.text or "",
             "buttons": [[{"t": b.text, "u": b.url or None} for b in row] for row in (m.buttons or [])]}
            for m in msgs]


async def render(chat, hid, uid, reply_to):
    v = get_view(hid)
    for k, it in enumerate(v["items"]):
        if not it["text"] and not it["buttons"]:
            continue
        btns = None
        if it["buttons"]:
            btns = []
            for i, row in enumerate(it["buttons"]):
                r = []
                for j, b in enumerate(row):
                    if b["u"]:
                        r.append(Button.url(b["t"], b["u"]))
                    else:
                        r.append(Button.inline(b["t"], f"b:{hid}:{k}:{i}:{j}:{uid}".encode()))
                btns.append(r)
        sent = await bot.send_message(chat, it["text"] or "…", buttons=btns, reply_to=reply_to)
        with db:
            db.execute("INSERT OR REPLACE INTO menus(key,hid) VALUES(?,?)", (f"{chat}:{sent.id}", hid))


async def open_view(chat, uid, reply_to, path):
    hid = hid_of(path)
    if fresh(hid):
        return await render(chat, hid, uid, reply_to)
    if uid in active_users:
        return await bot.send_message(chat, "⚠️ Aapki pichli request chal rahi hai, uske baad bhejo.",
                                      reply_to=reply_to)

    async def job():
        msgs = await goto(path)
        if not msgs:
            return await bot.send_message(chat, "❌ Kuch nahi mila.", reply_to=reply_to)
        with db:
            db.execute("INSERT OR REPLACE INTO views(hid,path,items,ts) VALUES(?,?,?,?)",
                       (hid, json.dumps(path), json.dumps(make_items(msgs)), int(time.time())))
        await render(chat, hid, uid, reply_to)

    await enqueue(chat, uid, reply_to, job)


# ======================= EPISODES =======================
EPISODE_RE = re.compile(r"^[\d\s,\-–]+$")


def parse_eps(s):
    s = re.sub(r"\s*[-–]\s*", "-", s.strip())
    out = []
    for part in re.split(r"[,\s]+", s):
        m = re.fullmatch(r"(\d+)-(\d+)", part)
        if m:
            a, b = int(m[1]), int(m[2])
            if a <= b:
                out += list(range(a, min(b, a + MAX_EPS) + 1))
        elif part.isdigit():
            out.append(int(part))
    return list(dict.fromkeys(out))


def cached_fids(hid, n):
    r = db.execute("SELECT fids FROM eps WHERE key=?", (f"{hid}:{n}",)).fetchone()
    return json.loads(r[0]) if r else None


async def fetch_episode(chat, hid, n, reply_to):
    fids = []

    async def on_new(msg):
        if msg.media and not getattr(msg.media, "webpage", None):
            p = await userbot.download_media(msg, file="downloads/")
            try:
                sent = await bot.send_file(
                    chat, p, caption=msg.text or "", reply_to=reply_to, voice_note=bool(msg.voice),
                    attributes=msg.document.attributes if msg.document else None)
                fids.append(sent.file.id)
            finally:
                if p and os.path.exists(p):
                    os.remove(p)
        elif msg.text:
            await bot.send_message(chat, msg.text, reply_to=reply_to)

    await run_step(lambda: userbot.send_message(TARGET_BOT, str(n)), on_new)
    if fids:
        with db:
            db.execute("INSERT OR REPLACE INTO eps(key,fids) VALUES(?,?)", (f"{hid}:{n}", json.dumps(fids)))
    return bool(fids)


def make_job(job_id, chat, uid, hid, nums, path, reply_to):
    async def job():
        ok, moved = 0, False
        try:
            for n in nums:
                fids = cached_fids(hid, n)
                if fids:
                    for fid in fids:
                        await bot.send_file(chat, fid, reply_to=reply_to)
                    ok += 1
                else:
                    if not moved:
                        await goto(path)
                        moved = True
                    ok += await fetch_episode(chat, hid, n, reply_to)
        finally:
            finish_job(job_id, uid, ok, len(nums))
        msg = f"✅ Done ({ok}/{len(nums)} mile). Balance: {rs(bal(uid))}"
        if ok < len(nums):
            msg += f"\n↩️ {len(nums) - ok} episode ka paisa wallet me wapas aa gaya."
        await bot.send_message(chat, msg, reply_to=reply_to)
    return job


# ======================= HANDLERS =======================
def allowed(chat):
    return not ALLOWED_CHATS or chat in ALLOWED_CHATS


@bot.on(events.NewMessage(incoming=True, pattern=r"(?i)^/(?:start|help)"))
async def on_start(event):
    if not allowed(event.chat_id):
        return
    await event.reply(
        "🎧 **Audio Bot**\n"
        "• Search: `/s series name`\n"
        "• Button dabao, phir menu ko **reply** karke `360,370-399` likho\n"
        f"• Rate: {rs(PRICE_PER_EP * 100)}/episode, max {MAX_EPS} ek baar me, {DAILY_LIMIT}/din\n"
        "• `/add 100` balance add | `/balance` | `/terms`")


@bot.on(events.NewMessage(incoming=True, pattern=r"(?i)^/terms"))
async def on_terms(event):
    if allowed(event.chat_id):
        await event.reply(TERMS)


@bot.on(events.NewMessage(incoming=True, pattern=r"(?i)^/(?:s|search)(?:@\w+)?\s+(.+)"))
async def on_search(event):
    if not allowed(event.chat_id):
        return
    await open_view(event.chat_id, event.sender_id, event.id, [["t", event.pattern_match.group(1).strip()]])


@bot.on(events.NewMessage(incoming=True, pattern=r"(?i)^/balance"))
async def on_balance(event):
    if not allowed(event.chat_id):
        return
    uid = event.sender_id
    rows = db.execute("SELECT amt,why FROM ledger WHERE uid=? ORDER BY id DESC LIMIT 5", (uid,)).fetchall()
    hist = "\n".join(f"{'+' if a > 0 else '-'}{rs(abs(a))}  {w}" for a, w in rows)
    await event.reply(f"💰 Balance: **{rs(bal(uid))}**\nAaj use: {used_today(uid)}/{DAILY_LIMIT}\n\n"
                      f"{hist or 'Koi transaction nahi.'}")


@bot.on(events.NewMessage(incoming=True, pattern=r"(?i)^/add(?:@\w+)?\s+(\d+)"))
async def on_add(event):
    if not allowed(event.chat_id):
        return
    if not (RZP_KEY_ID and RZP_KEY_SECRET):
        return await event.reply("Payments abhi band hain.")
    rupees = int(event.pattern_match.group(1))
    if not MIN_TOPUP <= rupees <= MAX_TOPUP:
        return await event.reply(f"Amount ₹{MIN_TOPUP} se ₹{MAX_TOPUP} ke beech rakho. Jaise: `/add 100`")
    ref = "top_" + uuid.uuid4().hex[:12]
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post("https://api.razorpay.com/v1/payment_links", auth=(RZP_KEY_ID, RZP_KEY_SECRET), json={
                "amount": rupees * 100, "currency": "INR", "description": f"Wallet top-up ₹{rupees}",
                "reference_id": ref, "expire_by": int(time.time()) + LINK_VALID_MIN * 60,
                "notes": {"ref": ref}})
            r.raise_for_status()
            url = r.json()["short_url"]
    except Exception as e:
        log.exception("link create failed")
        return await event.reply(f"Payment link nahi ban paya: {e}")
    with db:
        db.execute("INSERT INTO orders(ref,uid,chat,amount,reply_to) VALUES(?,?,?,?,?)",
                   (ref, event.sender_id, event.chat_id, rupees * 100, event.id))
    await bot.send_message(
        event.chat_id,
        f"💳 ₹{rupees} add karne ke liye pay karo. Link {LINK_VALID_MIN} min valid hai.\n"
        "Payment ke baad balance apne aap update ho jayega.",
        buttons=[Button.url(f"Pay ₹{rupees}", url)], reply_to=event.id)


@bot.on(events.NewMessage(incoming=True, pattern=r"(?i)^/credit\s+(\d+)\s+(-?\d+)"))
async def on_credit(event):
    if event.sender_id not in ADMIN_IDS:
        return
    uid, rupees = int(event.pattern_match.group(1)), int(event.pattern_match.group(2))
    with db:
        _credit(uid, rupees * 100, "admin")
    await event.reply(f"Done. New balance: {rs(bal(uid))}")


@bot.on(events.NewMessage(incoming=True, pattern=r"(?i)^/stats"))
async def on_stats(event):
    if event.sender_id not in ADMIN_IDS:
        return
    users = db.execute("SELECT COUNT(*), COALESCE(SUM(paise),0) FROM wallet").fetchone()
    eps_n = db.execute("SELECT COUNT(*) FROM eps").fetchone()[0]
    await event.reply(f"Users: {users[0]}\nTotal wallet liability: {rs(users[1])}\n"
                      f"Cached episodes: {eps_n}\nQueue: {len(pending)}")


@bot.on(events.NewMessage(incoming=True))
async def on_episode(event):
    chat, uid = event.chat_id, event.sender_id
    text = (event.text or "").strip()
    if not allowed(chat) or not event.is_reply or not EPISODE_RE.match(text):
        return
    reply = await event.get_reply_message()
    r = db.execute("SELECT hid FROM menus WHERE key=?", (f"{chat}:{reply.id}",)).fetchone() if reply else None
    v = get_view(r[0]) if r else None
    if not v or len(v["path"]) < 2:
        return
    hid, path = r[0], v["path"]
    nums = parse_eps(text)[:MAX_EPS]
    if not nums:
        return await event.reply("Episode numbers galat hain.")
    if uid in active_users:
        return await event.reply("⚠️ Aapki pichli request chal rahi hai, uske baad bhejo.")
    if used_today(uid) + len(nums) > DAILY_LIMIT:
        return await event.reply(f"❌ Daily limit {DAILY_LIMIT} episodes hai. Aaj use: {used_today(uid)}")

    cost = PRICE_PER_EP * 100 * len(nums)
    job_id = start_job(uid, chat, event.id, cost)
    if job_id is None:
        return await event.reply(f"❌ Balance kam hai. Zaroori: {rs(cost)}, aapke paas: {rs(bal(uid))}\n"
                                 "Balance add karo: `/add 100`")
    await bot.send_message(chat, f"💰 {rs(cost)} kate. Balance: {rs(bal(uid))}", reply_to=event.id)
    job = make_job(job_id, chat, uid, hid, nums, path, event.id)
    if all(cached_fids(hid, n) for n in nums):
        active_users.add(uid)
        try:
            await job()
        finally:
            active_users.discard(uid)
    else:
        await enqueue(chat, uid, event.id, job)


@bot.on(events.CallbackQuery(pattern=rb"b:(\w+):(\d+):(\d+):(\d+):(\d+)"))
async def on_click(event):
    hid, k, i, j, owner = (g.decode() for g in event.pattern_match.groups())
    k, i, j, owner = int(k), int(i), int(j), int(owner)
    if event.sender_id != owner:
        return await event.answer("Ye menu aapka nahi hai. /s se apna search karo.", alert=True)
    v = get_view(hid)
    if not v:
        return await event.answer("Menu expire ho gaya, dobara search karo.", alert=True)
    await event.answer("⏳ Processing...")
    path = v["path"] + [["c", k, i, j, v["items"][k]["buttons"][i][j]["t"]]]
    await open_view(event.chat_id, event.sender_id, event.message_id, path)


# ======================= RAZORPAY WEBHOOK =======================
async def rzp_webhook(request):
    raw = await request.read()
    sig = request.headers.get("X-Razorpay-Signature", "")
    expected = hmac.new(RZP_WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    if not RZP_WEBHOOK_SECRET or not hmac.compare_digest(sig, expected):
        return web.Response(status=400, text="bad signature")

    data = json.loads(raw)
    if data.get("event") == "payment_link.paid":
        link = data["payload"]["payment_link"]["entity"]
        pid = data["payload"]["payment"]["entity"]["id"]
        ref = (link.get("notes") or {}).get("ref")
        row = db.execute("SELECT uid,chat,amount,reply_to,paid FROM orders WHERE ref=?", (ref,)).fetchone()
        if row and not row[4] and link.get("amount_paid") == row[2]:
            uid, chat, amount, reply_to, _ = row
            try:
                with db:
                    db.execute("INSERT INTO paid_ids(pid) VALUES(?)", (pid,))      # duplicate = error
                    db.execute("UPDATE orders SET paid=1, payment_id=? WHERE ref=?", (pid, ref))
                    _credit(uid, amount, f"top-up {pid[-6:]}")
                await bot.send_message(chat, f"✅ {rs(amount)} add ho gaye. Balance: {rs(bal(uid))}",
                                       reply_to=reply_to)
            except sqlite3.IntegrityError:
                log.info("duplicate webhook %s", pid)
    return web.Response(text="ok")          # hamesha 200, warna Razorpay retry karta rahega


# ======================= STARTUP =======================
async def recover_jobs():
    """Crash ke waqt adhoore jobs ka paisa wapas karo."""
    rows = db.execute("SELECT id,uid,chat,reply_to,cost FROM jobs WHERE status='pending'").fetchall()
    for jid, uid, chat, reply_to, cost in rows:
        with db:
            db.execute("UPDATE jobs SET status='aborted' WHERE id=?", (jid,))
            _credit(uid, cost, "refund (restart)")
        try:
            await bot.send_message(chat, f"↩️ Bot restart hua, {rs(cost)} wallet me wapas kar diye. "
                                         "Dobara try karo.", reply_to=reply_to)
        except Exception:
            pass


async def main():
    global target_id
    await userbot.start()
    target_id = (await userbot.get_entity(TARGET_BOT)).id
    userbot.add_event_handler(collect, events.NewMessage(incoming=True, from_users=target_id))
    userbot.add_event_handler(collect, events.MessageEdited(incoming=True, from_users=target_id))
    await bot.start(bot_token=BOT_TOKEN)
    await recover_jobs()
    asyncio.create_task(worker_loop())

    app = web.Application()
    app.add_routes([web.post("/rzp-webhook", rzp_webhook)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", WEBHOOK_PORT).start()

    log.info("Running...")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
