import os
import sys
import time
import json
import tempfile
import subprocess
import re
import shutil
import requests
import threading
import queue
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

# 导入 SM2 加密依赖
try:
    from Utils import pwdEncrypt
    print("✅ 成功加载 SM2 加密依赖")
except ImportError:
    print("❌ 错误: 未找到 Utils.py，请确保同目录下存在该文件")
    sys.exit(1)

# 尝试导入 serverchan3
try:
    from serverchan_sdk import sc_send
    HAS_SERVERCHAN3 = True
except ImportError:
    HAS_SERVERCHAN3 = False

# ======================== 全局变量 ========================
in_summary = False
summary_logs = []

# 代理相关全局状态
GLOBAL_PROXY_DISABLE = False
CONSECUTIVE_PROXY_ACCOUNT_FAILS = 0


def log(msg, show_time=True):
    """带时间戳的日志输出"""
    if show_time:
        full_msg = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    else:
        full_msg = msg
    print(full_msg, flush=True)
    if in_summary:
        summary_logs.append(msg)


# ======================== 代理相关 ========================
def get_valid_proxy(account_proxy_fails):
    """获取可用代理IP"""
    global GLOBAL_PROXY_DISABLE
    if GLOBAL_PROXY_DISABLE:
        return None, account_proxy_fails

    if account_proxy_fails >= 100:
        return None, account_proxy_fails

    api_url = "http://api.dmdaili.com/dmgetip.asp?apikey=b8ea786f&pwd=8c2eb32b847f8f930f2e0cf6a08c45de&getnum=1&httptype=1&geshi=2&fenge=1&fengefu=&operate=all"

    while True:
        if account_proxy_fails >= 100:
            return None, account_proxy_fails

        try:
            resp = requests.get(api_url, timeout=10)
            try:
                data = resp.json()
            except Exception:
                log(f"⚠ 获取代理API返回非JSON原文: {resp.text}")
                account_proxy_fails += 1
                time.sleep(2)
                continue

            if data.get("code") == 605:
                log(f"ℹ 代理API返回: {data.get('msg')}，程序等待15秒后继续获取...")
                time.sleep(15)
                continue
            elif data.get("code") == 1 and "Too Many Requests" in data.get("msg", ""):
                log("ℹ 代理API返回Too Many Requests，等待5秒后继续获取...")
                time.sleep(5)
                continue
            elif data.get("code") == 0 and data.get("data"):
                p_info = data["data"][0]
                ip = p_info.get("ip")
                port = p_info.get("port")
                city = p_info.get("city", "未知位置")
                proxy_str = f"http://{ip}:{port}"
                log(f"🔗 成功获取到代理: {ip}:{port} [位置: {city}]，正在进行可用性测试...")

                try:
                    test_resp = requests.get("https://m.jlc.com", proxies={"http": proxy_str, "https": proxy_str}, timeout=5)
                    if test_resp.status_code == 200:
                        log("✅ 代理测试成功，延迟正常")
                        return proxy_str, account_proxy_fails
                    else:
                        log(f"⚠ 代理测试失败 (HTTP {test_resp.status_code})，重新获取IP...")
                        continue 
                except Exception:
                    log(f"⚠ 代理测试请求超时或连接失败，重新获取IP...")
                    continue 
            else:
                log(f"⚠ 获取代理失败，API返回内容: {data}")
                account_proxy_fails += 1
                time.sleep(2)
                continue

        except Exception as e:
            log(f"⚠ 请求代理API异常: {e}")
            account_proxy_fails += 1
            time.sleep(2)
            continue


# ======================== 浏览器 ========================
def create_chrome_driver(user_data_dir=None):
    """创建 Chrome 浏览器实例（启用性能日志以抓取 secretkey）"""
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option("useAutomationExtension", False)
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-software-rasterizer")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--window-size=1920,1080")
    # 启用性能日志
    chrome_options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    if user_data_dir:
        chrome_options.add_argument(f"--user-data-dir={user_data_dir}")

    driver = webdriver.Chrome(options=chrome_options)
    driver.set_page_load_timeout(60)
    driver.set_script_timeout(60)

    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"},
    )
    return driver


# ======================== 登录相关========================
def call_aliv3min_with_timeout(timeout_seconds=180, max_retries=18):
    """调用 AliV3min.py 获取 captchaTicket"""
    for attempt in range(max_retries):
        log(f"📞 正在调用登录脚本获取 captchaTicket (尝试 {attempt + 1}/{max_retries})...")
        process = None
        try:
            if not os.path.exists("AliV3min.py"):
                log("❌ 错误: 找不到登录依赖 AliV3min.py")
                sys.exit(1)

            process = subprocess.Popen(
                [sys.executable, "AliV3min.py"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="ignore",
            )

            q = queue.Queue()
            def enqueue_output(out, queue_obj):
                try:
                    for line in iter(out.readline, ''):
                        queue_obj.put(line)
                except Exception:
                    pass
                finally:
                    try:
                        out.close()
                    except Exception:
                        pass

            t = threading.Thread(target=enqueue_output, args=(process.stdout, q))
            t.daemon = True
            t.start()

            start_time = time.time()
            wait_for_next_line = False

            while True:
                if time.time() - start_time > timeout_seconds:
                    log(f"⏰ 登录脚本超过 {timeout_seconds} 秒未完成，强制终止...")
                    try:
                        process.kill()
                        process.wait(timeout=5)
                    except Exception:
                        pass
                    break

                try:
                    line = q.get(timeout=0.5)
                except queue.Empty:
                    if process.poll() is not None and not t.is_alive():
                        break
                    continue

                if line:
                    if wait_for_next_line:
                        captcha_ticket = line.strip()
                        log("✅ 成功获取 captchaTicket")
                        try:
                            process.terminate()
                            process.wait(timeout=5)
                        except Exception:
                            pass
                        return captcha_ticket

                    if "SUCCESS: Obtained CaptchaTicket:" in line:
                        wait_for_next_line = True
                        continue

                    if "captchaTicket" in line:
                        match = re.search(r'"captchaTicket"\s*:\s*"([^"]+)"', line)
                        if match:
                            log("✅ 成功获取 captchaTicket")
                            try:
                                process.terminate()
                                process.wait(timeout=5)
                            except Exception:
                                pass
                            return match.group(1)

            # 确保进程终止
            if process and process.poll() is None:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except Exception:
                    pass

            if attempt < max_retries - 1:
                log(f"⚠ 未获取到 CaptchaTicket，等待5秒后第 {attempt + 2} 次重试...")
                time.sleep(5)

        except Exception as e:
            log(f"❌ 调用登录脚本异常: {e}")
            if process and process.poll() is None:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except Exception:
                    pass
            if attempt < max_retries - 1:
                log(f"⚠ 等待5秒后第 {attempt + 2} 次重试...")
                time.sleep(5)

    log("❌ 登录脚本存在异常，无法获取 CaptchaTicket")
    return None


def send_login_request(driver, url, method="POST", body=None):
    """通过浏览器发送登录相关请求"""
    try:
        if body:
            body_str = json.dumps(body, ensure_ascii=False)
            js_code = """
            var url=arguments[0],bodyData=arguments[1],cb=arguments[2];
            fetch(url,{method:'POST',headers:{'Content-Type':'application/json',
            'Accept':'application/json, text/plain, */*','AppId':'JLC_PORTAL_PC',
            'ClientType':'PC-WEB'},body:bodyData,credentials:'include'})
            .then(r=>r.json().then(d=>cb(JSON.stringify(d))))
            .catch(e=>cb(JSON.stringify({error:e.toString()})));
            """
            result = driver.execute_async_script(js_code, url, body_str)
        else:
            js_code = """
            var url=arguments[0],cb=arguments[1];
            fetch(url,{method:'GET',headers:{'Content-Type':'application/json',
            'Accept':'application/json, text/plain, */*'},credentials:'include'})
            .then(r=>r.json().then(d=>cb(JSON.stringify(d))))
            .catch(e=>cb(JSON.stringify({error:e.toString()})));
            """
            result = driver.execute_async_script(js_code, url)
        return json.loads(result) if result else None
    except Exception as e:
        log(f"❌ 登录请求执行失败: {e}")
        return None


def perform_init_session(driver, max_retries=3):
    """初始化 Session"""
    for i in range(max_retries):
        log(f"📡 初始化会话 (尝试 {i + 1}/{max_retries})...")
        resp = send_login_request(
            driver,
            "https://passport.jlc.com/api/cas/login/get-init-session",
            "POST",
            {"appId": "JLC_PORTAL_PC", "clientType": "PC-WEB"},
        )
        if resp and resp.get("success") and resp.get("code") == 200:
            log("✅ 初始化会话成功")
            return True
        log(f"⚠ 初始化会话失败，接口返回: {resp}")
        if i < max_retries - 1:
            time.sleep(2)
    return False


def login_with_password(driver, username, password, captcha_ticket):
    """使用密码登录"""
    try:
        enc_user = pwdEncrypt(username)
        enc_pass = pwdEncrypt(password)
    except Exception as e:
        log(f"❌ SM2 加密失败: {e}")
        return "other_error", None

    body = {
        "username": enc_user,
        "password": enc_pass,
        "isAutoLogin": False,
        "captchaTicket": captcha_ticket,
    }
    log("📡 发送登录请求...")
    resp = send_login_request(
        driver, "https://passport.jlc.com/api/cas/login/with-password", "POST", body
    )
    if not resp:
        return "other_error", None

    if resp.get("success") and resp.get("code") == 2017:
        return "success", resp
    if resp.get("code") == 10208:
        log(f"❌ 账号或密码不正确，接口返回: {resp}")
        return "password_error", resp

    log(f"⚠ 登录返回未知状态，接口返回: {resp}")
    return "other_error", resp


def verify_login_on_member_page(driver, max_retries=3):
    """在 member.jlc.com 验证登录状态"""
    for attempt in range(max_retries):
        log(f"🔍 验证登录状态 ({attempt + 1}/{max_retries})...")
        try:
            try:
                driver.get("https://member.jlc.com/")
            except TimeoutException:
                log("⚠ 验证页面加载超时，停止加载并尝试检查...")
                driver.execute_script("window.stop();")

            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            time.sleep(3)
            page_source = driver.page_source
            if "客编" in page_source or "customerCode" in page_source:
                log("✅ 验证登录成功")
                return True
        except Exception as e:
            log(f"⚠ 验证登录失败: {e}")
        if attempt < max_retries - 1:
            time.sleep(2)
    return False


def perform_login_flow(driver, username, password, max_retries=3):
    """完整登录流程"""
    session_fail_count = 0
    for login_attempt in range(max_retries):
        log(f"🔐 开始登录流程 (尝试 {login_attempt + 1}/{max_retries})...")
        try:
            try:
                driver.get("https://passport.jlc.com")
            except TimeoutException:
                log("⚠ 登录页面加载超时，尝试停止加载继续...")
                driver.execute_script("window.stop();")

            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )

            if not perform_init_session(driver):
                session_fail_count += 1
                if session_fail_count >= 3:
                    log("❌ 浏览器环境存在异常")
                raise Exception("初始化 Session 失败")

            session_fail_count = 0

            captcha_ticket = call_aliv3min_with_timeout()
            if not captcha_ticket:
                raise Exception("获取 CaptchaTicket 失败")

            status, resp = login_with_password(driver, username, password, captcha_ticket)
            if status == "password_error":
                return "password_error"
            if status != "success":
                raise Exception(f"登录失败，状态: {status}")

            if not verify_login_on_member_page(driver):
                raise Exception("登录验证失败")

            log("✅ 登录流程完成")
            return "success"

        except Exception as e:
            log(f"❌ 登录流程异常: {e}")
            if login_attempt < max_retries - 1:
                log("⏳ 等待3秒后重试登录流程...")
                time.sleep(3)
            else:
                log("❌ 登录流程已达最大重试次数")
                return "login_failed"
    return "login_failed"


# ======================== BBS 功能函数 ========================
def extract_secretkey(driver, max_retries=5):
    """从浏览器性能日志中提取 secretkey"""
    for attempt in range(max_retries):
        try:
            logs = driver.get_log("performance")
            for entry in logs:
                try:
                    message = json.loads(entry["message"])
                    msg_method = message.get("message", {}).get("method", "")

                    headers = {}
                    if msg_method == "Network.requestWillBeSent":
                        req = message["message"]["params"]["request"]
                        url = req.get("url", "")
                        if "jlc-bbs.com" in url:
                            headers = req.get("headers", {})
                    elif msg_method == "Network.responseReceived":
                        resp = message["message"]["params"]["response"]
                        url = resp.get("url", "")
                        if "jlc-bbs.com" in url:
                            headers = resp.get("requestHeaders", {})

                    if headers:
                        sk = (
                            headers.get("secretkey")
                            or headers.get("SecretKey")
                            or headers.get("secretKey")
                            or headers.get("SECRETKEY")
                        )
                        if sk:
                            log(f"✅ 成功提取 secretkey: {sk[:20]}...")
                            return sk
                except Exception:
                    continue
        except Exception as e:
            log(f"⚠ 提取 secretkey 异常: {e}")

        if attempt < max_retries - 1:
            log(f"⚠ 未提取到 secretkey，等待3秒后重试 ({attempt + 1}/{max_retries})...")
            time.sleep(3)
            try:
                driver.refresh()
                time.sleep(5)
            except Exception:
                pass
    return None


def send_bbs_request(driver, url, method="POST", body=None, secretkey="", max_retries=3):
    """通过浏览器发送 BBS API 请求（自动携带 cookie）"""
    for attempt in range(max_retries):
        try:
            if method.upper() == "POST":
                if body is not None:
                    body_str = json.dumps(body, ensure_ascii=False)
                    js_code = """
                    var url=arguments[0],bodyData=arguments[1],sk=arguments[2],cb=arguments[3];
                    fetch(url,{method:'POST',headers:{'Content-Type':'application/json','secretkey':sk},
                    body:bodyData,credentials:'include'})
                    .then(function(r){return r.text();})
                    .then(function(d){cb(d);})
                    .catch(function(e){cb(JSON.stringify({error:e.toString()}));});
                    """
                    result = driver.execute_async_script(js_code, url, body_str, secretkey)
                else:
                    js_code = """
                    var url=arguments[0],sk=arguments[1],cb=arguments[2];
                    fetch(url,{method:'POST',headers:{'Content-Type':'application/json','secretkey':sk},
                    credentials:'include'})
                    .then(function(r){return r.text();})
                    .then(function(d){cb(d);})
                    .catch(function(e){cb(JSON.stringify({error:e.toString()}));});
                    """
                    result = driver.execute_async_script(js_code, url, secretkey)
            else:  # GET
                js_code = """
                var url=arguments[0],sk=arguments[1],cb=arguments[2];
                fetch(url,{method:'GET',headers:{'secretkey':sk},credentials:'include'})
                .then(function(r){return r.text();})
                .then(function(d){cb(d);})
                .catch(function(e){cb(JSON.stringify({error:e.toString()}));});
                """
                result = driver.execute_async_script(js_code, url, secretkey)

            if result:
                try:
                    parsed = json.loads(result)
                    return parsed
                except json.JSONDecodeError:
                    log(f"⚠ 接口返回非JSON，原文: {result[:500]}")
            else:
                log("⚠ 接口返回空内容")

        except Exception as e:
            log(f"⚠ 请求执行失败 (尝试 {attempt + 1}/{max_retries}): {e}")

        if attempt < max_retries - 1:
            time.sleep(2)

    return None


def is_bbs_auth_error(resp):
    """检查BBS API响应是否为认证/会话错误"""
    if not resp or not isinstance(resp, dict):
        return False
    code = resp.get("code")
    msg = resp.get("message", "")
    if code == 401:
        return True
    if "客户不存在" in msg or "未登录" in msg or "会话失效" in msg:
        return True
    return False


def validate_and_fix_bbs_session(driver, secretkey, target_url, max_fix_attempts=3):
    """验证BBS会话有效性，如果无效则尝试通过重新触发SSO来修复"""
    test_resp = send_bbs_request(
        driver,
        "https://www.jlc-bbs.com/api/bbs/signInRecordWeb/getSignInfo",
        "POST", None, secretkey, max_retries=1,
    )

    if test_resp and not is_bbs_auth_error(test_resp):
        return secretkey

    if test_resp is None:
        return secretkey

    auth_msg = test_resp.get("message", "未知")
    log(f"⚠ BBS会话无效 ({auth_msg})，尝试重新建立会话...")

    for attempt in range(max_fix_attempts):
        try:
            log(f"🔄 重新建立BBS会话 (尝试 {attempt + 1}/{max_fix_attempts})...")

            try:
                driver.get("https://member.jlc.com/")
            except TimeoutException:
                driver.execute_script("window.stop();")
            time.sleep(3)

            try:
                driver.get("https://www.jlc-bbs.com/")
            except TimeoutException:
                driver.execute_script("window.stop();")
            time.sleep(5)

            try:
                driver.get(target_url)
            except TimeoutException:
                driver.execute_script("window.stop();")
            time.sleep(10)

            new_sk = extract_secretkey(driver)
            if not new_sk:
                log(f"⚠ 重建会话时未能提取 secretkey")
                continue

            test_resp = send_bbs_request(
                driver,
                "https://www.jlc-bbs.com/api/bbs/signInRecordWeb/getSignInfo",
                "POST", None, new_sk, max_retries=1,
            )

            if test_resp and not is_bbs_auth_error(test_resp):
                log("✅ BBS会话已重新建立")
                return new_sk

            if test_resp:
                log(f"⚠ BBS会话仍然无效: {test_resp.get('message', '未知')}")
        except Exception as e:
            log(f"⚠ 重建BBS会话异常: {e}")

    log("❌ 无法建立有效的BBS会话")
    return None


def get_sign_info(driver, secretkey, label="", max_retries=3):
    """获取签到信息（含当前积分）"""
    for attempt in range(max_retries):
        resp = send_bbs_request(
            driver,
            "https://www.jlc-bbs.com/api/bbs/signInRecordWeb/getSignInfo",
            "POST",
            None,
            secretkey,
            max_retries=1,
        )
        if resp:
            if resp.get("success") and resp.get("code") == 200:
                data = resp.get("data", {})
                total_score = data.get("totalScore", 0)
                sign_days = data.get("signInDays", 0)
                continue_days = data.get("signInContinueDays", 0)
                if label:
                    log(f"📊 {label}积分: {total_score} (累计签到{sign_days}天, 连续{continue_days}天)")
                return {"success": True, "totalScore": total_score, "data": data}
            else:
                msg = resp.get("message", "未知错误")
                log(f"⚠ 获取积分信息失败，接口返回: {resp}")
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                return {"success": False, "error": msg, "raw": resp}
        else:
            if attempt < max_retries - 1:
                log(f"⚠ 获取积分信息请求失败，重试中 ({attempt + 1}/{max_retries})...")
                time.sleep(2)

    return {"success": False, "error": "请求失败"}


def do_sign_in(driver, secretkey, proxy_str=None, max_retries=3):
    """执行签到"""
    cookies = {c['name']: c['value'] for c in driver.get_cookies()} if proxy_str else None
    headers = {
        'Content-Type': 'application/json',
        'secretkey': secretkey,
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    } if proxy_str else None
    proxies = {"http": proxy_str, "https": proxy_str} if proxy_str else None

    for attempt in range(max_retries):
        try:
            if proxy_str:
                resp_obj = requests.post(
                    "https://www.jlc-bbs.com/api/bbs/signInRecordWeb/signIn",
                    json={"signInContent": "", "signInExpression": ""},
                    headers=headers,
                    cookies=cookies,
                    proxies=proxies,
                    timeout=15
                )
                resp = resp_obj.json()
            else:
                resp = send_bbs_request(
                    driver,
                    "https://www.jlc-bbs.com/api/bbs/signInRecordWeb/signIn",
                    "POST",
                    {"signInContent": "", "signInExpression": ""},
                    secretkey,
                    max_retries=1,
                )

            if resp:
                if resp.get("success") and resp.get("code") == 200:
                    task_score = resp.get("data", {}).get("taskScore", 0)
                    return {"status": "success", "taskScore": task_score}
                elif resp.get("message") and "已经签到" in resp.get("message", ""):
                    return {"status": "already_signed", "message": resp.get("message")}
                else:
                    msg = resp.get("message", "未知错误")
                    log(f"⚠ 签到失败，接口返回: {resp}")
                    if attempt < max_retries - 1:
                        time.sleep(2)
                        continue
                    return {"status": "failed", "error": msg, "raw": resp}
            else:
                if attempt < max_retries - 1:
                    log(f"⚠ 签到请求失败，重试中 ({attempt + 1}/{max_retries})...")
                    time.sleep(2)
        except Exception as e:
            log(f"⚠ 签到异常: {e}")
            if attempt < max_retries - 1:
                time.sleep(2)

    return {"status": "failed", "error": "请求失败"}


def get_remaining_lottery_times(driver, max_retries=3):
    """从前端页面提取剩余抽奖次数"""
    for attempt in range(max_retries):
        try:
            page_source = driver.page_source
            # 匹配 "今日可抽奖次数：" 后面的数字
            match = re.search(r"今日可抽奖次数：\s*</span>\s*(\d+)\s*次", page_source)
            if match:
                times = int(match.group(1))
                log(f"🎰 剩余抽奖次数: {times}")
                return {"success": True, "times": times}
            # 尝试更宽松的匹配
            match2 = re.search(r"今日可抽奖次数[：:]\s*(\d+)\s*次", page_source)
            if match2:
                times = int(match2.group(1))
                log(f"🎰 剩余抽奖次数: {times}")
                return {"success": True, "times": times}
            # 尝试更宽松的匹配（纯文本）
            text = driver.find_element(By.TAG_NAME, "body").text
            match3 = re.search(r"今日可抽奖次数[：:]\s*(\d+)\s*次", text)
            if match3:
                times = int(match3.group(1))
                log(f"🎰 剩余抽奖次数: {times}")
                return {"success": True, "times": times}
        except Exception as e:
            log(f"⚠ 获取抽奖次数异常: {e}")

        if attempt < max_retries - 1:
            log(f"⚠ 未能获取抽奖次数，等待3秒后重试 ({attempt + 1}/{max_retries})...")
            time.sleep(3)
            try:
                driver.refresh()
                time.sleep(5)
            except Exception:
                pass

    log("⚠ 无法从页面获取剩余抽奖次数")
    return {"success": False, "error": "无法从页面提取抽奖次数"}


def do_lottery(driver, secretkey, proxy_str=None):
    """执行单次抽奖"""
    cookies = {c['name']: c['value'] for c in driver.get_cookies()} if proxy_str else None
    headers = {
        'Content-Type': 'application/json',
        'secretkey': secretkey,
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    } if proxy_str else None
    proxies = {"http": proxy_str, "https": proxy_str} if proxy_str else None

    for attempt in range(2):
        try:
            if proxy_str:
                resp_obj = requests.post(
                    "https://www.jlc-bbs.com/api/bbs/luckyDrawActivityRecord/executeLuckDraw",
                    json={"luckyDrawActivityAccessId": "ab69ff00332949328ba578c086d42141"},
                    headers=headers,
                    cookies=cookies,
                    proxies=proxies,
                    timeout=15
                )
                resp = resp_obj.json()
            else:
                resp = send_bbs_request(
                    driver,
                    "https://www.jlc-bbs.com/api/bbs/luckyDrawActivityRecord/executeLuckDraw",
                    "POST",
                    {"luckyDrawActivityAccessId": "ab69ff00332949328ba578c086d42141"},
                    secretkey,
                    max_retries=1,
                )
            if resp:
                if resp.get("success") and resp.get("code") == 200:
                    name = resp.get("data", {}).get("name", "未知奖品")
                    return {"status": "success", "name": name, "data": resp.get("data", {})}
                elif resp.get("message") and "次数" in resp.get("message", ""):
                    return {"status": "no_times", "message": resp.get("message")}
                elif resp.get("message") and "积分" in resp.get("message", ""):
                    return {"status": "no_points", "message": resp.get("message")}
                else:
                    log(f"⚠ 抽奖返回异常，接口返回: {resp}")
                    return {"status": "failed", "error": resp.get("message", "未知错误"), "raw": resp}
        except Exception as e:
            log(f"⚠ 抽奖异常: {e}")
    return {"status": "failed", "error": "请求失败"}


def get_koi_cards(driver, secretkey, max_retries=3):
    """获取鲤鱼卡数量"""
    for attempt in range(max_retries):
        timestamp = int(time.time() * 1000)
        url = f"https://www.jlc-bbs.com/api/bbs/prizeOrder/getPrizeCard?_t={timestamp}"
        resp = send_bbs_request(driver, url, "GET", None, secretkey, max_retries=1)
        if resp:
            if resp.get("success") and resp.get("code") == 200:
                count = resp.get("data", 0)
                return {"success": True, "count": count}
            else:
                log(f"⚠ 获取鲤鱼卡失败，接口返回: {resp}")
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                return {"success": False, "error": resp.get("message", "未知错误"), "raw": resp}
        else:
            if attempt < max_retries - 1:
                log(f"⚠ 获取鲤鱼卡请求失败，重试中 ({attempt + 1}/{max_retries})...")
                time.sleep(2)

    return {"success": False, "error": "请求失败"}


# ======================== 单账号处理 ========================
def process_single_account(username, password, account_index, total_accounts, start_pwd_idx=0):
    """处理单个账号的完整流程，包含密码重试及断点记忆"""
    global CONSECUTIVE_PROXY_ACCOUNT_FAILS, GLOBAL_PROXY_DISABLE
    backup_passwords = [
        "Aa123123",
        "134613461346zzY"
    ]
    
    all_passwords = [password]
    for bp in backup_passwords:
        if bp != password:
            all_passwords.append(bp)

    result = {
        "account_index": account_index,
        "password_error": False,
        "login_error": False,
        "has_error": False,
        "error_msg": None,
        "last_pwd_idx": start_pwd_idx,
        # 签到
        "sign_before_points": None,
        "sign_after_points": None,
        "sign_status": None,       # success / already_signed / failed
        "sign_points_gained": None,
        "sign_error_msg": None,
        # 抽奖
        "lottery_before_points": None,
        "lottery_after_points": None,
        "lottery_status": None,     # success / skipped / failed
        "lottery_skip_reason": None,
        "lottery_prizes": [],
        "lottery_error_msg": None,
        # 最终
        "final_points": None,
        "final_points_error": None,
        # 鲤鱼卡
        "koi_cards": None,
        "koi_cards_error": None,
    }

    current_pwd_idx = start_pwd_idx
    account_proxy_fails = 0
    proxy_used_successfully = False

    while current_pwd_idx < len(all_passwords):
        current_password = all_passwords[current_pwd_idx]
        result["last_pwd_idx"] = current_pwd_idx
        
        driver = None
        user_data_dir = tempfile.mkdtemp()

        try:
            log(f"🌐 启动浏览器 (账号 {account_index}/{total_accounts} - 尝试密码 {current_pwd_idx + 1}/{len(all_passwords)})...")
            driver = create_chrome_driver(user_data_dir)

            # ============ 登录阶段 ============
            login_status = perform_login_flow(driver, username, current_password, max_retries=3)

            if login_status == "password_error":
                log(f"❌ 密码错误: {current_password}，尝试下一个备用密码...")
                current_pwd_idx += 1
                continue

            if login_status != "success":
                result["login_error"] = True
                result["has_error"] = True
                result["error_msg"] = "登录失败"
                return result

            # ============ 签到阶段 ============
            log("📄 打开签到页面...")
            try:
                driver.get("https://www.jlc-bbs.com/platform/sign")
            except TimeoutException:
                log("⚠ 签到页面加载超时，停止加载继续...")
                driver.execute_script("window.stop();")

            log("⏳ 等待10秒让页面完全加载...")
            time.sleep(10)

            # 提取 secretkey
            secretkey = extract_secretkey(driver)
            if not secretkey:
                log("❌ 无法提取 secretkey，此账号流程异常")
                result["has_error"] = True
                result["error_msg"] = "secretkey 提取失败"
                return result

            # 验证BBS会话有效性，无效则尝试通过SSO重新建立
            secretkey = validate_and_fix_bbs_session(
                driver, secretkey, "https://www.jlc-bbs.com/platform/sign"
            )
            if not secretkey:
                log("❌ BBS会话无法建立，此账号流程异常")
                result["has_error"] = True
                result["error_msg"] = "BBS会话无效"
                return result

            # 1. 获取签到前积分
            log("📡 获取签到前积分...")
            info_before = get_sign_info(driver, secretkey, label="签到前")
            if info_before.get("success"):
                result["sign_before_points"] = info_before["totalScore"]
            else:
                log(f"⚠ 获取签到前积分失败: {info_before.get('error', '未知')}")

            # 2. 执行签到
            log("📡 准备执行签到...")
            sign_proxy_str, account_proxy_fails = get_valid_proxy(account_proxy_fails)
            if sign_proxy_str:
                proxy_used_successfully = True

            sign_result = do_sign_in(driver, secretkey, sign_proxy_str)
            result["sign_status"] = sign_result["status"]

            if sign_result["status"] == "success":
                result["sign_points_gained"] = sign_result["taskScore"]
                log(f"✅ 签到成功，获得 {sign_result['taskScore']} 积分")
            elif sign_result["status"] == "already_signed":
                log(f"ℹ {sign_result.get('message', '今天已经签到过了')}")
            else:
                result["sign_error_msg"] = sign_result.get("error", "未知原因")
                result["has_error"] = True
                log(f"❌ 签到失败: {result['sign_error_msg']}")

            # 3. 获取签到后积分
            log("📡 获取签到后积分...")
            info_after = get_sign_info(driver, secretkey, label="签到后")
            if info_after.get("success"):
                result["sign_after_points"] = info_after["totalScore"]
            else:
                log(f"⚠ 获取签到后积分失败: {info_after.get('error', '未知')}")

            # ============ 抽奖阶段 ============
            log("📄 打开抽奖页面...")
            try:
                driver.get(
                    "https://www.jlc-bbs.com/platform/points-paradise"
                    "?type=index&id=ab69ff00332949328ba578c086d42141"
                )
            except TimeoutException:
                log("⚠ 抽奖页面加载超时，停止加载继续...")
                driver.execute_script("window.stop();")

            log("⏳ 等待10秒让页面完全加载...")
            time.sleep(10)

            # 刷新 secretkey
            new_sk = extract_secretkey(driver)
            if new_sk:
                secretkey = new_sk

            # 检查当前积分
            log("📡 检查当前积分...")
            points_info = get_sign_info(driver, secretkey, label="当前")
            current_points = 0
            if points_info.get("success"):
                current_points = points_info["totalScore"]
                result["lottery_before_points"] = current_points
            else:
                log(f"⚠ 获取当前积分失败: {points_info.get('error', '未知')}")
                # 尝试使用签到后积分作为备选
                if result["sign_after_points"] is not None:
                    current_points = result["sign_after_points"]
                    result["lottery_before_points"] = current_points
                    log(f"ℹ 使用签到后积分作为参考: {current_points}")

            # 检查剩余抽奖次数
            times_info = get_remaining_lottery_times(driver)
            remaining_times = 0
            if times_info.get("success"):
                remaining_times = times_info["times"]
            else:
                log(f"⚠ 获取抽奖次数失败: {times_info.get('error', '未知')}")

            # 判断是否抽奖
            if remaining_times == 0:
                result["lottery_status"] = "skipped"
                result["lottery_skip_reason"] = "抽奖次数为0"
                log("ℹ 抽奖次数为0，跳过抽奖")
            elif current_points < 10:
                result["lottery_status"] = "skipped"
                result["lottery_skip_reason"] = f"积分不足10（当前{current_points}）"
                log(f"ℹ 积分不足10（当前{current_points}），跳过抽奖")
            else:
                # 执行抽奖循环
                log("🎰 开始准备抽奖...")
                lot_proxy_str, account_proxy_fails = get_valid_proxy(account_proxy_fails)
                if lot_proxy_str:
                    proxy_used_successfully = True

                result["lottery_status"] = "success"
                lottery_count = 0

                while True:
                    lottery_result = do_lottery(driver, secretkey, lot_proxy_str)

                    if lottery_result["status"] == "success":
                        lottery_count += 1
                        prize_name = lottery_result["name"]
                        result["lottery_prizes"].append(prize_name)
                        log(f"🎉 抽奖{lottery_count}: {prize_name}")
                        time.sleep(1)
                    elif lottery_result["status"] == "no_times":
                        log(f"ℹ {lottery_result.get('message', '抽奖次数已用完')}")
                        break
                    elif lottery_result["status"] == "no_points":
                        log(f"ℹ {lottery_result.get('message', '积分不足')}")
                        break
                    else:
                        result["lottery_error_msg"] = lottery_result.get("error", "未知原因")
                        result["has_error"] = True
                        log(f"❌ 抽奖失败: {result['lottery_error_msg']}")
                        break

                if lottery_count > 0:
                    log(f"🎰 共完成 {lottery_count} 次抽奖")

            # 获取抽奖后积分
            log("📡 获取最终积分...")
            final_info = get_sign_info(driver, secretkey, label="最终")
            if final_info.get("success"):
                result["final_points"] = final_info["totalScore"]
                result["lottery_after_points"] = final_info["totalScore"]
            else:
                result["final_points_error"] = final_info.get("error", "未知")
                log(f"⚠ 获取最终积分失败: {result['final_points_error']}")
                # 尝试使用之前的积分信息
                if result["sign_after_points"] is not None and not result["lottery_prizes"]:
                    result["final_points"] = result["sign_after_points"]

            # ============ 鲤鱼卡 ============
            log("📡 检查鲤鱼卡数量...")
            koi_result = get_koi_cards(driver, secretkey)
            if koi_result.get("success"):
                result["koi_cards"] = koi_result["count"]
                log(f"🐟 鲤鱼卡数量: {result['koi_cards']}")
            else:
                result["koi_cards_error"] = koi_result.get("error", "未知")
                log(f"⚠ 获取鲤鱼卡数量失败: {result['koi_cards_error']}")

            log(f"✅ 账号 {account_index} 处理完成")
            return result

        except Exception as e:
            log(f"❌ 账号 {account_index} 处理过程中发生异常: {e}")
            result["has_error"] = True
            result["error_msg"] = str(e)
            return result
            
        finally:
            if account_proxy_fails >= 100 and not proxy_used_successfully:
                CONSECUTIVE_PROXY_ACCOUNT_FAILS += 1
                log(f"⚠ 该账号未能成功挂上任何可用代理，当前连续获取代理失败账号数: {CONSECUTIVE_PROXY_ACCOUNT_FAILS}")
                if CONSECUTIVE_PROXY_ACCOUNT_FAILS >= 5 and not GLOBAL_PROXY_DISABLE:
                    GLOBAL_PROXY_DISABLE = True
                    log("❌ 连续5个账号获取代理失败，已触发保护机制，后续所有账号将全部放弃代理使用本地IP")
            else:
                if proxy_used_successfully:
                    CONSECUTIVE_PROXY_ACCOUNT_FAILS = 0
                    
            if driver:
                try:
                    driver.quit()
                    log(f"🔒 浏览器已关闭 (账号 {account_index})")
                except Exception:
                    pass
            if os.path.exists(user_data_dir):
                try:
                    shutil.rmtree(user_data_dir, ignore_errors=True)
                except Exception:
                    pass

    # 如果所有候选密码均验证失败
    result["password_error"] = True
    result["has_error"] = True
    result["error_msg"] = "所有候选密码均验证失败"
    log(f"❌ 账号 {account_index} 所有候选密码均提示错误，跳过此账号")
    return result

def process_account_with_retry(username, password, account_index, total_accounts, max_retries=2):
    """带重试的账号处理"""
    last_pwd_idx = 0
    for attempt in range(max_retries + 1):
        if attempt > 0:
            log(f"🔄 账号 {account_index} 第 {attempt} 次重试...")
            time.sleep(5)

        result = process_single_account(username, password, account_index, total_accounts, start_pwd_idx=last_pwd_idx)
        
        if "last_pwd_idx" in result:
            last_pwd_idx = result["last_pwd_idx"]

        # 密码错误不重试 (所有备用密码均试过)
        if result.get("password_error"):
            return result

        # 没有错误就返回
        if not result.get("has_error"):
            return result

        # 如果还有重试机会
        if attempt < max_retries:
            log(f"⚠ 账号 {account_index} 执行异常，准备重试 (原因: {result.get('error_msg', '未知')})")
        else:
            log(f"❌ 账号 {account_index} 重试 {max_retries} 次后仍然失败")

    return result

# ======================== 推送相关========================
def push_summary(push_text, title=None):
    """推送总结日志到各平台"""
    if not push_text:
        return

    if title is None:
        title = "嘉立创BBS签到&抽奖总结"
    full_text = f"{title}\n{push_text}"
    pushed_any = False

    # Telegram
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN")
    tg_chat = os.getenv("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat:
        try:
            url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
            resp = requests.get(url, params={"chat_id": tg_chat, "text": full_text}, timeout=15)
            if resp.status_code == 200 and resp.json().get("ok"):
                log("Telegram-日志已推送")
            else:
                log(f"Telegram-推送失败，返回原文: {resp.text}")
        except Exception as e:
            log(f"Telegram-推送异常: {e}")
        pushed_any = True

    # 企业微信
    wechat_key = os.getenv("WECHAT_WEBHOOK_KEY")
    if wechat_key:
        try:
            wechat_url = wechat_key if wechat_key.startswith("https://") else f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={wechat_key}"
            resp = requests.post(wechat_url, json={"msgtype": "text", "text": {"content": full_text}}, timeout=15)
            # 检查状态码
            if resp.status_code != 200:
                log(f"企业微信-推送失败 (HTTP {resp.status_code}): {resp.text}")
            else:
                # 解析 JSON
                try:
                    resp_json = resp.json()
                    errcode = resp_json.get("errcode")
                    if errcode == 0:
                        log("企业微信-日志已推送")
                    else:
                        errmsg = resp_json.get("errmsg", "未知错误")
                        log(f"企业微信-推送失败 (errcode={errcode}, errmsg={errmsg})")
                except Exception as e:
                    log(f"企业微信-推送响应解析失败: {e}, 原始响应: {resp.text}")
        except Exception as e:
            log(f"企业微信-推送异常: {e}")
        pushed_any = True

    # 钉钉
    dingtalk = os.getenv("DINGTALK_WEBHOOK")
    if dingtalk:
        try:
            dd_url = dingtalk if dingtalk.startswith("https://") else f"https://oapi.dingtalk.com/robot/send?access_token={dingtalk}"
            resp = requests.post(dd_url, json={"msgtype": "text", "text": {"content": full_text}}, timeout=15)
            if resp.status_code != 200:
                log(f"钉钉-推送失败 (HTTP {resp.status_code}): {resp.text}")
            else:
                try:
                    resp_json = resp.json()
                    errcode = resp_json.get("errcode")
                    if errcode == 0:
                        log("钉钉-日志已推送")
                    else:
                        errmsg = resp_json.get("errmsg", "未知错误")
                        log(f"钉钉-推送失败 (errcode={errcode}, errmsg={errmsg})")
                except Exception as e:
                    log(f"钉钉-推送响应解析失败: {e}, 原始响应: {resp.text}")
        except Exception as e:
            log(f"钉钉-推送异常: {e}")
        pushed_any = True

    # PushPlus
    pp_token = os.getenv("PUSHPLUS_TOKEN")
    if pp_token:
        try:
            resp = requests.post("http://www.pushplus.plus/send", json={"token": pp_token, "title": title, "content": push_text}, timeout=15)
            if resp.status_code == 200:
                log("PushPlus-日志已推送")
            else:
                log(f"PushPlus-推送失败，返回原文: {resp.text}")
        except Exception as e:
            log(f"PushPlus-推送异常: {e}")
        pushed_any = True

    # Server酱
    sc_key = os.getenv("SERVERCHAN_SCKEY")
    if sc_key:
        try:
            resp = requests.post(f"https://sctapi.ftqq.com/{sc_key}.send", data={"title": title, "desp": push_text}, timeout=15)
            if resp.status_code == 200:
                log("Server酱-日志已推送")
            else:
                log(f"Server酱-推送失败，返回原文: {resp.text}")
        except Exception as e:
            log(f"Server酱-推送异常: {e}")
        pushed_any = True

    # Server酱3
    sc3_key = os.getenv("SERVERCHAN3_SCKEY")
    if sc3_key and HAS_SERVERCHAN3:
        try:
            resp = sc_send(sc3_key, title, push_text, {"tags": "嘉立创|BBS签到"})
            if resp.get("code") == 0:
                log("Server酱3-日志已推送")
            else:
                log(f"Server酱3-推送失败，返回原文: {resp}")
        except Exception as e:
            log(f"Server酱3-推送异常: {e}")
        pushed_any = True

    # 酷推
    cp_skey = os.getenv("COOLPUSH_SKEY")
    if cp_skey:
        try:
            resp = requests.get(f"https://push.xuthus.cc/send/{cp_skey}", params={"c": full_text}, timeout=15)
            if resp.status_code == 200:
                log("酷推-日志已推送")
            else:
                log(f"酷推-推送失败，返回原文: {resp.text}")
        except Exception as e:
            log(f"酷推-推送异常: {e}")
        pushed_any = True

    # 自定义 API
    custom = os.getenv("CUSTOM_WEBHOOK")
    if custom:
        try:
            resp = requests.post(custom, json={"title": title, "content": push_text}, timeout=15)
            if resp.status_code == 200:
                log("自定义API-日志已推送")
            else:
                log(f"自定义API-推送失败，返回原文: {resp.text}")
        except Exception as e:
            log(f"自定义API-推送异常: {e}")
        pushed_any = True

    if not pushed_any:
        log("ℹ 未配置任何推送链接，跳过实际推送")

def has_any_push_config():
    """检查是否配置了任何推送渠道"""
    keys = [
        "TELEGRAM_BOT_TOKEN", "WECHAT_WEBHOOK_KEY", "DINGTALK_WEBHOOK",
        "PUSHPLUS_TOKEN", "SERVERCHAN_SCKEY", "SERVERCHAN3_SCKEY",
        "COOLPUSH_SKEY", "CUSTOM_WEBHOOK",
    ]
    return any(os.getenv(k) for k in keys)

# ======================== 主函数 ========================
def main():
    global in_summary

    if len(sys.argv) < 3:
        print("用法: python bbs_sign.py 账号1,账号2... 密码1,密码2... [失败退出标志] [账号组编号]")
        print("示例: python bbs_sign.py user1,user2 pwd1,pwd2")
        print("示例: python bbs_sign.py user1,user2 pwd1,pwd2 true")
        print("示例: python bbs_sign.py user1,user2 pwd1,pwd2 true 4")
        print("账号组编号: 只能输入数字，输入其他值则忽略")
        sys.exit(1)

    usernames = [u.strip() for u in sys.argv[1].split(",") if u.strip()]
    passwords = [p.strip() for p in sys.argv[2].split(",") if p.strip()]

    fail_exit = False
    if len(sys.argv) >= 4:
        fail_exit = sys.argv[3].lower() == "true"

    # 解析第4个参数（账号组编号），只接受纯数字，其他值忽略
    account_group = None
    if len(sys.argv) >= 5:
        if sys.argv[4].isdigit():
            account_group = sys.argv[4]

    if len(usernames) != len(passwords):
        log("❌ 错误: 账号和密码数量不匹配!")
        sys.exit(1)

    total = len(usernames)
    log(f"检测到 {total} 个账号需要处理，失败退出功能已{'开启' if fail_exit else '关闭'}", show_time=False)

    all_results = []

    for i, (username, password) in enumerate(zip(usernames, passwords), 1):
        log(f"\n{'='*50}", show_time=False)
        log(f"开始处理账号 {i}/{total}", show_time=False)
        log(f"{'='*50}", show_time=False)

        result = process_account_with_retry(username, password, i, total, max_retries=2)
        all_results.append(result)

        if i < total:
            log("⏳ 等待5秒后处理下一个账号...")
            time.sleep(5)

    # ======================== 总结输出 ========================

    log("", show_time=False)
    log("=" * 60, show_time=False)
    if account_group is not None:
        log(f"📊嘉立创BBS签到 & 抽奖 账号组{account_group}结果总结", show_time=False)
    else:
        log("📊 嘉立创BBS签到 & 抽奖 结果总结", show_time=False)
    log("=" * 60, show_time=False)

    push_reasons = []
    any_error = False

    for res in all_results:
        idx = res["account_index"]
        log("--------------------------------------------------", show_time=False)
        log(f"账号{idx}:", show_time=False)

        # === 密码错误 ===
        if res.get("password_error"):
            log("├── 状态: ❌ 账号或密码错误，已跳过", show_time=False)
            any_error = True
            push_reasons.append(f"账号{idx}密码错误")
            log("--------------------------------------------------", show_time=False)
            continue

        # === 登录失败 ===
        if res.get("login_error"):
            log(f"├── 状态: ❌ 登录失败 ({res.get('error_msg', '未知')})", show_time=False)
            any_error = True
            push_reasons.append(f"账号{idx}登录异常")
            log("--------------------------------------------------", show_time=False)
            continue

        # === 签到积分变化 ===
        sign_status = res.get("sign_status")
        before_p = res.get("sign_before_points")
        after_p = res.get("sign_after_points")

        if sign_status == "success":
            if before_p is not None and after_p is not None:
                diff = after_p - before_p
                sign_str = f"{before_p} → {after_p} (+{diff})"
            elif res.get("sign_points_gained") is not None:
                sign_str = f"签到成功 (+{res['sign_points_gained']})"
            else:
                sign_str = "签到成功"
        elif sign_status == "already_signed":
            sign_str = "已签到过"
        elif sign_status == "failed":
            sign_str = f"签到失败，原因: {res.get('sign_error_msg', '未知')}"
            any_error = True
            push_reasons.append(f"账号{idx}签到失败")
        elif res.get("has_error") and res.get("error_msg"):
            sign_str = f"运行异常: {res.get('error_msg')}"
            any_error = True
            push_reasons.append(f"账号{idx}运行失败")
        else:
            sign_str = "未执行"
            if res.get("has_error"):
                any_error = True
                push_reasons.append(f"账号{idx}运行失败")

        log(f"├── 签到积分变化: {sign_str}", show_time=False)

        # === 抽奖积分变化 ===
        lottery_status = res.get("lottery_status")
        lot_before = res.get("lottery_before_points")
        lot_after = res.get("lottery_after_points")

        if lottery_status == "success":
            if lot_before is not None and lot_after is not None:
                diff = lot_after - lot_before
                lottery_str = f"{lot_before} → {lot_after} ({diff})"
            else:
                lottery_str = "抽奖完成"
        elif lottery_status == "skipped":
            lottery_str = f"未抽奖，原因: {res.get('lottery_skip_reason', '未知')}"
        elif lottery_status == "failed":
            lottery_str = f"抽奖失败，原因: {res.get('lottery_error_msg', '未知')}"
            # 抽奖失败如果不是积分不足/次数用尽，算异常
            err_msg = res.get("lottery_error_msg", "")
            if "积分" not in err_msg and "次数" not in err_msg:
                any_error = True
                push_reasons.append(f"账号{idx}抽奖异常")
        else:
            lottery_str = "未执行"

        log(f"├── 抽奖积分变化: {lottery_str}", show_time=False)

        # === 最终积分 ===
        final_p = res.get("final_points")
        if final_p is not None:
            log(f"├── 最终积分: {final_p}", show_time=False)
        else:
            err = res.get("final_points_error", "未知")
            log(f"├── 最终积分: 获取失败，原因: {err}", show_time=False)

        # === 鲤鱼卡 ===
        koi = res.get("koi_cards")
        if koi is not None:
            log(f"├── 鲤鱼卡数量: {koi}", show_time=False)
        else:
            err = res.get("koi_cards_error", "未知")
            log(f"├── 鲤鱼卡数量: 获取失败，原因: {err}", show_time=False)

        # === 抽奖奖品 ===
        for pi, prize in enumerate(res.get("lottery_prizes", []), 1):
            log(f"├── 抽奖{pi}奖品: {prize}", show_time=False)
            # 检查是否中了非积分且非鲤鱼卡的奖品
            if "积分" not in prize and "鲤鱼卡" not in prize:
                push_reasons.append(f"账号{idx}中奖{prize}")

        log("--------------------------------------------------", show_time=False)

    # === 整体异常判断（处理 has_error 但前面可能未捕获的情况）===
    for res in all_results:
        idx = res["account_index"]
        if res.get("has_error") and not res.get("password_error") and not res.get("login_error"):
            reason_str = f"账号{idx}运行失败"
            if reason_str not in push_reasons and f"账号{idx}签到失败" not in push_reasons and f"账号{idx}抽奖异常" not in push_reasons:
                any_error = True
                push_reasons.append(reason_str)

    # === 推送决策 ===
    # 去重
    push_reasons = list(dict.fromkeys(push_reasons))
    should_push = len(push_reasons) > 0

    if should_push:
        reason_text = "/".join(push_reasons)
        in_summary = True  # 启用总结收集（推送内容从此处开始）
        if account_group is not None:
            log(f"账号组{account_group}:本次运行推送，推送原因: {reason_text}", show_time=False)
        else:
            log(f"本次运行推送，推送原因: {reason_text}", show_time=False)

        push_text = "\n".join(summary_logs)
        # 确定推送标题
        if account_group is not None:
            push_title = f"📊嘉立创BBS签到 & 抽奖 账号组{account_group}结果总结"
        else:
            push_title = "嘉立创BBS签到&抽奖总结"
        if has_any_push_config():
            push_summary(push_text, push_title)
        else:
            log("ℹ 未配置任何推送链接，跳过实际推送", show_time=False)
    else:
        log("本次运行不推送，无推送条件命中", show_time=False)

    in_summary = False

    # === 退出码 ===
    has_any_account_error = any(
        r.get("has_error") for r in all_results
    )

    if fail_exit and has_any_account_error:
        log("❌ 由于失败退出功能已开启且有账号异常，返回退出码 1")
        sys.exit(1)
    else:
        if fail_exit:
            log("✅ 所有账号执行完成，无异常，程序正常退出")
        else:
            log("✅ 程序正常退出")
        sys.exit(0)

if __name__ == "__main__":
    main()
