import base64
import datetime
import io
import os
import re
import subprocess
import time
import winsound

from PIL import Image, ImageEnhance, ImageOps
from playwright.sync_api import sync_playwright
from rapidocr_onnxruntime import RapidOCR

# ==================== تنظیمات پایه ====================
PRODUCT_ID = "YOUR_PRODUCT_ID"  # شناسه عددی محصول هدف را اینجا وارد کنید
API_URL = f"https://api.digikala.com/product/v1/products/{PRODUCT_ID}/"

# ساعت شروع رصد خودکار
TARGET_HOUR = 20
TARGET_MINUTE = 59
TARGET_SECOND = 50

POLL_INTERVAL = 1.1  # فاصله بین هر بررسی (ثانیه)

# سلکتورهای مربوط به فرم اعمال کد در صفحه پرداخت
DISCOUNT_INPUT_SELECTOR = 'input[placeholder*="کد تخفیف"]'
DISCOUNT_SUBMIT_SELECTOR = 'button[data-cro-id="payment-submit-voucher"]'
# ======================================================

print("[*] در حال آماده‌سازی موتور OCR...")
ocr_engine = RapidOCR()
CODE_REGEX = re.compile(r"DK[A-Z0-9]{3,5}", re.IGNORECASE)


def beep_alarm():
    try:
        for _ in range(3):
            winsound.Beep(1500, 220)
            time.sleep(0.05)
    except Exception:
        print("\a")


def copy_to_clipboard(text: str):
    try:
        subprocess.run("clip", input=text.encode("utf-8"), check=False)
    except Exception:
        pass


def dedupe_urls(urls: list[str]) -> list[str]:
    seen = {}
    for u in urls:
        if not isinstance(u, str) or "digikala-products" not in u:
            continue
        base = u.split("?")[0]
        if base not in seen or (".webp" in seen[base].lower() and ".webp" not in u.lower()):
            seen[base] = u
    return list(seen.values())


def extract_gallery_urls(api_json) -> list[str]:
    """استخراج تمام لینک‌های عکس گالری محصول از ساختار JSON"""
    if not isinstance(api_json, dict):
        return []
    data_node = api_json.get("data")
    if not isinstance(data_node, dict):
        return []
    product_node = data_node.get("product")
    if not isinstance(product_node, dict):
        return []
    images_node = product_node.get("images")
    if not isinstance(images_node, dict):
        return []

    urls = []

    def walk(node):
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str):
            if node.startswith("http") and "digikala-products" in node:
                urls.append(node)

    walk(images_node)
    return dedupe_urls(urls)


def fetch_gallery_fast(monitor_page) -> tuple[list[str], int]:
    """فراخوانی مستقیم API از داخل نشست مرورگر"""
    try:
        res = monitor_page.evaluate(
            """async (baseUrl) => {
                const url = `${baseUrl}?_=${Date.now()}`;
                try {
                    const r = await fetch(url);
                    const status = r.status;
                    const json = r.ok ? await r.json() : null;
                    return { status, json };
                } catch (e) {
                    return { status: -1, json: null };
                }
            }""",
            API_URL,
        )
        status = res.get("status", 0)
        if status == 200 and res.get("json"):
            return extract_gallery_urls(res["json"]), 200
        return [], status
    except Exception:
        return [], -1


def fetch_gallery_via_reload(monitor_page) -> tuple[list[str], int]:
    """روش پشتیبان: بازخوانی صفحه و شنود ترافیک شبکه (Network Interception)"""
    captured = []

    def on_response(resp):
        if resp.status == 200 and PRODUCT_ID in resp.url and "json" in resp.headers.get("content-type", "").lower():
            try:
                urls = extract_gallery_urls(resp.json())
                if urls:
                    captured.extend(urls)
            except Exception:
                pass

    monitor_page.on("response", on_response)
    try:
        monitor_page.reload(wait_until="domcontentloaded", timeout=15000)
        monitor_page.wait_for_timeout(1500)
    except Exception:
        pass
    finally:
        monitor_page.remove_listener("response", on_response)

    return dedupe_urls(captured), (200 if captured else 0)


def download_image_bytes(context, monitor_page, img_url: str) -> bytes | None:
    try:
        resp = context.request.get(
            img_url,
            headers={"Referer": "https://www.digikala.com/"},
            timeout=8000,
        )
        if resp.ok and len(resp.body()) > 1000:
            return resp.body()
    except Exception:
        pass

    try:
        b64 = monitor_page.evaluate(
            """async (url) => {
                const r = await fetch(url);
                if (!r.ok) return null;
                const buf = await r.arrayBuffer();
                let bin = '';
                const bytes = new Uint8Array(buf);
                for (let i = 0; i < bytes.byteLength; i++) bin += String.fromCharCode(bytes[i]);
                return btoa(bin);
            }""",
            img_url,
        )
        if b64:
            return base64.b64decode(b64)
    except Exception:
        pass
    return None


def run_ocr(img_bytes: bytes) -> tuple[str | None, list[str]]:
    """اجرای OCR دو مرحله‌ای (تصویر خام + پیش‌پردازش و تقویت کنتراست)"""
    raw_texts = []

    def scan(data: bytes):
        result, _ = ocr_engine(data)
        if not result:
            return None
        for item in result:
            txt = item[1].strip()
            raw_texts.append(txt)
            cleaned = re.sub(r"[\s\-_.:]+", "", txt).upper()
            match = CODE_REGEX.search(cleaned)
            if match:
                return match.group(0)
        return None

    code = scan(img_bytes)
    if code:
        return code, raw_texts

    try:
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img = img.resize((img.width * 2, img.height * 2), Image.Resampling.LANCZOS)
        gray = ImageOps.grayscale(img)
        enhanced = ImageEnhance.Contrast(gray).enhance(2.2)
        buf = io.BytesIO()
        enhanced.save(buf, format="PNG")
        code = scan(buf.getvalue())
        if code:
            return code, raw_texts
    except Exception:
        pass

    return None, raw_texts


def find_checkout_page(context, exclude_pages: list):
    open_pages = [pg for pg in context.pages if not pg.is_closed() and pg not in exclude_pages]
    for pg in reversed(open_pages):
        url = pg.url.lower()
        if any(k in url for k in ("checkout", "payment", "shipping", "cart")):
            return pg
    for pg in reversed(open_pages):
        if "digikala.com" in pg.url.lower():
            return pg
    return open_pages[-1] if open_pages else None


def show_image_popup(context, img_bytes: bytes | None, img_url: str, detected_code: str | None, bring_front: bool):
    viewer = context.new_page()
    if img_bytes:
        b64 = base64.b64encode(img_bytes).decode("ascii")
        src = f"data:image/jpeg;base64,{b64}"
    else:
        src = img_url

    banner = (
        f"<div style='background:#00c853;color:#000;font-size:36px;font-weight:bold;padding:12px;'>"
        f"کد شناسایی شد (کپی شد): {detected_code}</div>"
        if detected_code
        else "<div style='background:#ff9100;color:#000;font-size:28px;font-weight:bold;padding:12px;'>"
        "تصویر جدید شناسایی شد — بررسی دستی</div>"
    )
    html = f"""
    <html dir="rtl"><body style="margin:0;background:#111;color:#fff;text-align:center;font-family:tahoma;">
      {banner}
      <img src="{src}" style="max-width:98vw;max-height:86vh;border:3px solid #00e676;margin-top:8px;" />
    </body></html>
    """
    viewer.set_content(html)
    if bring_front:
        viewer.bring_to_front()


def apply_discount(checkout_page, code: str) -> bool:
    """باز کردن بخش کد، درج مقدار و کلیک روی دکمه ثبت"""
    if checkout_page is None or checkout_page.is_closed():
        print("\n[!] تب صفحه پرداخت پیدا نشد.")
        print(f">>> کد '{code}' در کلیپ‌بورد کپی شد. <<<")
        return False

    try:
        checkout_page.bring_to_front()

        inp = checkout_page.locator(DISCOUNT_INPUT_SELECTOR).first
        if inp.count() == 0 or not inp.is_visible():
            toggles = checkout_page.locator('span:text-is("کد تخفیف"), div:text-is("کد تخفیف")').locator("visible=true")
            for i in range(toggles.count()):
                try:
                    toggles.nth(i).click(timeout=1500)
                    checkout_page.wait_for_timeout(350)
                    if visible_inputs := checkout_page.locator(f"{DISCOUNT_INPUT_SELECTOR} >> visible=true"):
                        if visible_inputs.count() > 0:
                            break
                except Exception:
                    pass

        inp = checkout_page.locator(f"{DISCOUNT_INPUT_SELECTOR} >> visible=true").first
        inp.wait_for(state="visible", timeout=2500)
        inp.click()
        inp.fill("")
        inp.fill(code)

        btn = checkout_page.locator(f"{DISCOUNT_SUBMIT_SELECTOR} >> visible=true").first
        btn.wait_for(state="visible", timeout=2000)
        for _ in range(15):
            if btn.is_enabled():
                break
            checkout_page.wait_for_timeout(40)

        btn.click(timeout=2000)
        print(f"\n[✓] کد '{code}' با موفقیت در کادر وارد و ثبت شد.")
        return True
    except Exception as e:
        print(f"\n[!] خطا در ثبت خودکار کد: {e}")
        print(f">>> کد '{code}' در کلیپ‌بورد کپی شد. <<<")
        return False


def wait_until_start():
    now = datetime.datetime.now()
    target = now.replace(hour=TARGET_HOUR, minute=TARGET_MINUTE, second=TARGET_SECOND, microsecond=0)
    if now >= target:
        return

    print(f"\n[*] آماده و منتظر تا ساعت {TARGET_HOUR:02d}:{TARGET_MINUTE:02d}:{TARGET_SECOND:02d} ...")
    while True:
        rem = (target - datetime.datetime.now()).total_seconds()
        if rem <= 0:
            break
        m, s = divmod(int(rem), 60)
        print(f"\r⏳ باقی‌مانده تا شروع: {m:02d}:{s:02d} ", end="", flush=True)
        time.sleep(min(0.5, rem))
    print("\n🚀 شروع رصد لحظه‌ای محصول هدف!")


def main():
    # هشدار: پوشه dk_profile حاوی سشن مرورگر شماست؛ در مخازن عمومی آپلود نشود
    profile_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dk_profile")

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=False,
            viewport={"width": 1366, "height": 820},
            args=["--disable-blink-features=AutomationControlled"],
        )

        user_page = context.pages[0] if context.pages else context.new_page()
        if "digikala.com" not in user_page.url:
            user_page.goto("https://www.digikala.com/checkout/payment/", wait_until="domcontentloaded")

        print("\n================ مرحله آماده‌سازی ================")
        print("1. مطمئن شوید در تب اول، صفحه پرداخت باز است و محصول هدف در سبد قرار دارد.")
        print("2. بخش «کد تخفیف» را باز نگه دارید.")
        input("\nپس از آماده‌سازی، Enter بزنید...")

        monitor_page = context.new_page()
        monitor_page.goto(f"https://www.digikala.com/product/dkp-{PRODUCT_ID}/", wait_until="domcontentloaded", timeout=20000)
        monitor_page.wait_for_timeout(1200)

        use_reload_mode = False
        initial_urls, status = fetch_gallery_fast(monitor_page)
        if not initial_urls:
            use_reload_mode = True
            initial_urls, status = fetch_gallery_via_reload(monitor_page)

        baseline = {u.split("?")[0] for u in initial_urls}
        print(f"[✓] وضعیت API: HTTP {status} | تصاویر فعلی گالری: {len(baseline)} عدد")

        checkout_page = find_checkout_page(context, exclude_pages=[monitor_page])
        if checkout_page:
            checkout_page.bring_to_front()
            print(f"[✓] تب پرداخت آماده است: {checkout_page.url}")

        if input("\nتست عملکرد فرم با کد نمونه DK0000 انجام شود؟ (y/n): ").strip().lower() == "y":
            checkout_page = find_checkout_page(context, exclude_pages=[monitor_page])
            apply_discount(checkout_page, "DK0000")

        mode = input("\nشروع زمان‌بندی‌شده (1) یا اسکن فوری (2)؟ [1/2]: ").strip()
        if mode != "2":
            wait_until_start()

        checked_new = set()
        attempt = 0

        try:
            while True:
                attempt += 1
                now_str = datetime.datetime.now().strftime("%H:%M:%S")

                if use_reload_mode:
                    current_urls, status = fetch_gallery_via_reload(monitor_page)
                else:
                    current_urls, status = fetch_gallery_fast(monitor_page)
                    if status in (403, -1) and not current_urls:
                        use_reload_mode = True
                        current_urls, status = fetch_gallery_via_reload(monitor_page)

                new_urls = [
                    u for u in current_urls
                    if u.split("?")[0] not in baseline and u.split("?")[0] not in checked_new
                ]

                if new_urls:
                    print(f"\n🔥 [{now_str}] تصویر جدید شناسایی شد! ({len(new_urls)} تصویر)")
                    beep_alarm()

                    for img_url in new_urls:
                        checked_new.add(img_url.split("?")[0])
                        img_bytes = download_image_bytes(context, monitor_page, img_url)

                        code, raw_texts = (None, [])
                        if img_bytes:
                            code, raw_texts = run_ocr(img_bytes)
                            print(f"  متن‌های استخراج‌شده توسط OCR: {raw_texts}")

                        if code:
                            print(f"\n🎯 کد شناسایی شد: {code}")
                            copy_to_clipboard(code)
                            checkout_page = find_checkout_page(context, exclude_pages=[monitor_page])
                            apply_discount(checkout_page, code)
                            show_image_popup(context, img_bytes, img_url, code, bring_front=False)
                            if checkout_page and not checkout_page.is_closed():
                                checkout_page.bring_to_front()
                            beep_alarm()
                            print("\n>>> لطفاً مبلغ نهایی را بررسی و تایید کنید. <<<")
                            input("برای ادامه رصد Enter بزنید (یا Ctrl+C برای خروج)...")
                        else:
                            show_image_popup(context, img_bytes, img_url, None, bring_front=True)
                            print("  [!] الگوی کد در OCR یافت نشد؛ تصویر جهت بررسی دستی نمایش داده شد.")
                else:
                    print(
                        f"\r[{now_str}] بررسی #{attempt} (HTTP {status}) | {len(current_urls)} تصویر (بدون تغییر) ",
                        end="",
                        flush=True,
                    )

                time.sleep(POLL_INTERVAL if not use_reload_mode else 1.8)

        except KeyboardInterrupt:
            print("\n[*] رصد متوقف شد.")
        finally:
            input("برای بستن مرورگر Enter بزنید...")
            context.close()


if __name__ == "__main__":
    main()