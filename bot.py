# -*- coding: utf-8 -*-
"""
ربات پایش بلیط قطار — پنل وب زنده + مدیریت از تلگرام
اجرا:  python bot.py      پنل:  http://127.0.0.1:8080
"""
import asyncio
import hashlib
import json
import os
import random
import re
import sys
import time
import socket
import subprocess
import urllib.request
import uuid
import webbrowser
from collections import deque
from datetime import datetime
from urllib.parse import urlparse

from aiohttp import web, ClientSession, ClientTimeout, TCPConnector
from playwright.async_api import async_playwright

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# در نسخه exe فایل‌های همراه (panel.html و ...) داخل پوشه موقت PyInstaller هستند
BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
VERSION = "7"


def _data_dir():
    """تنظیمات در پوشه ثابت کاربر ذخیره می‌شود تا با بستن/آپدیت برنامه از بین نرود"""
    d = os.environ.get("TICKETBOT_DATA")
    if not d:
        if sys.platform == "win32" and os.environ.get("APPDATA"):
            d = os.path.join(os.environ["APPDATA"], "TicketBot")
        else:
            d = os.path.join(os.path.expanduser("~"), ".ticket_bot")
    os.makedirs(d, exist_ok=True)
    return d


DATA = _data_dir()
CONFIG_PATH = os.path.join(DATA, "config.json")
SEARCHES_PATH = os.path.join(DATA, "searches.json")
PROFILE_DIR = os.path.join(DATA, "browser_profile")
SEED_CONFIG = os.path.join(BASE, "config.json")      # فقط مقدار اولیه
SEED_SEARCHES = os.path.join(BASE, "searches.json")
PANEL_PATH = os.path.join(BASE, "panel.html")

DEFAULT_CONFIG = {
    "telegram_token": "",
    "admin_chat_id": "",
    "proxy": "",                 # فقط برای تلگرام؛ مثلا http://127.0.0.1:10809
    "bale_token": "",
    "bale_chat_id": "",
    "panel_host": "127.0.0.1",   # برای دسترسی از گوشی در همان وای‌فای: 0.0.0.0
    "panel_port": 8080,
    "open_panel_on_start": True,
    "interval_min_sec": 150,
    "interval_max_sec": 210,
    "default_min_seats": 2,
    "headless": True,            # false = پنجره کروم دیده می‌شود (برای لاگین یا عیب‌یابی)
    "block_images": True,        # عکس‌ها لود نشوند تا اینترنت کمتر مصرف شود
    "autostart": False,
    "train_only": True,          # فقط قطار؛ اتوبوس و سفر ترکیبی حساب نشوند
}

PERSIST_FIELDS = ("id", "name", "url", "enabled", "min_seats", "filter")

FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

# پیدا کردن کارت‌های نتیجه به‌صورت عمومی: کوچک‌ترین بخش‌هایی از صفحه که هم قیمت دارند هم ساعت
PARSE_JS = r"""
() => {
  const fa = s => (s || '').replace(/[۰-۹]/g, d => '۰۱۲۳۴۵۶۷۸۹'.indexOf(d))
                          .replace(/[٠-٩]/g, d => '٠١٢٣٤٥٦٧٨٩'.indexOf(d));
  // قیمت واقعی = عدد چندرقمی کنار تومان/ریال، یا عدد بزرگ سه‌رقم‌سه‌رقم (مثل 1,510,000) اگر واحد جدا نوشته شده
  const priceRe = /(\d[\d,٬٫.]{3,}\s*\|?\s*(تومان|ریال))|(\b\d{1,3}([,٬]\d{3}){2,}\b)/;
  const quickRe = /(تومان|ریال|\d{1,3}[,٬]\d{3}[,٬]\d{3}|[۰-۹]{1,3}[,٬][۰-۹]{3}[,٬][۰-۹]{3})/;
  const timeRe = /\b\d{1,2}:\d{2}\b/;
  const timesRe = /\b\d{1,2}:\d{2}\b/g;
  const filterRe = /(حداکثر|حداقل|فیلتر|مرتب\s*سازی|شرکت‌های ریلی|شرکت های ریلی|ساعت حرکت\s*\|?\s*00:00)/;
  const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','SVG','PATH','HEAD','BODY','HTML','OPTION','SELECT']);
  // متن هر بخش جدا خوانده می‌شود و با « | » کنار هم می‌آید تا اعداد به هم نچسبند (مثل 17:301,510,000)
  const cache = new Map();
  const textOf = el => {
    if (cache.has(el)) return cache.get(el);
    const parts = [];
    const w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = w.nextNode())) {
      const t = n.nodeValue.replace(/\s+/g, ' ').trim();
      if (!t) continue;
      const p = n.parentElement;
      if (!p || SKIP.has(p.tagName) || p.getClientRects().length === 0) continue;
      parts.push(t);
    }
    const out = fa(parts.join(' | '));
    cache.set(el, out);
    return out;
  };
  const ok = el => {
    const raw = el.textContent || '';
    if (raw.length > 4000 || !quickRe.test(raw)) return false;
    const tc = textOf(el);
    return priceRe.test(tc) && timeRe.test(tc);
  };
  const els = [];
  for (const el of document.body.querySelectorAll('*')) {
    if (SKIP.has(el.tagName) || el.closest('aside, header, footer, nav')) continue;
    if (ok(el)) els.push(el);
  }
  const set = new Set(els);
  let cards = els.filter(el => !els.some(o => o !== el && el.contains(o)));
  const cardSet = new Set(cards);
  // کارت را تا جایی بزرگ کن که فقط همین یک سفر را داشته باشد (تا وضعیت «تکمیل ظرفیت» یا تعداد صندلی هم داخلش بیفتد)
  cards = cards.map(c => {
    let e = c;
    while (e.parentElement && e.parentElement !== document.body) {
      const p = e.parentElement;
      let n = 0;
      for (const o of cardSet) if (p.contains(o)) { n++; if (n > 1) break; }
      if (n > 1) break;
      const raw = p.textContent || '';
      if (raw.length > 3000) break;
      const t = textOf(p);
      if (n > 1 || t.length > 1500 || filterRe.test(t) || (t.match(timesRe) || []).length > 6) break;
      e = p;
    }
    return e;
  });
  const seen = new Set();
  return cards
    .filter(el => !seen.has(el) && seen.add(el))
    .map(el => textOf(el))
    .filter(t => t.length > 10 && !filterRe.test(t) && (t.match(timesRe) || []).length <= 6)
    .slice(0, 80);
}
"""

COUNT_JS = r"""
() => {
  const t = (document.body ? document.body.innerText : '').replace(/[۰-۹]/g, d => '۰۱۲۳۴۵۶۷۸۹'.indexOf(d));
  return (t.match(/\d[\d,٬]{3,}\s*(تومان|ریال)|\b\d{1,3}[,٬]\d{3}[,٬]\d{3}\b/g) || []).length + ':' + t.length;
}
"""

READY_JS = r"""
() => {
  const t = document.body ? document.body.innerText : '';
  return /تومان|ریال|\d{1,3}[,٬]\d{3}[,٬]\d{3}|[۰-۹]{1,3}[,٬][۰-۹]{3}[,٬][۰-۹]{3}|یافت نشد|نتیجه‌ای|نتیجه ای|موجود نیست|سفری وجود|قطاری/.test(t);
}
"""

FULL_WORDS = ["تکمیل", "ظرفیت پر", "پر شده", "پر شد", "ناموجود", "فروش رفته", "اتمام", "غیرقابل خرید",
              "ظرفیت ندارد", "بدون ظرفیت", "تمام شد", "فروش تمام", "غیر فعال", "غیرفعال"]
SEAT_RES = [
    re.compile(r"(?<![\d,٬:])(\d{1,3})\s*\|?\s*(?:صندلی|نفر|جا|عدد)\s*(?:باقی|خالی|موجود|مانده)"),
    re.compile(r"(?:ظرفیت|صندلی(?:\s|‌)*(?:های)?\s*خالی|جای\s*خالی|باقی\s*‌?مانده|موجودی)\s*(?:باقی\s*‌?مانده|خالی)?\s*[:：]?\s*\|?\s*(\d{1,3})(?![\d:,٬])"),
]
PRICE_RE = re.compile(r"(\d[\d,٬]{3,})\s*\|?\s*(تومان|ریال)")
# اتوبوس و سفر ترکیبی (قطار+اتوبوس) نباید حساب شوند.
# «اتوبوسی» نوع واگن قطار است، پس فقط کلمه جدای «اتوبوس» رد می‌شود.
NON_TRAIN_RE = re.compile(r"سفر\s*ترکیبی|تغییر\s*سرویس|پایانه|پايانه|ترمینال|ترمينال|بیهقی|بيهقي|"
                          r"(?:^|\|)\s*اتوبوس\s*(?:\||$)|تخت\s*خواب\s*شو|سواری|مینی\s*بوس")
# پیام‌های «سامانه راه‌آهن در دسترس نیست» در سایت‌های مختلف (متن نرمال‌شده: ی/ک فارسی، نیم‌فاصله = فاصله)
MAINT_PATTERNS = [
    r"(به ?روز ?رسانی|اختلال|قطعی).{0,60}سامانه",                                  # فلای‌تودی
    r"سامانه.{0,80}(در دسترس نیست|امکان.{0,60}وجود ندارد)",
    r"ارتباط با تامین ?کنندگان ریلی برقرار نیست",                                    # علی‌بابا
    r"عملیات پشتیبانی (تامین ?کنندگان ریلی|راه ?آهن)",                              # علی‌بابا، یوتراوز
    r"عدم دریافت اطلاعات از (سرور|سامانه)",                                         # مستربلیط
    r"امکان (خدمت ?رسانی|نمایش و خرید|فروش).{0,30}(فراهم نمی ?باشد|وجود ندارد)",
]
MAINT_RE = re.compile("|".join(f"(?:{p})" for p in MAINT_PATTERNS))
NOT_FOUND_RE = re.compile(r"(قطار|قطاری|نتیجه ای|سفری|بلیطی) (یافت|پیدا) نشد")      # رجا
UNTIL_RE = re.compile(r"(?:تا|حدود ساعت)\s*(?:حدود\s*)?(?:ساعت\s*)?(\d{1,2}:\d{2})(?![\s\S]*(?:تا|حدود ساعت)\s*(?:ساعت\s*)?\d{1,2}:\d{2})")


def normalize_fa(t):
    return (t.translate(FA_DIGITS).replace("ي", "ی").replace("ك", "ک").replace("\u200c", " ")
            .replace("أ", "ا").replace("ـ", ""))


def detect_notice(body, n_cards):
    t = re.sub(r"\s+", " ", normalize_fa(body))
    m = MAINT_RE.search(t)
    if m:
        around = t[max(0, m.start() - 150): m.end() + 250]
        u = UNTIL_RE.search(around)
        return "سامانه راه‌آهن موقتاً در دسترس نیست" + (f" (حدود ساعت {u.group(1)} درست می‌شود)" if u else "")
    if n_cards == 0 and NOT_FOUND_RE.search(t):
        return "سایت قطاری پیدا نکرد (ممکن است سامانه راه‌آهن در دسترس نباشد)"
    return None
PRICE_BARE_RE = re.compile(r"\b(\d{1,3}(?:[,٬]\d{3}){2,})\b")
JUNK_PARTS = {"اطلاعات قطار", "ایستگاه ها", "ایستگاه‌ها", "قوانین استرداد", "انتخاب بلیط",
              "انتخاب", "جزئیات", "مشاهده جزئیات", "خرید"}
SITE_NAMES = {"alibaba": "علی‌بابا", "flytoday": "فلای‌تودی", "raja": "رجا", "mrbilit": "مستربلیط",
              "safarmarket": "سفرمارکت", "samtik": "سام‌تیک", "utravs": "یوتراوز", "snapptrip": "اسنپ‌تریپ",
              "tikban": "تیکبان", "ghasedak24": "قاصدک۲۴", "safar724": "سفر۷۲۴"}
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def now_str():
    return datetime.now().strftime("%H:%M:%S")


def analyze(raw_text):
    parts = [p for p in raw_text.split(" | ") if p not in JUNK_PARTS]
    text = " | ".join(parts)
    seats = None
    for r in SEAT_RES:
        m = r.search(text)
        if m:
            seats = int(m.group(1))
            break
    full = any(w in text for w in FULL_WORDS) or seats == 0
    m = PRICE_RE.search(text)
    price = int(re.sub(r"[^\d]", "", m.group(1))) if m else None
    if m and m.group(2) == "ریال":
        price //= 10  # همیشه به تومان
    if price is None:
        mb = PRICE_BARE_RE.search(text)
        if mb:
            price = int(re.sub(r"[^\d]", "", mb.group(1)))
            if price >= 6_000_000:  # بلیط قطار معمولاً زیر ۶ میلیون تومان است؛ بزرگ‌تر یعنی ریال
                price //= 10
    key_src = text
    for r in SEAT_RES:
        key_src = r.sub("", key_src)
    key = hashlib.md5(key_src[:250].encode("utf-8")).hexdigest()[:12]
    return {"text": text, "seats": seats, "price": price, "available": not full, "key": key,
            "non_train": bool(NON_TRAIN_RE.search(text))}


def humanize_error(e):
    """خطای فنی مرورگر را کوتاه و فارسی می‌کند (بدون لینک طولانی)"""
    msg = re.sub(r"\s+at\s+https?://\S+", "", str(e).split("\n")[0])
    table = [
        ("ERR_TUNNEL_CONNECTION_FAILED", "سایت باز نشد؛ فیلترشکن/پروکسی مانع است (بیشتر سایت‌های ایرانی بدون فیلترشکن باز می‌شوند)"),
        ("ERR_PROXY_CONNECTION_FAILED", "سایت باز نشد؛ پروکسی سیستم جواب نمی‌دهد"),
        ("ERR_NAME_NOT_RESOLVED", "آدرس سایت پیدا نشد (اینترنت یا DNS)"),
        ("ERR_INTERNET_DISCONNECTED", "اینترنت قطع است"),
        ("ERR_CONNECTION_REFUSED", "سایت اتصال را رد کرد"),
        ("ERR_CONNECTION_RESET", "اتصال سایت قطع شد"),
        ("ERR_CONNECTION_TIMED_OUT", "سایت جواب نداد"),
        ("ERR_TIMED_OUT", "سایت جواب نداد"),
        ("Timeout", "سایت دیر جواب داد"),
        ("ERR_HTTP2_PROTOCOL_ERROR", "سایت درخواست را رد کرد (احتمالاً ربات را تشخیص داده)"),
    ]
    for key, fa_msg in table:
        if key.lower() in msg.lower():
            return fa_msg
    return msg[:160]


def site_name(url):
    host = urlparse(url).netloc.lower()
    for k, v in SITE_NAMES.items():
        if k in host:
            return v
    return host or "جستجو"


class Messenger:
    """تلگرام و بله API یکسانی دارند؛ فقط آدرس فرق می‌کند"""
    def __init__(self, cfg, name, title, base, token_key, chat_key, use_proxy):
        self.cfg, self.name, self.title, self.base = cfg, name, title, base
        self.token_key, self.chat_key, self.use_proxy = token_key, chat_key, use_proxy
        self.offset = 0
        self.err_at = 0
        self.ok = None      # None = هنوز امتحان نشده، True = وصل، False = خطا
        self.err = ""

    @property
    def token(self):
        return (self.cfg.get(self.token_key) or "").strip()

    @property
    def chat(self):
        return str(self.cfg.get(self.chat_key) or "").strip()


class Monitor:
    def __init__(self):
        seed = load_json(SEED_CONFIG, {})
        self.cfg = {**DEFAULT_CONFIG, **seed, **load_json(CONFIG_PATH, {})}
        for k, v in seed.items():  # توکنی که در config.json پوشه برنامه گذاشته شده ولی هنوز ذخیره نشده
            if v not in ("", None) and self.cfg.get(k) in ("", None):
                self.cfg[k] = v
        save_json(CONFIG_PATH, self.cfg)
        self.searches = {}
        saved = load_json(SEARCHES_PATH, None)
        for s in (saved if saved is not None else load_json(SEED_SEARCHES, [])):
            self._register(s)
        self.running = bool(self.cfg.get("autostart"))
        self.force_once = False
        self.checking = False
        self.next_check = None
        self.cycle = 0
        self.bytes_total = 0
        self.started_at = time.time()
        self.logs = deque(maxlen=300)
        self.subs = set()
        self.wake = asyncio.Event()
        self.pages = {}
        self.page_urls = {}
        self.context = None
        self.http = None
        self.messengers = [
            Messenger(self.cfg, "telegram", "تلگرام", "https://api.telegram.org", "telegram_token", "admin_chat_id", True),
            Messenger(self.cfg, "bale", "بله", "https://tapi.bale.ai", "bale_token", "bale_chat_id", False),
        ]

    # ---------- داده‌ها ----------
    def _register(self, s):
        s.setdefault("id", uuid.uuid4().hex[:8])
        s.setdefault("name", site_name(s.get("url", "")))
        s.setdefault("enabled", True)
        s.setdefault("min_seats", self.cfg["default_min_seats"])
        s.setdefault("filter", "")
        s["rt"] = {"status": "idle", "last_check": None, "found": 0, "available": 0,
                   "eligible": 0, "results": [], "error": None, "errors": 0, "prev_keys": set(),
                   "skipped": 0, "notice": None, "bytes": 0}
        self.searches[s["id"]] = s
        return s

    def persist(self):
        save_json(SEARCHES_PATH, [{k: s[k] for k in PERSIST_FIELDS} for s in self.searches.values()])

    def view(self, s):
        rt = s["rt"]
        v = {k: s[k] for k in PERSIST_FIELDS}
        v.update({k: rt[k] for k in ("status", "last_check", "found", "available", "eligible", "results", "error",
                                     "skipped", "notice", "bytes")})
        return v

    def state(self):
        return {
            "type": "state",
            "now": time.time(),
            "running": self.running,
            "checking": self.checking,
            "next_check": self.next_check,
            "cycle": self.cycle,
            "bytes_total": self.bytes_total,
            "started_at": self.started_at,
            "train_only": self.cfg.get("train_only", True),
            "interval": [self.cfg["interval_min_sec"], self.cfg["interval_max_sec"]],
            "default_min_seats": self.cfg["default_min_seats"],
            "settings": self.settings_view(),
            "messengers": [{"name": m.name, "title": m.title, "token": bool(m.token), "admin": bool(m.chat),
                            "ok": m.ok, "err": m.err}
                           for m in self.messengers],
            "searches": [self.view(s) for s in self.searches.values()],
        }

    def emit(self, obj):
        for q in list(self.subs):
            q.put_nowait(obj)

    def emit_state(self):
        self.emit(self.state())

    def log(self, msg):
        line = f"[{now_str()}] {msg}"
        print(line, flush=True)
        self.logs.append(line)
        self.emit({"type": "log", "line": line})

    # ---------- کنترل ----------
    def set_running(self, on):
        self.running = bool(on)
        self.cfg["autostart"] = self.running
        save_json(CONFIG_PATH, self.cfg)
        self.log("▶️ ربات روشن شد" if on else "⏸ ربات خاموش شد")
        self.wake.set()
        self.emit_state()

    def check_now(self):
        self.force_once = True
        self.wake.set()

    def add_search(self, url, name="", min_seats=None, flt=""):
        url = (url or "").strip()
        if not url.startswith("http"):
            raise ValueError("لینک باید با http شروع شود")
        if not name:
            n = sum(1 for s in self.searches.values() if site_name(s["url"]) == site_name(url))
            name = site_name(url) + (f" {n + 1}" if n else "")
        s = self._register({"name": name.strip(), "url": url,
                            "min_seats": int(min_seats or self.cfg["default_min_seats"]),
                            "filter": (flt or "").strip()})
        self.persist()
        self.log(f"➕ جستجوی جدید: {s['name']}")
        self.emit_state()
        return s

    async def update_search(self, sid, **fields):
        s = self.searches[sid]
        for k in ("name", "url", "enabled", "min_seats", "filter"):
            if k in fields and fields[k] is not None:
                s[k] = fields[k]
        s["min_seats"] = max(1, int(s["min_seats"]))
        if "filter" in fields or "min_seats" in fields:
            s["rt"]["prev_keys"] = set()
        if not s["enabled"]:
            await self.close_page(sid)
            s["rt"]["status"] = "idle"
        self.persist()
        self.emit_state()
        return s

    async def delete_search(self, sid):
        s = self.searches.pop(sid, None)
        await self.close_page(sid)
        self.persist()
        if s:
            self.log(f"🗑 حذف شد: {s['name']}")
        self.emit_state()

    MSG_KEYS = ("telegram_token", "admin_chat_id", "proxy", "bale_token", "bale_chat_id")

    def update_settings(self, **kw):
        for k in ("interval_min_sec", "interval_max_sec", "default_min_seats"):
            if kw.get(k) not in (None, ""):
                self.cfg[k] = max(1, int(kw[k]))
        if self.cfg["interval_max_sec"] < self.cfg["interval_min_sec"]:
            self.cfg["interval_max_sec"] = self.cfg["interval_min_sec"]
        msg_changed = False
        for k in self.MSG_KEYS:
            if k in kw and kw[k] is not None:
                v = str(kw[k]).strip()
                if k.endswith("token") and v == "":
                    continue  # خالی = بدون تغییر (برای پاک کردن از دکمه حذف استفاده می‌شود)
                if v == "-":
                    v = ""
                if self.cfg.get(k) != v:
                    self.cfg[k] = v
                    msg_changed = True
        if kw.get("train_only") is not None:
            self.cfg["train_only"] = bool(kw["train_only"])
            for s in self.searches.values():
                s["rt"]["prev_keys"] = set()
        for k in ("headless", "block_images"):
            if kw.get(k) is not None and bool(kw[k]) != self.cfg[k]:
                self.cfg[k] = bool(kw[k])
                self.log(f"تنظیم «{k}» ذخیره شد؛ بعد از بستن و باز کردن برنامه اعمال می‌شود")
        save_json(CONFIG_PATH, self.cfg)
        self.log("💾 تنظیمات ذخیره شد")
        if msg_changed:
            self.restart_messengers()
        self.emit_state()

    def restart_messengers(self):
        t = getattr(self, "msg_task", None)
        if t and not t.done():
            t.cancel()
        self.msg_task = asyncio.create_task(self.messenger_loops())

    def settings_view(self):
        def mask(v):
            return f"…{v[-4:]}" if v else ""
        c = self.cfg
        return {"telegram_token": mask(c["telegram_token"]), "admin_chat_id": c["admin_chat_id"],
                "proxy": c["proxy"], "bale_token": mask(c["bale_token"]), "bale_chat_id": c["bale_chat_id"],
                "headless": c["headless"], "block_images": c["block_images"], "data_dir": DATA}

    # ---------- مرورگر ----------
    def _install_chromium(self):
        """اولین اجرا: کروم مخصوص ربات را دانلود می‌کند (یک بار، حدود ۱۵۰ مگابایت)"""
        from playwright._impl._driver import compute_driver_executable, get_driver_env
        drv = compute_driver_executable()
        cmd = list(drv) if isinstance(drv, (tuple, list)) else [str(drv)]
        subprocess.run(cmd + ["install", "chromium"], env=get_driver_env(), check=True)

    async def start_browser(self):
        self.pw = await async_playwright().start()
        # ۱) مرورگر خود ربات  ۲) Edge که روی همه ویندوزها هست  ۳) Chrome  ۴) دانلود مرورگر ربات
        tried = []
        for channel in (None, "msedge", "chrome"):
            try:
                await self._launch(channel)
                if channel:
                    self.log(f"از مرورگر {'Edge' if channel == 'msedge' else 'Chrome'} سیستم استفاده می‌شود")
                return
            except Exception as e:
                tried.append(f"{channel or 'chromium'}: {str(e).splitlines()[0][:120]}")
        self.log("مرورگری پیدا نشد؛ در حال دانلود مرورگر ربات (یک بار، حدود ۱۵۰ مگابایت)…")
        try:
            await asyncio.to_thread(self._install_chromium)
        except Exception as e:
            raise RuntimeError("مرورگر پیدا نشد و دانلود هم ناموفق بود. Microsoft Edge یا Google Chrome را نصب کنید.\n"
                               + "\n".join(tried)) from e
        self.log("✅ مرورگر نصب شد")
        await self._launch(None)

    async def _launch(self, channel):
        args = ["--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows", "--disable-blink-features=AutomationControlled"]
        if self.cfg["block_images"]:
            # از route استفاده نمی‌کنیم چون کش مرورگر را خاموش می‌کند و مصرف اینترنت بیشتر می‌شود
            args.append("--blink-settings=imagesEnabled=false")
        opts = dict(headless=self.cfg["headless"], locale="fa-IR", timezone_id="Asia/Tehran",
                    viewport={"width": 1366, "height": 900}, args=args)
        if channel is None:
            opts["user_agent"] = USER_AGENT   # Edge/Chrome سیستم user-agent واقعی خودشان را دارند
        else:
            opts["channel"] = channel
        self.context = await self.pw.chromium.launch_persistent_context(PROFILE_DIR, **opts)
        # بعضی سایت‌ها مرورگر خودکار را تشخیص می‌دهند و نتیجه نشان نمی‌دهند
        await self.context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")

    async def track_bytes(self, page, s):
        """شمارش حجم واقعی دانلودشده (بعد از فشرده‌سازی، بدون چیزهایی که از کش آمده)"""
        try:
            cdp = await self.context.new_cdp_session(page)
            await cdp.send("Network.enable")

            def on_done(ev, s=s):
                n = int(ev.get("encodedDataLength") or 0)
                s["rt"]["bytes"] += n
                self.bytes_total += n
            cdp.on("Network.loadingFinished", on_done)
        except Exception as e:
            self.log(f"شمارش حجم برای {s['name']} فعال نشد: {e}")

    async def close_page(self, sid):
        p = self.pages.pop(sid, None)
        self.page_urls.pop(sid, None)
        if p and not p.is_closed():
            try:
                await p.close()
            except Exception:
                pass

    async def load_page(self, s):
        sid, url = s["id"], s["url"]
        p = self.pages.get(sid)
        if p is None or p.is_closed():
            p = await self.context.new_page()
            self.pages[sid] = p
            await self.track_bytes(p, s)
            await p.goto(url, wait_until="domcontentloaded", timeout=60000)
        elif self.page_urls.get(sid) != url:
            await p.goto(url, wait_until="domcontentloaded", timeout=60000)
        else:
            await p.reload(wait_until="domcontentloaded", timeout=60000)
        self.page_urls[sid] = url
        try:
            await p.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        try:
            await p.wait_for_function(READY_JS, timeout=20000)
        except Exception:
            pass
        # سایت‌هایی مثل سفرمارکت و اسنپ‌تریپ نتایج را کم‌کم اضافه می‌کنند؛ صبر تا ثابت شدن صفحه
        last, stable = None, 0
        for _ in range(12):
            await p.wait_for_timeout(1500)
            try:
                cur = await p.evaluate(COUNT_JS)
            except Exception:
                break
            stable = stable + 1 if cur == last else 0
            last = cur
            if stable >= 2 and not cur.startswith("0:"):
                break
        return p

    # ---------- بررسی ----------
    async def check_one(self, s):
        rt = s["rt"]
        rt["status"] = "checking"
        self.emit_state()
        try:
            page = await self.load_page(s)
            texts = await page.evaluate(PARSE_JS)
            seen, cards = set(), []
            for t in texts:
                c = analyze(t)
                if c["price"] is None:   # بدون قیمت = کارت سفر نیست
                    continue
                if c["key"] not in seen:
                    seen.add(c["key"])
                    cards.append(c)
            skipped = 0
            if self.cfg.get("train_only", True):
                trains = [c for c in cards if not c["non_train"]]
                skipped = len(cards) - len(trains)
                cards = trains
            flt = s.get("filter", "").translate(FA_DIGITS).split()
            if flt:
                # «328» نباید با قیمت 13,280,000 یا ساعت جور شود
                pats = [re.compile(r"(?<![\d,٬:])" + re.escape(w) + r"(?![\d,٬])") for w in flt]
                cards = [c for c in cards if all(p.search(c["text"]) for p in pats)]
            try:
                body = await page.evaluate("() => (document.body ? document.body.innerText : '').slice(0, 6000)")
            except Exception:
                body = ""
            notice = detect_notice(body, len(cards))
            for c in cards:
                c["eligible"] = c["available"] and (c["seats"] is None or c["seats"] >= s["min_seats"])
            elig = [c for c in cards if c["eligible"]]
            new = [c for c in elig if c["key"] not in rt["prev_keys"]]
            rt["prev_keys"] = {c["key"] for c in elig}
            avail = sum(1 for c in cards if c["available"])
            rt.update(status="ok", last_check=time.time(), found=len(cards), available=avail,
                      eligible=len(elig), results=cards[:25], error=None, errors=0, skipped=skipped, notice=notice)
            extra = f" ({skipped} اتوبوس/ترکیبی رد شد)" if skipped else ""
            if notice:
                extra += " — " + notice
            self.log(f"{s['name']}: {len(cards)} قطار، {avail} آزاد، {len(elig)} با حداقل {s['min_seats']} صندلی{extra}")
            for c in new:
                await self.alert(s, c)
        except Exception as e:
            rt["errors"] += 1
            rt.update(status="error", last_check=time.time(), error=humanize_error(e))
            self.log(f"⚠️ {s['name']}: خطا — {rt['error']}")
            if rt["errors"] >= 3:
                await self.close_page(s["id"])  # دفعه بعد صفحه از نو باز شود
            if rt["errors"] == 3:
                await self.tg_send(f"⚠️ جستجوی «{s['name']}» سه بار پشت سر هم خطا داد:\n{rt['error']}")
        self.emit_state()

    async def alert(self, s, c):
        seats = f"{c['seats']} صندلی" if c["seats"] is not None else "نامشخص (معمولاً یعنی زیاد)"
        price = f"{c['price']:,} تومان" if c["price"] else "—"
        self.log(f"🎉 بلیط آزاد: {s['name']} — {seats} — {c['text'][:120]}")
        self.emit({"type": "alert", "search": s["name"], "seats": seats, "price": price,
                   "text": c["text"], "url": s["url"]})
        msg = (f"🚆 بلیط آزاد شد!\n\n📍 {s['name']}\n💺 {seats}\n💰 {price}\n\n"
               f"{c['text'][:350]}\n\n🔗 {s['url']}")
        await self.tg_send(msg, [[{"text": "🔗 باز کردن صفحه جستجو", "url": s["url"]}]])

    async def run_loop(self):
        while True:
            if self.running or self.force_once:
                self.force_once = False
                active = [s for s in self.searches.values() if s["enabled"]]
                if active:
                    self.checking = True
                    self.cycle += 1
                    self.next_check = None
                    self.emit_state()
                    await asyncio.gather(*(self.check_one(s) for s in active))
                    self.checking = False
                if self.running:
                    wait = random.uniform(self.cfg["interval_min_sec"], self.cfg["interval_max_sec"])
                    self.next_check = time.time() + wait
                    self.log(f"چک بعدی تا {wait / 60:.1f} دقیقه دیگر…")
                    self.emit_state()
                    await self._sleep(wait)
                    continue
            self.next_check = None
            self.emit_state()
            await self._sleep(None)

    async def _sleep(self, t):
        try:
            await asyncio.wait_for(self.wake.wait(), t)
        except asyncio.TimeoutError:
            pass
        self.wake.clear()

    # ---------- تلگرام و بله ----------
    QUIET = ("answerCallbackQuery", "deleteWebhook")

    def tg_proxy(self, mg):
        """پروکسی فقط برای تلگرام: اول تنظیم دستی، بعد پروکسی سیستم ویندوز (مثل v2rayN)"""
        if not mg.use_proxy:
            return None
        if self.cfg["proxy"]:
            return self.cfg["proxy"]
        try:
            sysp = urllib.request.getproxies()
            p = sysp.get("https") or sysp.get("http")
            if p and not p.startswith("socks"):
                return p if "://" in p else "http://" + p
        except Exception:
            pass
        return None

    def _set_status(self, mg, ok, err=""):
        if mg.ok != ok or mg.err != err:
            mg.ok, mg.err = ok, err
            self.emit_state()

    async def api(self, mg, method, **payload):
        if not mg.token:
            return None
        proxy = self.tg_proxy(mg)
        try:
            async with self.http.post(f"{mg.base}/bot{mg.token}/{method}", json=payload, proxy=proxy,
                                      timeout=ClientTimeout(total=75, connect=20)) as r:
                data = await r.json(content_type=None)
                desc = str(data.get("description") or "")
                if data.get("ok") or method in self.QUIET or "not modified" in desc:
                    self._set_status(mg, True)
                else:
                    if r.status in (401, 404) or "unauthorized" in desc.lower() or "not found" in desc.lower():
                        self._set_status(mg, False, "توکن اشتباه است")
                    self.log(f"{mg.title}: {desc}")
                return data
        except Exception as e:
            detail = str(e).split("\n")[0][:140] or type(e).__name__
            self._set_status(mg, False, detail)
            if time.time() - mg.err_at > 120:
                mg.err_at = time.time()
                if mg.use_proxy:
                    hint = (f" — پروکسی {proxy} جواب نداد" if proxy else
                            " — تلگرام فیلتر است؛ فیلترشکن را روشن کنید یا آدرس پروکسی را در پنل بدهید")
                else:
                    hint = " — اگر فیلترشکن در حالت TUN است، بله را مسدود می‌کند؛ آن را روی System Proxy بگذارید"
                self.log(f"⚠️ اتصال به {mg.title} برقرار نشد: {detail}{hint}")
            return None

    async def send_to(self, mg, chat, text, buttons=None):
        if not chat:
            return None
        p = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
        if buttons:
            p["reply_markup"] = {"inline_keyboard": buttons}
        return await self.api(mg, "sendMessage", **p)

    async def tg_send(self, text, buttons=None):
        """ارسال به همه پیام‌رسان‌های فعال (تلگرام و بله)"""
        res = await asyncio.gather(*(self.send_to(m, m.chat, text, buttons)
                                     for m in self.messengers if m.token and m.chat))
        return any(r and r.get("ok") for r in res)

    async def tg_edit(self, mg, msg, text, buttons):
        r = await self.api(mg, "editMessageText", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
                           text=text, disable_web_page_preview=True, reply_markup={"inline_keyboard": buttons})
        if not r or (not r.get("ok") and "not modified" not in str(r.get("description"))):
            await self.send_to(mg, msg["chat"]["id"], text, buttons)  # اگر ویرایش پشتیبانی نشد، پیام تازه

    def menu_kb(self):
        run = ({"text": "⏸ خاموش کردن", "callback_data": "off"} if self.running
               else {"text": "▶️ روشن کردن", "callback_data": "on"})
        return [[run, {"text": "🔄 بررسی الان", "callback_data": "check"}],
                [{"text": "📋 جستجوها", "callback_data": "list"},
                 {"text": "📊 وضعیت", "callback_data": "status"}],
                [{"text": "➕ افزودن جستجو", "callback_data": "addhelp"}]]

    def status_text(self):
        lines = [f"🤖 ربات: {'🟢 روشن' if self.running else '🔴 خاموش'}   |   دور: {self.cycle}"]
        if self.next_check:
            lines.append(f"⏱ بررسی بعدی: {max(0, int(self.next_check - time.time()))} ثانیه دیگر")
        lines.append("")
        if not self.searches:
            lines.append("هنوز جستجویی اضافه نشده. یک لینک بفرستید.")
        for s in self.searches.values():
            rt = s["rt"]
            mark = "🟢" if s["enabled"] else "🔴"
            res = (f"{rt['found']} سفر، {rt['available']} آزاد، {rt['eligible']} مناسب"
                   if rt["last_check"] else "هنوز بررسی نشده")
            if rt["status"] == "error":
                res = "⚠️ خطا"
            elif rt.get("notice"):
                res += f" — ⏳ {rt['notice']}"
            lines.append(f"{mark} {s['name']} (💺≥{s['min_seats']}): {res}")
        return "\n".join(lines)

    def list_kb(self):
        rows = []
        for s in self.searches.values():
            rows.append([{"text": f"{'🟢' if s['enabled'] else '🔴'} {s['name'][:24]}", "callback_data": f"t:{s['id']}"},
                         {"text": f"💺{s['min_seats']}", "callback_data": f"m:{s['id']}"},
                         {"text": "🗑", "callback_data": f"d:{s['id']}"}])
        rows.append([{"text": "⬅️ منو", "callback_data": "menu"}])
        return rows

    LIST_HELP = ("📋 جستجوها\n🟢/🔴 روی اسم بزنید تا روشن/خاموش شود\n"
                 "💺 حداقل صندلی برای خبر دادن (هر بار زدن یکی زیاد می‌شود)\n🗑 حذف")
    ADD_HELP = ("➕ برای افزودن جستجو، لینک صفحه نتایج را بفرستید.\n\nقالب کامل (اختیاری):\n"
                "اسم | لینک | فیلتر\n\nمثال:\nقطار ۳۲۸ | https://... | 17:30\n\n"
                "فیلتر یعنی فقط کارت‌هایی که این کلمه‌ها را دارند (مثلا ساعت یا شماره قطار).")

    async def messenger_loops(self):
        active = [m for m in self.messengers if m.token]
        if not active:
            self.log("بله یا تلگرام تنظیم نشده؛ از بخش «پیام‌رسان‌ها و مرورگر» در پنل توکن را وارد کنید")
            return
        await asyncio.gather(*(self.poll(m) for m in active))

    async def poll(self, mg):
        await self.api(mg, "deleteWebhook")
        if mg.chat:
            await self.send_to(mg, mg.chat, "🤖 ربات بالا آمد.\n\n" + self.status_text(), self.menu_kb())
        else:
            self.log(f"در {mg.title} به ربات /start بفرستید تا مدیر ثبت شود")
        while True:
            data = await self.api(mg, "getUpdates", offset=mg.offset, timeout=50,
                                  allowed_updates=["message", "callback_query"])
            if not data or not data.get("ok"):
                await asyncio.sleep(5)
                continue
            for u in data.get("result") or []:
                mg.offset = u["update_id"] + 1
                try:
                    await self.handle_update(mg, u)
                except Exception as e:
                    self.log(f"⚠️ خطای پردازش پیام {mg.title}: {e}")

    async def handle_update(self, mg, u):
        if "callback_query" in u:
            q = u["callback_query"]
            if str(q["message"]["chat"]["id"]) != mg.chat:
                await self.api(mg, "answerCallbackQuery", callback_query_id=q["id"], text="دسترسی ندارید")
                return
            await self.on_callback(mg, q)
            return
        m = u.get("message")
        if not m:
            return
        chat = str(m["chat"]["id"])
        text = (m.get("text") or "").strip()
        if not mg.chat and text.startswith("/start"):
            self.cfg[mg.chat_key] = chat
            save_json(CONFIG_PATH, self.cfg)
            self.log(f"✅ مدیر {mg.title} ثبت شد ({chat})")
            self.emit_state()
            await self.send_to(mg, chat, "✅ شما مدیر این ربات شدید.")
        if chat != mg.chat:
            await self.send_to(mg, chat, "⛔️ این ربات خصوصی است.")
            return
        url_m = re.search(r"https?://\S+", text)
        if url_m:
            url = url_m.group(0)
            pieces = [p.strip() for p in text.replace(url, "").split("|") if p.strip()]
            s = self.add_search(url, pieces[0] if pieces else "", None, pieces[1] if len(pieces) > 1 else "")
            await self.send_to(mg, chat, f"✅ اضافه شد: {s['name']}\n💺 حداقل صندلی: {s['min_seats']}"
                               + (f"\n🔎 فیلتر: {s['filter']}" if s["filter"] else "")
                               + ("" if self.running else "\n\nربات خاموش است؛ برای شروع روشنش کنید."),
                               self.list_kb())
            if self.running:
                self.check_now()
        else:
            await self.send_to(mg, chat, self.status_text(), self.menu_kb())

    async def on_callback(self, mg, q):
        d, msg = q.get("data", ""), q["message"]
        note = None
        if d in ("on", "off"):
            self.set_running(d == "on")
            note = "روشن شد" if d == "on" else "خاموش شد"
            await self.tg_edit(mg, msg, self.status_text(), self.menu_kb())
        elif d == "check":
            self.check_now()
            note = "بررسی شروع شد"
        elif d in ("menu", "status"):
            await self.tg_edit(mg, msg, self.status_text(), self.menu_kb())
        elif d == "list":
            await self.tg_edit(mg, msg, self.LIST_HELP, self.list_kb())
        elif d == "addhelp":
            await self.send_to(mg, msg["chat"]["id"], self.ADD_HELP)
        elif ":" in d:
            act, sid = d.split(":", 1)
            s = self.searches.get(sid)
            if not s:
                note = "پیدا نشد"
            elif act == "t":
                await self.update_search(sid, enabled=not s["enabled"])
                note = "روشن شد" if s["enabled"] else "خاموش شد"
                await self.tg_edit(mg, msg, self.LIST_HELP, self.list_kb())
            elif act == "m":
                await self.update_search(sid, min_seats=s["min_seats"] % 6 + 1)
                note = f"حداقل صندلی: {s['min_seats']}"
                await self.tg_edit(mg, msg, self.LIST_HELP, self.list_kb())
            elif act == "d":
                await self.tg_edit(mg, msg, f"«{s['name']}» حذف شود؟",
                                   [[{"text": "✅ بله، حذف کن", "callback_data": f"dy:{sid}"},
                                     {"text": "انصراف", "callback_data": "list"}]])
            elif act == "dy":
                await self.delete_search(sid)
                note = "حذف شد"
                await self.tg_edit(mg, msg, self.LIST_HELP, self.list_kb())
        await self.api(mg, "answerCallbackQuery", callback_query_id=q["id"], text=note or "")


# ---------- وب ----------
def build_app(mon):
    app = web.Application()

    async def index(_):
        return web.FileResponse(PANEL_PATH, headers={"Cache-Control": "no-store"})

    async def state(_):
        return web.json_response(mon.state())

    async def events(req):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream",
                                           "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        await resp.prepare(req)
        q = asyncio.Queue()
        mon.subs.add(q)

        async def send(obj):
            await resp.write(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode("utf-8"))
        try:
            await send(mon.state())
            await send({"type": "logs", "lines": list(mon.logs)})
            while True:
                try:
                    obj = await asyncio.wait_for(q.get(), 20)
                except asyncio.TimeoutError:
                    await resp.write(b": ping\n\n")
                    continue
                await send(obj)
        except (ConnectionResetError, asyncio.CancelledError, RuntimeError):
            pass
        finally:
            mon.subs.discard(q)
        return resp

    async def body(req):
        try:
            return await req.json()
        except Exception:
            return {}

    def err(e):
        return web.json_response({"ok": False, "error": str(e)}, status=400)

    async def run(req):
        mon.set_running((await body(req)).get("on"))
        return web.json_response({"ok": True})

    async def check(_):
        mon.check_now()
        return web.json_response({"ok": True})

    async def add(req):
        b = await body(req)
        try:
            s = mon.add_search(b.get("url"), b.get("name", ""), b.get("min_seats"), b.get("filter", ""))
        except Exception as e:
            return err(e)
        if mon.running:
            mon.check_now()
        return web.json_response({"ok": True, "id": s["id"]})

    async def patch(req):
        sid = req.match_info["sid"]
        if sid not in mon.searches:
            return err("پیدا نشد")
        b = await body(req)
        try:
            await mon.update_search(sid, **{k: b.get(k) for k in ("name", "url", "enabled", "min_seats", "filter")})
        except Exception as e:
            return err(e)
        return web.json_response({"ok": True})

    async def delete(req):
        await mon.delete_search(req.match_info["sid"])
        return web.json_response({"ok": True})

    async def settings(req):
        try:
            mon.update_settings(**(await body(req)))
        except Exception as e:
            return err(e)
        return web.json_response({"ok": True})

    async def tg_test(_):
        if not any(m.token and m.chat for m in mon.messengers):
            return err("توکن و chat_id تلگرام یا بله در config.json تنظیم نشده (یا /start نزده‌اید)")
        ok = await mon.tg_send("✅ پیام آزمایشی از پنل")
        return web.json_response({"ok": ok} if ok else {"ok": False, "error": "ارسال نشد؛ گزارش زنده را ببینید"})

    async def debug(req):
        sid = req.match_info["sid"]
        p = mon.pages.get(sid)
        if sid not in mon.searches or not p or p.is_closed():
            return web.Response(text="این جستجو هنوز بررسی نشده؛ اول یک بار بررسی کنید.", content_type="text/plain", charset="utf-8")
        cards = await p.evaluate(PARSE_JS)
        body_text = await p.inner_text("body")
        txt = (f"URL: {p.url}\n\n=== کارت‌های تشخیص‌داده‌شده ({len(cards)}) ===\n\n" + "\n\n".join(cards)
               + "\n\n=== متن کامل صفحه ===\n\n" + body_text)
        return web.Response(text=txt, content_type="text/plain", charset="utf-8",
                            headers={"Content-Disposition": f'attachment; filename="debug_{sid}.txt"'})

    app.router.add_get("/api/searches/{sid}/debug", debug)
    app.router.add_get("/", index)
    app.router.add_get("/api/state", state)
    app.router.add_get("/events", events)
    app.router.add_post("/api/run", run)
    app.router.add_post("/api/check", check)
    app.router.add_post("/api/searches", add)
    app.router.add_patch("/api/searches/{sid}", patch)
    app.router.add_delete("/api/searches/{sid}", delete)
    app.router.add_post("/api/settings", settings)
    app.router.add_post("/api/telegram/test", tg_test)
    return app


async def main():
    mon = Monitor()
    # فقط IPv4 (بعضی اینترنت‌های ایران با IPv6 مشکل دارند)؛ پروکسی سیستم روی بله اعمال نمی‌شود
    mon.http = ClientSession(connector=TCPConnector(family=socket.AF_INET, ttl_dns_cache=300))
    mon.log("در حال باز کردن مرورگر…")
    await mon.start_browser()
    runner = web.AppRunner(build_app(mon))
    await runner.setup()
    host, port = mon.cfg["panel_host"], int(mon.cfg["panel_port"])
    panel = f"http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}"
    try:
        await web.TCPSite(runner, host, port).start()
    except OSError:
        print(f"\n⚠️ پورت {port} در حال استفاده است؛ احتمالاً ربات از قبل در یک پنجره دیگر روشن است.")
        print(f"   همان پنجره را استفاده کنید یا ببندیدش. پنل: {panel}\n")
        try:
            webbrowser.open(panel)
        except Exception:
            pass
        await mon.context.close()
        await mon.http.close()
        return
    mon.log(f"پنل آماده است: {panel}")
    if mon.cfg["open_panel_on_start"]:
        try:
            webbrowser.open(panel)
        except Exception:
            pass
    mon.log(f"تنظیمات و جستجوها اینجا ذخیره می‌شوند: {DATA}")
    mon.restart_messengers()
    await mon.run_loop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("خداحافظ!")
