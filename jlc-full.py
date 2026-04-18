import os
import sys
import time
import json
import tempfile
import random
import requests
import io
import platform
import multiprocessing
import shutil
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver import ActionChains
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from serverchan_sdk import sc_send

# 修复 Python 3.7 在 CI 环境下的 platform Bug
try:
    platform.system()
except TypeError:
    print("⚠ 检测到 Python 3.7 platform Bug，正在应用补丁...")
    platform.system = lambda: 'Linux'

# 带重试机制的 AliV3 导入逻辑
AliV3 = None
max_import_retries = 5
for attempt in range(max_import_retries):
    try:
        from AliV3 import AliV3
        print("✅ 成功加载 AliV3 登录依赖")
        break
    except ImportError:
        print("❌ 错误: 未找到 登录依赖(AliV3.py) 文件，请确保同目录下存在该文件")
        sys.exit(1)
    except Exception as e:
        print(f"⚠ 导入 AliV3 失败 (尝试 {attempt + 1}/{max_import_retries}): {e}")
        if attempt < max_import_retries - 1:
            wait_time = random.randint(3, 6)
            print(f"⏳ 网络可能不稳定，等待 {wait_time} 秒后重试导入...")
            time.sleep(wait_time)
        else:
            print("❌ 无法导入 AliV3，可能是网络问题导致其初始化失败，程序退出。")
            sys.exit(1)

# 全局变量用于收集总结日志
in_summary = False
summary_logs = []

# 全局连续失败状态控制
consecutive_oshwhub_fails = 0
skip_oshwhub_signin = False

consecutive_jindou_fails = 0
skip_jindou_signin = False

consecutive_proxy_fails = 0
disable_global_proxy = False

def log(msg):
    full_msg = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(full_msg, flush=True)
    if in_summary:
        summary_logs.append(msg)  # 只收集纯消息，无时间戳

def format_nickname(nickname):
    """格式化昵称，只显示第一个字和最后一个字，中间用星号代替"""
    if not nickname or len(nickname.strip()) == 0:
        return "未知用户"
    
    nickname = nickname.strip()
    if len(nickname) == 1:
        return f"{nickname}*"
    elif len(nickname) == 2:
        return f"{nickname[0]}*"
    else:
        return f"{nickname[0]}{'*' * (len(nickname)-2)}{nickname[-1]}"

def desensitize_password(pwd):
    """脱敏密码显示"""
    if len(pwd) <= 3:
        return pwd
    return pwd[:3] + '*****'

def with_retry(func, max_retries=5, delay=1):
    """如果函数返回None或抛出异常，静默重试"""
    def wrapper(*args, **kwargs):
        for attempt in range(max_retries):
            try:
                result = func(*args, **kwargs)
                if result is not None:
                    return result
                time.sleep(delay + random.uniform(0, 1))  # 随机延迟
            except Exception:
                time.sleep(delay + random.uniform(0, 1))  # 随机延迟
        return None
    return wrapper

@with_retry
def extract_token_from_local_storage(driver):
    """从 localStorage 提取 X-JLC-AccessToken"""
    try:
        token = driver.execute_script("return window.localStorage.getItem('X-JLC-AccessToken');")
        if token:
            log(f"✅ 成功从 localStorage 提取 token: {token[:30]}...")
            return token
        else:
            alternative_keys = [
                "x-jlc-accesstoken",
                "accessToken", 
                "token",
                "jlc-token"
            ]
            for key in alternative_keys:
                token = driver.execute_script(f"return window.localStorage.getItem('{key}');")
                if token:
                    log(f"✅ 从 localStorage 的 {key} 提取到 token: {token[:30]}...")
                    return token
    except Exception as e:
        log(f"❌ 从 localStorage 提取 token 失败: {e}")
    
    return None

@with_retry
def extract_secretkey_from_devtools(driver):
    """使用 DevTools 从网络请求中提取 secretkey"""
    secretkey = None
    
    try:
        logs = driver.get_log('performance')
        
        for entry in logs:
            try:
                message = json.loads(entry['message'])
                message_type = message.get('message', {}).get('method', '')
                
                if message_type == 'Network.requestWillBeSent':
                    request = message.get('message', {}).get('params', {}).get('request', {})
                    url = request.get('url', '')
                    
                    if 'm.jlc.com' in url:
                        headers = request.get('headers', {})
                        secretkey = (
                            headers.get('secretkey') or 
                            headers.get('SecretKey') or
                            headers.get('secretKey') or
                            headers.get('SECRETKEY')
                        )
                        
                        if secretkey:
                            log(f"✅ 从请求中提取到 secretkey: {secretkey[:20]}...")
                            return secretkey
                
                elif message_type == 'Network.responseReceived':
                    response = message.get('message', {}).get('params', {}).get('response', {})
                    url = response.get('url', '')
                    
                    if 'm.jlc.com' in url:
                        headers = response.get('requestHeaders', {})
                        secretkey = (
                            headers.get('secretkey') or 
                            headers.get('SecretKey') or
                            headers.get('secretKey') or
                            headers.get('SECRETKEY')
                        )
                        
                        if secretkey:
                            log(f"✅ 从响应中提取到 secretkey: {secretkey[:20]}...")
                            return secretkey
                            
            except:
                continue
                
    except Exception as e:
        log(f"❌ DevTools 提取 secretkey 出错: {e}")
    
    return secretkey

def get_oshwhub_points(driver, account_index):
    """获取开源平台积分数量"""
    max_retries = 5
    for attempt in range(max_retries):
        try:
            # 获取当前页面的Cookie
            cookies = driver.get_cookies()
            cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
            
            headers = {
                'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'accept': 'application/json, text/plain, */*',
                'cookie': cookie_str
            }
            
            # 调用用户信息API获取积分
            response = requests.get("https://oshwhub.com/api/users", headers=headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                if data and data.get('success'):
                    points = data.get('result', {}).get('points', 0)
                    return points
        except Exception:
            pass  # 静默重试
        
        # 重试前刷新页面
        if attempt < max_retries - 1:
            try:
                driver.refresh()
                WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                time.sleep(1 + random.uniform(0, 1))
            except:
                pass
    
    log(f"账号 {account_index} - ⚠ 无法获取积分信息")
    return 0

def get_valid_proxy(account_index):
    proxy_api_url = "http://api.dmdaili.com/dmgetip.asp?apikey=b345ad7e&pwd=bca1fcb138fb91448d9cfe7f1099c6f6&getnum=1&httptype=1&geshi=2&fenge=1&fengefu=&operate=all"
    max_attempts = 3
    attempt = 0
    
    while attempt < max_attempts:
        try:
            log(f"账号 {account_index} - 正在获取代理IP (尝试 {attempt + 1}/{max_attempts})...")
            response = requests.get(proxy_api_url, timeout=10)
            
            try:
                data = response.json()
            except Exception:
                log(f"账号 {account_index} - ⚠ 代理API返回非JSON数据，接口返回: {response.text}")
                attempt += 1
                time.sleep(2)
                continue

            if data.get("code") == 605:
                log(f"账号 {account_index} - 代理IP已自动添加到白名单，等待15秒后重试...")
                time.sleep(15)
                continue 
            elif data.get("code") == 1 and "Too Many Requests" in data.get("msg", ""):
                log(f"账号 {account_index} - 代理API请求过快，等待5秒后重试...")
                time.sleep(5)
                continue
            elif data.get("code") == 0 and data.get("data"):
                proxy_info = data["data"][0]
                ip = proxy_info.get("ip")
                port = proxy_info.get("port")
                city = proxy_info.get("city", "未知地区")
                if ip and port:
                    proxy_url = f"http://{ip}:{port}"
                    proxies = {
                        "http": proxy_url,
                        "https": proxy_url
                    }
                    log(f"账号 {account_index} - ✅ 代理获取成功: {ip}:{port} [{city}]")
                    return proxies
            
            log(f"账号 {account_index} - ⚠ 代理获取失败，接口返回: {json.dumps(data, ensure_ascii=False)}")
            attempt += 1
            time.sleep(2)
        except Exception as e:
            log(f"账号 {account_index} - ❌ 获取代理IP异常: {e}")
            attempt += 1
            time.sleep(2)
    
    log(f"账号 {account_index} - ❌ 连续3次获取代理失败，放弃使用代理")
    return None

class JLCClient:
    """调用嘉立创接口"""
    
    def __init__(self, access_token, secretkey, account_index, driver, proxies=None):
        self.base_url = "https://m.jlc.com"
        self.headers = {
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'x-jlc-clienttype': 'WEB',
            'accept': 'application/json, text/plain, */*',
            'x-jlc-accesstoken': access_token,
            'secretkey': secretkey,
            'Referer': 'https://m.jlc.com/mapp/pages/my/index',
        }
        self.account_index = account_index
        self.driver = driver
        self.proxies = proxies
        self.message = ""
        self.initial_jindou = 0  # 签到前金豆数量
        self.final_jindou = 0    # 签到后金豆数量
        self.jindou_reward = 0   # 本次获得金豆（通过差值计算）
        self.sign_status = "未知"  # 签到状态
        self.has_reward = False  # 是否领取了额外奖励
        
    def send_request(self, url, method='GET', use_proxy=False):
        """发送 API 请求"""
        global disable_global_proxy, consecutive_proxy_fails
        
        max_retries = 5 if use_proxy else 1
        
        for attempt in range(max_retries):
            try:
                # 根据 use_proxy 参数决定是否使用代理
                req_proxies = self.proxies if use_proxy and not disable_global_proxy else None
                
                if method.upper() == 'GET':
                    response = requests.get(url, headers=self.headers, timeout=10, proxies=req_proxies)
                else:
                    response = requests.post(url, headers=self.headers, timeout=10, proxies=req_proxies)
                
                if response.status_code == 200:
                    return response.json()
                else:
                    log(f"账号 {self.account_index} - ❌ 请求失败，状态码: {response.status_code}")
                    return None
            except requests.exceptions.RequestException as e:
                if use_proxy and not disable_global_proxy:
                    if isinstance(e, requests.exceptions.ProxyError):
                        error_type = "代理拒绝连接/代理错误"
                    elif isinstance(e, requests.exceptions.ConnectTimeout):
                        error_type = "连接代理超时"
                    elif isinstance(e, requests.exceptions.ReadTimeout):
                        error_type = "代理响应超时"
                    elif isinstance(e, requests.exceptions.Timeout):
                        error_type = "请求超时"
                    elif isinstance(e, requests.exceptions.ConnectionError):
                        error_type = "连接错误"
                    else:
                        error_type = "未知请求异常"
                        
                    log(f"账号 {self.account_index} - ⚠ 代理无效 ({error_type}: {e})，准备重新获取代理...")
                    
                    self.proxies = get_valid_proxy(self.account_index)
                    if not self.proxies:
                        consecutive_proxy_fails += 1
                        if consecutive_proxy_fails >= 5:
                            disable_global_proxy = True
                            log("⚠ 连续5个账号代理获取失败，接下来的账号全部放弃使用代理！")
                else:
                    log(f"账号 {self.account_index} - ❌ 请求异常 ({url}): {e}")
                    return None
        
        if use_proxy:
            log(f"账号 {self.account_index} - ❌ 连续多次代理请求失败")
        return None
    
    def get_user_info(self):
        """获取用户信息"""
        log(f"账号 {self.account_index} - 获取用户信息...")
        url = f"{self.base_url}/api/appPlatform/center/setting/selectPersonalInfo"
        data = self.send_request(url)
        
        if data and data.get('success'):
            log(f"账号 {self.account_index} - ✅ 用户信息获取成功")
            return True
        else:
            error_msg = data.get('message', '未知错误') if data else '请求失败'
            log(f"账号 {self.account_index} - ❌ 获取用户信息失败: {error_msg}")
            return False
    
    def get_points(self):
        """获取金豆数量"""
        url = f"{self.base_url}/api/activity/front/getCustomerIntegral"
        max_retries = 5
        for attempt in range(max_retries):
            data = self.send_request(url)
            
            if data and data.get('success'):
                jindou_count = data.get('data', {}).get('integralVoucher', 0)
                return jindou_count
            
            # 重试前刷新页面，重新提取 token 和 secretkey
            if attempt < max_retries - 1:
                try:
                    self.driver.get("https://m.jlc.com/")
                    self.driver.refresh()
                    WebDriverWait(self.driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                    time.sleep(1 + random.uniform(0, 1))
                    navigate_and_interact_m_jlc(self.driver, self.account_index)
                    access_token = extract_token_from_local_storage(self.driver)
                    secretkey = extract_secretkey_from_devtools(self.driver)
                    if access_token:
                        self.headers['x-jlc-accesstoken'] = access_token
                    if secretkey:
                        self.headers['secretkey'] = secretkey
                except:
                    pass  # 静默继续
        
        log(f"账号 {self.account_index} - ❌ 获取金豆数量失败")
        return 0
    
    def check_sign_status(self):
        """检查签到状态"""
        log(f"账号 {self.account_index} - 检查签到状态...")
        url = f"{self.base_url}/api/activity/sign/getCurrentUserSignInConfig"
        data = self.send_request(url)
        
        if data and data.get('success'):
            have_sign_in = data.get('data', {}).get('haveSignIn', False)
            if have_sign_in:
                log(f"账号 {self.account_index} - ✅ 今日已签到")
                self.sign_status = "已签到过"
                return True
            else:
                log(f"账号 {self.account_index} - 今日未签到")
                self.sign_status = "未签到"
                return False
        else:
            error_msg = data.get('message', '未知错误') if data else '请求失败'
            log(f"账号 {self.account_index} - ❌ 检查签到状态失败: {error_msg}")
            self.sign_status = "检查失败"
            return None
    
    def sign_in(self):
        """执行签到"""
        log(f"账号 {self.account_index} - 执行签到 (使用代理)...")
        url = f"{self.base_url}/api/activity/sign/signIn?source=4"
        # ⚠️ 仅在签到接口显式使用代理
        data = self.send_request(url, use_proxy=True)
        
        if data and data.get('success'):
            gain_num = data.get('data', {}).get('gainNum')
            if gain_num:
                # 直接签到成功，获得金豆
                log(f"账号 {self.account_index} - ✅ 签到成功，签到使金豆+{gain_num}")
                self.sign_status = "签到成功"
                return True
            else:
                # 有奖励可领取，先领取奖励
                log(f"账号 {self.account_index} - 有奖励可领取，先领取奖励")
                self.has_reward = True
                
                # 领取奖励
                if self.receive_voucher():
                    # 领取奖励成功后，视为签到完成
                    log(f"账号 {self.account_index} - ✅ 奖励领取成功，签到完成")
                    self.sign_status = "领取奖励成功"
                    return True
                else:
                    self.sign_status = "领取奖励失败"
                    return False
        else:
            error_msg = data.get('message', '未知错误') if data else '请求失败'
            log(f"账号 {self.account_index} - ❌ 签到失败: {error_msg}")
            self.sign_status = "签到失败"
            return False
    
    def receive_voucher(self):
        """领取奖励"""
        log(f"账号 {self.account_index} - 领取奖励...")
        url = f"{self.base_url}/api/activity/sign/receiveVoucher"
        data = self.send_request(url)
        
        if data and data.get('success'):
            log(f"账号 {self.account_index} - ✅ 领取成功")
            return True
        else:
            error_msg = data.get('message', '未知错误') if data else '请求失败'
            log(f"账号 {self.account_index} - ❌ 领取奖励失败: {error_msg}")
            return False
    
    def calculate_jindou_difference(self):
        """计算金豆差值"""
        self.jindou_reward = self.final_jindou - self.initial_jindou
        if self.jindou_reward > 0:
            reward_text = f" (+{self.jindou_reward})"
            if self.has_reward:
                reward_text += "（有奖励）"
            log(f"账号 {self.account_index} - 🎉 总金豆增加: {self.initial_jindou} → {self.final_jindou}{reward_text}")
        elif self.jindou_reward == 0:
            log(f"账号 {self.account_index} - ⚠ 总金豆无变化，可能今天已签到过: {self.initial_jindou} → {self.final_jindou} (0)")
        else:
            log(f"账号 {self.account_index} - ❗ 金豆减少: {self.initial_jindou} → {self.final_jindou} ({self.jindou_reward})")
        
        return self.jindou_reward
    
    def execute_full_process(self):
        """执行金豆签到流程"""        
        # 1. 获取用户信息
        if not self.get_user_info():
            return False
        
        time.sleep(random.randint(1, 2))
        
        # 2. 获取签到前金豆数量
        self.initial_jindou = self.get_points()
        if self.initial_jindou is None:
            self.initial_jindou = 0
        log(f"账号 {self.account_index} - 签到前金豆💰: {self.initial_jindou}")
        
        time.sleep(random.randint(1, 2))
        
        # 3. 检查签到状态
        sign_status = self.check_sign_status()
        if sign_status is None:  # 检查失败
            return False
        elif sign_status:  # 已签到
            # 已签到，直接获取金豆数量
            log(f"账号 {self.account_index} - 今日已签到，跳过签到操作")
        else:  # 未签到
            # 4. 执行签到
            time.sleep(random.randint(2, 3))
            if not self.sign_in():
                return False
        
        time.sleep(random.randint(1, 2))
        
        # 5. 获取签到后金豆数量
        self.final_jindou = self.get_points()
        if self.final_jindou is None:
            self.final_jindou = 0
        log(f"账号 {self.account_index} - 签到后金豆💰: {self.final_jindou}")
        
        # 6. 计算金豆差值
        self.calculate_jindou_difference()
        
        return True

def navigate_and_interact_m_jlc(driver, account_index):
    """在 m.jlc.com 刷新以触发网络请求"""
    log(f"账号 {account_index} - 刷新页面以获取 Token 和 SecretKey...")
    
    try:
        # 只需要刷新，等待页面加载，网络请求会自动发出
        driver.refresh()
        WebDriverWait(driver, 12).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(2)
        
    except Exception as e:
        log(f"账号 {account_index} - 页面刷新出错: {e}")

def is_sunday():
    """检查今天是否是周日"""
    return datetime.now().weekday() == 6

def is_last_day_of_month():
    """检查今天是否是当月最后一天"""
    today = datetime.now()
    next_month = today.replace(day=28) + timedelta(days=4)
    last_day = next_month - timedelta(days=next_month.day)
    return today.day == last_day.day

def capture_reward_info(driver, account_index, gift_type):
    """抓取并输出奖励信息，返回礼包领取结果"""
    try:
        reward_elem = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.XPATH, '//p[contains(text(), "恭喜获取")]'))
        )
        reward_text = reward_elem.text.strip()
        gift_name = "七日礼包" if gift_type == "7天" else "月度礼包"
        log(f"账号 {account_index} - {gift_name}领取结果：{reward_text}")
        return f"开源平台{gift_name}领取结果: {reward_text}"
    except Exception as e:
        log(f"账号 {account_index} - 已点击{gift_type}好礼，未获取到奖励信息(可能已领取过或未达到领取条件)，请自行前往开源平台查看。")
        return None

def click_gift_buttons(driver, account_index):
    """根据日期条件点击7天好礼和月度好礼按钮，并抓取奖励信息，返回所有领取结果"""
    reward_results = []
    
    if not is_sunday() and not is_last_day_of_month():
        return reward_results

    try:
        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        
        log(f"账号 {account_index} - 开始点击礼包按钮...")
        
        sunday = is_sunday()
        last_day = is_last_day_of_month()

        if sunday:
            # 尝试点击7天好礼
            try:
                seven_day_gift = driver.find_element(By.XPATH, '//div[contains(@class, "sign_text__r9zaN")]/span[text()="7天好礼"]')
                seven_day_gift.click()
                log(f"账号 {account_index} - ✅ 检测到今天是周日，成功点击7天好礼，祝你周末愉快~")
                
                WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                reward_result = capture_reward_info(driver, account_index, "7天")
                if reward_result:
                    reward_results.append(reward_result)
                
                # 如果也是月底，刷新页面
                if last_day:
                    driver.refresh()
                    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                    time.sleep(10)
                
            except Exception as e:
                log(f"账号 {account_index} - ⚠ 无法点击7天好礼: {e}")

        if last_day:
            # 尝试点击月度好礼
            try:
                monthly_gift = driver.find_element(By.XPATH, '//div[contains(@class, "sign_text__r9zaN")]/span[text()="月度好礼"]')
                monthly_gift.click()
                log(f"账号 {account_index} - ✅ 检测到今天是月底，成功点击月度好礼～")          
                
                WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                reward_result = capture_reward_info(driver, account_index, "月度")
                if reward_result:
                    reward_results.append(reward_result)
                
            except Exception as e:
                log(f"账号 {account_index} - ⚠ 无法点击月度好礼: {e}")
            
    except Exception as e:
        log(f"账号 {account_index} - ❌ 点击礼包按钮时出错: {e}")

    return reward_results

@with_retry
def get_user_nickname_from_api(driver, account_index):
    """通过API获取用户昵称"""
    try:
        # 获取当前页面的Cookie
        cookies = driver.get_cookies()
        cookie_str = "; ".join([f"{c['name']}={c['value']}" for c in cookies])
        
        headers = {
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'accept': 'application/json, text/plain, */*',
            'cookie': cookie_str
        }
        
        # 调用用户信息API
        response = requests.get("https://oshwhub.com/api/users", headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data and data.get('success'):
                nickname = data.get('result', {}).get('nickname', '')
                if nickname:
                    formatted_nickname = format_nickname(nickname)
                    log(f"账号 {account_index} - 👤 昵称: {formatted_nickname}")
                    return formatted_nickname
        
        log(f"账号 {account_index} - ⚠ 无法获取用户昵称")
        return None
    except Exception as e:
        log(f"账号 {account_index} - ⚠ 获取用户昵称失败: {e}")
        return None

def run_aliv3_task(username, password, output_file):
    """
    独立进程运行 AliV3，将日志写入文件。
    这样即使进程被 kill，文件内容依然存在。
    """
    with open(output_file, 'w', encoding='utf-8') as f:
        with redirect_stdout(f):
            try:
                # 尝试从全局获取 AliV3，或者重新导入
                if 'AliV3' in globals() and globals()['AliV3']:
                    ali_cls = globals()['AliV3']
                else:
                    from AliV3 import AliV3 as ali_cls
                
                ali = ali_cls()
                ali.main(username=username, password=password)
            except Exception as e:
                print(f"Error executing AliV3 in process: {e}")

def get_ali_auth_code(username, password, account_index=0):
    """
    调用 AliV3 获取 authCode，超时控制 (180s)
    """
    if AliV3 is None:
        return None
    
    # 创建临时文件用于存储子进程的 stdout
    fd, temp_path = tempfile.mkstemp()
    os.close(fd) # 关闭文件描述符，只保留路径
    
    auth_code = None
    ali_output = ""
    
    try:
        # 启动子进程运行 AliV3
        p = multiprocessing.Process(target=run_aliv3_task, args=(username, password, temp_path))
        p.start()
        
        # 等待进程结束，超时 180 秒
        p.join(timeout=180)
        
        if p.is_alive():
            log(f"账号 {account_index} - ❌ 登录超时 (超过180秒)，正在强制终止 登录脚本...")
            p.terminate()
            p.join() # 确保进程已退出
            
            # 读取已生成的日志以便调试
            try:
                with open(temp_path, 'r', encoding='utf-8') as f:
                    ali_output = f.read()
            except Exception:
                ali_output = "无法读取超时日志"
            
            log(f"--- 超时前的 登录脚本(AliV3) 日志 ---\n{ali_output}\n--------------------------")
            return None # 超时返回 None
            
        else:
            # 正常结束，读取日志
            try:
                with open(temp_path, 'r', encoding='utf-8') as f:
                    ali_output = f.read()
            except Exception:
                ali_output = ""

    finally:
        # 清理临时文件
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except:
                pass

    # 解析输出获取 authCode
    for line in ali_output.split('\n'):
        line = line.strip()
        if not line:
            continue
        
        # 尝试提取 JSON 部分，应对带前缀的情况
        json_str = line
        if not json_str.startswith('{') and '{' in json_str:
            json_str = json_str[json_str.find('{'):]

        try:
            data = json.loads(json_str)
            # 检查 authCode
            if isinstance(data, dict):
                # 兼容 success 字段，有些接口返回 true, 有些返回 "true" 或不返回
                # 重点检查 data.authCode
                inner_data = data.get('data')
                if isinstance(inner_data, dict) and 'authCode' in inner_data:
                    auth_code = inner_data['authCode']
                    break
            
            # 检查密码错误 (用于在外部判断)
            if isinstance(data, dict) and data.get('code') == 10208:
                pass
        except json.JSONDecodeError:
            continue
            
    # 如果没获取到 authCode，返回整个输出供外部记录日志
    if not auth_code:
        return ali_output 
        
    return auth_code

def sign_in_account(username, password, account_index, total_accounts, retry_count=0):
    """为单个账号执行完整的签到流程"""
    retry_label = ""
    if retry_count > 0:
        retry_label = f" (重试{retry_count})"
    
    log(f"开始处理账号 {account_index}/{total_accounts}{retry_label}")
    
    # 初始化结果字典
    result = {
        'account_index': account_index,
        'nickname': '未知',
        'oshwhub_status': '未知',
        'oshwhub_success': False,
        'initial_points': 0,      # 签到前积分
        'final_points': 0,        # 签到后积分
        'points_reward': 0,       # 本次获得积分
        'reward_results': [],     # 礼包领取结果
        'jindou_status': '未知',
        'jindou_success': False,
        'initial_jindou': 0,
        'final_jindou': 0,
        'jindou_reward': 0,
        'has_jindou_reward': False,  # 金豆是否有额外奖励
        'token_extracted': False,
        'secretkey_extracted': False,
        'retry_count': retry_count,
        'password_error': False,  #标记密码错误
        'actual_password': None,  # 实际使用的密码
        'backup_index': -1,  # 使用的备用密码索引，-1表示原密码
        'critical_error': False,  #标记严重错误（如多次调用依赖失败），需跳过重试
        'login_success': False,   # 标记开源平台登录是否成功
        'jlc_login_success': False # 标记金豆签到的JLC登录是否成功
    }
    
    # 显式创建临时目录用于 user-data-dir，以便后续清理
    user_data_dir = tempfile.mkdtemp()

    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-software-rasterizer") # 禁用软件光栅化
    chrome_options.add_argument("--window-size=1920,1080")
    chrome_options.add_argument(f"--user-data-dir={user_data_dir}")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_argument("--blink-settings=imagesEnabled=false")  # 禁用图像加载
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option('useAutomationExtension', False)

    # 替换 DesiredCapabilities 提高兼容性
    chrome_options.set_capability('goog:loggingPrefs', {'performance': 'ALL'})
    
    driver = None
    
    backup_passwords = [
        "Aa123123",
        "Zz123123",
        "Qq123123",
        "Ss123123",
        "Xx123123",
        "Yuanxd20031024",
        "jjl1775774A",
        "qeowowe5472",
        "Wyf349817236",
        "Bb123123"
    ]

    try:
        # 尝试初始化 Driver
        try:
            driver = webdriver.Chrome(options=chrome_options)
            driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            wait = WebDriverWait(driver, 25)
        except Exception as e:
            log(f"账号 {account_index} - ❌ 浏览器初始化失败: {e}")
            result['oshwhub_status'] = '浏览器启动失败'
            # 返回当前结果，外层逻辑会根据重试机制处理
            return result

        # 1. 登录流程
        log(f"账号 {account_index} - 正在调用 登录(AliV3) 依赖进行登录...")
        
        # 确保 AliV3 已加载
        if AliV3 is None:
             log(f"账号 {account_index} - ❌ 登录依赖未正确加载，无法登录")
             result['oshwhub_status'] = '依赖缺失'
             return result

        current_password = password  # 默认原密码
        current_backup_index = -1  # -1 表示原密码
        auth_code = None
        auth_result = None

        # 尝试密码（原密码 + 备用密码）
        while True:
            # 在这里加入 18 次重试循环，以处理网络不稳定导致的 authCode 获取失败
            # 如果是 10208 密码错误，会立即中断重试并切换密码
            is_pwd_error = False
            max_auth_retries = 18
            
            for auth_attempt in range(max_auth_retries):
                # 调用get_ali_auth_code，支持超时
                auth_result = get_ali_auth_code(username, current_password, account_index)
                
                # get_ali_auth_code 返回 None 表示超时
                if auth_result is None:
                    pass # 超时，继续重试
                elif isinstance(auth_result, str) and len(auth_result) > 100:
                    # 说明返回的是日志内容，未提取到 authCode
                    ali_output = auth_result
                    
                    # 检查是否包含错误码 10208（账密错误）
                    for line in ali_output.split('\n'):
                        line = line.strip()
                        if not line.startswith('{') and '{' in line:
                            line = line[line.find('{'):]
                        try:
                            data = json.loads(line)
                            if isinstance(data, dict) and data.get('code') == 10208:
                                is_pwd_error = True
                                break
                        except:
                            continue
                    
                    if is_pwd_error:
                        # 密码错误不需要重试调用，直接跳出内层循环进行密码切换
                        break
                else:
                    # 成功获取 authCode
                    auth_code = auth_result
                    break
                
                # 仅在非密码错误且未达到最大尝试次数时等待重试
                if auth_attempt < max_auth_retries - 1 and not is_pwd_error:
                    log(f"账号 {account_index} - ⚠ 未获取到AuthCode，等待5秒后第 {auth_attempt + 2} 次重试...")
                    time.sleep(5)

            # 处理重试循环后的结果
            
            if is_pwd_error:
                log(f"账号 {account_index} - ❌ 密码错误 ({'原密码' if current_backup_index == -1 else f'备用密码{current_backup_index + 1}'})")
                
                # 尝试下一个备用密码
                if current_backup_index == -1:
                    current_backup_index = 0
                else:
                    current_backup_index += 1
                    
                if current_backup_index >= len(backup_passwords):
                    # 所有密码都尝试完毕
                    log(f"账号 {account_index} - ❌ 所有备用密码尝试失败，跳过此账号")
                    result['password_error'] = True
                    result['oshwhub_status'] = '所有密码错误'
                    return result
                
                current_password = backup_passwords[current_backup_index]
                log(f"账号 {account_index} - 🔄 尝试备用密码: {desensitize_password(current_password)}")
                continue # 继续循环尝试新密码
            
            if not auth_code:
                if auth_result is None:
                     result['oshwhub_status'] = '登录超时'
                     return result
                else:
                     log(f"账号 {account_index} - ❌ 连续 {max_auth_retries} 次调用登录依赖失败，未返回有效AuthCode")
                     log("❌ 登录脚本输出如下：")
                     log(auth_result)
                     result['oshwhub_status'] = 'authCode获取异常'
                     result['critical_error'] = True  # 标记为严重错误
                     return result
            else:
                # 成功获取 authCode
                result['actual_password'] = current_password
                result['backup_index'] = current_backup_index
                log(f"账号 {account_index} - ✅ 成功获取 authCode")
                break

        # 判断登录结果
        if auth_code:
            # 拼接 URL 并跳转
            login_url = f"https://oshwhub.com/sign_in?code={auth_code}"
            log(f"账号 {account_index} - 正在使用 authCode 登录...")
            driver.get(login_url)
            
            # 等待登录成功 (通过检测URL或页面元素)
            try:
                # 等待页面加载且没有 error 提示
                WebDriverWait(driver, 20).until(
                    lambda d: "oshwhub.com" in d.current_url and "code=" not in d.current_url
                )
                log(f"账号 {account_index} - ✅ 登录跳转成功")
            except Exception:
                log(f"账号 {account_index} - ⚠ 登录跳转超时或未检测到预期URL，尝试继续后续流程...")

            result['login_success'] = True  # 标记基本登录成功，后续失败计入非登录异常

        # 3. 获取用户昵称
        time.sleep(2) # 稍作等待确保 Cookie 生效
        nickname = get_user_nickname_from_api(driver, account_index)
        if nickname:
            result['nickname'] = nickname
        else:
            result['nickname'] = '未知'

        # 4. 获取签到前积分数量
        global skip_oshwhub_signin
        if skip_oshwhub_signin:
            log(f"账号 {account_index} - ⚠ 由于前面账号连续失败，跳过开源平台签到流程")
            result['oshwhub_status'] = '连续异常,跳过签到'
            result['oshwhub_success'] = False
        else:
            initial_points = get_oshwhub_points(driver, account_index)
            result['initial_points'] = initial_points if initial_points is not None else 0
            log(f"账号 {account_index} - 签到前积分💰: {result['initial_points']}")

            # 5. 开源平台签到
            log(f"账号 {account_index} - 正在签到中...")
            WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))

            try:
                # 确保在签到页
                if "sign_in" not in driver.current_url:
                    driver.get("https://oshwhub.com/sign_in")
                    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                
                time.sleep(2)
            except:
                pass
                
            time.sleep(4)
            
            # 执行开源平台签到
            try:
                # 先检查是否已经签到
                try:
                    signed_element = driver.find_element(By.XPATH, '//span[contains(text(),"已签到")]')
                    log(f"账号 {account_index} - ✅ 今天已经在开源平台签到过了！")
                    result['oshwhub_status'] = '已签到过'
                    result['oshwhub_success'] = True
                    
                    # 即使已签到，也尝试点击礼包按钮
                    result['reward_results'] = click_gift_buttons(driver, account_index)
                    
                except:
                    # 如果没有找到"已签到"元素，则尝试点击"立即签到"按钮，并验证是否变为"已签到"
                    signed = False
                    max_attempts = 5
                    for attempt in range(max_attempts):
                        try:
                            sign_btn = wait.until(
                                EC.element_to_be_clickable((By.XPATH, '//span[contains(text(),"立即签到")]'))
                            )
                            sign_btn.click()
                            time.sleep(2)  # 等待页面更新
                            driver.refresh()  # 刷新页面以确保状态更新
                            WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                            time.sleep(2)  # 额外等待

                            # 检查是否变为"已签到"
                            signed_element = driver.find_element(By.XPATH, '//span[contains(text(),"已签到")]')
                            signed = True
                            break  # 成功，退出循环
                        except:
                            pass  # 静默继续下一次尝试

                    if signed:
                        log(f"账号 {account_index} - ✅ 开源平台签到成功！")
                        result['oshwhub_status'] = '签到成功'
                        result['oshwhub_success'] = True
                        
                        # 等待签到完成
                        WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
                        
                        # 6. 签到完成后点击7天好礼和月度好礼
                        result['reward_results'] = click_gift_buttons(driver, account_index)
                    else:
                        log(f"账号 {account_index} - ❌ 开源平台签到失败")
                        result['oshwhub_status'] = '签到失败'
                        
            except Exception as e:
                log(f"账号 {account_index} - ❌ 开源平台签到异常: {e}")
                result['oshwhub_status'] = '签到异常'

            WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))

            # 7. 获取签到后积分数量
            final_points = get_oshwhub_points(driver, account_index)
            result['final_points'] = final_points if final_points is not None else 0
            log(f"账号 {account_index} - 签到后积分💰: {result['final_points']}")

            # 8. 计算积分差值
            result['points_reward'] = result['final_points'] - result['initial_points']
            if result['points_reward'] > 0:
                log(f"账号 {account_index} - 🎉 总积分增加: {result['initial_points']} → {result['final_points']} (+{result['points_reward']})")
            elif result['points_reward'] == 0:
                log(f"账号 {account_index} - ⚠ 总积分无变化，可能今天已签到过: {result['initial_points']} → {result['final_points']} (0)")
            else:
                log(f"账号 {account_index} - ❗ 积分减少: {result['initial_points']} → {result['final_points']} ({result['points_reward']})")

        # 9. 金豆签到流程
        global skip_jindou_signin
        if skip_jindou_signin:
            log(f"账号 {account_index} - ⚠ 由于前面账号连续失败，跳过金豆签到流程")
            result['jindou_status'] = '连续异常,跳过签到'
            result['jindou_success'] = False
        else:
            log(f"账号 {account_index} - 开始金豆签到流程...")
            driver.get("https://m.jlc.com/")
            WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            
            # 重新获取 AuthCode，使用之前验证成功的密码
            log(f"账号 {account_index} - 正在重新调用 登录依赖 获取 m.jlc.com 登录凭证...")
            
            auth_result_jlc = None
            auth_code_jlc = None
            max_auth_retries = 18
            
            for auth_attempt in range(max_auth_retries):
                # 这里已经通过了密码验证，所以只重试网络/API错误
                auth_result_jlc = get_ali_auth_code(username, result['actual_password'], account_index)
                
                if auth_result_jlc is None:
                    pass 
                elif isinstance(auth_result_jlc, str) and len(auth_result_jlc) > 100:
                    pass # 未获取到有效code
                else:
                    auth_code_jlc = auth_result_jlc
                    break
                
                if auth_attempt < max_auth_retries - 1:
                    log(f"账号 {account_index} - ⚠ JLC登录凭证获取失败，等待5秒后第 {auth_attempt + 2} 次重试...")
                    time.sleep(5)
            
            if auth_code_jlc is None:
                 log(f"账号 {account_index} - ❌ 连续 {max_auth_retries} 次无法获取 m.jlc.com 登录凭证")
                 if isinstance(auth_result_jlc, str):
                     log("❌ 登录脚本输出如下：")
                     log(auth_result_jlc)
                 result['jindou_status'] = 'authCode获取异常'
                 result['critical_error'] = True # 标记严重错误
            else:
                auth_code_jlc = auth_result_jlc
                log(f"账号 {account_index} - ✅ 成功获取 m.jlc.com 登录 authCode")
                
                # 使用 JS 进行登录
                login_js = """
                var code = arguments[0];
                var callback = arguments[1];
                var formData = new FormData();
                formData.append('code', code);
                
                fetch('/api/login/login-by-code', {
                    method: 'POST',
                    body: formData,
                    headers: {
                        'X-JLC-AccessToken': 'NONE'
                    }
                })
                .then(response => response.json())
                .then(data => {
                    if (data.code === 200 && data.data && data.data.accessToken) {
                        window.localStorage.setItem('X-JLC-AccessToken', data.data.accessToken);
                        callback(true);
                    } else {
                        console.error('Login failed:', data);
                        callback(false);
                    }
                })
                .catch(err => {
                    console.error('Login error:', err);
                    callback(false);
                });
                """
                
                try:
                    login_success = driver.execute_async_script(login_js, auth_code_jlc)
                except Exception as e:
                    log(f"账号 {account_index} - ❌ 执行 JS 登录脚本出错: {e}")
                    login_success = False
                
                if login_success:
                    result['jlc_login_success'] = True  # 标记金豆签到的JLC登录成功
                    log(f"账号 {account_index} - ✅ m.jlc.com 登录接口调用成功")
                    
                    navigate_and_interact_m_jlc(driver, account_index)
                    
                    access_token = extract_token_from_local_storage(driver)
                    secretkey = extract_secretkey_from_devtools(driver)
                    
                    result['token_extracted'] = bool(access_token)
                    result['secretkey_extracted'] = bool(secretkey)
                    
                    if access_token and secretkey:
                        log(f"账号 {account_index} - ✅ 成功提取 token 和 secretkey")
                        
                        global disable_global_proxy, consecutive_proxy_fails
                        current_proxies = None
                        
                        if not disable_global_proxy:
                            current_proxies = get_valid_proxy(account_index)
                            if current_proxies:
                                consecutive_proxy_fails = 0
                            else:
                                consecutive_proxy_fails += 1
                                if consecutive_proxy_fails >= 5:
                                    disable_global_proxy = True
                                    log("⚠ 连续5个账号代理获取失败，接下来的账号全部放弃使用代理！")
                        else:
                            log(f"账号 {account_index} - ⚠ 已全局禁用代理，直接使用本地IP")

                        jlc_client = JLCClient(access_token, secretkey, account_index, driver, current_proxies)
                        jindou_success = jlc_client.execute_full_process()
                        
                        # 记录金豆签到结果
                        result['jindou_success'] = jindou_success
                        result['jindou_status'] = jlc_client.sign_status
                        result['initial_jindou'] = jlc_client.initial_jindou
                        result['final_jindou'] = jlc_client.final_jindou
                        result['jindou_reward'] = jlc_client.jindou_reward
                        result['has_jindou_reward'] = jlc_client.has_reward
                        
                        if jindou_success:
                            log(f"账号 {account_index} - ✅ 金豆签到流程完成")
                        else:
                            log(f"账号 {account_index} - ❌ 金豆签到流程失败")
                    else:
                        log(f"账号 {account_index} - ❌ 无法提取到 token 或 secretkey，跳过金豆签到")
                        result['jindou_status'] = 'Token提取失败'
                else:
                    log(f"账号 {account_index} - ❌ m.jlc.com 登录接口返回失败")
                    result['jindou_status'] = '登录失败'

    except Exception as e:
        log(f"账号 {account_index} - ❌ 程序执行错误: {e}")
        result['oshwhub_status'] = '执行异常'
    finally:
        # 安全退出 Driver
        if driver:
            try:
                driver.quit()
                log(f"账号 {account_index} - 浏览器已关闭")
            except Exception:
                pass
        
        # 清理临时目录
        if user_data_dir and os.path.exists(user_data_dir):
            try:
                shutil.rmtree(user_data_dir, ignore_errors=True)
            except Exception:
                pass
    
    return result

def should_retry(merged_success, password_error):
    """判断是否需要重试：如果开源平台或金豆签到未成功，且不是密码错误"""
    global skip_oshwhub_signin, skip_jindou_signin
    oshwhub_needs_retry = not merged_success['oshwhub'] and not skip_oshwhub_signin
    jindou_needs_retry = not merged_success['jindou'] and not skip_jindou_signin
    need_retry = (oshwhub_needs_retry or jindou_needs_retry) and not password_error
    return need_retry

def process_single_account(username, password, account_index, total_accounts):
    """处理单个账号，包含重试机制，并合并多次尝试的最佳结果"""
    max_retries = 3  # 最多重试3次
    merged_result = {
        'account_index': account_index,
        'nickname': '未知',
        'oshwhub_status': '未知',
        'oshwhub_success': False,
        'initial_points': 0,
        'final_points': 0,
        'points_reward': 0,
        'reward_results': [],
        'jindou_status': '未知',
        'jindou_success': False,
        'initial_jindou': 0,
        'final_jindou': 0,
        'jindou_reward': 0,
        'has_jindou_reward': False,
        'token_extracted': False,
        'secretkey_extracted': False,
        'retry_count': 0,  # 记录最后使用的retry_count
        'password_error': False,  # 标记密码错误
        'actual_password': None,  # 实际使用的密码
        'backup_index': -1,  # 使用的备用密码索引，-1表示原密码
        'critical_error': False,   # 标记严重错误
        'login_success': False,
        'jlc_login_success': False
    }
    
    merged_success = {'oshwhub': False, 'jindou': False}

    for attempt in range(max_retries + 1):  # 第一次执行 + 重试次数
        try:
            result = sign_in_account(username, password, account_index, total_accounts, retry_count=attempt)
        except Exception as e:
            log(f"账号 {account_index} - ⚠ 发生未捕获异常，将进行重试: {e}")
            result = merged_result.copy()
            result['oshwhub_status'] = '程序异常'
        
        # 如果检测到密码错误，立即停止重试
        if result.get('password_error'):
            merged_result['password_error'] = True
            merged_result['oshwhub_status'] = '密码错误'
            merged_result['nickname'] = '未知'
            # 停止后续尝试
            break
        
        # 如果检测到严重错误（如多次调用登录依赖失败），立即停止重试，处理下一个账号
        if result.get('critical_error'):
            merged_result['critical_error'] = True
            merged_result['oshwhub_status'] = result.get('oshwhub_status', '严重错误')
            if result.get('jindou_status') != '未知':
                 merged_result['jindou_status'] = result.get('jindou_status')
            break

        # 合并结果
        if result.get('login_success'):
            merged_result['login_success'] = True
        if result.get('jlc_login_success'):
            merged_result['jlc_login_success'] = True
        
        # 合并开源平台结果：如果本次成功且之前未成功，则更新
        if result['oshwhub_success'] and not merged_success['oshwhub']:
            merged_success['oshwhub'] = True
            merged_result['oshwhub_status'] = result['oshwhub_status']
            merged_result['initial_points'] = result['initial_points']
            merged_result['final_points'] = result['final_points']
            merged_result['points_reward'] = result['points_reward']
            merged_result['reward_results'] = result['reward_results']  # 合并礼包结果
            # 更新实际密码信息
            merged_result['actual_password'] = result['actual_password']
            merged_result['backup_index'] = result['backup_index']
        
        # 合并金豆结果：如果本次成功且之前未成功，则更新
        if result['jindou_success'] and not merged_success['jindou']:
            merged_success['jindou'] = True
            merged_result['jindou_status'] = result['jindou_status']
            merged_result['initial_jindou'] = result['initial_jindou']
            merged_result['final_jindou'] = result['final_jindou']
            merged_result['jindou_reward'] = result['jindou_reward']
            merged_result['has_jindou_reward'] = result['has_jindou_reward']
            # 更新实际密码信息（如果之前未更新）
            if merged_result['actual_password'] is None:
                merged_result['actual_password'] = result['actual_password']
                merged_result['backup_index'] = result['backup_index']
        
        # 更新其他字段（如果之前未知）
        if merged_result['nickname'] == '未知' and result['nickname'] != '未知':
            merged_result['nickname'] = result['nickname']
        
        if not merged_result['token_extracted'] and result['token_extracted']:
            merged_result['token_extracted'] = result['token_extracted']
        
        if not merged_result['secretkey_extracted'] and result['secretkey_extracted']:
            merged_result['secretkey_extracted'] = result['secretkey_extracted']
        
        # 更新retry_count为最后一次尝试的
        merged_result['retry_count'] = result['retry_count']
        
        # 检查是否还需要重试（排除密码错误的情况）
        if not should_retry(merged_success, merged_result['password_error']) or attempt >= max_retries:
            break
        else:
            log(f"账号 {account_index} - 🔄 准备第 {attempt + 1} 次重试，等待 {random.randint(2, 6)} 秒后重新开始...")
            time.sleep(random.randint(2, 6))
    
    # 最终设置success字段基于合并
    merged_result['oshwhub_success'] = merged_success['oshwhub']
    merged_result['jindou_success'] = merged_success['jindou']

    # ---------------- 连续失败跳过逻辑 ----------------
    global consecutive_oshwhub_fails, skip_oshwhub_signin
    global consecutive_jindou_fails, skip_jindou_signin

    # 检查开源平台签到连续失败 (确保已经通过了开源平台登录)
    if not skip_oshwhub_signin and merged_result['login_success']:
        if not merged_result['oshwhub_success']:
            consecutive_oshwhub_fails += 1
            if consecutive_oshwhub_fails >= 3:
                skip_oshwhub_signin = True
                log("⚠ 连续3个账号开源平台签到失败，接下来的账号跳过开源平台签到流程！")
        else:
            consecutive_oshwhub_fails = 0

    # 检查金豆签到连续失败 (确保已经通过了金豆平台的JLC登录)
    if not skip_jindou_signin and merged_result['jlc_login_success']:
        if not merged_result['jindou_success']:
            consecutive_jindou_fails += 1
            if consecutive_jindou_fails >= 3:
                skip_jindou_signin = True
                log("⚠ 连续3个账号金豆签到失败，接下来的账号跳过金豆签到流程！")
        else:
            consecutive_jindou_fails = 0
    # ------------------------------------------------
    
    return merged_result

# 推送函数
def push_summary():
    if not summary_logs:
        return
    
    title = "嘉立创签到总结"
    text = "\n".join(summary_logs)
    full_text = f"{title}\n{text}"  # 有些平台不需要单独标题
    
    # Telegram
    telegram_bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
    telegram_chat_id = os.getenv('TELEGRAM_CHAT_ID')
    if telegram_bot_token and telegram_chat_id:
        try:
            url = f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage"
            params = {'chat_id': telegram_chat_id, 'text': full_text}
            response = requests.get(url, params=params)
            if response.status_code == 200:
                log("Telegram-日志已推送")
            else:
                log(f"Telegram-推送失败: {response.text}")
        except Exception as e:
            log(f"Telegram-推送异常: {e}")

    # 企业微信 (WeChat Work)
    wechat_webhook_key = os.getenv('WECHAT_WEBHOOK_KEY')
    if wechat_webhook_key:
        try:
            if wechat_webhook_key.startswith('https://'):
                url = wechat_webhook_key
            else:
                url = f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={wechat_webhook_key}"
            body = {"msgtype": "text", "text": {"content": full_text}}
            response = requests.post(url, json=body)
            # 检查状态码
            if response.status_code != 200:
                log(f"企业微信-推送失败 (HTTP {response.status_code}): {response.text}")
            else:
                # 解析 JSON
                try:
                    resp_json = response.json()
                    errcode = resp_json.get('errcode')
                    if errcode == 0:
                        log("企业微信-日志已推送")
                    else:
                        errmsg = resp_json.get('errmsg', '未知错误')
                        log(f"企业微信-推送失败 (errcode={errcode}, errmsg={errmsg})")
                except Exception as e:
                    log(f"企业微信-推送响应解析失败: {e}, 原始响应: {response.text}")
        except Exception as e:
            log(f"企业微信-推送异常: {e}")

    # 钉钉 (DingTalk)
    dingtalk_webhook = os.getenv('DINGTALK_WEBHOOK')
    if dingtalk_webhook:
        try:
            if dingtalk_webhook.startswith('https://'):
                url = dingtalk_webhook
            else:
                url = f"https://oapi.dingtalk.com/robot/send?access_token={dingtalk_webhook}"
            body = {"msgtype": "text", "text": {"content": full_text}}
            response = requests.post(url, json=body)
            if response.status_code != 200:
                log(f"钉钉-推送失败 (HTTP {response.status_code}): {response.text}")
            else:
                try:
                    resp_json = response.json()
                    errcode = resp_json.get('errcode')
                    if errcode == 0:
                        log("钉钉-日志已推送")
                    else:
                        errmsg = resp_json.get('errmsg', '未知错误')
                        log(f"钉钉-推送失败 (errcode={errcode}, errmsg={errmsg})")
                except Exception as e:
                    log(f"钉钉-推送响应解析失败: {e}, 原始响应: {response.text}")
        except Exception as e:
            log(f"钉钉-推送异常: {e}")

    # PushPlus
    pushplus_token = os.getenv('PUSHPLUS_TOKEN')
    if pushplus_token:
        try:
            url = "http://www.pushplus.plus/send"
            body = {"token": pushplus_token, "title": title, "content": text}
            response = requests.post(url, json=body)
            if response.status_code == 200:
                log("PushPlus-日志已推送")
            else:
                log(f"PushPlus-推送失败: {response.text}")
        except Exception as e:
            log(f"PushPlus-推送异常: {e}")

    # Server酱
    serverchan_sckey = os.getenv('SERVERCHAN_SCKEY')
    if serverchan_sckey:
        try:
            url = f"https://sctapi.ftqq.com/{serverchan_sckey}.send"
            body = {"title": title, "desp": text}
            response = requests.post(url, data=body)
            if response.status_code == 200:
                log("Server酱-日志已推送")
            else:
                log(f"Server酱-推送失败: {response.text}")
        except Exception as e:
            log(f"Server酱-推送异常: {e}")

    # Server酱3
    serverchan3_sckey = os.getenv('SERVERCHAN3_SCKEY') 
    if serverchan3_sckey:
        try:
            textSC3 = "\n\n".join(summary_logs)
            titleSC3 = title
            options = {"tags": "嘉立创|签到"}  # 可选参数，根据需求添加
            response = sc_send(serverchan3_sckey, titleSC3, textSC3, options)            
            if response.get("code") == 0:  # 新版成功返回 code=0
                log("Server酱3-日志已推送")
            else:
                log(f"Server酱3-推送失败: {response}")                
        except Exception as e:
            log(f"Server酱3-推送异常: {str(e)}")    

    # 酷推 (CoolPush)
    coolpush_skey = os.getenv('COOLPUSH_SKEY')
    if coolpush_skey:
        try:
            url = f"https://push.xuthus.cc/send/{coolpush_skey}?c={full_text}"
            response = requests.get(url)
            if response.status_code == 200:
                log("酷推-日志已推送")
            else:
                log(f"酷推-推送失败: {response.text}")
        except Exception as e:
            log(f"酷推-推送异常: {e}")

    # 自定义API
    custom_webhook = os.getenv('CUSTOM_WEBHOOK')
    if custom_webhook:
        try:
            body = {"title": title, "content": text}
            response = requests.post(custom_webhook, json=body)
            if response.status_code == 200:
                log("自定义API-日志已推送")
            else:
                log(f"自定义API-推送失败: {response.text}")
        except Exception as e:
            log(f"自定义API-推送异常: {e}")

def calculate_year_end_prediction(current_beans):
    """计算年底金豆预测数量"""
    try:
        now = datetime.now()
        year_end = datetime(now.year, 12, 31)
        # 计算剩余天数（从明天开始算）
        remaining_days = (year_end - now).days
        if remaining_days < 0:
            remaining_days = 0
            
        # 按照一周大约22个金豆计算
        # 每天平均约 22/7 个
        estimated_future_beans = int(remaining_days * (22 / 7))
        return current_beans + estimated_future_beans
    except Exception:
        return current_beans

def main():
    global in_summary
    
    if len(sys.argv) < 3:
        print("用法: python jlc.py 账号1,账号2,账号3... 密码1,密码2,密码3... [失败退出标志] [账号组编号]")
        print("示例: python jlc.py user1,user2,user3 pwd1,pwd2,pwd3")
        print("示例: python jlc.py user1,user2,user3 pwd1,pwd2,pwd3 true")
        print("示例: python jlc.py user1,user2,user3 pwd1,pwd2,pwd3 true 4")
        print("失败退出标志: 不传或任意值-关闭, true-开启(任意账号签到失败时返回非零退出码)")
        print("账号组编号: 只能输入数字，输入其他值则忽略")
        sys.exit(1)
    
    usernames = [u.strip() for u in sys.argv[1].split(',') if u.strip()]
    passwords = [p.strip() for p in sys.argv[2].split(',') if p.strip()]
    
    # 解析失败退出标志，默认为关闭
    enable_failure_exit = False
    if len(sys.argv) >= 4:
        enable_failure_exit = (sys.argv[3].lower() == 'true')
    
    # 解析第4个参数（账号组编号），只接受纯数字，其他值忽略
    account_group = None
    if len(sys.argv) >= 5:
        if sys.argv[4].isdigit():
            account_group = sys.argv[4]
    
    log(f"失败退出功能: {'开启' if enable_failure_exit else '关闭'}")
    
    if len(usernames) != len(passwords):
        log("❌ 错误: 账号和密码数量不匹配!")
        sys.exit(1)
    
    total_accounts = len(usernames)
    log(f"开始处理 {total_accounts} 个账号的签到任务")
    
    # 存储所有账号的结果
    all_results = []
    
    for i, (username, password) in enumerate(zip(usernames, passwords), 1):
        log(f"开始处理第 {i} 个账号")
        result = process_single_account(username, password, i, total_accounts)
        all_results.append(result)
        
        if i < total_accounts:
            wait_time = random.randint(3, 5)
            log(f"等待 {wait_time} 秒后处理下一个账号...")
            time.sleep(wait_time)
    
    # 输出详细总结
    log("=" * 70)
    log("📊 详细签到任务完成总结")
    log("=" * 70)
    
    oshwhub_success_count = 0
    jindou_success_count = 0
    total_points_reward = 0
    total_jindou_reward = 0
    retried_accounts = []  # 合并所有重试过的账号
    password_error_accounts = []  # 密码错误的账号
    
    # 记录失败的账号
    failed_accounts = []
    
    for result in all_results:
        account_index = result['account_index']
        nickname = result.get('nickname', '未知')
        retry_count = result.get('retry_count', 0)
        password_error = result.get('password_error', False)
        
        if password_error:
            password_error_accounts.append(account_index)
        
        if retry_count > 0:
            retried_accounts.append(account_index)
        
        # 检查是否有失败情况（排除密码错误）
        if (not result['oshwhub_success'] or not result['jindou_success']) and not password_error:
            failed_accounts.append(account_index)
        
        retry_label = ""
        if retry_count > 0:
             retry_label = f" [重试{retry_count}次]"
        
        # 密码错误账号的特殊显示
        if password_error:
            log(f"账号 {account_index} (未知) 详细结果: [密码错误]")
            log("  └── 状态: ❌ 账号或密码错误，跳过此账号")
        else:
            log(f"账号 {account_index} ({nickname}) 详细结果:{retry_label}")
            log(f"  ├── 开源平台: {result['oshwhub_status']}")
            
            # 显示积分变化
            if result['points_reward'] > 0:
                log(f"  ├── 积分变化: {result['initial_points']} → {result['final_points']} (+{result['points_reward']})")
                total_points_reward += result['points_reward']
            elif result['points_reward'] == 0 and result['initial_points'] > 0:
                log(f"  ├── 积分变化: {result['initial_points']} → {result['final_points']} (0)")
            else:
                log(f"  ├── 积分状态: 无法获取积分信息")
            
            log(f"  ├── 金豆签到: {result['jindou_status']}")
            
            # 显示金豆变化
            current_jindou = result['final_jindou']
            if current_jindou == 0 and result['initial_jindou'] > 0:
                current_jindou = result['initial_jindou']
                
            if result['jindou_reward'] > 0:
                jindou_text = f"  ├── 金豆变化: {result['initial_jindou']} → {result['final_jindou']} (+{result['jindou_reward']})"
                if result['has_jindou_reward']:
                    jindou_text += "（有奖励）"
                log(jindou_text)
                total_jindou_reward += result['jindou_reward']
            elif result['jindou_reward'] == 0 and result['initial_jindou'] > 0:
                log(f"  ├── 金豆变化: {result['initial_jindou']} → {result['final_jindou']} (0)")
            else:
                log(f"  ├── 金豆状态: 无法获取金豆信息")
            
            # 预测年底金豆
            if current_jindou > 0:
                predicted_beans = calculate_year_end_prediction(current_jindou)
                log(f"  ├── 预计年底: ≈{predicted_beans} 金豆 (按周均22个预测)")
            
            # 显示礼包领取结果
            for reward_result in result['reward_results']:
                log(f"  ├── {reward_result}")
            
            if result['oshwhub_success']:
                oshwhub_success_count += 1
            if result['jindou_success']:
                jindou_success_count += 1
        
        log("  " + "-" * 50)
    
    # 总体统计
    in_summary = True  # 启用总结收集（推送内容从此处开始）
    if account_group is not None:
        log(f"📈账号组{account_group} 嘉立创签到总体统计:")
    else:
        log("📈 嘉立创签到总体统计:")
    log(f"  ├── 总账号数: {total_accounts}")
    log(f"  ├── 开源平台签到成功: {oshwhub_success_count}/{total_accounts}")
    log(f"  ├── 金豆签到成功: {jindou_success_count}/{total_accounts}")
    
    if total_points_reward > 0:
        log(f"  ├── 总计获得积分: +{total_points_reward}")
    
    if total_jindou_reward > 0:
        log(f"  ├── 总计获得金豆: +{total_jindou_reward}")
    
    # 计算成功率
    oshwhub_rate = (oshwhub_success_count / total_accounts) * 100 if total_accounts > 0 else 0
    jindou_rate = (jindou_success_count / total_accounts) * 100 if total_accounts > 0 else 0
    
    log(f"  ├── 开源平台成功率: {oshwhub_rate:.1f}%")
    log(f"  └── 金豆签到成功率: {jindou_rate:.1f}%")
    
    # 失败账号列表（排除密码错误）
    failed_oshwhub = [r['account_index'] for r in all_results if not r['oshwhub_success'] and not r.get('password_error', False)]
    failed_jindou = [r['account_index'] for r in all_results if not r['jindou_success'] and not r.get('password_error', False)]
    
    if failed_oshwhub:
        log(f"  ⚠ 开源平台失败账号: {', '.join(map(str, failed_oshwhub))}")
    
    if failed_jindou:
        log(f"  ⚠ 金豆签到失败账号: {', '.join(map(str, failed_jindou))}")
        
    if password_error_accounts:
        log(f"  ⚠密码错误的账号: {', '.join(map(str, password_error_accounts))}")
       
    if not failed_oshwhub and not failed_jindou and not password_error_accounts:
        log("  🎉 所有账号全部签到成功!")
    elif password_error_accounts and not failed_oshwhub and not failed_jindou:
        log("  ⚠除了密码错误账号，其他账号全部签到成功!")
    
    log("=" * 70)

    # 推送总结 - 只有在有失败时推送（包括密码错误）
    all_failed_accounts = failed_accounts + password_error_accounts
    if all_failed_accounts:
        push_summary()
    
    # 生成 password-changed.txt
    changed_accounts = [result for result in all_results if result.get('backup_index', -1) >= 0 and not result.get('password_error', False) and result['actual_password'] is not None]
    if changed_accounts:
        with open('password-changed.txt', 'w', encoding='utf-8') as f:
            for result in changed_accounts:
                username = usernames[result['account_index'] - 1]
                f.write(f"{username}:{result['actual_password']}\n")
                f.write(f"# 昵称: {result['nickname']}\n\n")
        log("✅ 已生成 password-changed.txt 文件")
    else:
        log("✅ 没有使用非原密码的账号，无需生成 password-changed.txt")
    
    # 根据失败退出标志决定退出码
    all_failed_accounts = failed_accounts + password_error_accounts
    if enable_failure_exit and all_failed_accounts:
        log(f"❌ 检测到失败的账号: {', '.join(map(str, all_failed_accounts))}")
        if password_error_accounts:
            log(f"❌ 其中密码错误的账号: {', '.join(map(str, password_error_accounts))}")
        log("❌ 由于失败退出功能已开启，返回报错退出码以获得邮件提醒")
        sys.exit(1)
    else:
        if enable_failure_exit:
            log("✅ 所有账号签到成功，程序正常退出")
        else:
            log("✅ 程序正常退出")
        sys.exit(0)

if __name__ == "__main__":
    main()
