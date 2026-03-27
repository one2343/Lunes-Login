import os
import platform
import time
import random
import re
from typing import List, Dict, Optional, Tuple

import requests
from seleniumbase import SB
from pyvirtualdisplay import Display

"""
批量登录 https://betadash.lunes.host/login?next=/
登录成功后：
  0) 从登录成功后的“Manage Servers”界面里，找到 <a href="/servers/63585" class="server-card">
     - 提取 href 里的数字作为 server_id（例如 63585）
     - 点击该 a（或 open 对应 URL），进入 server 控制台页（等 “Now managing” 出现）
  1) server 页停留 4-6 秒
  2) 返回 https://betadash.lunes.host/ 页面，停留 3-5 秒
  3) 点击退出按钮 /logout 退出（不做 JS 强制点击、不做重试）

环境变量：ACCOUNTS_BATCH（多行，每行一套，英文逗号分隔）
  1) 不发 TG：email,password
  2) 发 TG：email,password,tg_bot_token,tg_chat_id

示例：
export ACCOUNTS_BATCH='a1@example.com,pass1
a2@example.com,pass2,123456:AAxxxxxx,123456789
'
"""

LOGIN_URL = "https://betadash.lunes.host/login?next=/"
HOME_URL = "https://betadash.lunes.host/"
SERVER_URL_TPL = "https://betadash.lunes.host/servers/{server_id}"

SCREENSHOT_DIR = "screenshots"
os.makedirs(SCREENSHOT_DIR, exist_ok=True)

# ✅ 登录表单选择器（你给的）
EMAIL_SEL = "#email"
PASS_SEL = "#password"
SUBMIT_SEL = 'button.submit-btn[type="submit"]'

# ✅ 登录成功后出现的退出按钮（你给的）
LOGOUT_SEL = 'a[href="/logout"].action-btn.ghost'

# ✅ server 页面加载成功标志：出现 “Now managing”
NOW_MANAGING_XPATH = 'xpath=//p[contains(normalize-space(.), "Now managing")]'

# ✅ 服务器卡片（你给的）：<a href="/servers/63585" class="server-card">
SERVER_CARD_LINK_SEL = 'a.server-card[href^="/servers/"]'


def mask_email_keep_domain(email: str) -> str:
    e = (email or "").strip()
    if "@" not in e:
        return "***"
    name, domain = e.split("@", 1)
    if len(name) <= 1:
        name_mask = name or "*"
    elif len(name) == 2:
        name_mask = name[0] + name[1]
    else:
        name_mask = name[0] + ("*" * (len(name) - 2)) + name[-1]
    return f"{name_mask}@{domain}"


def setup_xvfb():
    if platform.system().lower() == "linux" and not os.environ.get("DISPLAY"):
        display = Display(visible=False, size=(1920, 1080))
        display.start()
        os.environ["DISPLAY"] = display.new_display_var
        print("🖥️ Xvfb 已启动")
        return display
    return None


def screenshot(sb, name: str):
    path = f"{SCREENSHOT_DIR}/{name}"
    sb.save_screenshot(path)
    print(f"📸 {path}")


def tg_send(text: str, token: Optional[str] = None, chat_id: Optional[str] = None):
    token = (token or "").strip()
    chat_id = (chat_id or "").strip()
    if not token or not chat_id:
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(
            url,
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=15,
        ).raise_for_status()
    except Exception as e:
        print(f"⚠️ TG 发送失败：{e}")


def build_accounts_from_env() -> List[Dict[str, str]]:
    batch = (os.getenv("ACCOUNTS_BATCH") or "").strip()
    if not batch:
        raise RuntimeError("❌ 缺少环境变量：请设置 ACCOUNTS_BATCH（即使只有一个账号也用它）")

    accounts: List[Dict[str, str]] = []
    for idx, raw in enumerate(batch.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        parts = [p.strip() for p in line.split(",")]

        # ✅ 新格式：2列 or 4列
        if len(parts) not in (2, 4):
            raise RuntimeError(
                f"❌ ACCOUNTS_BATCH 第 {idx} 行格式不对（必须是 email,password 或 "
                f"email,password,tg_bot_token,tg_chat_id）：{raw!r}"
            )

        email, password = parts[0], parts[1]
        tg_token = parts[2] if len(parts) == 4 else ""
        tg_chat = parts[3] if len(parts) == 4 else ""

        if not email or not password:
            raise RuntimeError(f"❌ ACCOUNTS_BATCH 第 {idx} 行存在空字段：{raw!r}")

        accounts.append(
            {
                "email": email,
                "password": password,
                "tg_token": tg_token,
                "tg_chat": tg_chat,
            }
        )

    if not accounts:
        raise RuntimeError("❌ ACCOUNTS_BATCH 里没有有效账号行（空行/注释行不算）")

    return accounts


def _has_cf_clearance(sb: SB) -> bool:
    """
    # CF: 通过检查 Cloudflare 下发的 cf_clearance cookie 来判断是否过盾（仅用于日志/诊断）
    """
    try:
        cookies = sb.get_cookies()  # CF
        cf_clearance = next((c["value"] for c in cookies if c.get("name") == "cf_clearance"), None)  # CF
        print("🧩 cf_clearance:", "OK" if cf_clearance else "NONE")  # CF
        return bool(cf_clearance)  # CF
    except Exception:
        return False


def _try_click_captcha(sb: SB, stage: str):
    """
    # CF: 尝试自动点击 Turnstile / Cloudflare Challenge（能点则点）
    """
    try:
        sb.uc_gui_click_captcha()  # CF
        time.sleep(3)  # CF
    except Exception as e:
        print(f"⚠️ captcha 点击异常（{stage}）：{e}")  # CF


def _is_logged_in(sb: SB) -> Tuple[bool, Optional[str]]:
    """
    登录成功特征（业务判定，不属于 CF 逻辑）：
      - h1.hero-title 包含 Welcome back
      - 或 LOGOUT 按钮可见
    """
    welcome_text = None
    try:
        if sb.is_element_visible("h1.hero-title"):
            welcome_text = (sb.get_text("h1.hero-title") or "").strip()
            if "welcome back" in welcome_text.lower():
                return True, welcome_text
    except Exception:
        pass

    try:
        if sb.is_element_visible(LOGOUT_SEL):
            return True, welcome_text
    except Exception:
        pass

    return False, welcome_text


def _extract_server_id_from_href(href: str) -> Optional[str]:
    """
    从 "/servers/63585" 或 "https://.../servers/63585" 提取 63585
    """
    if not href:
        return None
    m = re.search(r"/servers/(\d+)", href)
    return m.group(1) if m else None


def _find_server_id_and_go_server_page(sb: SB) -> Tuple[Optional[str], bool]:
    """
    在登录成功后的页面里：
      - 找到 a.server-card[href^="/servers/"]
      - 提取 server_id
      - 点击这个 a 进入 server 页
    返回 (server_id, entered_ok)
    """
    try:
        # 先确保 server-card 出现（说明 Manage Servers 区块渲染出来了）
        sb.wait_for_element_visible(SERVER_CARD_LINK_SEL, timeout=25)
    except Exception:
        screenshot(sb, f"server_card_not_found_{int(time.time())}.png")
        return None, False

    try:
        href = sb.get_attribute(SERVER_CARD_LINK_SEL, "href") or ""
    except Exception:
        href = ""

    server_id = _extract_server_id_from_href(href)

    if not server_id:
        screenshot(sb, f"server_id_extract_failed_{int(time.time())}.png")
        return None, False

    try:
        print(f"🧭 提取到 server_id={server_id}，点击 server-card 跳转...")
        
        # 【修改 1】使用 js=True 进行点击，直接在 DOM 层级触发，无视顶部 Sticky 导航栏的物理遮挡
        sb.click(SERVER_CARD_LINK_SEL, js=True)

        # 【修改 2】弃用对 "Now managing" 的死等。
        # 改为等待基础的 body 加载完成，并额外休眠 3 秒确保 Pterodactyl 面板/图表渲染
        sb.wait_for_element_visible("body", timeout=15)
        time.sleep(3)
        
        return server_id, True
    except Exception as e:
        # click 失败兜底：直接 open 目标 URL
        try:
            server_url = SERVER_URL_TPL.format(server_id=server_id)
            print(f"⚠️ 点击跳转失败，改为直接打开：{server_url} (异常原因: {e})")
            sb.open(server_url)
            
            # 同样不再等 "Now managing"
            sb.wait_for_element_visible("body", timeout=15)
            time.sleep(3)
            
            return server_id, True
        except Exception:
            screenshot(sb, f"goto_server_failed_{int(time.time())}.png")
            return server_id, False


def _post_login_visit_then_logout(sb: SB) -> Tuple[Optional[str], bool]:
    """
    登录成功后：
      0) 从 Manage Servers 卡片中提取 server_id，并进入 server 页（等待 Now managing）
      1) server 页停留 4-6 秒
      2) 返回首页 / 停留 3-5 秒
      3) 点击 logout，并验证回到登录页
    返回 (server_id, logout_ok)
    """
    # 0) 提取 server_id 并进 server 页
    server_id, entered_ok = _find_server_id_and_go_server_page(sb)
    if not entered_ok:
        return server_id, False

    # 1) server 页停留
    stay1 = random.randint(4, 6)
    print(f"⏳ 服务器页停留 {stay1} 秒...")
    time.sleep(stay1)

    # 2) 回首页
    try:
        print(f"↩️ 返回首页：{HOME_URL}")
        sb.open(HOME_URL)
        sb.wait_for_element_visible("body", timeout=30)
    except Exception:
        screenshot(sb, f"back_home_failed_{int(time.time())}.png")
        return server_id, False

    stay2 = random.randint(3, 5)
    print(f"⏳ 首页停留 {stay2} 秒...")
    time.sleep(stay2)

    # 3) 点退出
    try:
        sb.wait_for_element_visible(LOGOUT_SEL, timeout=15)
        sb.scroll_to(LOGOUT_SEL)
        time.sleep(0.3)
        sb.click(LOGOUT_SEL)
    except Exception:
        screenshot(sb, f"logout_click_failed_{int(time.time())}.png")
        return server_id, False

    sb.wait_for_element_visible("body", timeout=30)
    time.sleep(1)

    # 退出成功：回到登录页（#email 出现）或 URL 包含 /login
    try:
        url_now = (sb.get_current_url() or "").lower()
    except Exception:
        url_now = ""

    if "/login" in url_now:
        return server_id, True

    try:
        if sb.is_element_visible(EMAIL_SEL) and sb.is_element_visible(PASS_SEL):
            return server_id, True
    except Exception:
        pass

    screenshot(sb, f"logout_verify_failed_{int(time.time())}.png")
    return server_id, False


def login_then_flow_one_account(email: str, password: str) -> Tuple[str, Optional[str], bool, str, Optional[str], bool]:
    """
    返回：
      (status, welcome_text, has_cf_clearance, current_url, server_id, logout_ok)

    status:
      - "OK"   登录成功（无论 logout 是否成功）
      - "FAIL" 登录失败
    """
    # CF: UC 模式是绕过 CF 自动化识别的关键基础
    with SB(uc=True, locale="en", test=True) as sb:  # CF
        print("🚀 浏览器启动（UC Mode）")  # CF

        # CF: 用 UC 方式打开页面 + 重连机制
        sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=5.0)  # CF
        time.sleep(2)

        # ===== 业务：填写并提交登录表单 =====
        try:
            sb.wait_for_element_visible(EMAIL_SEL, timeout=25)
            sb.wait_for_element_visible(PASS_SEL, timeout=25)
            sb.wait_for_element_visible(SUBMIT_SEL, timeout=25)
        except Exception:
            url_now = sb.get_current_url() or ""
            return "FAIL", None, _has_cf_clearance(sb), url_now, None, False

        sb.clear(EMAIL_SEL)
        sb.type(EMAIL_SEL, email)
        sb.clear(PASS_SEL)
        sb.type(PASS_SEL, password)

        # CF: 提交前尽量过盾（有的站提交前就需要点 Turnstile）
        _try_click_captcha(sb, "提交前")  # CF

        sb.click(SUBMIT_SEL)
        sb.wait_for_element_visible("body", timeout=30)
        time.sleep(2)

        # CF: 提交后再试一次（很多站是提交后才弹）
        _try_click_captcha(sb, "提交后")  # CF

        # CF: 获取 cf_clearance 判断是否过盾（不是必须，但可用于日志/诊断）
        has_cf = _has_cf_clearance(sb)  # CF
        current_url = (sb.get_current_url() or "").strip()

        # ===== 业务：判定登录成功 =====
        welcome_text = None
        logged_in = False
        for _ in range(10):  # 最多等 ~10 秒
            logged_in, welcome_text = _is_logged_in(sb)
            if logged_in:
                break
            time.sleep(1)

        if not logged_in:
            return "FAIL", welcome_text, has_cf, current_url, None, False

        # ===== 业务：登录后提取 server_id -> 进 server 页 -> 回首页 -> 退出 =====
        server_id, logout_ok = _post_login_visit_then_logout(sb)

        # 更新一下当前 URL
        try:
            current_url = (sb.get_current_url() or "").strip()
        except Exception:
            pass

        return "OK", welcome_text, has_cf, current_url, server_id, logout_ok


def main():
    accounts = build_accounts_from_env()
    display = setup_xvfb()

    ok = 0
    fail = 0
    logout_ok_count = 0
    tg_dests = set()

    try:
        for i, acc in enumerate(accounts, start=1):
            email = acc["email"]
            password = acc["password"]
            tg_token = (acc.get("tg_token") or "").strip()
            tg_chat = (acc.get("tg_chat") or "").strip()
            if tg_token and tg_chat:
                tg_dests.add((tg_token, tg_chat))

            safe_email = mask_email_keep_domain(email)

            print("\n" + "=" * 70)
            print(f"👤 [{i}/{len(accounts)}] 账号：{safe_email}")
            print("=" * 70)

            try:
                status, welcome_text, has_cf, url_now, server_id, logout_ok = login_then_flow_one_account(
                    email, password
                )

                if status == "OK":
                    ok += 1
                    if logout_ok:
                        logout_ok_count += 1
                    msg = (
                        f"✅ Lunes BetaDash 登录成功\n"
                        f"账号：{safe_email}\n"
                        f"server_id：{server_id or '未提取到'}\n"
                        f"welcome：{welcome_text or '未读取到'}\n"
                        f"退出：{'✅ 成功' if logout_ok else '❌ 失败'}\n"
                        f"当前页：{url_now}\n"
                        f"cf_clearance：{'OK' if has_cf else 'NONE'}"
                    )
                else:
                    fail += 1
                    msg = (
                        f"❌ Lunes BetaDash 登录失败\n"
                        f"账号：{safe_email}\n"
                        f"welcome：{welcome_text or '未检测到'}\n"
                        f"当前页：{url_now}\n"
                        f"cf_clearance：{'OK' if has_cf else 'NONE'}"
                    )

                print(msg)
                tg_send(msg, tg_token, tg_chat)

            except Exception as e:
                fail += 1
                msg = f"❌ Lunes BetaDash 脚本异常\n账号：{safe_email}\n错误：{e}"
                print(msg)
                tg_send(msg, tg_token, tg_chat)

            # 账号之间冷却
            time.sleep(5)
            if i < len(accounts):
                time.sleep(5)

        summary = f"📌 本次批量完成：登录成功 {ok} / 失败 {fail} | 退出成功 {logout_ok_count}/{ok}"
        print("\n" + summary)
        for token, chat in sorted(tg_dests):
            tg_send(summary, token, chat)

    finally:
        if display:
            display.stop()


if __name__ == "__main__":
    main()
