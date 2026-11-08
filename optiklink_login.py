#!/usr/bin/env python3
"""
OptikLink 自动登录脚本 v4.4-enhanced
- 修复 Discord OAuth 真实参数动态探测（优先请求 /login?action=login）
- 修复环境变量为空字符串时默认值失效的问题
- 支持 socks5h:// 远程 DNS 解析与代理容错重试
- 优化数学题算式解析与容错（支持全角符号与实体解码）
- 完善 Telegram 详细通知报告
"""

import os
import re
import sys
import time
import html
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs, urlencode

import requests as _req

# 优先使用 cloudscraper 绕过 Cloudflare 基础防护
try:
    import cloudscraper
    USE_CLOUDSCRAPER = True
    print("[信息] 使用 cloudscraper 强化 HTTP 会话")
except ImportError:
    USE_CLOUDSCRAPER = False
    print("[警告] cloudscraper 未安装，将使用普通 requests 会话")

# ─────────────────────────────────────────────────────────────
# 配置（环境变量）
# ─────────────────────────────────────────────────────────────
DISCORD_TOKEN        = os.environ.get("DISCORD_TOKEN", "").strip()
TG_BOT_TOKEN         = os.environ.get("BOT_TOKEN", "").strip()
TG_CHAT_ID           = os.environ.get("CHAT_ID", "").strip()
EXPIRE_DATE_RAW      = os.environ.get("EXPIRE_DATE", "").strip()
DISCORD_CLIENT_ID    = os.environ.get("DISCORD_CLIENT_ID", "").strip() or "933437142254887052"
DISCORD_REDIRECT_URI = os.environ.get("DISCORD_REDIRECT_URI", "").strip() or "https://optiklink.com/login"
PANEL_URL            = os.environ.get("PANEL_URL", "https://control.optiklink.net").strip()
PANEL_API_KEY        = os.environ.get("PANEL_API_KEY", "").strip()
PANEL_SERVER_ID      = os.environ.get("PANEL_SERVER_ID", "").strip()
SERVER_START_WAIT    = int(os.environ.get("SERVER_START_WAIT", "60"))
PROXY_URL            = os.environ.get("PROXY_URL", "").strip()

# 硬编码后备参数
FALLBACK_CLIENT_ID = "933437142254887052"
FALLBACK_REDIRECT_URI = "https://optiklink.com/login"
FALLBACK_SCOPE = "guilds guilds.join identify email"

HEADERS_BROWSER = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

# ─────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────
def mask(value: str, keep: int = 4) -> str:
    if not value:
        return "***"
    if len(value) <= keep * 2:
        return "***"
    return value[:keep] + "***" + value[-keep:]

def mask_url(url: str) -> str:
    return re.sub(r'(code|token|access_token|refresh_token)=[^&]+', r'\1=***', url)

def create_session():
    """创建带有代理和浏览器头部的会话"""
    if USE_CLOUDSCRAPER:
        sess = cloudscraper.create_scraper()
    else:
        sess = _req.Session()

    if PROXY_URL:
        # requests 优先支持 socks5h:// 做远程 DNS 解析
        proxy_target = PROXY_URL
        if proxy_target.startswith("socks5://"):
            proxy_target = proxy_target.replace("socks5://", "socks5h://")
        sess.proxies = {"http": proxy_target, "https": proxy_target}
        print(f"[信息] requests 使用代理: {proxy_target}")
    else:
        print("[信息] 直连模式（未配置代理）")

    sess.headers.update(HEADERS_BROWSER)
    return sess

_tg_session = None

def tg_send(title: str, content: str):
    """发送 Telegram 消息（Markdown），带重试机制"""
    global _tg_session
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("[Telegram] 未配置 BOT_TOKEN 或 CHAT_ID，跳过通知。")
        return

    if _tg_session is None:
        _tg_session = _req.Session()
        _tg_session.headers.update({"Content-Type": "application/json"})

    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    text = f"*{title}*\n\n{content}"
    payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True}

    for attempt in range(3):
        try:
            resp = _tg_session.post(url, json=payload, timeout=15)
            result = resp.json()
            if result.get("ok"):
                print(f"[Telegram] 推送成功 | chat_id={mask(TG_CHAT_ID)}")
                return
            print(f"[Telegram] 推送失败: {result.get('description', 'unknown')}")
        except Exception as e:
            print(f"[Telegram] 请求异常 (第 {attempt+1} 次): {e}")
        time.sleep(2)
    print("[Telegram] 推送最终失败（重试耗尽）")

# ─────────────────────────────────────────────────────────────
# 登录核心流程
# ─────────────────────────────────────────────────────────────
def discover_oauth_params(session):
    """动态探测 OptikLink 当前使用的 Discord OAuth 参数"""
    params = {
        "client_id": DISCORD_CLIENT_ID or FALLBACK_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI or FALLBACK_REDIRECT_URI,
        "response_type": "code",
        "scope": FALLBACK_SCOPE,
    }
    print("[A] 探测 OAuth 参数...")

    # 优先方案：直接请求登录动作端点 /login?action=login（直接返回 302 到 Discord）
    for attempt in range(1, 4):
        try:
            print(f"    尝试获取登录重定向 (第 {attempt}/3 次)...")
            r = session.get("https://optiklink.net/login?action=login", timeout=20, allow_redirects=False)
            if r.status_code in (301, 302, 303, 307, 308) and "Location" in r.headers:
                loc = r.headers["Location"]
                print(f"    ✅ 探测到 OAuth 跳转: {mask_url(loc)}")
                qs = parse_qs(urlparse(loc).query)
                for k in ("client_id", "redirect_uri", "scope", "state"):
                    if qs.get(k):
                        params[k] = qs[k][0]
                break
        except Exception as e:
            print(f"    ⚠️ 探测登录端点重定向异常 (第 {attempt} 次): {e}")
            time.sleep(3)
    else:
        # 次选备用：访问 /auth 页面提取
        try:
            print("    尝试从 /auth 页面提取...")
            r = session.get("https://optiklink.net/auth", timeout=20, allow_redirects=True)
            for pat in [r'https?://(?:discordapp|discord)\.com(?:/api)?/oauth2/authorize[^\s\'"<>\\]+']:
                m = re.search(pat, r.text)
                if m:
                    raw_url = m.group(0).replace("&amp;", "&")
                    qs = parse_qs(urlparse(raw_url).query)
                    for k in ("client_id", "redirect_uri", "scope", "state"):
                        if qs.get(k):
                            params[k] = qs[k][0]
                    break
        except Exception as e:
            print(f"    ⚠️ 探测 /auth 异常: {e}")

    # 如果探测出的 client_id 有变化，输出给 GitHub Actions 供自动更新 Secret
    if params.get("client_id") and params["client_id"] != DISCORD_CLIENT_ID:
        new_cid = params["client_id"]
        print(f"    client_id 发现更新: {mask(DISCORD_CLIENT_ID)} → {mask(new_cid)}")
        github_output = os.environ.get("GITHUB_OUTPUT")
        if github_output:
            try:
                with open(github_output, "a") as f:
                    f.write(f"new_client_id={new_cid}\n")
            except Exception:
                pass

    return params

def discord_authorize(session, oauth_params):
    """向 Discord API 发送授权确认，获取登录回调 URL"""
    print("[B] Discord 授权...")
    if not DISCORD_TOKEN:
        raise RuntimeError("未配置 DISCORD_TOKEN，无法完成 Discord 授权！")

    client_id = oauth_params.get("client_id") or FALLBACK_CLIENT_ID
    redirect_uri = oauth_params.get("redirect_uri") or FALLBACK_REDIRECT_URI
    scope = oauth_params.get("scope") or FALLBACK_SCOPE

    print(f"    client_id: {mask(client_id)}")
    print(f"    redirect_uri: {redirect_uri}")

    post_params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
    }
    if "state" in oauth_params:
        post_params["state"] = oauth_params["state"]

    headers = {
        "Authorization": DISCORD_TOKEN,
        "Content-Type": "application/json",
        "User-Agent": HEADERS_BROWSER["User-Agent"],
        "Referer": "https://discord.com/oauth2/authorize?" + urlencode(post_params),
        "X-Super-Properties": "eyJvcyI6IldpbmRvd3MiLCJicm93c2VyIjoiQ2hyb21lIn0=",
    }

    for attempt in range(1, 4):
        try:
            r = session.post(
                "https://discord.com/api/v10/oauth2/authorize",
                params=post_params,
                json={"authorize": True, "permissions": "0"},
                headers=headers,
                timeout=20,
                allow_redirects=False,
            )
            print(f"    Discord 响应状态: {r.status_code}")

            if r.status_code == 200:
                try:
                    data = r.json()
                    if "location" in data:
                        return data["location"]
                except Exception:
                    pass

            if r.status_code in (301, 302, 303, 307, 308) and "Location" in r.headers:
                return r.headers["Location"]

            if r.status_code == 400:
                err_data = r.json() if r.text else {}
                if "captcha_key" in err_data or "captcha_sitekey" in err_data:
                    raise RuntimeError("Discord 授权触发人机验证 (hCaptcha)，代理 IP 被风控，请更换代理节点")
                raise RuntimeError(f"Discord 授权参数错误 (HTTP 400): {r.text[:200]}")
            elif r.status_code == 401:
                raise RuntimeError("Discord Token 无效或已过期，请重新获取 DISCORD_TOKEN")

        except Exception as e:
            if "人机验证" in str(e) or "Token 无效" in str(e):
                raise
            print(f"    ⚠️ Discord 授权请求异常 (第 {attempt}/3 次): {e}")
            if attempt < 3:
                time.sleep(3)
            else:
                raise

    raise RuntimeError(f"Discord 授权最终失败")

# ─────────────────────────────────────────────────────────────
# 浏览器登录 + Quick Verification (数学题 + Turnstile)
# ─────────────────────────────────────────────────────────────
VERIFY_STRONG_RE = re.compile(r'<strong[^>]*>([^<]*)</strong>', re.I)
VERIFY_TEXT_RE = re.compile(r'What\s+is\s+([0-9+\-×*/÷\s]+?)\s*\?', re.I)
MATH_TOKEN_RE = re.compile(r'\d+|[+\-×*/÷xX]')
TURNSTILE_TOKEN_JS = (
    'var e=document.querySelector(\'input[name="cf-turnstile-response"]\');'
    'return e ? String(e.value) : "";'
)
TURNSTILE_MAX_RETRY = 6
TURNSTILE_POLL_STEP = 3
MATH_VALUE_JS = (
    'var e=document.querySelector(\'input[name="math_answer"]\');'
    'return e ? String(e.value) : "";'
)

def set_math_answer(sb, answer):
    """填入数学题答案并读回校验"""
    want = str(answer)
    sb.update_text('input[name="math_answer"]', want)
    got = sb.execute_script(MATH_VALUE_JS)
    if got != want:
        js = (
            'var e=document.querySelector(\'input[name="math_answer"]\');'
            'e.value="' + want + '";'
            'e.dispatchEvent(new Event("input",{bubbles:true}));'
        )
        sb.execute_script(js)
        time.sleep(1)
        got = sb.execute_script(MATH_VALUE_JS)
    print(f"    数学题答案填入确认: {got}")
    if got != want:
        raise RuntimeError(f"数学答案填入失败（期望 {want}，实际 {got!r}）")

def sb_proxy():
    """将代理转换为 Chrome 认识的 socks5:// 格式"""
    if not PROXY_URL:
        return None
    p = PROXY_URL.replace("socks5h://", "socks5://")
    return p

def solve_math(a, op, b):
    a, b = int(a), int(b)
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op in ("*", "×", "x", "X"):
        return a * b
    if op in ("/", "÷"):
        return a // b if b != 0 else 0
    raise ValueError(f"未知运算符: {op}")

def read_math_question(raw_html):
    """提取数学题原文"""
    unescaped = html.unescape(raw_html)
    m = VERIFY_STRONG_RE.search(unescaped) or VERIFY_TEXT_RE.search(re.sub(r'<[^>]+>', ' ', unescaped))
    if not m:
        raise RuntimeError("验证页未找到数学题题目")
    return re.sub(r'\s+', ' ', m.group(1)).strip()

def solve_math_expr(expr):
    """计算算式结果"""
    tokens = MATH_TOKEN_RE.findall(expr)
    if len(tokens) < 3 or not tokens[0].isdigit():
        raise ValueError(f"算式格式不完整: {expr!r}")
    acc = int(tokens[0])
    i = 1
    while i < len(tokens):
        if tokens[i].isdigit() or i + 1 >= len(tokens) or not tokens[i + 1].isdigit():
            raise ValueError(f"算式语法错误: {expr!r}")
        acc = solve_math(acc, tokens[i], int(tokens[i + 1]))
        i += 2
    return acc

def browser_login(callback_url):
    """通过 SeleniumBase 有头浏览器过 Quick Verification 并返回最终页面"""
    try:
        from seleniumbase import SB
    except ImportError:
        raise RuntimeError("缺少 seleniumbase 模块，请执行 pip install seleniumbase 并安装 Chrome")

    print(f"[C] 启动浏览器处理登录与人机验证: {mask_url(callback_url)}")
    kwargs = {"uc": True, "test": True, "locale": "en", "raise_test_failure": True}
    proxy = sb_proxy()
    if proxy:
        print(f"    浏览器代理: {proxy}")
        kwargs["proxy"] = proxy

    with SB(**kwargs) as sb:
        sb.uc_open_with_reconnect(callback_url, reconnect_time=6.0)
        time.sleep(3)

        for attempt in range(1, 4):
            if not sb.is_element_present('input[name="math_answer"]'):
                print("    未检测到验证题目，已直接通过或进入主站。")
                break
            print(f"[C2] 遇到 Quick Verification（第 {attempt} 次）...")

            expr = read_math_question(sb.get_page_source())
            answer = solve_math_expr(expr)
            print(f"    题目: {expr} = {answer}")

            set_math_answer(sb, answer)
            try:
                sb.execute_script("document.querySelector('.cf-turnstile').scrollIntoView({block:'center'})")
            except Exception:
                pass
            time.sleep(1)

            token = ""
            for t in range(1, TURNSTILE_MAX_RETRY + 1):
                try:
                    sb.uc_gui_click_captcha()
                except Exception as e:
                    print(f"    ⚠️ 点击验证码控件提示: {e}")

                for _ in range(8):
                    time.sleep(TURNSTILE_POLL_STEP)
                    token = sb.execute_script(TURNSTILE_TOKEN_JS)
                    if len(token) > 50:
                        break
                if len(token) > 50:
                    break
                print(f"    ⚠️ Turnstile 令牌尚未生成 (轮次 {t}/{TURNSTILE_MAX_RETRY})，重试中...")

            if len(token) <= 50:
                raise RuntimeError("Cloudflare Turnstile 人机验证超时未通过")

            print(f"    ✅ Turnstile 令牌就绪 (长度 {len(token)})，提交中...")
            sb.click('button[type="submit"]')
            time.sleep(4)

        final_url = sb.get_current_url()
        print(f"    验证后最终 URL: {mask_url(final_url)}")
        if '/error/vpn' in final_url:
            raise RuntimeError("OptikLink 拦截：检测到机房/VPN出口 IP (/error/vpn)，请更换干净的住宅节点")
        if '/error/' in final_url:
            raise RuntimeError(f"登录受阻: {final_url}")

        sb.open("https://optiklink.net/")
        sb.wait_for_ready_state_complete()
        time.sleep(2)
        return sb.get_page_source(), sb.get_current_url()

def check_dashboard_html(html_content, final_url):
    """检测 Dashboard 页面信息"""
    info = {
        "logged_in": False,
        "username": "N/A",
        "expire_date": EXPIRE_DATE_RAW or "N/A",
        "running_servers": "N/A"
    }

    if "DASHBOARD" in html_content.upper() and "/error/" not in final_url:
        info["logged_in"] = True
        m = re.search(r'Welcome\s+<[^>]+>([^<]+)</[^>]+>\s+to your Dashboard', html_content, re.I)
        if m:
            info["username"] = m.group(1).strip()
        m2 = re.search(r'(\d+)\s+servers?', html_content, re.I)
        if m2:
            info["running_servers"] = m2.group(1)
        m3 = re.search(r'(\d{2}\.\d{2}\.\d{4})', html_content)
        if m3:
            info["expire_date"] = m3.group(1)
            print(f"    提取到服务到期日: {info['expire_date']}")

    return info

# ─────────────────────────────────────────────────────────────
# 服务器保活（Pterodactyl 面板）
# ─────────────────────────────────────────────────────────────
def panel_headers():
    return {"Authorization": f"Bearer {PANEL_API_KEY}", "Accept": "application/json"}

def get_server_identifier(session):
    if PANEL_SERVER_ID:
        return PANEL_SERVER_ID
    r = session.get(f"{PANEL_URL}/api/client", headers=panel_headers(), timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"获取服务器列表失败 HTTP {r.status_code}")
    servers = r.json().get("data", [])
    if not servers:
        raise RuntimeError("面板中无可用服务器")
    return servers[0]["attributes"]["identifier"]

def get_server_status(session, identifier):
    r = session.get(f"{PANEL_URL}/api/client/servers/{identifier}/resources",
                    headers=panel_headers(), timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"状态查询失败 HTTP {r.status_code}")
    return r.json()["attributes"]["current_state"]

def send_power_action(session, identifier, action):
    r = session.post(f"{PANEL_URL}/api/client/servers/{identifier}/power",
                     headers=panel_headers(), json={"signal": action}, timeout=15)
    if r.status_code not in (200, 204):
        raise RuntimeError(f"电源指令执行失败 HTTP {r.status_code}")

def check_and_start_server(session):
    result = {"skipped": True, "server_id": "", "status_before": "unknown", "status_after": "unknown", "action_taken": "none"}
    if not PANEL_API_KEY:
        return result
    result["skipped"] = False
    try:
        identifier = get_server_identifier(session)
        result["server_id"] = identifier
        status = get_server_status(session, identifier)
        result["status_before"] = status
        if status.lower() == "offline":
            send_power_action(session, identifier, "start")
            result["action_taken"] = "start"
            deadline = time.time() + SERVER_START_WAIT
            while time.time() < deadline:
                time.sleep(5)
                new_status = get_server_status(session, identifier)
                if new_status.lower() in ("starting", "running"):
                    result["status_after"] = new_status
                    break
            else:
                result["status_after"] = get_server_status(session, identifier)
        else:
            result["status_after"] = status
    except Exception as e:
        result["error"] = str(e)
    return result

# ─────────────────────────────────────────────────────────────
# 报告构建
# ─────────────────────────────────────────────────────────────
def build_report(info, server_result, err_detail=""):
    now = datetime.now(timezone.utc)
    status_str = "✅ 签到成功" if info["logged_in"] else "❌ 签到失败"
    days_left = "N/A"
    try:
        expire = datetime.strptime(info["expire_date"], "%d.%m.%Y").replace(tzinfo=timezone.utc)
        days_left = str((expire - now).days)
    except Exception:
        pass

    lines = [
        "## OptikLink 自动签到与保活报告",
        f"**状态**: {status_str}",
        f"**用户名**: `{info['username']}`",
        f"**服务到期**: `{info['expire_date']}`",
        f"**剩余天数**: `{days_left}` 天",
        f"**执行时间**: `{now.strftime('%Y-%m-%d %H:%M:%S')} UTC`",
    ]

    if err_detail:
        lines.append(f"**异常原因**: `{err_detail[:180]}`")

    if not server_result.get("skipped"):
        if "error" in server_result:
            lines.append(f"**服务器保活**: ❌ `{server_result['error'][:100]}`")
        else:
            lines.append(f"**服务器ID**: `{server_result['server_id']}`")
            lines.append(f"**运行状态**: `{server_result['status_before']}` → `{server_result['status_after']}`")
            if server_result['action_taken'] == 'start':
                lines.append("**保活操作**: ▶️ 已发送启动指令")

    return "\n".join(lines)

# ─────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────
def main():
    print("=" * 55)
    print("      OptikLink 自动登录 & 服务器保活 v4.4")
    print("=" * 55)

    session = create_session()
    info = {"logged_in": False, "username": "N/A", "expire_date": EXPIRE_DATE_RAW, "running_servers": "N/A"}
    server_result = {"skipped": True}
    err_detail = ""

    try:
        oauth_params = discover_oauth_params(session)
        callback_url = discord_authorize(session, oauth_params)
        html_page, final_url = browser_login(callback_url)
        print(f"[D] 检查 Dashboard... 最终URL: {mask_url(final_url)}")
        info = check_dashboard_html(html_page, final_url)
        server_result = check_and_start_server(session)

        if not info["logged_in"]:
            raise RuntimeError("登录完成，但未能识别到有效的 Dashboard 页面")

    except Exception as e:
        err_detail = str(e)
        print(f"❌ 运行失败: {err_detail}")
        report = build_report(info, server_result, err_detail)
        tg_send("❌ OptikLink 签到失败", report)
        sys.exit(1)

    print("🎉 运行成功！")
    report = build_report(info, server_result)
    tg_send("✅ OptikLink 签到成功", report)

if __name__ == "__main__":
    main()
