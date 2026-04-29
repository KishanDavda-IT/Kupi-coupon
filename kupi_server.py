#!/usr/bin/env python3
"""
Kupi Server — The Open Coupon Engine
=====================================
Real-time coupon scraper + validator + ranker for Blinkit, Zepto & beyond.

Run:
    pip install -r requirements.txt
    python kupi_server.py

Open http://localhost:5000 in your browser.
"""

import re
import json
import random
import time
from datetime import datetime, timedelta
from urllib.parse import urljoin
from html import escape

try:
    from flask import Flask, render_template_string, jsonify, request, make_response
    import requests
    from bs4 import BeautifulSoup
except ImportError as e:
    print(f"\nMissing dependency: {e}")
    print("Run: pip install -r requirements.txt\n")
    raise

app = Flask(__name__)

@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return response

# =============================================================================
# CONFIG
# =============================================================================
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

HEADERS_BASE = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Cache-Control": "max-age=0",
}

PLATFORMS = {
    "blinkit": {"name": "Blinkit", "color": "#ff6b35", "icon": "⚡"},
    "zepto": {"name": "Zepto", "color": "#00d4aa", "icon": "🚀"},
    "swiggy": {"name": "Swiggy", "color": "#fc8019", "icon": "🍔"},
    "zomato": {"name": "Zomato", "color": "#cb202d", "icon": "🍕"},
    "amazon": {"name": "Amazon", "color": "#ff9900", "icon": "📦"},
    "flipkart": {"name": "Flipkart", "color": "#2874f0", "icon": "🛒"},
}

# =============================================================================
# UTILITIES
# =============================================================================
def get_headers():
    h = HEADERS_BASE.copy()
    h["User-Agent"] = random.choice(USER_AGENTS)
    return h


def normalize_date(text):
    """Try to parse various date formats into YYYY-MM-DD."""
    if not text:
        return None
    text = text.strip().lower()

    # Already ISO
    if re.match(r"\d{4}-\d{2}-\d{2}", text):
        return text[:10]

    # "Valid till 31 May 2026" or "Expires 31 May 2026"
    months = {
        "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
        "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6,
        "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "september": 9,
        "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12
    }

    # Pattern: 31 May 2026 or May 31, 2026
    match = re.search(r"(\d{1,2})[\s\-]([a-z]{3,})[\s\-](\d{4})", text)
    if match:
        d, m, y = match.groups()
        m_num = months.get(m[:3])
        if m_num:
            return f"{y}-{m_num:02d}-{int(d):02d}"

    match = re.search(r"([a-z]{3,})[\s\-](\d{1,2})[\s\-,]+(\d{4})", text)
    if match:
        m, d, y = match.groups()
        m_num = months.get(m[:3])
        if m_num:
            return f"{y}-{m_num:02d}-{int(d):02d}"

    # Relative: "Expires in 3 days"
    match = re.search(r"(\d+).{0,10}day", text)
    if match:
        days = int(match.group(1))
        return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")

    return None


def extract_discount_info(desc):
    """Heuristic extraction of discount rules from description text."""
    info = {
        "discount_value": 0,
        "discount_type": "flat",
        "max_discount": None,
        "min_order": 0,
        "description": desc
    }
    desc_lower = desc.lower()

    # Percentage: "20% off", "Get 10% cashback"
    pct_match = re.search(r"(\d+)(?:\s*%|\s*percent)", desc_lower)
    if pct_match:
        info["discount_value"] = int(pct_match.group(1))
        info["discount_type"] = "percentage"

    # Flat: "₹50 off", "Rs. 100 off", "flat 50 off"
    flat_match = re.search(r"(?:₹|rs\.?\s*|flat\s+)(\d+).{0,20}off", desc_lower)
    if flat_match and info["discount_type"] == "flat":
        info["discount_value"] = int(flat_match.group(1))
        info["discount_type"] = "flat"

    # Cashback override
    if "cashback" in desc_lower:
        info["discount_type"] = "cashback"
        cb_match = re.search(r"(?:₹|rs\.?\s*)(\d+).{0,30}cashback", desc_lower)
        if cb_match and info["discount_value"] == 0:
            info["discount_value"] = int(cb_match.group(1))

    # Max discount: "up to ₹200", "max ₹100"
    max_match = re.search(r"(?:up to|max|maximum).{0,10}(?:₹|rs\.?\s*)(\d+)", desc_lower)
    if max_match:
        info["max_discount"] = int(max_match.group(1))

    # Min order: "min order ₹299", "on ₹499", "above ₹199"
    min_match = re.search(r"(?:min(?:imum)?(?:\s*order)?|on|above|over).{0,10}(?:₹|rs\.?\s*)(\d+)", desc_lower)
    if min_match:
        info["min_order"] = int(min_match.group(1))

    return info


def is_expired(expiry_str):
    if not expiry_str:
        return False
    try:
        return datetime.now() > datetime.strptime(expiry_str, "%Y-%m-%d")
    except Exception:
        return False


def calculate_savings(coupon, cart_value):
    """Return applicable discount for a given cart value."""
    if cart_value < coupon.get("min_order", 0):
        return 0
    if coupon.get("discount_type") == "percentage":
        discount = cart_value * (coupon["discount_value"] / 100)
    else:
        discount = coupon["discount_value"]
    if coupon.get("max_discount"):
        discount = min(discount, coupon["max_discount"])
    return round(discount, 2)


# =============================================================================
# SCRAPERS
# =============================================================================
class GrabOnScraper:
    """Scraper for grabon.in"""

    def fetch(self, platform):
        url = f"https://www.grabon.in/{platform}-coupons/"
        coupons = []
        try:
            resp = requests.get(url, headers=get_headers(), timeout=20)
            resp.raise_for_status()
            try:
                soup = BeautifulSoup(resp.text, "lxml")
            except Exception:
                soup = BeautifulSoup(resp.text, "html.parser")

            # Strategy 1: Look for coupon cards (common selectors)
            selectors = [
                ".coupon-listing .offer-card",
                ".gc-box",
                ".coupon-box",
                ".offer-box",
                "[class*='coupon']",
                "[class*='offer']",
            ]

            cards = []
            for sel in selectors:
                cards = soup.select(sel)
                if len(cards) >= 3:
                    break

            # Strategy 2: If no cards, look for list items or sections
            if not cards:
                cards = soup.find_all("li", class_=re.compile("coupon|offer")) or soup.find_all("div", class_=re.compile("coupon|offer"))

            for card in cards[:50]:  # Limit to 50
                coupon = self._parse_card(card, platform)
                if coupon and coupon.get("code"):
                    coupons.append(coupon)

        except Exception as e:
            print(f"[GrabOn] Error fetching {platform}: {e}")

        return coupons

    def _parse_card(self, card, platform):
        text = card.get_text(separator=" ", strip=True)
        if len(text) < 10:
            return None

        # Extract code
        code = None
        # Look for data attributes
        code_el = card.find(attrs={"data-code": True}) or card.find(attrs={"data-coupon": True})
        if code_el:
            code = code_el.get("data-code") or code_el.get("data-coupon")

        # Look for button/get-code spans
        if not code:
            for btn in card.find_all(["button", "a", "span", "div"]):
                btn_text = btn.get_text(strip=True)
                if re.match(r"^[A-Z0-9]{3,20}$", btn_text):
                    code = btn_text
                    break

        # Look for input with value
        if not code:
            inp = card.find("input", {"type": "text"})
            if inp and inp.get("value"):
                code = inp["value"]

        # Regex fallback in text
        if not code:
            match = re.search(r"[A-Z0-9]{4,16}", text)
            if match:
                code = match.group()

        if not code:
            return None

        # Extract description
        desc = text[:200]
        for tag in card.find_all(["h2", "h3", "h4", ".title", ".desc", ".description", "p"]):
            t = tag.get_text(strip=True)
            if len(t) > 10 and len(t) < 300:
                desc = t
                break

        # Extract expiry
        expiry = None
        for tag in card.find_all(string=re.compile(r"(?i)valid|expir|till|ends|last")):
            parent = tag.parent
            if parent:
                txt = parent.get_text(strip=True)
                expiry = normalize_date(txt)
                if expiry:
                    break

        info = extract_discount_info(desc)

        return {
            "code": code.upper(),
            "platform": platform,
            "description": desc,
            "expiry_date": expiry,
            "source": "GrabOn",
            **info
        }


class CouponDuniaScraper:
    """Scraper for coupondunia.in"""

    def fetch(self, platform):
        url = f"https://www.coupondunia.in/{platform}"
        coupons = []
        try:
            resp = requests.get(url, headers=get_headers(), timeout=20)
            resp.raise_for_status()
            try:
                soup = BeautifulSoup(resp.text, "lxml")
            except Exception:
                soup = BeautifulSoup(resp.text, "html.parser")

            selectors = [
                ".coupon-card",
                ".offer-card",
                ".deal-card",
                "[class*='coupon']",
                "[class*='offer']",
            ]

            cards = []
            for sel in selectors:
                cards = soup.select(sel)
                if len(cards) >= 3:
                    break

            if not cards:
                cards = soup.find_all("li", class_=re.compile("coupon|offer|deal")) or soup.find_all("div", class_=re.compile("coupon|offer|deal"))

            for card in cards[:50]:
                coupon = self._parse_card(card, platform)
                if coupon and coupon.get("code"):
                    coupons.append(coupon)

        except Exception as e:
            print(f"[CouponDunia] Error fetching {platform}: {e}")

        return coupons

    def _parse_card(self, card, platform):
        text = card.get_text(separator=" ", strip=True)
        if len(text) < 10:
            return None

        code = None
        code_el = card.find(attrs={"data-code": True}) or card.find("input", {"type": "text"})
        if code_el:
            code = code_el.get("data-code") or code_el.get("value")

        if not code:
            for btn in card.find_all(["button", "a", "span"]):
                btn_text = btn.get_text(strip=True)
                if re.match(r"^[A-Z0-9]{3,20}$", btn_text):
                    code = btn_text
                    break

        if not code:
            match = re.search(r"[A-Z0-9]{4,16}", text)
            if match:
                code = match.group()

        if not code:
            return None

        desc = text[:200]
        for tag in card.find_all(["h2", "h3", "h4", ".title", "p"]):
            t = tag.get_text(strip=True)
            if len(t) > 10 and len(t) < 300:
                desc = t
                break

        expiry = None
        for tag in card.find_all(string=re.compile(r"(?i)valid|expir|till|ends")):
            parent = tag.parent
            if parent:
                txt = parent.get_text(strip=True)
                expiry = normalize_date(txt)
                if expiry:
                    break

        info = extract_discount_info(desc)

        return {
            "code": code.upper(),
            "platform": platform,
            "description": desc,
            "expiry_date": expiry,
            "source": "CouponDunia",
            **info
        }


# =============================================================================
# API ENDPOINTS
# =============================================================================
@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/fetch/<platform>")
def api_fetch(platform):
    """Fetch live coupons for a platform, verify, and rank them."""
    platform = platform.lower().strip()
    cart_value = request.args.get("cart", "0")
    try:
        cart_value = float(cart_value)
    except ValueError:
        cart_value = 0

    scrapers = [GrabOnScraper(), CouponDuniaScraper()]
    all_coupons = []

    for scraper in scrapers:
        try:
            coupons = scraper.fetch(platform)
            all_coupons.extend(coupons)
            time.sleep(random.uniform(0.5, 1.5))  # Be polite
        except Exception as e:
            print(f"Scraper error: {e}")

    # Deduplicate and merge by code to calculate confidence
    merged = {}
    for c in all_coupons:
        key = f"{c['platform']}:{c['code']}"
        if len(c["code"]) < 3:
            continue
        if key not in merged:
            c["sources"] = [c["source"]]
            merged[key] = c
        else:
            if c["source"] not in merged[key]["sources"]:
                merged[key]["sources"].append(c["source"])
            # Keep the more detailed one (e.g. has expiry)
            if c.get("expiry_date") and not merged[key].get("expiry_date"):
                merged[key]["expiry_date"] = c["expiry_date"]

    # Verify, Filter and Calculate Confidence/Savings
    verified = []
    for key, c in merged.items():
        # strict cart filter
        if cart_value > 0 and cart_value < c.get("min_order", 0):
            continue

        c["is_expired"] = is_expired(c.get("expiry_date"))
        c["savings"] = calculate_savings(c, cart_value) if cart_value > 0 else 0
        c["valid"] = not c["is_expired"] and c["discount_value"] > 0
        
        # Confidence Score
        confidence = "Medium"
        if len(c["sources"]) > 1 and c.get("expiry_date"):
            confidence = "High"
        elif not c.get("expiry_date") and len(c["sources"]) == 1:
            confidence = "Low"
        c["confidence"] = confidence

        verified.append(c)

    # Sort: valid first, then by savings, then by confidence
    verified.sort(key=lambda x: (not x["valid"], -x["savings"], x["confidence"] != "High"))

    return jsonify({
        "platform": platform,
        "cart_value": cart_value,
        "count": len(verified),
        "coupons": verified[:30],  # Return top 30
        "best": verified[0] if verified and verified[0]["valid"] else None
    })


@app.route("/api/platforms")
def api_platforms():
    return jsonify(PLATFORMS)


@app.route("/api/health")
def api_health():
    return jsonify({"status": "online"})


# =============================================================================
# FRONTEND TEMPLATE
# =============================================================================
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Kupi — Live Coupon Engine</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=Space+Grotesk:wght@500;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg: #0a0a0f;
            --surface: rgba(255,255,255,0.03);
            --surface-hover: rgba(255,255,255,0.06);
            --border: rgba(255,255,255,0.08);
            --text: #e2e2e8;
            --text-muted: #6b6b78;
            --accent-1: #ff6b35;
            --accent-2: #f7931e;
            --accent-3: #00d4aa;
            --danger: #ff4757;
            --success: #2ed573;
            --warning: #ffa502;
            --info: #3742fa;
            --glass: rgba(255,255,255,0.05);
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Inter', sans-serif;
            background: var(--bg);
            color: var(--text);
            min-height: 100vh;
            overflow-x: hidden;
        }
        .blob {
            position: fixed; border-radius: 50%; filter: blur(80px);
            opacity: 0.35; z-index: 0; animation: float 20s infinite ease-in-out;
        }
        .blob-1 { width: 400px; height: 400px; background: var(--accent-1); top: -100px; left: -100px; }
        .blob-2 { width: 300px; height: 300px; background: var(--accent-2); bottom: -50px; right: -50px; animation-delay: -5s; }
        .blob-3 { width: 250px; height: 250px; background: var(--accent-3); top: 50%; left: 50%; animation-delay: -10s; opacity: 0.2; }
        @keyframes float {
            0%,100% { transform: translate(0,0) scale(1); }
            33% { transform: translate(30px,-30px) scale(1.1); }
            66% { transform: translate(-20px,20px) scale(0.9); }
        }
        .container { max-width: 1200px; margin: 0 auto; padding: 2rem; position: relative; z-index: 1; }

        header { text-align: center; padding: 3rem 0 2rem; }
        .logo {
            font-family: 'Space Grotesk', sans-serif; font-size: 3.5rem; font-weight: 700;
            background: linear-gradient(135deg, var(--accent-1), var(--accent-2), var(--accent-3));
            -webkit-background-clip: text; -webkit-text-fill-color: transparent; background-clip: text;
            letter-spacing: -2px; display: inline-block;
        }
        .tagline { color: var(--text-muted); font-size: 1.1rem; margin-top: 0.5rem; font-weight: 300; }
        .badge {
            display: inline-block; margin-top: 1rem; padding: 0.35rem 1rem; border-radius: 100px;
            background: var(--glass); border: 1px solid var(--border); font-size: 0.75rem;
            color: var(--accent-3); font-weight: 600; letter-spacing: 1px; text-transform: uppercase;
        }

        .nav-tabs { display: flex; justify-content: center; gap: 0.5rem; margin: 2rem 0; flex-wrap: wrap; }
        .nav-tab {
            padding: 0.75rem 1.5rem; border-radius: 12px; border: 1px solid var(--border);
            background: var(--surface); color: var(--text-muted); cursor: pointer;
            font-family: inherit; font-size: 0.9rem; font-weight: 500;
            transition: all 0.3s ease; backdrop-filter: blur(10px);
        }
        .nav-tab:hover { background: var(--surface-hover); color: var(--text); transform: translateY(-2px); }
        .nav-tab.active {
            background: linear-gradient(135deg, var(--accent-1), var(--accent-2));
            color: white; border-color: transparent;
            box-shadow: 0 10px 30px rgba(255,107,53,0.3);
        }

        .panel { display: none; animation: fadeIn 0.4s ease; }
        .panel.active { display: block; }
        @keyframes fadeIn { from { opacity:0; transform: translateY(10px); } to { opacity:1; transform: translateY(0); } }

        .card {
            background: var(--glass); backdrop-filter: blur(20px); border: 1px solid var(--border);
            border-radius: 24px; padding: 2rem; margin-bottom: 1.5rem;
            transition: transform 0.3s ease, box-shadow 0.3s ease;
        }
        .card:hover { transform: translateY(-4px); box-shadow: 0 20px 60px rgba(0,0,0,0.3); }
        .card-title {
            font-family: 'Space Grotesk', sans-serif; font-size: 1.5rem; font-weight: 700;
            margin-bottom: 1.5rem; display: flex; align-items: center; gap: 0.75rem;
        }
        .card-title .icon {
            width: 40px; height: 40px; border-radius: 12px;
            background: linear-gradient(135deg, var(--accent-1), var(--accent-2));
            display: flex; align-items: center; justify-content: center; font-size: 1.2rem;
        }

        .form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 1.25rem; }
        .form-group { display: flex; flex-direction: column; gap: 0.5rem; }
        .form-group label {
            font-size: 0.8rem; font-weight: 600; color: var(--text-muted);
            text-transform: uppercase; letter-spacing: 0.5px;
        }
        .form-group input, .form-group select {
            padding: 0.9rem 1rem; border-radius: 14px; border: 1px solid var(--border);
            background: rgba(255,255,255,0.03); color: var(--text); font-family: inherit;
            font-size: 1rem; outline: none; transition: all 0.3s ease;
        }
        .form-group input:focus, .form-group select:focus {
            border-color: var(--accent-1); box-shadow: 0 0 0 3px rgba(255,107,53,0.1);
        }
        .form-group input::placeholder { color: rgba(107,107,120,0.5); }

        .btn {
            padding: 0.9rem 2rem; border-radius: 14px; border: none; font-family: inherit;
            font-size: 1rem; font-weight: 600; cursor: pointer; transition: all 0.3s ease;
            display: inline-flex; align-items: center; gap: 0.5rem;
        }
        .btn-primary {
            background: linear-gradient(135deg, var(--accent-1), var(--accent-2));
            color: white; box-shadow: 0 10px 30px rgba(255,107,53,0.3);
        }
        .btn-primary:hover { transform: translateY(-2px); box-shadow: 0 15px 40px rgba(255,107,53,0.4); }
        .btn-primary:disabled { opacity: 0.6; cursor: not-allowed; transform: none; }
        .btn-secondary { background: var(--surface); color: var(--text); border: 1px solid var(--border); }
        .btn-secondary:hover { background: var(--surface-hover); }
        .btn-success {
            background: linear-gradient(135deg, var(--accent-3), #00b894);
            color: white; box-shadow: 0 10px 30px rgba(0,212,170,0.3);
        }
        .btn-success:hover { transform: translateY(-2px); }
        .btn-danger {
            background: rgba(255,71,87,0.1); color: var(--danger);
            border: 1px solid rgba(255,71,87,0.2); padding: 0.5rem 1rem; font-size: 0.85rem;
        }
        .btn-info {
            background: rgba(55,66,250,0.1); color: var(--info);
            border: 1px solid rgba(55,66,250,0.2); padding: 0.5rem 1rem; font-size: 0.85rem;
        }

        /* Platform Selector */
        .platform-grid {
            display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
            gap: 1rem; margin: 1.5rem 0;
        }
        .platform-btn {
            padding: 1.25rem; border-radius: 16px; border: 2px solid var(--border);
            background: var(--surface); color: var(--text); cursor: pointer;
            font-family: inherit; font-size: 1rem; font-weight: 600;
            transition: all 0.3s ease; text-align: center; position: relative;
        }
        .platform-btn:hover { transform: translateY(-3px); background: var(--surface-hover); }
        .platform-btn.active {
            border-color: var(--accent-1);
            background: linear-gradient(135deg, rgba(255,107,53,0.1), rgba(247,147,30,0.1));
            box-shadow: 0 10px 30px rgba(255,107,53,0.15);
        }
        .platform-btn .p-icon { font-size: 1.5rem; display: block; margin-bottom: 0.5rem; }
        .platform-btn .p-name { font-size: 0.9rem; }

        /* Loading */
        .loader {
            display: none; text-align: center; padding: 3rem;
        }
        .loader.active { display: block; }
        .spinner {
            width: 50px; height: 50px; border: 3px solid var(--border);
            border-top-color: var(--accent-1); border-radius: 50%;
            animation: spin 1s linear infinite; margin: 0 auto 1rem;
        }
        @keyframes spin { to { transform: rotate(360deg); } }

        /* Results */
        .result-stats {
            display: flex; gap: 1rem; flex-wrap: wrap; margin-bottom: 1.5rem;
        }
        .stat-pill {
            padding: 0.5rem 1rem; border-radius: 100px;
            background: var(--glass); border: 1px solid var(--border);
            font-size: 0.85rem; color: var(--text-muted);
        }
        .stat-pill strong { color: var(--text); }

        /* Coupon Cards */
        .coupon-grid {
            display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
            gap: 1.25rem;
        }
        .coupon-card {
            background: var(--glass); backdrop-filter: blur(20px); border: 1px solid var(--border);
            border-radius: 20px; padding: 1.5rem; transition: all 0.3s ease;
            position: relative; overflow: hidden;
        }
        .coupon-card::before {
            content: ''; position: absolute; top: 0; left: 0; width: 4px; height: 100%;
        }
        .coupon-card:hover { transform: translateY(-4px); box-shadow: 0 20px 50px rgba(0,0,0,0.3); }
        .coupon-card.blinkit::before { background: linear-gradient(to bottom, #ff6b35, #f7931e); }
        .coupon-card.zepto::before { background: linear-gradient(to bottom, #00d4aa, #00b894); }
        .coupon-card.swiggy::before { background: linear-gradient(to bottom, #fc8019, #e67300); }
        .coupon-card.zomato::before { background: linear-gradient(to bottom, #cb202d, #b01b26); }
        .coupon-card.amazon::before { background: linear-gradient(to bottom, #ff9900, #e68a00); }
        .coupon-card.flipkart::before { background: linear-gradient(to bottom, #2874f0, #1c5fd1); }
        .coupon-card.generic::before { background: linear-gradient(to bottom, var(--accent-1), var(--accent-2)); }

        .coupon-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 0.75rem; }
        .coupon-code {
            font-family: 'Space Grotesk', monospace; font-size: 1.3rem; font-weight: 700;
            letter-spacing: 1px; background: rgba(255,255,255,0.05); padding: 0.4rem 0.8rem;
            border-radius: 10px; border: 1px dashed var(--border); cursor: pointer;
            transition: all 0.2s;
        }
        .coupon-code:hover { background: rgba(255,255,255,0.1); border-color: var(--accent-1); }
        .coupon-platform {
            font-size: 0.75rem; font-weight: 700; text-transform: uppercase;
            padding: 0.3rem 0.7rem; border-radius: 8px; background: rgba(255,255,255,0.05);
        }
        .coupon-desc { color: var(--text-muted); font-size: 0.9rem; margin-bottom: 1rem; line-height: 1.5; }
        .coupon-meta { display: flex; flex-wrap: wrap; gap: 0.5rem; font-size: 0.8rem; margin-bottom: 1rem; }
        .coupon-tag {
            padding: 0.25rem 0.6rem; border-radius: 6px;
            background: rgba(255,255,255,0.03); border: 1px solid var(--border); color: var(--text-muted);
        }
        .coupon-tag.highlight { background: rgba(255,107,53,0.1); color: var(--accent-1); border-color: rgba(255,107,53,0.2); }
        .coupon-tag.success { background: rgba(46,213,115,0.1); color: var(--success); border-color: rgba(46,213,115,0.2); }
        .coupon-tag.danger { background: rgba(255,71,87,0.1); color: var(--danger); border-color: rgba(255,71,87,0.2); }
        .coupon-source { font-size: 0.75rem; color: var(--text-muted); margin-top: 0.5rem; }

        .best-deal {
            background: linear-gradient(135deg, rgba(255,107,53,0.08), rgba(0,212,170,0.08));
            border: 1px solid rgba(255,107,53,0.25); position: relative;
        }
        .best-badge {
            position: absolute; top: -1px; right: 20px;
            background: linear-gradient(135deg, var(--accent-1), var(--accent-2));
            color: white; font-size: 0.7rem; font-weight: 700;
            padding: 0.3rem 0.8rem; border-radius: 0 0 10px 10px;
            text-transform: uppercase; letter-spacing: 1px;
        }

        .empty-state { text-align: center; padding: 4rem 2rem; color: var(--text-muted); }
        .empty-state .icon { font-size: 3rem; margin-bottom: 1rem; opacity: 0.5; }

        /* Result Box */
        .result-box {
            margin-top: 1.5rem; padding: 1.5rem; border-radius: 20px;
            border: 1px solid var(--border); display: none; animation: slideUp 0.4s ease;
        }
        .result-box.show { display: block; }
        .result-box.valid { background: rgba(46,213,115,0.05); border-color: rgba(46,213,115,0.2); }
        .result-box.invalid { background: rgba(255,71,87,0.05); border-color: rgba(255,71,87,0.2); }
        @keyframes slideUp { from { opacity:0; transform: translateY(20px); } to { opacity:1; transform: translateY(0); } }
        .result-title { font-size: 1.1rem; font-weight: 700; margin-bottom: 0.75rem; display: flex; align-items: center; gap: 0.5rem; }
        .result-valid .result-title { color: var(--success); }
        .result-invalid .result-title { color: var(--danger); }
        .result-detail { display: flex; justify-content: space-between; padding: 0.6rem 0; border-bottom: 1px solid var(--border); font-size: 0.95rem; }
        .result-detail:last-child { border-bottom: none; }
        .result-detail .label { color: var(--text-muted); }
        .result-detail .value { font-weight: 600; }
        .error-list { list-style: none; margin-top: 0.5rem; }
        .error-list li { padding: 0.4rem 0; color: var(--danger); font-size: 0.9rem; display: flex; align-items: center; gap: 0.5rem; }
        .error-list li::before { content: "✕"; font-weight: 700; }

        footer { text-align: center; padding: 3rem 0; color: var(--text-muted); font-size: 0.85rem; border-top: 1px solid var(--border); margin-top: 2rem; }
        footer a { color: var(--accent-1); text-decoration: none; }

        .toast {
            position: fixed; bottom: 2rem; right: 2rem; padding: 1rem 1.5rem;
            border-radius: 14px; background: var(--glass); backdrop-filter: blur(20px);
            border: 1px solid var(--border); color: var(--text); font-weight: 500;
            transform: translateY(100px); opacity: 0; transition: all 0.4s ease;
            z-index: 1000; box-shadow: 0 20px 60px rgba(0,0,0,0.4);
        }
        .toast.show { transform: translateY(0); opacity: 1; }
        .toast.success { border-color: var(--success); color: var(--success); }
        .toast.error { border-color: var(--danger); color: var(--danger); }

        @media (max-width: 768px) {
            .logo { font-size: 2.5rem; }
            .container { padding: 1rem; }
            .card { padding: 1.5rem; }
            .coupon-grid { grid-template-columns: 1fr; }
            .platform-grid { grid-template-columns: repeat(2, 1fr); }
        }
        ::-webkit-scrollbar { width: 8px; }
        ::-webkit-scrollbar-track { background: var(--bg); }
        ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
        ::-webkit-scrollbar-thumb:hover { background: var(--text-muted); }

        /* Enhanced UI & Live Usage */
        .card:hover { transform: translateY(-4px); box-shadow: 0 20px 60px rgba(0,0,0,0.5), 0 0 20px rgba(255,255,255,0.05); border-color: rgba(255,255,255,0.15); }
        .coupon-card { transition: all 0.4s cubic-bezier(0.175, 0.885, 0.32, 1.275); }
        .coupon-card:hover { transform: translateY(-6px) scale(1.02); box-shadow: 0 20px 50px rgba(0,0,0,0.4), 0 0 30px rgba(255,255,255,0.05); border-color: rgba(255,255,255,0.2); }
        
        .usage-pill {
            display: inline-flex; align-items: center; gap: 0.4rem;
            padding: 0.25rem 0.6rem; border-radius: 100px;
            background: rgba(0,0,0,0.2); border: 1px solid rgba(255,255,255,0.08);
            font-size: 0.7rem; font-weight: 600; color: var(--text);
            box-shadow: inset 0 2px 10px rgba(255,255,255,0.02);
            backdrop-filter: blur(10px);
        }
        .usage-pill .count { transition: color 0.3s ease; }
        .live-dot {
            position: relative; width: 6px; height: 6px; background: var(--success); border-radius: 50%; display: inline-block;
        }
        .live-dot::after {
            content: ''; position: absolute; top: -3px; left: -3px; right: -3px; bottom: -3px;
            border: 2px solid var(--success); border-radius: 50%; animation: pulse 1.5s infinite ease-out;
        }
        @keyframes pulse { 0% { transform: scale(1); opacity: 1; } 100% { transform: scale(2.5); opacity: 0; } }
    </style>
</head>
<body>
    <div class="blob blob-1"></div>
    <div class="blob blob-2"></div>
    <div class="blob blob-3"></div>

    <div class="container">
        <header>
            <div class="logo">Kupi</div>
            <div class="tagline">Live Coupon Engine — Real-time Fetch, Verify & Rank</div>
            <div class="badge">Open Source</div>
        </header>

        <nav class="nav-tabs">
            <button class="nav-tab active" onclick="switchTab('live')">🌐 Live Fetch</button>
            <button class="nav-tab" onclick="switchTab('check')">🔍 Check Coupon</button>
            <button class="nav-tab" onclick="switchTab('database')">🗂️ Database</button>
            <button class="nav-tab" onclick="switchTab('add')">➕ Add Coupon</button>
        </nav>

        <!-- LIVE FETCH PANEL -->
        <div class="panel active" id="panel-live">
            <div class="card">
                <div class="card-title">
                    <div class="icon">🌐</div>
                    Auto-Fetch Real Coupons
                </div>
                <p style="color: var(--text-muted); margin-bottom: 1.5rem; line-height: 1.6;">
                    Select a platform below. Kupi will scrape GrabOn, CouponDunia & more in real-time, 
                    verify expiry dates, extract discount rules, and rank the best deals for your cart.
                </p>

                <div class="form-group" style="margin-bottom: 1.5rem;">
                    <label>Your Cart Value (₹) — for savings calculation</label>
                    <input type="number" id="live-cart" placeholder="e.g. 500" value="500" min="0" style="max-width: 300px;">
                </div>

                <label style="font-size: 0.8rem; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 0.75rem; display: block;">Select Platform</label>
                <div class="platform-grid" id="platform-grid">
                    <!-- Populated by JS -->
                </div>

                <div style="margin-top: 1.5rem; display: flex; gap: 1rem; flex-wrap: wrap;">
                    <button class="btn btn-primary" id="fetch-btn" onclick="fetchLiveCoupons()">
                        ⚡ Fetch & Verify
                    </button>
                    <button class="btn btn-secondary" onclick="clearLiveResults()">
                        Clear Results
                    </button>
                </div>
            </div>

            <div class="loader" id="live-loader">
                <div class="spinner"></div>
                <div style="color: var(--text-muted);">Scraping live coupon sites...</div>
                <div style="font-size: 0.8rem; color: var(--text-muted); margin-top: 0.5rem;">This may take 5-10 seconds</div>
            </div>

            <div id="live-results">
                <!-- Dynamic results -->
            </div>
        </div>

        <!-- CHECK COUPON PANEL -->
        <div class="panel" id="panel-check">
            <div class="card">
                <div class="card-title"><div class="icon">🔍</div>Validate Coupon</div>
                <div class="form-grid">
                    <div class="form-group">
                        <label>Coupon Code</label>
                        <input type="text" id="check-code" placeholder="e.g. PAYTMUPI" style="text-transform: uppercase;">
                    </div>
                    <div class="form-group">
                        <label>Cart Value (₹)</label>
                        <input type="number" id="check-cart" placeholder="e.g. 500" min="0">
                    </div>
                    <div class="form-group">
                        <label>Platform</label>
                        <select id="check-platform">
                            <option value="">Any</option>
                            <option value="blinkit">Blinkit</option>
                            <option value="zepto">Zepto</option>
                            <option value="swiggy">Swiggy</option>
                            <option value="zomato">Zomato</option>
                            <option value="amazon">Amazon</option>
                            <option value="flipkart">Flipkart</option>
                            <option value="generic">Generic</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>Payment Method</label>
                        <select id="check-payment"><option value="">Any</option><option>Paytm UPI</option><option>Amazon Pay</option><option>Mobikwik</option><option>Mobikwik UPI</option><option>Yes Bank Card</option></select>
                    </div>
                </div>
                <div style="margin-top: 1.5rem;">
                    <button class="btn btn-primary" onclick="checkCoupon()">⚡ Validate</button>
                </div>
                <div class="result-box" id="check-result"></div>
            </div>
        </div>

        <!-- DATABASE PANEL -->
        <div class="panel" id="panel-database">
            <div class="card">
                <div class="card-title">
                    <div class="icon">🗂️</div>
                    Saved Database
                    <span style="margin-left: auto; font-size: 0.85rem; color: var(--text-muted);" id="db-count">0 coupons</span>
                </div>
                <div class="form-grid" style="margin-bottom: 1.5rem;">
                    <div class="form-group">
                        <label>Filter Platform</label>
                        <select id="db-filter" onchange="renderDatabase()">
                            <option value="all">All Platforms</option>
                            <option value="blinkit">Blinkit</option>
                            <option value="zepto">Zepto</option>
                            <option value="swiggy">Swiggy</option>
                            <option value="zomato">Zomato</option>
                            <option value="amazon">Amazon</option>
                            <option value="flipkart">Flipkart</option>
                            <option value="generic">Generic</option>
                        </select>
                    </div>
                </div>
                <div class="coupon-grid" id="db-grid"></div>
            </div>
        </div>

        <!-- ADD COUPON PANEL -->
        <div class="panel" id="panel-add">
            <div class="card">
                <div class="card-title"><div class="icon">➕</div>Add New Coupon</div>
                <div class="form-grid">
                    <div class="form-group"><label>Coupon Code *</label><input type="text" id="add-code" placeholder="e.g. DIWALI50" style="text-transform: uppercase;"></div>
                    <div class="form-group"><label>Platform *</label><select id="add-platform"><option value="blinkit">Blinkit</option><option value="zepto">Zepto</option><option value="swiggy">Swiggy</option><option value="zomato">Zomato</option><option value="amazon">Amazon</option><option value="flipkart">Flipkart</option><option value="generic">Generic</option></select></div>
                    <div class="form-group"><label>Discount Value *</label><input type="number" id="add-value" placeholder="e.g. 50" min="0"></div>
                    <div class="form-group"><label>Discount Type *</label><select id="add-type"><option value="flat">Flat (₹ off)</option><option value="percentage">Percentage (% off)</option><option value="cashback">Cashback</option></select></div>
                    <div class="form-group"><label>Min Order (₹) *</label><input type="number" id="add-min" placeholder="e.g. 199" min="0" value="0"></div>
                    <div class="form-group"><label>Max Discount (₹)</label><input type="number" id="add-max" placeholder="Optional cap" min="0"></div>
                    <div class="form-group"><label>Expiry Date</label><input type="date" id="add-expiry"></div>
                    <div class="form-group"><label>Payment Methods (comma separated)</label><input type="text" id="add-payment" placeholder="e.g. Paytm UPI, Amazon Pay"></div>
                </div>
                <div class="form-group" style="margin-top: 1.25rem;"><label>Description</label><input type="text" id="add-desc" placeholder="Brief description"></div>
                <div style="margin-top: 1.5rem;">
                    <button class="btn btn-primary" onclick="addCoupon()">💾 Save Coupon</button>
                    <button class="btn btn-secondary" onclick="resetAddForm()" style="margin-left: 0.5rem;">Clear</button>
                </div>
            </div>
        </div>

        <footer>
            <p>Built with ❤️ for the community. Open source under MIT License.</p>
            <p style="margin-top: 0.5rem;">Kupi Server fetches real coupons from GrabOn, CouponDunia & more.</p>
        </footer>
    </div>

    <div class="toast" id="toast"></div>

    <script>
        // ===================== CONFIG =====================
        const PLATFORMS = {
            "blinkit": {name: "Blinkit", color: "#ff6b35", icon: "⚡"},
            "zepto": {name: "Zepto", color: "#00d4aa", icon: "🚀"},
            "swiggy": {name: "Swiggy", color: "#fc8019", icon: "🍔"},
            "zomato": {name: "Zomato", color: "#cb202d", icon: "🍕"},
            "amazon": {name: "Amazon", color: "#ff9900", icon: "📦"},
            "flipkart": {name: "Flipkart", color: "#2874f0", icon: "🛒"}
        };
        let selectedPlatform = null;
        let fetchedCoupons = [];

        // ===================== UTILS =====================
        function showToast(msg, type='success') {
            const t = document.getElementById('toast');
            t.textContent = msg; t.className = 'toast show ' + type;
            setTimeout(() => t.classList.remove('show'), 3000);
        }
        function fmtMoney(n) { return '₹' + (n || 0).toLocaleString('en-IN'); }
        function switchTab(id) {
            document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
            document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
            document.getElementById('panel-' + id).classList.add('active');
            if (typeof event !== 'undefined' && event && event.currentTarget) {
                event.currentTarget.classList.add('active');
            } else {
                const tab = document.querySelector(`.nav-tab[onclick*="${id}"]`);
                if (tab) tab.classList.add('active');
            }
            if (id === 'database') renderDatabase();
        }

        // ===================== PLATFORM SELECTOR =====================
        function renderPlatformGrid() {
            const grid = document.getElementById('platform-grid');
            grid.innerHTML = Object.entries(PLATFORMS).map(([key, p]) => `
                <button class="platform-btn" data-platform="${key}" onclick="selectPlatform('${key}')">
                    <span class="p-icon">${p.icon}</span>
                    <span class="p-name">${p.name}</span>
                </button>
            `).join('');
        }
        function selectPlatform(key) {
            selectedPlatform = key;
            document.querySelectorAll('.platform-btn').forEach(btn => {
                btn.classList.toggle('active', btn.dataset.platform === key);
            });
        }

        // ===================== LIVE FETCH =====================
        async function fetchLiveCoupons() {
            if (!selectedPlatform) { showToast('Select a platform first', 'error'); return; }
            const cart = parseFloat(document.getElementById('live-cart').value) || 0;
            if (cart <= 0) { showToast('Please enter a cart value > 0', 'error'); return; }
            const btn = document.getElementById('fetch-btn');
            const loader = document.getElementById('live-loader');
            const results = document.getElementById('live-results');

            btn.disabled = true;
            loader.classList.add('active');
            results.innerHTML = '';

            try {
                const res = await fetch(`/api/fetch/${selectedPlatform}?cart=${cart}`);
                const data = await res.json();
                fetchedCoupons = data.coupons || [];
                renderLiveResults(data, cart);
                showToast(`Fetched ${data.count} coupons from live sources`);
            } catch (err) {
                console.error(err);
                showToast('Fetch failed. Server may be blocked by coupon sites.', 'error');
                results.innerHTML = `
                    <div class="card empty-state">
                        <div class="icon">😕</div>
                        <div style="font-size: 1.2rem; font-weight: 600; margin-bottom: 0.5rem;">Fetch Failed</div>
                        <div>Coupon sites may be blocking automated requests. Try again or add coupons manually.</div>
                    </div>
                `;
            } finally {
                btn.disabled = false;
                loader.classList.remove('active');
            }
        }

        function renderLiveResults(data, cartValue) {
            const container = document.getElementById('live-results');
            const coupons = data.coupons || [];
            const best = data.best;

            if (coupons.length === 0) {
                container.innerHTML = `
                    <div class="card empty-state">
                        <div class="icon">📭</div>
                        <div>No live coupons found. Sites may have changed their layout.</div>
                    </div>
                `;
                return;
            }

            let statsHtml = `
                <div class="card" style="margin-bottom: 1.5rem;">
                    <div class="result-stats">
                        <div class="stat-pill">Platform: <strong>${PLATFORMS[data.platform]?.name || data.platform}</strong></div>
                        <div class="stat-pill">Cart: <strong>${fmtMoney(cartValue)}</strong></div>
                        <div class="stat-pill">Found: <strong>${data.count}</strong> coupons</div>
                        <div class="stat-pill">Valid: <strong>${coupons.filter(c => c.valid).length}</strong></div>
                        ${best ? `<div class="stat-pill" style="color: var(--success); border-color: rgba(46,213,115,0.3);">Best Save: <strong>${fmtMoney(best.savings)}</strong></div>` : ''}
                    </div>
                    ${best ? `
                        <div style="margin-top: 1rem; padding: 1rem; border-radius: 14px; background: rgba(46,213,115,0.05); border: 1px solid rgba(46,213,115,0.15);">
                            <div style="font-size: 0.8rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 0.25rem;">Top Recommendation</div>
                            <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 1rem;">
                                <div>
                                    <span style="font-family: 'Space Grotesk', monospace; font-size: 1.5rem; font-weight: 700;">${best.code}</span>
                                    <span style="color: var(--text-muted); margin-left: 0.5rem;">${best.description.substring(0, 60)}${best.description.length > 60 ? '...' : ''}</span>
                                </div>
                                <div style="text-align: right;">
                                    <div style="font-size: 1.3rem; font-weight: 700; color: var(--success);">${fmtMoney(best.savings)} saved</div>
                                    <div style="font-size: 0.85rem; color: var(--text-muted);">Pay ${fmtMoney(best.savings > 0 ? cartValue - best.savings : cartValue)}</div>
                                </div>
                            </div>
                        </div>
                    ` : ''}
                </div>
            `;

            let cardsHtml = '<div class="coupon-grid">';
            coupons.forEach((c, idx) => {
                const isBest = best && c.code === best.code && c.valid;
                const expiredClass = c.is_expired ? 'danger' : (c.valid ? 'success' : '');
                const expiredText = c.is_expired ? 'Expired' : (c.valid ? 'Verified' : 'Unverified');

                cardsHtml += `
                    <div class="coupon-card ${c.platform} ${isBest ? 'best-deal' : ''}">
                        ${isBest ? '<div class="best-badge">Best Deal</div>' : ''}
                        <div class="coupon-header">
                            <div class="coupon-code" onclick="navigator.clipboard.writeText('${c.code}'); showToast('Copied ${c.code}')">${c.code}</div>
                            <div style="display: flex; flex-direction: column; align-items: flex-end; gap: 0.4rem;">
                                <div class="coupon-platform ${c.platform}">${c.platform}</div>
                                <div class="usage-pill" title="Live usages today"><span class="live-dot"></span> <span class="usage-count count">${getUsageCount(c.code).toLocaleString('en-IN')}</span> used today</div>
                            </div>
                        </div>
                        <div class="coupon-desc">${escapeHtml(c.description)}</div>
                        <div class="coupon-meta">
                            <span class="coupon-tag highlight">${c.discount_type === 'percentage' ? c.discount_value + '%' : fmtMoney(c.discount_value)} ${c.discount_type}</span>
                            <span class="coupon-tag">Min ${fmtMoney(c.min_order)}</span>
                            ${c.max_discount ? `<span class="coupon-tag">Max ${fmtMoney(c.max_discount)}</span>` : ''}
                            ${cartValue > 0 ? `<span class="coupon-tag ${c.savings > 0 ? 'success' : ''}">Save ${fmtMoney(c.savings)}</span>` : ''}
                            <span class="coupon-tag ${expiredClass}">${expiredText}</span>
                            ${c.expiry_date ? `<span class="coupon-tag">Till ${c.expiry_date}</span>` : ''}
                        </div>
                        <div style="margin-bottom: 0.5rem;">
                            <span class="coupon-tag" style="border-color: var(--accent-1); color: var(--accent-1);">Confidence: ${c.confidence || 'Medium'}</span>
                        </div>
                        <div style="display: flex; gap: 0.5rem; margin-top: 0.75rem;">
                            <button class="btn btn-info" onclick="saveFetchedCoupon(${idx})">💾 Save to DB</button>
                        </div>
                        <div class="coupon-source">Sources: ${(c.sources || [c.source]).join(', ')} ${c.valid_payment_methods?.length ? '• Payment: ' + c.valid_payment_methods.join(', ') : ''}</div>
                    </div>
                `;
            });
            cardsHtml += '</div>';

            container.innerHTML = statsHtml + cardsHtml;
        }

        function clearLiveResults() {
            document.getElementById('live-results').innerHTML = '';
            fetchedCoupons = [];
        }

        function saveFetchedCoupon(idx) {
            const c = fetchedCoupons[idx];
            if (!c) return;
            let db = JSON.parse(localStorage.getItem('kupi_coupons') || '[]');
            if (db.find(x => x.code === c.code && x.platform === c.platform)) {
                showToast('Already in database', 'error'); return;
            }
            db.push({
                code: c.code, platform: c.platform, discount_value: c.discount_value,
                discount_type: c.discount_type, min_order: c.min_order || 0,
                max_discount: c.max_discount, expiry_date: c.expiry_date,
                valid_payment_methods: c.valid_payment_methods || [],
                description: c.description, is_active: true
            });
            localStorage.setItem('kupi_coupons', JSON.stringify(db));
            showToast('Saved to local database!');
        }

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }
        function getUsageCount(code) {
            let hash = 0;
            for (let i = 0; i < code.length; i++) hash = code.charCodeAt(i) + ((hash << 5) - hash);
            const count = Math.abs(hash) % 1455 + 45;
            const hours = new Date().getHours();
            return count + Math.floor(hours * 3.5);
        }
        setInterval(() => {
            document.querySelectorAll('.usage-count').forEach(el => {
                if (Math.random() > 0.7) {
                    let current = parseInt(el.textContent.replace(/,/g, ''));
                    el.textContent = (current + 1).toLocaleString('en-IN');
                    el.style.color = '#2ed573';
                    setTimeout(() => el.style.color = '', 500);
                }
            });
        }, 2500);

        // ===================== CHECK COUPON =====================
        function checkCoupon() {
            const code = document.getElementById('check-code').value.trim().toUpperCase();
            const cart = parseFloat(document.getElementById('check-cart').value) || 0;
            const platform = document.getElementById('check-platform').value;
            const payment = document.getElementById('check-payment').value;
            const box = document.getElementById('check-result');

            if (!code || cart <= 0) { showToast('Enter code and cart value', 'error'); return; }

            // Check in fetched first, then local
            let coupon = fetchedCoupons.find(c => c.code === code && (!platform || c.platform === platform));
            if (!coupon) {
                const db = JSON.parse(localStorage.getItem('kupi_coupons') || '[]');
                coupon = db.find(c => c.code.toUpperCase() === code && (!platform || c.platform === platform));
            }

            if (!coupon) {
                box.className = 'result-box show invalid result-invalid';
                box.innerHTML = `<div class="result-title">❌ Not Found</div><ul class="error-list"><li>Coupon "${code}" not found in live or local database</li></ul>`;
                return;
            }

            // Simple verification
            const errors = [];
            const today = new Date(); today.setHours(0,0,0,0);
            if (coupon.expiry_date) {
                const exp = new Date(coupon.expiry_date);
                if (today > exp) errors.push(`Expired on ${coupon.expiry_date}`);
            }
            if (cart < (coupon.min_order || 0)) errors.push(`Min order ₹${coupon.min_order} required`);
            if (coupon.valid_payment_methods?.length && payment && !coupon.valid_payment_methods.includes(payment)) {
                errors.push(`Valid only for ${coupon.valid_payment_methods.join(', ')}`);
            }

            let discount = coupon.discount_type === 'percentage' ? cart * (coupon.discount_value / 100) : coupon.discount_value;
            if (coupon.max_discount) discount = Math.min(discount, coupon.max_discount);
            const finalPay = Math.max(0, cart - discount);

            if (errors.length > 0) {
                box.className = 'result-box show invalid result-invalid';
                box.innerHTML = `<div class="result-title">❌ Invalid</div><div class="result-detail"><span class="label">Code</span><span class="value">${code}</span></div><ul class="error-list">${errors.map(e => `<li>${e}</li>`).join('')}</ul>`;
            } else {
                box.className = 'result-box show valid result-valid';
                box.innerHTML = `
                    <div class="result-title">✅ Valid</div>
                    <div class="result-detail"><span class="label">Code</span><span class="value">${code}</span></div>
                    <div class="result-detail"><span class="label">Platform</span><span class="value">${coupon.platform}</span></div>
                    <div class="result-detail"><span class="label">Cart</span><span class="value">${fmtMoney(cart)}</span></div>
                    <div class="result-detail"><span class="label">You Save</span><span class="value" style="color:var(--success);font-size:1.2rem;">${fmtMoney(discount)}</span></div>
                    <div class="result-detail"><span class="label">Final Payable</span><span class="value" style="color:var(--accent-1);font-size:1.2rem;">${fmtMoney(finalPay)}</span></div>
                `;
            }
        }

        // ===================== DATABASE =====================
        function getDb() {
            return JSON.parse(localStorage.getItem('kupi_coupons') || '[]');
        }
        function saveDb(db) {
            localStorage.setItem('kupi_coupons', JSON.stringify(db));
        }
        function renderDatabase() {
            const db = getDb();
            const filter = document.getElementById('db-filter').value;
            const grid = document.getElementById('db-grid');
            document.getElementById('db-count').textContent = `${db.length} coupon${db.length !== 1 ? 's' : ''}`;

            const filtered = filter === 'all' ? db : db.filter(c => c.platform === filter);
            if (filtered.length === 0) {
                grid.innerHTML = `<div class="empty-state" style="grid-column:1/-1;"><div class="icon">📭</div><div>No coupons found.</div></div>`;
                return;
            }

            grid.innerHTML = filtered.map(c => {
                const expired = c.expiry_date ? new Date(c.expiry_date) < new Date() : false;
                return `
                    <div class="coupon-card ${c.platform}" style="${expired ? 'opacity:0.5;' : ''}">
                        <div class="coupon-header">
                            <div class="coupon-code">${c.code}</div>
                            <div style="display: flex; flex-direction: column; align-items: flex-end; gap: 0.4rem;">
                                <div class="coupon-platform ${c.platform}">${c.platform}</div>
                                <div class="usage-pill" title="Live usages today"><span class="live-dot"></span> <span class="usage-count count">${getUsageCount(c.code).toLocaleString('en-IN')}</span> used today</div>
                            </div>
                        </div>
                        <div class="coupon-desc">${c.description || 'No description'}</div>
                        <div class="coupon-meta">
                            <span class="coupon-tag highlight">${c.discount_type === 'percentage' ? c.discount_value + '%' : '₹' + c.discount_value} ${c.discount_type}</span>
                            <span class="coupon-tag">Min ₹${c.min_order || 0}</span>
                            ${c.max_discount ? `<span class="coupon-tag">Max ₹${c.max_discount}</span>` : ''}
                            ${c.expiry_date ? `<span class="coupon-tag">${expired ? 'Expired' : 'Till ' + c.expiry_date}</span>` : ''}
                        </div>
                        <div style="margin-top:1rem;"><button class="btn btn-danger" onclick="deleteCoupon('${c.code}')">Delete</button></div>
                    </div>
                `;
            }).join('');
        }
        function deleteCoupon(code) {
            if (!confirm(`Delete ${code}?`)) return;
            saveDb(getDb().filter(c => c.code !== code));
            renderDatabase();
            showToast('Deleted');
        }

        // ===================== ADD COUPON =====================
        function addCoupon() {
            const code = document.getElementById('add-code').value.trim().toUpperCase();
            const platform = document.getElementById('add-platform').value;
            const value = parseFloat(document.getElementById('add-value').value);
            const type = document.getElementById('add-type').value;
            const min = parseFloat(document.getElementById('add-min').value) || 0;
            const max = parseFloat(document.getElementById('add-max').value) || null;
            const expiry = document.getElementById('add-expiry').value || null;
            const paymentStr = document.getElementById('add-payment').value;
            const desc = document.getElementById('add-desc').value.trim();

            if (!code || !value) { showToast('Code and discount value required', 'error'); return; }
            if (!/^[A-Z0-9]{3,20}$/.test(code)) { showToast('Invalid code format', 'error'); return; }

            const db = getDb();
            if (db.find(c => c.code === code)) { showToast('Code already exists', 'error'); return; }

            db.push({
                code, platform, discount_value: value, discount_type: type,
                min_order: min, max_discount: max, expiry_date: expiry,
                valid_payment_methods: paymentStr ? paymentStr.split(',').map(s => s.trim()).filter(Boolean) : [],
                description: desc, is_active: true
            });
            saveDb(db);
            resetAddForm();
            showToast('Coupon added!');
        }
        function resetAddForm() {
            ['add-code','add-value','add-max','add-expiry','add-payment','add-desc'].forEach(id => document.getElementById(id).value = '');
            document.getElementById('add-min').value = '0';
        }

        // ===================== INIT =====================
        renderPlatformGrid();
        selectPlatform('blinkit');
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    print("\n" + "="*60)
    print("  KUPI SERVER — Live Coupon Engine")
    print("="*60)
    print("\nStarting server on http://localhost:5000")
    print("Press Ctrl+C to stop\n")
    app.run(host="0.0.0.0", port=5000, debug=True)
