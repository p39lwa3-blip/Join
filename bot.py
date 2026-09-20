import os, re, sqlite3, asyncio
from datetime import datetime, timezone
import discord
from discord import app_commands
from discord.ext import commands, tasks

TOKEN = os.getenv('DISCORD_TOKEN','').strip()
DB_PATH = os.getenv('DB_PATH','/data/trade_bot.db' if os.path.isdir('/data') else 'trade_bot.db')

PRODUCTS=['50M','100M']
COIN_PRODUCTS=['50M 有33等','100M 無33等','100M 有33等','不死號（不會被官方掃幣號封）']
BOOST_TIERS=[f'{x}M' for x in [50,100,150,200,300,400,500,600,700,800,900,1000]]
STATUSES=['結單','待付款','待確認付款','待洽談','待排單','待交貨','處理中','待收貨','有爭議','待處理','已取消']
AVAILABILITY=['正常提供','暫停提供','缺貨']
TICKET_RE=re.compile(r'(?<!\d)(\d{1,8})(?!\d)')
# 只把「看起來像工單」的頻道視為新工單，避免一般數字頻道被誤判。
TICKET_NAME_RE=re.compile(r'^ticket-(\d{1,8})$', re.I)
STATUS_TICKET_RE=re.compile(r'^(?:結單|待付款|待確認付款|待洽談|待排單|待交貨|處理中|待收貨|有爭議|待處理|已取消)-?(\d{1,8})(?:-|$)')
CLOSED_MARKERS=('結單','已結單','已關閉','closed','close','archived','archive')
PANEL_MARKER=''

db=sqlite3.connect(DB_PATH,check_same_thread=False)
db.row_factory=sqlite3.Row
lock=asyncio.Lock()

def q(sql,params=(),fetch=False):
    c=db.cursor(); c.execute(sql,params); rows=c.fetchall() if fetch else None; db.commit(); return rows

def now(): return datetime.now(timezone.utc).isoformat()

def key(guild_id,k): return f'g:{guild_id}:{k}'
def get(guild_id,k,default=''):
    r=q('SELECT value FROM settings WHERE key=?',(key(guild_id,k),),True); return r[0]['value'] if r else default
def setv(guild_id,k,v): q('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key(guild_id,k),str(v)))

def choices(vals): return [app_commands.Choice(name=x,value=x) for x in vals]

def init_db():
    q('''CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT,ticket_no TEXT NOT NULL,channel_id TEXT NOT NULL,guild_id TEXT NOT NULL,product TEXT NOT NULL,quantity INTEGER NOT NULL,unit_price INTEGER NOT NULL,total_price INTEGER NOT NULL,status TEXT NOT NULL DEFAULT '待付款',buyer_id TEXT,created_at TEXT NOT NULL,paid_at TEXT,completed_at TEXT,delivery_time TEXT,payment_method TEXT,payment_reminder_sent INTEGER NOT NULL DEFAULT 0)''')
    # 舊版資料庫相容：補上新流程需要的欄位。
    cols={r['name'] for r in q('PRAGMA table_info(orders)',(),True)}
    if 'delivery_time' not in cols: q('ALTER TABLE orders ADD COLUMN delivery_time TEXT')
    if 'payment_method' not in cols: q('ALTER TABLE orders ADD COLUMN payment_method TEXT')
    if 'payment_reminder_sent' not in cols: q('ALTER TABLE orders ADD COLUMN payment_reminder_sent INTEGER NOT NULL DEFAULT 0')
    if 'service_type' not in cols: q("ALTER TABLE orders ADD COLUMN service_type TEXT NOT NULL DEFAULT '幣號'")
    if 'game_account' not in cols: q('ALTER TABLE orders ADD COLUMN game_account TEXT')
    q('''CREATE TABLE IF NOT EXISTS products(product TEXT PRIMARY KEY,price INTEGER NOT NULL DEFAULT 0,stock INTEGER NOT NULL DEFAULT 0,enabled INTEGER NOT NULL DEFAULT 1,availability_status TEXT NOT NULL DEFAULT '正常提供')''')
    pcols={r['name'] for r in q('PRAGMA table_info(products)',(),True)}
    if 'availability_status' not in pcols: q("ALTER TABLE products ADD COLUMN availability_status TEXT NOT NULL DEFAULT '正常提供'")
    q('''CREATE TABLE IF NOT EXISTS boost_prices(guild_id TEXT NOT NULL,tier TEXT NOT NULL,price INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(guild_id,tier))''')
    q('''CREATE TABLE IF NOT EXISTS ticket_channels(channel_id TEXT PRIMARY KEY,guild_id TEXT NOT NULL,ticket_no TEXT NOT NULL,first_name TEXT NOT NULL,buyer_id TEXT,buyer_name TEXT,detected_at TEXT NOT NULL)''')
    q('''CREATE TABLE IF NOT EXISTS balances(user_id TEXT PRIMARY KEY,balance INTEGER NOT NULL DEFAULT 0)''')
    q('''CREATE TABLE IF NOT EXISTS balance_logs(id INTEGER PRIMARY KEY AUTOINCREMENT,guild_id TEXT NOT NULL,user_id TEXT NOT NULL,operator_id TEXT NOT NULL,amount INTEGER NOT NULL,balance_after INTEGER NOT NULL,action TEXT NOT NULL,created_at TEXT NOT NULL,note TEXT)''')
    q('''CREATE TABLE IF NOT EXISTS order_logs(id INTEGER PRIMARY KEY AUTOINCREMENT,order_id INTEGER NOT NULL,guild_id TEXT NOT NULL,operator_id TEXT,action TEXT NOT NULL,detail TEXT,created_at TEXT NOT NULL)''')
    q('''CREATE TABLE IF NOT EXISTS status_posts(order_id INTEGER PRIMARY KEY,channel_id TEXT NOT NULL,message_id TEXT NOT NULL,updated_at TEXT NOT NULL)''')
    q('''CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL DEFAULT '')''')
    for p in PRODUCTS: q('INSERT OR IGNORE INTO products(product,price,stock,enabled) VALUES(?,?,?,1)',(p,0,0))
init_db()
for _p in COIN_PRODUCTS:
    q('INSERT OR IGNORE INTO products(product,price,stock,enabled,availability_status) VALUES(?,?,?,?,?)',(_p,0,0,1,'正常提供'))

intents=discord.Intents.default(); intents.guilds=True; intents.message_content=True
bot=commands.Bot(command_prefix='!',intents=intents)

def admin(m):
    if not isinstance(m,discord.Member): return False
    rid=get(m.guild.id,'admin_role_id','')
    if rid and any(str(r.id)==str(rid) for r in m.roles): return True
    return m.guild_permissions.administrator or any(r.name=='管理員' for r in m.roles)

def ticket_no(name):
    # 新工單只由 ticket-編號 辨識；其他名稱僅作為已記錄工單改名後的相容解析。
    name=name or ''
    m=TICKET_NAME_RE.fullmatch(name.strip())
    if m: return m.group(1)
    m=STATUS_TICKET_RE.fullmatch(name.strip())
    if m: return m.group(1)
    m=re.fullmatch(r'(?i)工單-(\d{1,8})',name.strip())
    if m: return m.group(1)
    # 已關閉/封存的工單也要能保留原工單號，方便管理員再次執行 /改名工單。
    m=re.fullmatch(r'(?i)(?:closed|close|archived|archive)-?(\d{1,8})',name.strip())
    if m: return m.group(1)
    return None

def looks_closed(name):
    n=(name or '').lower()
    return any(marker.lower() in n for marker in CLOSED_MARKERS)

def is_ticket_candidate(ch):
    if not isinstance(ch,discord.TextChannel): return False
    # 只有原始 Ticket Bot 建立的 ticket-0015 這種名稱才算「剛開的工單」。
    # 結單／改名後的頻道一律不會再次觸發。
    return bool(TICKET_NAME_RE.fullmatch((ch.name or '').strip()))

def cn(n):
    if 1<=n<=10: return ['', '一','二','三','四','五','六','七','八','九','十'][n]
    return str(n)

def price(p):
    r=q('SELECT price FROM products WHERE product=? AND enabled=1',(p,),True); return int(r[0]['price']) if r else 0

def stock(p):
    r=q('SELECT stock FROM products WHERE product=?',(p,),True); return int(r[0]['stock']) if r else 0

def availability(p):
    r=q('SELECT availability_status FROM products WHERE product=?',(p,),True); return r[0]['availability_status'] if r else '正常提供'

def boost_price(gid,tier):
    r=q('SELECT price FROM boost_prices WHERE guild_id=? AND tier=?',(str(gid),tier),True); return int(r[0]['price']) if r else 0

def channel_link(guild,name):
    setting='coin_price_channel_id' if name=='幣號價目表' else 'boost_price_channel_id'
    cid=get(guild.id,setting,'')
    ch=guild.get_channel(int(cid)) if cid.isdigit() else None
    if not isinstance(ch,discord.TextChannel): ch=discord.utils.get(guild.text_channels,name=name)
    return ch.mention if ch else f'#{name}'

def off_hours(gid):
    return get(gid,'off_hours','0')=='1'

def ticket_record(ch):
    r=q('SELECT * FROM ticket_channels WHERE channel_id=?',(str(ch.id),),True); return r[0] if r else None

def remember(ch,no,buyer_id=None,buyer_name=None):
    r=ticket_record(ch)
    if r: return r['ticket_no']
    q('INSERT INTO ticket_channels(channel_id,guild_id,ticket_no,first_name,buyer_id,buyer_name,detected_at) VALUES(?,?,?,?,?,?,?)',(str(ch.id),str(ch.guild.id),no,ch.name,str(buyer_id) if buyer_id else None,buyer_name,now()))
    return no

def _is_staff_member(member):
    if not isinstance(member, discord.Member):
        return True
    if member.bot:
        return True
    if member.guild_permissions.administrator:
        return True
    # 以「管理員」名稱及已設定的管理員身分組雙重排除，避免把店長／管理員當成客人。
    admin_role_id = get(member.guild.id, 'admin_role_id', '')
    if admin_role_id and any(str(r.id) == str(admin_role_id) for r in member.roles):
        return True
    if any(r.name == '管理員' for r in member.roles):
        return True
    return False


def _member_can_view_channel(ch, member):
    try:
        return ch.permissions_for(member).view_channel
    except Exception:
        return False


async def detect_buyer(ch):
    """盡可能從「頻道本身」找出客人，而不是只依賴資料庫。

    判斷優先級：
    1. 頻道對「會員本人」的明確權限覆寫。
    2. 頻道實際可見的會員（在快取可用時）。
    3. 具有闆闆👑角色且可看頻道的會員。

    永遠排除 Bot、Administrator、管理員角色及設定的管理員角色。
    """
    candidates = []
    seen = set()

    # 第一層：最可靠——頻道對特定使用者的 Permission Overwrite。
    for target, overwrite in ch.overwrites.items():
        if not isinstance(target, discord.Member) or _is_staff_member(target):
            continue
        # Ticket Bot 通常會直接給客人 view_channel=True。
        # 即使沒有明確寫 True，也用實際權限再確認一次，兼容不同 Ticket Bot 的關閉方式。
        explicit_view = overwrite.view_channel is True
        actual_view = _member_can_view_channel(ch, target)
        if explicit_view or actual_view:
            if target.id not in seen:
                candidates.append(target)
                seen.add(target.id)

    if candidates:
        # 若有多個一般會員覆寫，優先闆闆👑角色；否則取第一個明確客人覆寫。
        candidates.sort(key=lambda m: (not any(r.name == '闆闆👑' for r in m.roles), m.id))
        m = candidates[0]
        return m.id, m.display_name

    # 第二層：discord.py 已有成員快取時，直接看「這個頻道誰看得到」。
    # 不要求闆闆👑角色，因為關閉工單後 Ticket Bot 可能會改角色／權限。
    try:
        for member in getattr(ch, 'members', []):
            if _is_staff_member(member):
                continue
            if _member_can_view_channel(ch, member) and member.id not in seen:
                candidates.append(member)
                seen.add(member.id)
    except Exception:
        pass

    if candidates:
        candidates.sort(key=lambda m: (not any(r.name == '闆闆👑' for r in m.roles), m.id))
        m = candidates[0]
        return m.id, m.display_name

    # 第三層：兼容舊 Ticket Bot——如果頻道仍保留闆闆👑會員權限，就使用它。
    try:
        for target, overwrite in ch.overwrites.items():
            if not isinstance(target, discord.Member) or _is_staff_member(target):
                continue
            if not any(r.name == '闆闆👑' for r in target.roles):
                continue
            if overwrite.view_channel is not False and target.id not in seen:
                return target.id, target.display_name
    except Exception:
        pass

    # 第四層：關閉工單最可靠的備援——直接讀取頻道歷史訊息。
    # 很多 Ticket Bot 關單時會刪掉客人的 Permission Overwrite，因此「頻道裡誰有權限」
    # 會失效；但客人曾經在這張工單發過訊息，Discord 的訊息作者仍然存在。
    # 以最近 200 則訊息找「非 Bot、非管理員、非管理員角色」的真人，
    # 優先取最早出現的非管理人員，避免把店長後續回覆誤判成客人。
    try:
        history_candidates = []
        history_seen = set()
        async for msg in ch.history(limit=200, oldest_first=True):
            author = getattr(msg, 'author', None)
            if not isinstance(author, discord.Member) or _is_staff_member(author):
                continue
            if author.id in history_seen:
                continue
            history_seen.add(author.id)
            history_candidates.append(author)
        if history_candidates:
            # 若其中有人有闆闆👑，優先；否則取最早發言的真人。
            history_candidates.sort(key=lambda m: (not any(r.name == '闆闆👑' for r in m.roles)))
            m = history_candidates[0]
            return m.id, m.display_name
    except Exception:
        pass

    return None, None

async def buyer_for(ch):
    """取得工單客人；已關閉工單仍盡量保留原客人。"""
    r = ticket_record(ch)

    # 1. 工單建立時已保存的 buyer_id 是第一優先。
    if r and r['buyer_id']:
        try:
            bid = int(r['buyer_id'])
        except (TypeError, ValueError):
            bid = None
        if bid:
            member = ch.guild.get_member(bid)
            bname = (member.display_name if member else r['buyer_name']) or '客人'
            if bname != (r['buyer_name'] or ''):
                q('UPDATE ticket_channels SET buyer_name=? WHERE channel_id=?', (bname, str(ch.id)))
            return bid, bname

    # 2. 從該工單最後一筆訂單記錄補回 buyer_id。
    rows = q('SELECT buyer_id FROM orders WHERE channel_id=? AND buyer_id IS NOT NULL ORDER BY id DESC LIMIT 1', (str(ch.id),), True)
    if rows and rows[0]['buyer_id']:
        try:
            bid = int(rows[0]['buyer_id'])
        except (TypeError, ValueError):
            bid = None
        if bid:
            member = ch.guild.get_member(bid)
            bname = member.display_name if member else (r['buyer_name'] if r else None)
            if r:
                q('UPDATE ticket_channels SET buyer_id=?,buyer_name=COALESCE(?,buyer_name) WHERE channel_id=?', (str(bid), bname, str(ch.id)))
            else:
                q('INSERT OR IGNORE INTO ticket_channels(channel_id,guild_id,ticket_no,first_name,buyer_id,buyer_name,detected_at) VALUES(?,?,?,?,?,?,?)', (str(ch.id), str(ch.guild.id), ticket_no(ch.name) or '0', ch.name, str(bid), bname, now()))
            return bid, bname

    # 3. 直接從「目前頻道」找客人。這是關閉工單最重要的備援。
    bid, bname = await detect_buyer(ch)
    if bid:
        if r:
            q('UPDATE ticket_channels SET buyer_id=?,buyer_name=? WHERE channel_id=?', (str(bid), bname, str(ch.id)))
        else:
            no = ticket_no(ch.name)
            if no:
                remember(ch, no, bid, bname)
        return bid, bname

    # 4. 如果曾經保存過名稱，即使現在找不到 Member，也保留原名稱，不回退成「客人」。
    if r and r['buyer_name']:
        return (int(r['buyer_id']) if r['buyer_id'] else None), r['buyer_name']

    return None, None

def rename_name(status,no,product,qty,buyer=None,service_type='幣號'):
    if service_type=='代肝':
        return f'{status}-{no}-{product}代肝'
    if '不死號' in product:
        return f'{status}-{no}-不死號'
    return f'{status}-{no}-{cn(qty)}隻{product}'

async def rename_order_channel(order, guild, reason='訂單狀態自動改名'):
    # 訂單成立／狀態變更時自動同步工單名稱，永遠不加入客人名稱。
    try:
        ch=guild.get_channel(int(order['channel_id']))
    except (TypeError, ValueError):
        return False
    if not isinstance(ch,discord.TextChannel):
        return False
    name=rename_name(order['status'],order['ticket_no'],order['product'],order['quantity'],service_type=order['service_type'] if 'service_type' in order.keys() else '幣號')
    if ch.name==name:
        return True
    try:
        await ch.edit(name=name,reason=reason)
        return True
    except discord.HTTPException as e:
        print('自動改名失敗:',repr(e))
        return False

def payment_info(g):
    vals=[('銀行',get(g,'pay_bank')),('代碼',get(g,'pay_code')),('帳號',get(g,'pay_account')),('戶名',get(g,'pay_name'))]
    s='\n'.join(f'{a}：{b}' for a,b in vals if b)
    return s or '目前尚未設定付款資訊，請聯絡店長。'

def template(g,k,default): return get(g,k,default)

async def post_channel_log(guild, setting_key, text):
    cid=get(guild.id,setting_key)
    if not cid: return
    ch=guild.get_channel(int(cid))
    if not isinstance(ch,discord.TextChannel): return
    try: await ch.send(text)
    except discord.HTTPException: pass

async def status_announce(order, guild):
    """建立或更新工單狀態頻道中的訂單卡。"""
    try:
        cid=get(guild.id,'status_channel_id','').strip()
        if not cid:
            print(f'[STATUS] 尚未設定狀態頻道｜guild={guild.id}｜order={order["id"]}')
            return False
        try:
            ch=guild.get_channel(int(cid))
        except (TypeError, ValueError):
            ch=None
        if ch is None:
            try:
                ch=await guild.fetch_channel(int(cid))
            except Exception as e:
                print(f'[STATUS] 找不到狀態頻道｜guild={guild.id}｜channel={cid}｜error={e!r}')
                return False
        if not isinstance(ch,discord.TextChannel):
            print(f'[STATUS] 設定的頻道不是文字頻道｜guild={guild.id}｜channel={cid}')
            return False

        buyer_name='客人'
        if order['buyer_id']:
            try:
                buyer=guild.get_member(int(order['buyer_id']))
                if buyer:
                    buyer_name=buyer.display_name
            except (TypeError, ValueError):
                pass
        if buyer_name=='客人':
            rec=q('SELECT buyer_name FROM ticket_channels WHERE channel_id=?',(str(order['channel_id']),),True)
            if rec and rec[0]['buyer_name']:
                buyer_name=rec[0]['buyer_name']

        delivery=order['delivery_time'] or get(guild.id,'delivery_time','') or '現貨'
        payment=order['payment_method'] or '尚未選擇'
        custom=template(guild.id,'status_template','')
        desc=''
        if custom:
            try:
                desc=custom.format(ticket=order['ticket_no'],status=order['status'],product=order['product'],quantity=order['quantity'],total=f"{order['total_price']:,}",customer=buyer_name,delivery=delivery,payment_method=payment)
            except Exception as e:
                print(f'[STATUS] 自訂狀態格式錯誤｜order={order["id"]}｜error={e!r}')
        if not desc:
            desc='請依照訂單狀態處理此筆訂單。'

        emb=discord.Embed(title=f'🧾 訂單 #{order["ticket_no"]}',description=desc)
        if order['service_type']=='代肝':
            emb.add_field(name='🛠️ 服務',value='代肝',inline=True)
            emb.add_field(name='📦 額度',value=order['product'],inline=True)
        else:
            emb.add_field(name='📦 商品',value=order['product'],inline=True)
            if not order['product'].startswith('不死號'): emb.add_field(name='🔢 數量',value=f'{order["quantity"]} 隻',inline=True)
        emb.add_field(name='💰 總價',value=f'NT${order["total_price"]:,}',inline=True)
        emb.add_field(name='👤 客人',value=buyer_name or '客人',inline=True)
        emb.add_field(name='📌 狀態',value=order['status'],inline=True)
        emb.add_field(name='💳 付款方式',value=payment,inline=True)
        emb.add_field(name='🕐 交貨時間',value=delivery,inline=False)
        emb.set_footer(text=f'工單 #{order["ticket_no"]}｜訂單 ID {order["id"]}')

        old=q('SELECT message_id,channel_id FROM status_posts WHERE order_id=?',(order['id'],),True)
        try:
            if old:
                try:
                    msg=await ch.fetch_message(int(old[0]['message_id']))
                    await msg.edit(content=None,embed=emb,view=OrderManageView(int(order['id'])))
                except discord.NotFound:
                    msg=await ch.send(embed=emb,view=OrderManageView(int(order['id'])))
                    q('UPDATE status_posts SET channel_id=?,message_id=?,updated_at=? WHERE order_id=?',(str(ch.id),str(msg.id),now(),order['id']))
                else:
                    q('UPDATE status_posts SET channel_id=?,updated_at=? WHERE order_id=?',(str(ch.id),now(),order['id']))
            else:
                msg=await ch.send(embed=emb,view=OrderManageView(int(order['id'])))
                q('INSERT OR REPLACE INTO status_posts(order_id,channel_id,message_id,updated_at) VALUES(?,?,?,?)',(order['id'],str(ch.id),str(msg.id),now()))
            return True
        except discord.Forbidden as e:
            print(f'[STATUS] 沒有權限在狀態頻道發送/編輯訊息｜guild={guild.id}｜channel={ch.id}｜order={order["id"]}｜error={e!r}')
            return False
        except discord.HTTPException as e:
            print(f'[STATUS] Discord API 錯誤｜guild={guild.id}｜channel={ch.id}｜order={order["id"]}｜error={e!r}')
            return False
        except Exception as e:
            print(f'[STATUS] 未預期錯誤｜guild={guild.id}｜channel={ch.id}｜order={order["id"]}｜error={e!r}')
            import traceback; traceback.print_exc()
            return False
    except Exception as e:
        print(f'[STATUS] 狀態公告流程錯誤｜order={order.get("id") if hasattr(order,"get") else "?"}｜error={e!r}')
        import traceback; traceback.print_exc()
        return False

def log_order(order_id,gid,op,action,detail=''): q('INSERT INTO order_logs(order_id,guild_id,operator_id,action,detail,created_at) VALUES(?,?,?,?,?,?)',(str(order_id),str(gid),str(op) if op else None,action,detail,now()))

async def send_panel(ch):
    if not isinstance(ch,discord.TextChannel): return False
    rec=ticket_record(ch); no=rec['ticket_no'] if rec else ticket_no(ch.name)
    if not no: return False
    if not rec: remember(ch,no)
    try:
        async for m in ch.history(limit=100):
            if m.author.id==bot.user.id and '三角洲交易系統' in (m.content or ''): return True
            if m.author.id==bot.user.id and m.embeds and m.embeds[0].title=='🛒 三角洲交易系統': return True
    except discord.HTTPException: return False
    desc='請先選擇您要購買的服務。\n\n🪙 **幣號**\n購買遊戲幣號\n\n🛠️ **代肝**\n選擇代肝額度並付款排單'
    if off_hours(ch.guild.id): desc += '\n\n🕐 **目前為非營業時間**\n目前可以正常下單及付款，但店長目前不在線，付款後會等店長回來再處理。'
    emb=discord.Embed(title='🛒 三角洲交易系統',description=desc)
    emb.set_footer(text=f'工單 #{no}')
    try: await ch.send(content='🛒 三角洲交易系統',embed=emb,view=ServiceTypeView()); return True
    except discord.HTTPException: return False

class ServiceTypeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label='🪙 幣號',style=discord.ButtonStyle.primary,custom_id='ct_service:coin'))
        self.children[0].callback=self.coin
        self.add_item(discord.ui.Button(label='🛠️ 代肝',style=discord.ButtonStyle.primary,custom_id='ct_service:boost'))
        self.children[1].callback=self.boost
    async def coin(self,i): await show_coin_menu(i)
    async def boost(self,i): await show_boost_menu(i)

async def show_coin_menu(i):
    if not isinstance(i.channel,discord.TextChannel): return await i.response.send_message('❌ 請在工單頻道使用。',ephemeral=True)
    desc=f'📋 幣號價目表：{channel_link(i.guild,"幣號價目表")}\n\n請選擇您要的幣號規格。\n🟢 正常提供｜🟡 暫停提供｜🔴 缺貨'
    e=discord.Embed(title='🪙 幣號',description=desc)
    await i.response.edit_message(content=None,embed=e,view=CoinProductView())

async def show_boost_menu(i):
    if not isinstance(i.channel,discord.TextChannel): return await i.response.send_message('❌ 請在工單頻道使用。',ephemeral=True)
    lines=[f'📋 代肝價目表：{channel_link(i.guild,"代肝價目表")}','\n請選擇代肝額度：']
    for t in BOOST_TIERS:
        v=boost_price(i.guild.id,t); lines.append(f'{t}：NT${v:,}' if v>0 else f'{t}：尚未設定')
    e=discord.Embed(title='🛠️ 代肝',description='\n'.join(lines))
    await i.response.edit_message(content=None,embed=e,view=BoostTierView())

class CoinProductView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for p in COIN_PRODUCTS:
            self.add_item(CoinProductButton(p))

class CoinProductButton(discord.ui.Button):
    def __init__(self,p):
        self.p=p
        st=availability(p) if p in [r['product'] for r in q('SELECT product FROM products',(),True)] else '正常提供'
        icon={'正常提供':'🟢','暫停提供':'🟡','缺貨':'🔴'}.get(st,'🟢')
        super().__init__(label=f'{icon} {p}',style=discord.ButtonStyle.primary,custom_id=f'ct_coin:{p}')
    async def callback(self,i):
        st=availability(self.p)
        if st=='暫停提供': return await i.response.send_message('⚠️ **目前暫停提供**\n此規格目前暫時不提供，請選擇其他規格。',ephemeral=True)
        if st=='缺貨': return await i.response.send_message('🔴 **目前缺貨**\n此規格目前沒有現貨，請選擇其他規格。',ephemeral=True)
        if '不死號' in self.p:
            await create_negotiation_order(i,self.p)
            return
        await i.response.send_modal(QuantityModal(self.p,i.message.id))

class BoostTierView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for t in BOOST_TIERS:
            b=discord.ui.Button(label=t,style=discord.ButtonStyle.primary,custom_id=f'ct_boost:{t}')
            b.callback=self.make_cb(t); self.add_item(b)
    def make_cb(self,tier):
        async def cb(i):
            await create_boost_order(i,tier)
        return cb

class QuantityModal(discord.ui.Modal,title='輸入購買數量'):
    qty=discord.ui.TextInput(label='購買數量',placeholder='請輸入數量，例如：7',min_length=1,max_length=6,required=True)
    def __init__(self,p,source_message_id):
        super().__init__(); self.p=p; self.source_message_id=int(source_message_id)
    async def on_submit(self,i):
        try: n=int(str(self.qty.value).strip())
        except (TypeError,ValueError): return await i.response.send_message('❌ 數量請輸入整數，例如：7。',ephemeral=True)
        if not 1<=n<=999999: return await i.response.send_message('❌ 數量請輸入 1～999999 的整數。',ephemeral=True)
        await i.response.defer(); await process_selection(i,self.p,n,source_message_id=self.source_message_id)

def quantity_embed(p,n,gid):
    unit=price(p); total=unit*n if unit>0 else 0
    desc=f'商品：**{p}**\n數量：**{n} 隻**'
    if unit>0: desc += f'\n單價：NT${unit:,}\n總價：**NT${total:,}**'
    else: desc += '\n⚠️ 此商品目前尚未設定價格。'
    return discord.Embed(title='📦 訂單資訊',description=desc)

async def edit_source_message(i,source_message_id,*,content=None,embed=None,view=None):
    if source_message_id is None: return await i.edit_original_response(content=content,embed=embed,view=view)
    ch=i.channel
    if not isinstance(ch,discord.TextChannel): return False
    try:
        msg=ch.get_partial_message(int(source_message_id)); await msg.edit(content=content,embed=embed,view=view); return True
    except (discord.NotFound,discord.Forbidden,discord.HTTPException) as e: print('edit_source_message error:',repr(e)); return False

async def process_selection(i,p,n,source_message_id=None):
    ch=i.channel
    if not isinstance(ch,discord.TextChannel): return await i.followup.send('❌ 請在工單頻道操作。',ephemeral=True) if i.response.is_done() else await i.response.send_message('❌ 請在工單頻道操作。',ephemeral=True)
    rec=ticket_record(ch); no=rec['ticket_no'] if rec else ticket_no(ch.name)
    if not no: return await i.followup.send('❌ 暫時無法辨識工單編號，請稍後再試。',ephemeral=True)
    if availability(p)!='正常提供': return await i.followup.send('❌ 此規格目前無法購買。',ephemeral=True)
    unit=price(p)
    if unit<=0: return await i.followup.send('❌ 這項商品目前尚未設定價格，請聯絡店長。',ephemeral=True)
    s=stock(p)
    if s<n:
        delivery=get(ch.guild.id,'delivery_time','').strip()
        if not delivery: return await i.followup.send(f'❌ 目前 {p} 庫存不足，店長尚未設定交貨時間。',ephemeral=True)
        e=discord.Embed(title='📦 缺貨訂單確認',description=f'目前僅剩 {s} 隻現貨，您需要 {n} 隻。\n\n🕐 本店交貨時間：**{delivery}**\n\n請確認是否接受。')
        e.add_field(name='商品',value=p); e.add_field(name='數量',value=f'{n} 隻'); e.add_field(name='總價',value=f'NT${unit*n:,}')
        return await edit_source_message(i,source_message_id,embed=e,view=DeliveryAcceptView(p,n,unit,unit*n,delivery))
    return await create_order_and_show_payment(i,p,n,unit,unit*n,'',source_message_id=source_message_id,already_deferred=True)

async def create_order_and_show_payment(i,p,qty,unit,total,delivery,source_message_id=None,already_deferred=False,service_type='幣號'):
    if not already_deferred:
        try: await i.response.defer()
        except discord.InteractionResponded: pass
    ch=i.channel
    if not isinstance(ch,discord.TextChannel): return await i.edit_original_response(content='❌ 請在工單頻道操作。',embed=None,view=None)
    rec=ticket_record(ch); no=rec['ticket_no'] if rec else ticket_no(ch.name)
    if not no: return await i.edit_original_response(content='❌ 暫時無法辨識工單編號，請稍後再試。',embed=None,view=None)
    existing=q("SELECT * FROM orders WHERE channel_id=? AND buyer_id=? AND status NOT IN ('結單','已取消') ORDER BY id DESC LIMIT 1",(str(ch.id),str(i.user.id)),True)
    if existing: return await i.edit_original_response(content=f'ℹ️ 這張工單已有進行中的訂單 **#{existing[0]["ticket_no"]}**，目前狀態：**{existing[0]["status"]}**。',embed=None,view=None)
    if service_type=='幣號' and availability(p)!='正常提供': return await i.edit_original_response(content='❌ 此規格目前無法購買。',embed=None,view=None)
    with db:
        c=db.cursor(); c.execute('INSERT INTO orders(ticket_no,channel_id,guild_id,product,quantity,unit_price,total_price,status,buyer_id,created_at,delivery_time,service_type) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(no,str(ch.id),str(ch.guild.id),p,qty,unit,total,'待付款',str(i.user.id),now(),delivery,service_type)); oid=c.lastrowid; db.commit()
    order=q('SELECT * FROM orders WHERE id=?',(oid,),True)[0]
    await rename_order_channel(order,ch.guild,'建立訂單後自動改名')
    await post_channel_log(ch.guild,'order_log_channel_id',f'🧾 **新{service_type}訂單**｜#{no}｜{p}｜NT${total:,}｜客人：{i.user.mention}')
    log_order(oid,ch.guild.id,i.user.id,'建立訂單',f'{service_type}｜{p}｜NT${total:,}')
    await status_announce(order,ch.guild)
    e=discord.Embed(title='💳 付款方式',description=template(ch.guild.id,'pay_method_intro','本店付款方式為 {bank}\n\n請問您要使用的付款方式是？').format(bank=get(ch.guild.id,'pay_bank','尚未設定')))
    e.add_field(name='訂單',value=f'#{no}｜{p}｜NT${total:,}',inline=False)
    if off_hours(ch.guild.id): e.add_field(name='🕐 非營業時間',value='目前可以付款，但店長目前不在線，付款後會等店長回來確認。',inline=False)
    if source_message_id is not None:
        await edit_source_message(i,source_message_id,embed=e,view=PaymentMethodView(oid)); return
    await i.edit_original_response(embed=e,view=PaymentMethodView(oid))

async def create_boost_order(i,tier):
    price_v=boost_price(i.guild.id,tier)
    if price_v<=0: return await i.response.send_message('❌ 此代肝額度目前尚未設定價格，請聯絡店長。',ephemeral=True)
    await i.response.defer()
    return await create_order_and_show_payment(i,tier,1,price_v,price_v,'',already_deferred=True,service_type='代肝')

async def create_negotiation_order(i,p):
    await i.response.defer()
    ch=i.channel; no=ticket_no(ch.name)
    existing=q("SELECT * FROM orders WHERE channel_id=? AND buyer_id=? AND status NOT IN ('結單','已取消') ORDER BY id DESC LIMIT 1",(str(ch.id),str(i.user.id)),True)
    if existing: return await i.followup.send(f'ℹ️ 此工單已有進行中的訂單 #{existing[0]["ticket_no"]}。',ephemeral=True)
    c=db.cursor(); c.execute('INSERT INTO orders(ticket_no,channel_id,guild_id,product,quantity,unit_price,total_price,status,buyer_id,created_at,service_type) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(no,str(ch.id),str(ch.guild.id),p,1,0,0,'待洽談',str(i.user.id),now(),'幣號')); oid=c.lastrowid; db.commit()
    order=q('SELECT * FROM orders WHERE id=?',(oid,),True)[0]
    await rename_order_channel(order,ch.guild,'不死號洽談')
    await ch.send('📋 **不死號**\n已通知店長，請稍候，店長會與您洽談。')
    await post_channel_log(ch.guild,'order_log_channel_id',f'📋 **不死號洽談通知**\n工單：{ch.mention}\n客人：{i.user.mention}\n客人選擇了不死號，請前往工單洽談。')
    await status_announce(order,ch.guild)
    await i.edit_original_response(content='📋 已通知店長，請稍候，店長會與您洽談。',embed=None,view=None)

class DeliveryAcceptView(discord.ui.View):
    def __init__(self,p,qty,unit,total,delivery):
        super().__init__(timeout=300); self.p=p; self.qty=qty; self.unit=unit; self.total=total; self.delivery=delivery
    @discord.ui.button(label='✅ 可以，接受時間',style=discord.ButtonStyle.success)
    async def accept(self,i,button):
        if price(self.p)<=0: return await i.response.send_message('❌ 商品價格已變更，請重新選購。',ephemeral=True)
        current_delivery=get(i.guild.id,'delivery_time','').strip()
        if not current_delivery:
            return await i.response.send_message('❌ 店長目前尚未設定統一交貨時間，請稍後再試。',ephemeral=True)
        await create_order_and_show_payment(i,self.p,self.qty,price(self.p),price(self.p)*self.qty,current_delivery)
        button.disabled=True
    @discord.ui.button(label='❌ 無法接受，取消',style=discord.ButtonStyle.danger)
    async def reject(self,i,button):
        await i.response.edit_message(content=template(i.guild.id,'cancel_text','❌ 已取消本次購買。'),embed=None,view=None)

class OrderManageView(discord.ui.View):
    def __init__(self,oid):
        super().__init__(timeout=None); self.oid=oid; self.build()
    def build(self):
        r=q('SELECT * FROM orders WHERE id=?',(self.oid,),True); status=r[0]['status'] if r else ''
        if status=='待確認付款':
            b=discord.ui.Button(label='💰 確認付款',style=discord.ButtonStyle.success,custom_id=f'ct_confirm:{self.oid}',row=0); b.callback=self.confirm_payment; self.add_item(b)
        elif status=='待交貨':
            b=discord.ui.Button(label='🛠️ 開始處理',style=discord.ButtonStyle.primary,custom_id=f'ct_process:{self.oid}',row=0); b.callback=self.start; self.add_item(b)
        elif status=='處理中':
            b=discord.ui.Button(label='📦 完成交貨',style=discord.ButtonStyle.primary,custom_id=f'ct_deliver:{self.oid}',row=0); b.callback=self.deliver; self.add_item(b)
        elif status=='待收貨':
            b=discord.ui.Button(label='✅ 完成訂單',style=discord.ButtonStyle.success,custom_id=f'ct_finish:{self.oid}',row=0); b.callback=self.finish; self.add_item(b)
        r2=q('SELECT channel_id FROM orders WHERE id=?',(self.oid,),True); url=None
        if r2:
            ch=bot.get_channel(int(r2[0]['channel_id']))
            if isinstance(ch,discord.TextChannel): url=ch.jump_url
        if url: self.add_item(discord.ui.Button(label='🎫 前往工單',style=discord.ButtonStyle.link,url=url,row=0))
    async def check(self,i): return admin(i.user)
    async def confirm_payment(self,i):
        if not await self.check(i): return await deny(i)
        r=q('SELECT * FROM orders WHERE id=?',(self.oid,),True)
        if not r: return await i.response.send_message('❌ 找不到訂單。',ephemeral=True)
        o=r[0]
        if o['status']!='待確認付款': return await i.response.send_message(f'ℹ️ 目前狀態為「{o["status"]}」。',ephemeral=True)
        if not await confirm_dialog(i,'確認這筆付款已收到嗎？','確認付款',self.oid): return
    async def _confirmed(self,i):
        r=q('SELECT * FROM orders WHERE id=?',(self.oid,),True)
        if not r: return
        o=r[0]; new='待排單' if o['service_type']=='代肝' else '待交貨'
        q('UPDATE orders SET status=? WHERE id=?',(new,self.oid)); log_order(self.oid,i.guild.id,i.user.id,'確認付款',f'{o["status"]} → {new}')
        ch=i.guild.get_channel(int(o['channel_id']))
        if isinstance(ch,discord.TextChannel):
            await ch.send('🎮 **請提供遊戲帳號**\n付款已確認，請直接在此工單提供您的遊戲帳號。\n📢 已通知店長，收到帳號後將安排排單。') if o['service_type']=='代肝' else None
        await rename_order_channel(q('SELECT * FROM orders WHERE id=?',(self.oid,),True)[0],i.guild,'店長確認付款後自動更新')
        await post_channel_log(i.guild,'action_log_channel_id',f'💰 **確認付款**｜#{o["ticket_no"]}｜{i.user.mention}')
        rr=q('SELECT * FROM orders WHERE id=?',(self.oid,),True)[0]; await status_announce(rr,i.guild)
        await i.response.send_message(f'✅ 訂單 #{o["ticket_no"]} 已確認付款。',ephemeral=True)
    async def start(self,i): await self.change(i,'處理中','開始處理')
    async def deliver(self,i): await self.change(i,'待收貨','完成交貨')
    async def change(self,i,new_status,action):
        if not await self.check(i): return await deny(i)
        r=q('SELECT * FROM orders WHERE id=?',(self.oid,),True)
        if not r: return await i.response.send_message('❌ 找不到訂單。',ephemeral=True)
        o=r[0]; q('UPDATE orders SET status=? WHERE id=?',(new_status,self.oid)); log_order(self.oid,i.guild.id,i.user.id,action,f'{o["status"]} → {new_status}')
        await rename_order_channel(q('SELECT * FROM orders WHERE id=?',(self.oid,),True)[0],i.guild,action)
        rr=q('SELECT * FROM orders WHERE id=?',(self.oid,),True)[0]; await status_announce(rr,i.guild); await i.response.send_message(f'✅ 訂單 #{o["ticket_no"]} 已更新為 **{new_status}**。',ephemeral=True)
    async def finish(self,i):
        if not await self.check(i): return await deny(i)
        r=q('SELECT * FROM orders WHERE id=?',(self.oid,),True)
        if not r: return await i.response.send_message('❌ 找不到訂單。',ephemeral=True)
        o=r[0]
        if o['status']!='待收貨': return await i.response.send_message(f'ℹ️ 目前狀態為「{o["status"]}」，無法直接完成訂單。',ephemeral=True)
        q('UPDATE orders SET status=?,completed_at=? WHERE id=?',('結單',now(),self.oid)); log_order(self.oid,i.guild.id,i.user.id,'完成訂單','管理員完成訂單')
        await rename_order_channel(q('SELECT * FROM orders WHERE id=?',(self.oid,),True)[0],i.guild,'完成訂單')
        rr=q('SELECT * FROM orders WHERE id=?',(self.oid,),True)[0]; await status_announce(rr,i.guild); await i.response.send_message(f'✅ 訂單 #{o["ticket_no"]} 已完成並結單。',ephemeral=True)

class ConfirmView(discord.ui.View):
    def __init__(self,oid): super().__init__(timeout=60); self.oid=oid
    @discord.ui.button(label='✅ 確認',style=discord.ButtonStyle.success)
    async def yes(self,i,b):
        if not admin(i.user): return await deny(i)
        r=q('SELECT * FROM orders WHERE id=?',(self.oid,),True)
        if not r: return await i.response.send_message('❌ 找不到訂單。',ephemeral=True)
        o=r[0]
        if o['status']!='待確認付款': return await i.response.send_message('ℹ️ 這筆訂單已不是待確認付款。',ephemeral=True)
        new='待排單' if o['service_type']=='代肝' else '待交貨'
        q('UPDATE orders SET status=? WHERE id=?',(new,self.oid)); log_order(self.oid,i.guild.id,i.user.id,'確認付款',f'{o["status"]} → {new}')
        ch=i.guild.get_channel(int(o['channel_id']))
        if isinstance(ch,discord.TextChannel) and o['service_type']=='代肝':
            await ch.send('🎮 **請提供遊戲帳號**\n付款已確認，請直接在此工單提供您的遊戲帳號。\n📢 已通知店長，收到帳號後將安排排單。')
        rr=q('SELECT * FROM orders WHERE id=?',(self.oid,),True)[0]; await rename_order_channel(rr,i.guild,'店長確認付款後自動更新'); await status_announce(rr,i.guild)
        await i.response.edit_message(content=f'✅ 已確認付款｜#{o["ticket_no"]}',view=None)
    @discord.ui.button(label='取消',style=discord.ButtonStyle.secondary)
    async def no(self,i,b): await i.response.edit_message(content='已取消確認操作。',view=None)

async def confirm_dialog(i,text,title,oid):
    if not admin(i.user): return await deny(i)
    await i.response.send_message(f'💰 **{title}**\n{text}',view=ConfirmView(oid),ephemeral=True); return True

class PaymentMethodView(discord.ui.View):
    def __init__(self,oid):
        super().__init__(timeout=None); self.oid=oid
        # 每張訂單使用獨立 custom_id，避免多張付款面板互相吃到別張訂單的 callback。
        b=discord.ui.Button(label='🏪 無卡存款（帶紙鈔至 7-11）',style=discord.ButtonStyle.primary,custom_id=f'ct_pay_nocard:{oid}',row=0)
        b.callback=self.nocard; self.add_item(b)
        b2=discord.ui.Button(label='🏦 匯款（轉帳）',style=discord.ButtonStyle.primary,custom_id=f'ct_pay_transfer:{oid}',row=0)
        b2.callback=self.transfer; self.add_item(b2)
    async def choose(self,i,method):
        # 先確認 Interaction，再進行資料庫、狀態頻道與付款頁更新，避免超過 Discord 3 秒限制。
        try:
            await i.response.defer()
        except discord.InteractionResponded:
            pass
        r=q('SELECT * FROM orders WHERE id=?',(self.oid,),True)
        if not r or str(r[0]['buyer_id'])!=str(i.user.id): return await i.followup.send('❌ 這不是您的訂單，無法操作。',ephemeral=True)
        o=r[0]
        if o['status']!='待付款': return await i.followup.send('ℹ️ 這筆訂單目前無法選擇付款方式。',ephemeral=True)
        q('UPDATE orders SET payment_method=? WHERE id=?',(method,self.oid)); log_order(self.oid,o['guild_id'],i.user.id,'選擇付款方式',method)
        if method=='無卡存款':
            text=template(i.guild.id,'no_card_text','🏪 **無卡存款**\n請帶紙鈔至 7-11，依照店家提供的存款方式完成付款。\n\n{bank_info}').format(bank_info=payment_info(i.guild.id))
        else:
            text=template(i.guild.id,'transfer_text','🏦 **匯款（轉帳）**\n請依下方資訊完成轉帳：\n\n{bank_info}').format(bank_info=payment_info(i.guild.id))
        e=discord.Embed(title=f'💳 {method}',description=text)
        e.set_footer(text=template(i.guild.id,'payment_selected_text','完成付款後，請按下「💳 完成付款」通知店家。'))
        rr=q('SELECT * FROM orders WHERE id=?',(self.oid,),True)[0]
        await status_announce(rr,i.guild)
        await i.edit_original_response(embed=e,view=PaymentView(self.oid))
    async def nocard(self,i): await self.choose(i,'無卡存款')
    async def transfer(self,i): await self.choose(i,'匯款（轉帳）')

class PaymentView(discord.ui.View):
    def __init__(self,oid):
        super().__init__(timeout=None); self.oid=oid
        b=discord.ui.Button(label='💳 完成付款',style=discord.ButtonStyle.success,custom_id=f'ct_paid:{oid}'); b.callback=self.paid; self.add_item(b)
        b2=discord.ui.Button(label='❌ 取消訂單',style=discord.ButtonStyle.danger,custom_id=f'ct_cancel:{oid}'); b2.callback=self.cancel; self.add_item(b2)
    async def paid(self,i):
        try: await i.response.defer()
        except discord.InteractionResponded: pass
        r=q('SELECT * FROM orders WHERE id=?',(self.oid,),True)
        if not r or str(r[0]['buyer_id'])!=str(i.user.id): return await i.followup.send('❌ 這不是您的訂單，無法操作。',ephemeral=True)
        o=r[0]
        if o['status']!='待付款': return await i.followup.send('ℹ️ 這筆訂單已經提交過付款通知，請勿重複操作。',ephemeral=True)
        q('UPDATE orders SET status=?,paid_at=? WHERE id=?',('待確認付款',now(),self.oid)); log_order(self.oid,o['guild_id'],i.user.id,'完成付款',f'付款方式：{o["payment_method"] or "未選擇"}')
        guild=i.guild; updated=q('SELECT * FROM orders WHERE id=?',(self.oid,),True)[0]
        await rename_order_channel(updated,guild,'客人完成付款後自動更新工單名稱')
        await post_channel_log(guild,'order_log_channel_id',f'💳 **付款通知**｜#{o["ticket_no"]}｜NT${o["total_price"]:,}｜方式：{o["payment_method"] or "未選擇"}｜客人：{i.user.mention}')
        if off_hours(guild.id): msg='💰 **付款已送出**\n已收到您的付款通知。\n目前為非營業時間，店長尚未在線。\n📌 已記錄您的訂單，店長上線後會進行確認。'
        else: msg='💰 **已通知店長確認付款**\n已收到您的付款通知。\n📌 請等待店長確認付款。'
        await i.edit_original_response(content=msg,embed=None,view=None)
        rr=q('SELECT * FROM orders WHERE id=?',(self.oid,),True)[0]; await status_announce(rr,guild)
    async def cancel(self,i):
        try: await i.response.defer()
        except discord.InteractionResponded: pass
        r=q('SELECT * FROM orders WHERE id=?',(self.oid,),True)
        if not r or str(r[0]['buyer_id'])!=str(i.user.id): return await i.followup.send('❌ 這不是您的訂單，無法操作。',ephemeral=True)
        if r[0]['status']!='待付款': return await i.followup.send('❌ 這筆訂單已進入付款流程，目前無法取消。',ephemeral=True)
        q('UPDATE orders SET status=? WHERE id=?',('已取消',self.oid)); updated=q('SELECT * FROM orders WHERE id=?',(self.oid,),True)[0]; await rename_order_channel(updated,i.guild,'客人取消訂單'); await i.edit_original_response(content='❌ 訂單已取消。',embed=None,view=None); await status_announce(updated,i.guild)

@bot.tree.command(name='設定代肝價格',description='設定代肝額度價格')
@app_commands.describe(額度='例如 300M',價格='價格（NT）')
@app_commands.choices(額度=choices(BOOST_TIERS))
async def set_boost_price(i,額度:str,價格:int):
    if not admin(i.user): return await deny(i)
    if 價格<0: return await i.response.send_message('❌ 價格不能小於 0。',ephemeral=True)
    q('INSERT INTO boost_prices(guild_id,tier,price) VALUES(?,?,?) ON CONFLICT(guild_id,tier) DO UPDATE SET price=excluded.price',(str(i.guild.id),額度,價格)); await i.response.send_message(f'✅ {額度} 代肝價格已設定為 NT${價格:,}。',ephemeral=True)

@bot.tree.command(name='代肝價格表',description='查看代肝價格')
async def boost_prices(i):
    if not admin(i.user): return await deny(i)
    await i.response.send_message('🛠️ **代肝價格**\n'+'\n'.join(f'{t}：NT${boost_price(i.guild.id,t):,}' if boost_price(i.guild.id,t)>0 else f'{t}：尚未設定' for t in BOOST_TIERS),ephemeral=True)

@bot.tree.command(name='設定幣號狀態',description='設定幣號規格是否正常提供')
@app_commands.describe(商品='幣號規格',狀態='正常提供／暫停提供／缺貨')
@app_commands.choices(商品=choices(COIN_PRODUCTS),狀態=choices(AVAILABILITY))
async def set_coin_status(i,商品:str,狀態:str):
    if not admin(i.user): return await deny(i)
    q('INSERT OR IGNORE INTO products(product,price,stock,enabled,availability_status) VALUES(?,?,?,?,?)',(商品,0,0,1,狀態)); q('UPDATE products SET availability_status=? WHERE product=?',(狀態,商品)); await i.response.send_message(f'✅ {商品} 狀態已設定為 **{狀態}**。',ephemeral=True)

@bot.tree.command(name='設定價目表頻道',description='設定幣號與代肝價目表頻道')
@app_commands.describe(類型='價目表類型',頻道='頻道')
@app_commands.choices(類型=choices(['幣號價目表','代肝價目表']))
async def set_price_channel(i,類型:str,頻道:discord.TextChannel):
    if not admin(i.user): return await deny(i)
    setv(i.guild.id,'coin_price_channel_id' if 類型=='幣號價目表' else 'boost_price_channel_id',頻道.id); await i.response.send_message(f'✅ {類型}已設定為 {頻道.mention}',ephemeral=True)

@bot.tree.command(name='設定非營業時間',description='開啟非營業時間模式')
async def set_off_hours(i):
    if not admin(i.user): return await deny(i)
    setv(i.guild.id,'off_hours','1'); await i.response.send_message('🕐 已開啟非營業時間。客人仍可正常下單及付款，付款後等待店長回來確認。',ephemeral=True)

@bot.tree.command(name='關閉非營業時間',description='關閉非營業時間模式')
async def close_off_hours(i):
    if not admin(i.user): return await deny(i)
    setv(i.guild.id,'off_hours','0'); await i.response.send_message('🟢 已關閉非營業時間模式。',ephemeral=True)

# ---- 管理指令 ----
async def deny(i): return await i.response.send_message('❌ 只有 @管理員 可以使用這個指令。',ephemeral=True)

@bot.tree.command(name='價格表',description='查看目前商品價格')
async def prices(i):
    if not admin(i.user): return await deny(i)
    rows=q('SELECT product,price FROM products ORDER BY CASE product WHEN "50M" THEN 1 WHEN "100M" THEN 2 ELSE 3 END',(),True)
    await i.response.send_message('💰 **目前價格**\n'+'\n'.join(f'{r["product"]}：NT${r["price"]:,}' for r in rows),ephemeral=True)

@bot.tree.command(name='設定價格',description='設定商品單價')
@app_commands.describe(商品='商品',價格='每隻單價（NT）')
@app_commands.choices(商品=choices(COIN_PRODUCTS))
async def set_price(i,商品:str,價格:int):
    if not admin(i.user): return await deny(i)
    if 價格<0: return await i.response.send_message('❌ 價格不能小於 0。',ephemeral=True)
    q('UPDATE products SET price=? WHERE product=?',(價格,商品)); await i.response.send_message(f'✅ {商品} 單價已設定為 NT${價格:,}。',ephemeral=True)

@bot.tree.command(name='設定庫存',description='設定目前現貨數量')
@app_commands.describe(商品='商品',數量='目前現貨隻數')
@app_commands.choices(商品=choices(COIN_PRODUCTS))
async def set_stock(i,商品:str,數量:int):
    if not admin(i.user): return await deny(i)
    if 數量<0: return await i.response.send_message('❌ 庫存不能小於 0。',ephemeral=True)
    q('UPDATE products SET stock=? WHERE product=?',(數量,商品)); await i.response.send_message(f'📦 {商品} 現貨已設定為 **{數量} 隻**。',ephemeral=True)

@bot.tree.command(name='庫存',description='查看目前現貨')
async def stocks(i):
    if not admin(i.user): return await deny(i)
    rows=q('SELECT product,stock FROM products ORDER BY CASE product WHEN "50M" THEN 1 WHEN "100M" THEN 2 ELSE 3 END',(),True)
    await i.response.send_message('📦 **目前現貨**\n'+'\n'.join(f'{r["product"]}：{r["stock"]} 隻' for r in rows),ephemeral=True)

@bot.tree.command(name='設定補貨',description='設定商品下一次補貨時間')
@app_commands.describe(商品='商品',時間='例如：9/6 20:00')
@app_commands.choices(商品=choices(COIN_PRODUCTS))
async def set_restock(i,商品:str,時間:str):
    if not admin(i.user): return await deny(i)
    setv(i.guild.id,f'restock_{商品}',時間); await i.response.send_message(f'📦 {商品} 下一次補貨時間已設定：**{時間}**',ephemeral=True)

@bot.tree.command(name='店長狀態',description='設定店長營業或休息')
@app_commands.describe(狀態='營業中／休息中')
@app_commands.choices(狀態=choices(['營業中','休息中']))
async def shop_status(i,狀態:str):
    if not admin(i.user): return await deny(i)
    setv(i.guild.id,'shop_status',狀態); await i.response.send_message(f'✅ 店長狀態：**{狀態}**。新工單會自動顯示。',ephemeral=True)

@bot.tree.command(name='暫停接單',description='暫停客人下單')
async def pause(i):
    if not admin(i.user): return await deny(i)
    setv(i.guild.id,'shop_status','暫停接單'); await i.response.send_message('⏸️ 已暫停接單。客人仍可開單，但無法建立付款訂單。',ephemeral=True)

@bot.tree.command(name='恢復接單',description='恢復客人下單')
async def resume(i):
    if not admin(i.user): return await deny(i)
    setv(i.guild.id,'shop_status','營業中'); await i.response.send_message('▶️ 已恢復接單。',ephemeral=True)

@bot.tree.command(name='付款資訊',description='查看目前付款資訊')
async def payinfo(i):
    if not admin(i.user): return await deny(i)
    await i.response.send_message(f'💳 **目前付款資訊**\n{payment_info(i.guild.id)}',ephemeral=True)

@bot.tree.command(name='設定付款',description='設定付款資訊')
@app_commands.describe(銀行='銀行名稱',代碼='銀行代碼',帳號='收款帳號',戶名='戶名')
async def setpay(i,銀行:str='',代碼:str='',帳號:str='',戶名:str=''):
    if not admin(i.user): return await deny(i)
    for k,v in [('pay_bank',銀行),('pay_code',代碼),('pay_account',帳號),('pay_name',戶名)]: setv(i.guild.id,k,v)
    await i.response.send_message('✅ 付款資訊已更新。',ephemeral=True)

@bot.tree.command(name='設定交貨時間',description='設定本店今日統一交貨時間')
@app_commands.describe(時間='例如：9/6 21:30')
async def set_delivery(i,時間:str):
    if not admin(i.user): return await deny(i)
    setv(i.guild.id,'delivery_time',時間); await i.response.send_message(f'🚚 本店今日統一交貨時間已設定為：**{時間}**',ephemeral=True)

@bot.tree.command(name='設定付款提醒',description='設定待付款訂單多久後提醒一次')
@app_commands.describe(分鐘='例如 15，填 0 可關閉自動提醒')
async def set_payment_reminder(i,分鐘:int):
    if not admin(i.user): return await deny(i)
    if 分鐘<0 or 分鐘>10080: return await i.response.send_message('❌ 分鐘請輸入 0～10080。',ephemeral=True)
    setv(i.guild.id,'payment_reminder_minutes',分鐘)
    await i.response.send_message('🔔 已關閉付款提醒。' if 分鐘==0 else f'🔔 已設定待付款 **{分鐘} 分鐘**後提醒一次。',ephemeral=True)

@bot.tree.command(name='設定付款方式',description='設定付款方式頁面與各付款方式說明')
@app_commands.describe(類型='要修改的付款訊息',內容='新的訊息內容')
@app_commands.choices(類型=choices(['付款方式選擇','無卡存款說明','匯款說明','選擇付款後提示','完成付款提示']))
async def set_payment_method(i,類型:str,內容:str):
    if not admin(i.user): return await deny(i)
    mp={'付款方式選擇':'pay_method_intro','無卡存款說明':'no_card_text','匯款說明':'transfer_text','選擇付款後提示':'payment_selected_text','完成付款提示':'paid_text'}
    setv(i.guild.id,mp[類型],內容); await i.response.send_message(f'✅「{類型}」已更新。',ephemeral=True)

@bot.tree.command(name='設定補貨頻道',description='設定補貨／庫存相關公告頻道')
async def dummy_restock_channel(i):
    if not admin(i.user): return await deny(i)
    await i.response.send_message('ℹ️ 補貨時間目前會直接顯示在客人選購時的提示中；若你要公告頻道，我可以下一版再獨立加上。',ephemeral=True)

@bot.tree.command(name='設定訂單紀錄',description='選擇訂單紀錄頻道')
@app_commands.describe(頻道='請選擇頻道')
async def set_order_log(i,頻道:discord.TextChannel):
    if not admin(i.user): return await deny(i)
    setv(i.guild.id,'order_log_channel_id',頻道.id); await i.response.send_message(f'✅ 訂單紀錄頻道已設定為 {頻道.mention}',ephemeral=True)

@bot.tree.command(name='設定操作紀錄',description='選擇管理員操作紀錄頻道')
@app_commands.describe(頻道='請選擇頻道')
async def set_action_log(i,頻道:discord.TextChannel):
    if not admin(i.user): return await deny(i)
    setv(i.guild.id,'action_log_channel_id',頻道.id); await i.response.send_message(f'✅ 操作紀錄頻道已設定為 {頻道.mention}',ephemeral=True)

@bot.tree.command(name='設定狀態頻道',description='選擇工單狀態公告頻道')
@app_commands.describe(頻道='請選擇頻道')
async def set_status_channel(i,頻道:discord.TextChannel):
    if not admin(i.user): return await deny(i)
    setv(i.guild.id,'status_channel_id',頻道.id); await i.response.send_message(f'✅ 工單狀態公告頻道已設定為 {頻道.mention}',ephemeral=True)

@bot.tree.command(name='設定訊息',description='設定客人看到的 Bot 訊息')
@app_commands.describe(類型='要修改的訊息',內容='新的訊息內容')
@app_commands.choices(類型=choices(['初始面板','沒貨提示','數量不足提示','付款完成提示','取消提示','暫停接單提示','狀態公告格式','訂單確認頁','缺貨交貨時間確認頁']))
async def setmsg(i,類型:str,內容:str):
    if not admin(i.user): return await deny(i)
    mp={'初始面板':'panel_text','沒貨提示':'out_of_stock','數量不足提示':'not_enough_stock','付款完成提示':'paid_text','取消提示':'cancel_text','暫停接單提示':'paused_text','狀態公告格式':'status_template','訂單確認頁':'order_confirm_text','缺貨交貨時間確認頁':'delivery_accept_text'}
    setv(i.guild.id,mp[類型],內容); await i.response.send_message(f'✅「{類型}」已更新。',ephemeral=True)

@bot.tree.command(name='改名工單',description='自動抓工單號與客人，只需選狀態商品數量')
@app_commands.describe(狀態='新狀態',商品='商品',數量='數量')
@app_commands.choices(狀態=choices(STATUSES),商品=choices(PRODUCTS))
async def rename_ticket(i,狀態:str,商品:str,數量:int):
    if not admin(i.user): return await deny(i)
    ch=i.channel
    if not isinstance(ch,discord.TextChannel): return await i.response.send_message('❌ 請在工單頻道使用。',ephemeral=True)
    rec=ticket_record(ch); no=rec['ticket_no'] if rec else ticket_no(ch.name)
    if not no: return await i.response.send_message('❌ 無法自動辨識工單號。',ephemeral=True)
    remember(ch,no); bid,bname=await buyer_for(ch); buyer=bname or '客人'
    if 數量<=0: return await i.response.send_message('❌ 數量必須大於 0。',ephemeral=True)
    name=rename_name(狀態,no,商品,數量,buyer,service_type=(q('SELECT service_type FROM orders WHERE channel_id=? ORDER BY id DESC LIMIT 1',(str(ch.id),),True)[0]['service_type'] if q('SELECT service_type FROM orders WHERE channel_id=? ORDER BY id DESC LIMIT 1',(str(ch.id),),True) else '幣號'))
    try: await ch.edit(name=name)
    except discord.HTTPException: return await i.response.send_message('❌ Discord 暫時無法修改頻道名稱。',ephemeral=True)
    rows=q('SELECT id FROM orders WHERE channel_id=? ORDER BY id DESC LIMIT 1',(str(ch.id),),True)
    if rows:
        q('UPDATE orders SET product=?,quantity=?,status=? WHERE id=?',(商品,數量,狀態,rows[0]['id'])); log_order(rows[0]['id'],i.guild.id,i.user.id,'修改工單',name); await post_channel_log(i.guild,'action_log_channel_id',f'👑 **修改工單**｜#{no}｜{i.user.mention}｜{name}')
    await i.response.send_message(f'✅ 已改名為 `{name}`',ephemeral=True)

@bot.tree.command(name='修改狀態',description='修改目前工單狀態')
@app_commands.describe(工單='選擇工單頻道',狀態='新狀態')
@app_commands.choices(狀態=choices(STATUSES))
async def change_status(i,工單:discord.TextChannel,狀態:str):
    if not admin(i.user): return await deny(i)
    r=q('SELECT * FROM orders WHERE channel_id=? ORDER BY id DESC LIMIT 1',(str(工單.id),),True)
    if not r: return await i.response.send_message('❌ 找不到這張工單的訂單紀錄。',ephemeral=True)
    o=r[0]; q('UPDATE orders SET status=? WHERE id=?',(狀態,o['id'])); buyer=(await buyer_for(工單))[1] or '客人'; name=rename_name(狀態,o['ticket_no'],o['product'],o['quantity'],buyer,service_type=o['service_type'])
    try: await 工單.edit(name=name)
    except discord.HTTPException: pass
    log_order(o['id'],i.guild.id,i.user.id,'修改狀態',f'{o["status"]} -> {狀態}'); await post_channel_log(i.guild,'action_log_channel_id',f'👑 **修改狀態**｜#{o["ticket_no"]}｜{o["status"]} → {狀態}｜{i.user.mention}')
    rr=q('SELECT * FROM orders WHERE id=?',(o['id'],),True)[0]; await status_announce(rr,i.guild)
    await i.response.send_message(f'✅ {工單.mention} 已更新為 **{狀態}**。',ephemeral=True)

@bot.tree.command(name='結單',description='結束工單並鎖定交易按鈕')
async def close_ticket(i):
    if not admin(i.user): return await deny(i)
    ch=i.channel
    if not isinstance(ch,discord.TextChannel): return await i.response.send_message('❌ 請在工單頻道使用。',ephemeral=True)
    r=q('SELECT * FROM orders WHERE channel_id=? ORDER BY id DESC LIMIT 1',(str(ch.id),),True)
    if not r: return await i.response.send_message('❌ 找不到訂單紀錄。',ephemeral=True)
    o=r[0]; q('UPDATE orders SET status=?,completed_at=? WHERE id=?',('結單',now(),o['id'])); log_order(o['id'],i.guild.id,i.user.id,'結單','管理員結單')
    await post_channel_log(i.guild,'action_log_channel_id',f'👑 **結單**｜#{o["ticket_no"]}｜{i.user.mention}')
    buyer=(await buyer_for(ch))[1] or '客人'; name=rename_name('結單',o['ticket_no'],o['product'],o['quantity'],buyer,service_type=o['service_type'])
    try: await ch.edit(name=name)
    except discord.HTTPException: pass
    await i.response.send_message(template(i.guild.id,'close_text','✅ 本筆訂單已結單，感謝您的購買！'))
    rr=q('SELECT * FROM orders WHERE id=?',(o['id'],),True)[0]; await status_announce(rr,i.guild)

@bot.tree.command(name='查詢工單',description='查詢工單')
@app_commands.describe(狀態='可不填',商品='可不填')
@app_commands.choices(狀態=choices(STATUSES),商品=choices(PRODUCTS))
async def query_orders(i,狀態:str|None=None,商品:str|None=None):
    if not admin(i.user): return await deny(i)
    sql='SELECT * FROM orders WHERE guild_id=?'; params=[str(i.guild.id)]
    if 狀態: sql+=' AND status=?'; params.append(狀態)
    if 商品: sql+=' AND product=?'; params.append(商品)
    sql+=' ORDER BY id DESC LIMIT 50'; rows=q(sql,params,True)
    if not rows: return await i.response.send_message('📋 目前沒有符合條件的工單。',ephemeral=True)
    lines=[]
    for o in rows:
        ch=i.guild.get_channel(int(o['channel_id'])); link=ch.jump_url if isinstance(ch,discord.TextChannel) else ''
        lines.append(f'`#{o["ticket_no"]}` {o["product"]} × {o["quantity"]}｜**{o["status"]}**'+(f'｜[前往工單]({link})' if link else ''))
    await i.response.send_message('📋 **工單查詢**\n'+'\n'.join(lines),ephemeral=True)

@bot.tree.command(name='今日統計',description='查看今天的訂單統計與金額')
async def today_stats(i):
    if not admin(i.user): return await deny(i)
    from zoneinfo import ZoneInfo
    tz=ZoneInfo('Asia/Taipei'); today=datetime.now(tz).date()
    rows=q('SELECT * FROM orders WHERE guild_id=?',(str(i.guild.id),),True)
    todays=[]
    for o in rows:
        try:
            dt=datetime.fromisoformat(o['created_at']).astimezone(tz)
            if dt.date()==today: todays.append(o)
        except Exception: pass
    total=sum(int(o['total_price']) for o in todays)
    paid=sum(int(o['total_price']) for o in todays if o['status'] not in ('待付款','已取消'))
    counts={st:sum(1 for o in todays if o['status']==st) for st in STATUSES}
    lines=[f'📊 **今日訂單統計｜{today.strftime("%Y/%m/%d")}**',f'🧾 訂單數：**{len(todays)} 筆**',f'💰 訂單總額：**NT${total:,}**',f'💳 已進入付款後流程：**NT${paid:,}**']
    lines.append('')
    lines += [f'🟡 待付款：{counts["待付款"]} 筆',f'🔵 待交貨：{counts["待交貨"]} 筆',f'🛠️ 處理中：{counts["處理中"]} 筆',f'📦 待收貨：{counts["待收貨"]} 筆',f'✅ 結單：{counts["結單"]} 筆',f'❌ 已取消：{counts["已取消"]} 筆']
    await i.response.send_message('\n'.join(lines),ephemeral=True)

@bot.tree.command(name='查詢餘額',description='查詢自己的或指定會員的餘額')
@app_commands.describe(會員='管理員可指定其他會員；一般會員留空')
async def balance(i,會員:discord.Member|None=None):
    target=會員 if admin(i.user) and 會員 else i.user
    r=q('SELECT balance FROM balances WHERE user_id=?',(str(target.id),),True); b=int(r[0]['balance']) if r else 0
    await i.response.send_message(f'💰 {target.mention} 目前餘額：**NT${b:,}**',ephemeral=True)

async def balance_change(i,會員,金額,action):
    if not admin(i.user): return await deny(i)
    if 金額<=0: return await i.response.send_message('❌ 金額必須大於 0。',ephemeral=True)
    r=q('SELECT balance FROM balances WHERE user_id=?',(str(會員.id),),True); old=int(r[0]['balance']) if r else 0
    new=old+金額 if action=='增加' else old-金額
    if new<0: return await i.response.send_message(f'❌ {會員.mention} 餘額不足，無法扣除 NT${金額:,}。',ephemeral=True)
    q('INSERT INTO balances(user_id,balance) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET balance=excluded.balance',(str(會員.id),new))
    q('INSERT INTO balance_logs(guild_id,user_id,operator_id,amount,balance_after,action,created_at,note) VALUES(?,?,?,?,?,?,?,?)',(str(i.guild.id),str(會員.id),str(i.user.id),金額,new,action,now(),''))
    await post_channel_log(i.guild,'action_log_channel_id',f'💰 **餘額{action}**｜{會員.mention}｜NT${金額:,}｜操作人：{i.user.mention}｜餘額：NT${new:,}')
    await i.response.send_message(f'✅ {會員.mention} 餘額已從 NT${old:,} 變為 **NT${new:,}**。',ephemeral=True)

@bot.tree.command(name='增加餘額',description='增加會員餘額')
async def add_balance(i,會員:discord.Member,金額:int): await balance_change(i,會員,金額,'增加')
@bot.tree.command(name='扣除餘額',description='扣除會員餘額')
async def sub_balance(i,會員:discord.Member,金額:int): await balance_change(i,會員,金額,'扣除')

@bot.tree.command(name='購買相關資訊',description='查看購買相關資訊')
async def buy_info(i):
    text=get(i.guild.id,'buy_info','請先確認商品、數量、價格及交易規則後再付款。\n如有疑問，請於付款前提出。')
    await i.response.send_message(text,ephemeral=True)

@bot.tree.command(name='設定購買資訊',description='設定購買相關資訊')
async def set_buy_info(i,內容:str):
    if not admin(i.user): return await deny(i)
    setv(i.guild.id,'buy_info',內容); await i.response.send_message('✅ 購買相關資訊已更新。',ephemeral=True)

@bot.tree.command(name='設定管理員',description='設定管理員身分組')
async def set_admin_role(i,身分組:discord.Role):
    if not i.user.guild_permissions.administrator: return await deny(i)
    setv(i.guild.id,'admin_role_id',身分組.id); await i.response.send_message(f'✅ 已設定管理員身分組為 {身分組.mention}。',ephemeral=True)

@bot.tree.command(name='設定店長公告',description='設定店長休息時顯示的公告')
async def set_rest_text(i,內容:str):
    if not admin(i.user): return await deny(i)
    setv(i.guild.id,'rest_text',內容); await i.response.send_message('✅ 店長休息公告已更新。',ephemeral=True)

# 自動偵測工單
async def delayed(ch):
    # 建立後固定等待 1 秒，讓原 Ticket Bot 先完成權限與頻道初始化。
    await asyncio.sleep(1)
    if ch.guild.get_channel(ch.id) is None: return
    if ticket_record(ch): return
    if not is_ticket_candidate(ch): return

    # 先發交易面板；send_panel 會把原始工單編號永久記錄下來。
    sent = await send_panel(ch)
    if not sent: return

    # 面板發出後再等 1 秒，然後把頻道改成「工單-原編號」。
    await asyncio.sleep(1)
    if ch.guild.get_channel(ch.id) is None: return
    rec=ticket_record(ch)
    if not rec: return
    no=rec['ticket_no']
    # 只有原始 ticket-編號 才執行這次自動改名；避免干擾其他頻道。
    if is_ticket_candidate(ch):
        try: await ch.edit(name=f'工單-{no}', reason='新工單自動辨識與改名')
        except discord.HTTPException: pass

@bot.event
async def on_message(message):
    if message.author.bot or not isinstance(message.channel,discord.TextChannel):
        return
    # 代肝付款確認後，客人在工單直接傳遊戲帳號即可；不需要額外確認步驟。
    rows=q("SELECT * FROM orders WHERE channel_id=? AND service_type='代肝' AND status='待排單' AND game_account IS NULL ORDER BY id DESC LIMIT 1",(str(message.channel.id),),True)
    if rows and message.content.strip():
        o=rows[0]
        q('UPDATE orders SET game_account=? WHERE id=?',(message.content.strip(),o['id']))
        log_order(o['id'],message.guild.id,message.author.id,'收到遊戲帳號','客人於工單提供遊戲帳號')
        await message.channel.send('🎮 **已收到遊戲帳號**\n您的代肝訂單已排入處理。\n📋 排隊進度請至排隊網站查看。')
        await post_channel_log(message.guild,'order_log_channel_id',f'🎮 **已收到遊戲帳號**｜#{o["ticket_no"]}｜{message.author.mention}')
        rr=q('SELECT * FROM orders WHERE id=?',(o['id'],),True)[0]
        await status_announce(rr,message.guild)
        return
    await bot.process_commands(message)

@bot.event
async def on_guild_channel_create(ch):
    if isinstance(ch,discord.TextChannel): asyncio.create_task(delayed(ch))


a=asyncio.Lock()
@tasks.loop(seconds=60)
async def payment_reminder_loop():
    from datetime import timedelta
    for g in bot.guilds:
        try: mins=int(get(g.id,'payment_reminder_minutes','15') or 15)
        except ValueError: mins=15
        if mins<=0: continue
        cutoff=datetime.now(timezone.utc)-timedelta(minutes=mins)
        rows=q("SELECT * FROM orders WHERE guild_id=? AND status='待付款' AND payment_reminder_sent=0",(str(g.id),),True)
        for o in rows:
            try: created=datetime.fromisoformat(o['created_at'])
            except Exception: continue
            if created>cutoff: continue
            ch=g.get_channel(int(o['channel_id']))
            q('UPDATE orders SET payment_reminder_sent=1 WHERE id=?',(o['id'],))
            log_order(o['id'],g.id,None,'付款逾時提醒',f'{mins} 分鐘未付款')
            await post_channel_log(g,'order_log_channel_id',f'⏰ **付款提醒**｜#{o["ticket_no"]}｜{o["product"]} × {o["quantity"]}｜NT${o["total_price"]:,}｜已超過 {mins} 分鐘未完成付款。')
            if isinstance(ch,discord.TextChannel):
                try: await ch.send(f'⏰ **付款提醒**\n您的訂單 #{o["ticket_no"]} 尚未完成付款，若仍要購買，請回到付款訊息按下「💳 完成付款」。')
                except discord.HTTPException: pass

@payment_reminder_loop.before_loop
async def before_payment_reminder(): await bot.wait_until_ready()


@bot.event
async def on_error(event_method, *args, **kwargs):
    import traceback
    print(f'Bot event error: {event_method}')
    traceback.print_exc()

@bot.event
async def on_ready():
    if not getattr(bot,'views',False):
        bot.add_view(ServiceTypeView())
        bot.add_view(CoinProductView())
        bot.add_view(BoostTierView())
        for r in q("SELECT id FROM orders WHERE status='待付款'",(),True):
            bot.add_view(PaymentMethodView(int(r['id'])))
            bot.add_view(PaymentView(int(r['id'])))
        for r in q("SELECT id FROM orders WHERE status IN ('待交貨','處理中','待收貨')",(),True):
            bot.add_view(OrderManageView(int(r['id'])))
        bot.views=True
    for g in bot.guilds:
        try:
            # Slash commands are defined globally; copy them to each guild for immediate availability.
            bot.tree.copy_global_to(guild=g)
            synced = await bot.tree.sync(guild=g)
            print(f'伺服器同步成功：{g.name} ({g.id})，{len(synced)} 個指令')
        except Exception as e:
            print('sync error',g.id,repr(e))
    if not payment_reminder_loop.is_running(): payment_reminder_loop.start()
    print('登入：',bot.user,'servers',len(bot.guilds))


# 指令名稱直接使用繁體中文；Discord CHAT_INPUT 指令支援 Unicode 名稱。

@bot.tree.error
async def tree_error(i,e):
    print('command error',repr(e))
    try:
        msg='❌ 指令執行失敗，請確認你有正確的權限與參數。'
        if i.response.is_done(): await i.followup.send(msg,ephemeral=True)
        else: await i.response.send_message(msg,ephemeral=True)
    except discord.HTTPException: pass

if __name__=='__main__':
    if not TOKEN: raise SystemExit('請設定 DISCORD_TOKEN 環境變數。')
    bot.run(TOKEN)
