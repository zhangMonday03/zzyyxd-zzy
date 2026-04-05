import os
import sys
import time
import json
import tempfile
import subprocess
import re
import shutil
import threading
import queue
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoAlertPresentException, UnexpectedAlertPresentException, TimeoutException, WebDriverException

# 导入SM2加密方法
try:
    from Utils import pwdEncrypt
    print("✅ 成功加载 SM2 加密依赖")
except ImportError:
    print("❌ 错误: 未找到 Utils.py ，请确保同目录下存在该文件")
    sys.exit(1)


def log(msg, show_time=True):
    """带时间戳的日志输出"""
    if show_time:
        full_msg = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    else:
        full_msg = msg
    print(full_msg, flush=True)


def create_chrome_driver(user_data_dir=None):
    """
    创建Chrome浏览器实例
    """
    chrome_options = Options()
    
    # --- 防检测核心配置 ---
    chrome_options.add_argument("--headless=new") 
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option('useAutomationExtension', False)
    
    # --- 稳定性配置 ---
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-software-rasterizer") # 禁用软件光栅化
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--window-size=1920,1080")
    
    if user_data_dir:
        chrome_options.add_argument(f"--user-data-dir={user_data_dir}")
    else:
        # 如果没传，创建一个临时的（但不推荐，因为不好清理）
        chrome_options.add_argument(f"--user-data-dir={tempfile.mkdtemp()}")
    
    driver = webdriver.Chrome(options=chrome_options)
    
    # 设置页面加载超时为60秒，防止卡死在默认的300秒
    driver.set_page_load_timeout(60)
    driver.set_script_timeout(60)
    
    # --- CDP 命令防检测 ---
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": """
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
        """
    })
    
    return driver


def call_aliv3min_with_timeout(timeout_seconds=180, max_retries=18):
    """调用 AliV3min.py 获取 captchaTicket - 最多重试18次"""
    for attempt in range(max_retries):
        log(f"📞 正在调用 登录脚本 获取 captchaTicket (尝试 {attempt + 1}/{max_retries})...")
        
        process = None
        output_lines = []  # 存储所有输出
        
        try:
            if not os.path.exists('AliV3min.py'):
                log("❌ 错误: 找不到登录依赖 AliV3min.py")
                log("❌ 登录脚本存在异常")
                sys.exit(1)

            process = subprocess.Popen(
                [sys.executable, 'AliV3min.py'],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding='utf-8',
                errors='ignore'
            )
            
            # 引入非阻塞队列读取
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
            captcha_ticket = None
            wait_for_next_line = False
            
            while True:
                elapsed = time.time() - start_time
                if elapsed > timeout_seconds:
                    # 情况1：超时强制停止，需要打印日志
                    log(f"⏰ 登录脚本超过 {timeout_seconds} 秒未完成，强制终止...")
                    try:
                        process.kill()
                        process.wait(timeout=5)
                    except:
                        pass
                    break
                
                try:
                    line = q.get(timeout=0.5)
                except queue.Empty:
                    if process.poll() is not None and not t.is_alive():
                        break
                    continue
                
                if line:
                    output_lines.append(line)  # 保存所有输出
                    
                    if wait_for_next_line:
                        captcha_ticket = line.strip()
                        log(f"✅ 成功获取 captchaTicket")
                        try:
                            process.terminate()
                            process.wait(timeout=5)
                        except:
                            pass
                        return captcha_ticket

                    if "SUCCESS: Obtained CaptchaTicket:" in line:
                        wait_for_next_line = True
                        continue

                    if "captchaTicket" in line:
                        try:
                            match = re.search(r'"captchaTicket"\s*:\s*"([^"]+)"', line)
                            if match:
                                captcha_ticket = match.group(1)
                                log(f"✅ 成功获取 captchaTicket")
                                try:
                                    process.terminate()
                                    process.wait(timeout=5)
                                except:
                                    pass
                                return captcha_ticket
                        except:
                            pass
            
            # 如果没有获取到 captchaTicket
            if not captcha_ticket:
                # 确保进程已终止
                if process and process.poll() is None:
                    try:
                        process.kill()
                        process.wait(timeout=5)
                    except:
                        pass
                
                if attempt < max_retries - 1:
                    log(f"⚠ 未获取到CaptchaTicket，等待5秒后第 {attempt + 2} 次重试...")
                    time.sleep(5)
            else:
                return captcha_ticket
                
        except Exception as e:
            log(f"❌ 调用登录脚本异常: {e}")
            
            # 确保进程已终止
            if process and process.poll() is None:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except:
                    pass
            
            if attempt < max_retries - 1:
                log(f"⚠ 未获取到CaptchaTicket，等待5秒后第 {attempt + 2} 次重试...")
                time.sleep(5)
    
    # 18次都失败，程序退出
    log("❌ 登录脚本存在异常")
    sys.exit(1)


def send_request_via_browser(driver, url, method='POST', body=None):
    """通过浏览器控制台发送请求"""
    try:
        if body:
            body_str = json.dumps(body, ensure_ascii=False)
            js_code = """
            var url = arguments[0];
            var bodyData = arguments[1];
            var callback = arguments[2];
            fetch(url, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'Accept': 'application/json, text/plain, */*',
                    'AppId': 'JLC_PORTAL_PC',
                    'ClientType': 'PC-WEB'
                },
                body: bodyData,
                credentials: 'include'
            }).then(response => {
                if (!response.ok) { return JSON.stringify({error: "HTTP Error " + response.status}); }
                return response.json().then(data => JSON.stringify(data));
            }).then(data => callback(data)).catch(error => callback(JSON.stringify({error: error.toString()})));
            """
            result = driver.execute_async_script(js_code, url, body_str)
        else:
            js_code = """
            var url = arguments[0];
            var callback = arguments[1];
            fetch(url, {
                method: 'GET',
                headers: {'Content-Type': 'application/json', 'Accept': 'application/json, text/plain, */*', credentials: 'include'}
            }).then(response => response.json().then(data => JSON.stringify(data))).then(data => callback(data)).catch(error => callback(JSON.stringify({error: error.toString()})));
            """
            result = driver.execute_async_script(js_code, url)
        try:
            return json.loads(result)
        except json.JSONDecodeError:
            return None
    except Exception as e:
        log(f"❌ 浏览器请求执行失败: {e}")
        return None


def perform_init_session(driver, max_retries=3):
    """执行 Session 初始化"""
    for i in range(max_retries):
        log(f"📡 初始化会话 (尝试 {i + 1}/{max_retries})...")
        response = send_request_via_browser(driver, "https://passport.jlc.com/api/cas/login/get-init-session", 'POST', {"appId": "JLC_PORTAL_PC", "clientType": "PC-WEB"})
        if response and response.get('success') == True and response.get('code') == 200:
            log("✅ 初始化会话成功")
            return True
        else:
            if i < max_retries - 1:
                log(f"⚠ 初始化会话失败，等待2秒后重试...")
                time.sleep(2)
    return False


def login_with_password(driver, username, password, captcha_ticket):
    """登录"""
    url = "https://passport.jlc.com/api/cas/login/with-password"
    try:
        encrypted_username = pwdEncrypt(username)
        encrypted_password = pwdEncrypt(password)
    except Exception as e:
        log(f"❌ SM2加密失败: {e}")
        return 'other_error', None
    
    body = {'username': encrypted_username, 'password': encrypted_password, 'isAutoLogin': False, 'captchaTicket': captcha_ticket}
    log(f"📡 发送登录请求...")
    response = send_request_via_browser(driver, url, 'POST', body)
    if not response: return 'other_error', None
    
    if response.get('success') == True and response.get('code') == 2017: return 'success', response
    if response.get('code') == 10208: return 'password_error', response
    return 'other_error', response


def verify_login_on_member_page(driver, max_retries=3):
    """验证登录"""
    for attempt in range(max_retries):
        log(f"🔍 验证登录状态 ({attempt + 1}/{max_retries})...")
        try:
            # 增加超时处理
            try:
                driver.get("https://member.jlc.com/")
            except TimeoutException:
                log("⚠ 验证页面加载超时，停止加载并尝试检查内容...")
                driver.execute_script("window.stop();")

            WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            time.sleep(5)
            page_source = driver.page_source
            if "客编" in page_source or "customerCode" in page_source:
                log(f"✅ 验证登录成功")
                return True
        except Exception as e:
            log(f"⚠ 验证登录失败: {e}")
        if attempt < max_retries - 1:
            log(f"⏳ 等待2秒后重试...")
            time.sleep(2)
    return False


def switch_to_exam_iframe(driver, wait_time=10):
    """尝试切换到答题系统的iframe"""
    try:
        driver.switch_to.default_content()
        # 先等待iframe出现
        iframe = WebDriverWait(driver, wait_time).until(
            EC.presence_of_element_located((By.ID, "client_context_frame"))
        )
        # 再等待iframe可切换
        WebDriverWait(driver, wait_time).until(
            EC.frame_to_be_available_and_switch_to_it((By.ID, "client_context_frame"))
        )
        # 切换后等待内容加载
        time.sleep(2)
        return True
    except:
        try:
            driver.switch_to.default_content()
            iframe = WebDriverWait(driver, wait_time).until(
                EC.presence_of_element_located((By.NAME, "context_iframe"))
            )
            WebDriverWait(driver, wait_time).until(
                EC.frame_to_be_available_and_switch_to_it((By.NAME, "context_iframe"))
            )
            time.sleep(2)
            return True
        except:
            pass
    return False


def extract_real_exam_url(driver, retry_attempt=0):
    """
    在 member.jlc.com 页面内等待iframe加载并出现开始按钮，
    然后提取真实URL。
    """
    log("🔗 正在打开立创答题中转页...")
    member_exam_url = "https://member.jlc.com/integrated/exam-center/intermediary?examinationRelationUrl=https%3A%2F%2Fexam.kaoshixing.com%2Fexam%2Fbefore_answer_notice%2F1647581&examinationRelationId=1647581"
    
    # 捕获页面加载超时
    try:
        driver.get(member_exam_url)
    except TimeoutException:
        log("⚠ 页面加载超时（可能是资源卡住），尝试停止加载并继续...")
        try:
            driver.execute_script("window.stop();")
        except:
            pass
    except Exception as e:
        log(f"⚠ 打开页面异常: {str(e)[:100]}")
    
    wait_time = 15
    log("⏳ 等待页面及 Iframe 加载 (15s)...")
    
    try:
        # 先等待页面基本加载
        try:
            WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        except:
            pass
        time.sleep(3)
        
        # 尝试切换到iframe
        if switch_to_exam_iframe(driver, wait_time=wait_time):
            try:
                # 关键优化: 等待按钮出现并且可点击,确保内容完全加载
                WebDriverWait(driver, wait_time).until(
                    EC.element_to_be_clickable((By.XPATH, '//*[@id="startExamBtn"]'))
                )
                # 额外等待,确保页面完全稳定
                time.sleep(2)
                
                # 提取当前 iframe 的真实 URL
                real_url = driver.execute_script("return window.location.href;")
                driver.switch_to.default_content()
                
                if real_url and "kaoshixing.com" in real_url:
                    log(f"✅ 提取答题链接成功")
                    return real_url
                else:
                    log(f"⚠ 提取的URL无效: {real_url}")
            except TimeoutException:
                log(f"⚠ iframe 内容加载超时")
                driver.switch_to.default_content()
    except Exception as e:
        log(f"⚠ 页面加载异常: {str(e)[:50]}")
        try:
            driver.switch_to.default_content()
        except: 
            pass

    return None


def click_start_exam_button(driver):
    """点击开始答题 (在顶层窗口)"""
    log(f"🔍 检查开始答题按钮...")
    xpaths = ['//*[@id="startExamBtn"]', '//button[contains(@class, "btn-primary")]//span[contains(text(), "开始答题")]', '//span[contains(text(), "开始答题")]']
    
    for xpath in xpaths:
        try:
            elem = WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.XPATH, xpath)))
            if elem.is_displayed():
                try:
                    elem.click()
                except:
                    driver.execute_script("arguments[0].click();", elem)
                log("✅ 已点击开始答题按钮")
                return True
        except:
            continue
    log("❌ 未找到开始答题按钮")
    return False


def handle_possible_alerts(driver):
    try:
        alert = driver.switch_to.alert
        log(f"⚠ 检测到弹窗: {alert.text}，正在接受...")
        alert.accept()
        return True
    except NoAlertPresentException:
        return False
    except Exception:
        return False


def inject_dati_js(driver):
    """读取并注入 dati.js"""
    log("💉 正在注入 dati.js 答题脚本...")
    try:
        if not os.path.exists('dati.js'):
            log("❌ 错误: 当前目录下找不到 dati.js 文件")
            return False
            
        with open('dati.js', 'r', encoding='utf-8') as f:
            js_content = f.read()
            
        # 注入 JS
        driver.execute_script(js_content)
        log("✅ 答题脚本注入成功，开始自动答题...")
        return True
    except Exception as e:
        log(f"❌ 注入脚本失败: {e}")
        return False


def wait_for_exam_completion_with_js(driver, timeout_seconds=180):
    """
    等待 JS 执行完成并跳转到结果页
    """
    log(f"⏳ 等待组卷...")
    start_time = time.time()
    last_log_time = start_time
    js_injected = False
    
    while time.time() - start_time < timeout_seconds:
        handle_possible_alerts(driver)
        
        try:
            current_url = driver.current_url
            
            # 定期日志
            if time.time() - last_log_time > 15:
                log(f"ℹ 当前页面: {current_url.split('?')[0]}")
                last_log_time = time.time()
            
            # 1. 成功跳转至结果页
            if '/result/' in current_url:
                log(f"✅ 成功跳转至答题结果页")
                return True
            
            # 2. 如果在答题页，且还没注入 JS，则注入
            if 'exam_start' in current_url and not js_injected:
                # 稍微等待页面加载
                time.sleep(2)
                if inject_dati_js(driver):
                    js_injected = True
                else:
                    # 注入失败，可能需要重试或者直接退出
                    pass
            
        except UnexpectedAlertPresentException:
            handle_possible_alerts(driver)
        except Exception:
            time.sleep(1)
            
        time.sleep(2)
    
    log("⏰ 等待超时，未检测到结果页 URL")
    return False


def get_exam_score(driver):
    """获取分数"""
    log("🔍 获取分数...")
    try:
        WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        time.sleep(2)
        try:
            score_elem = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CLASS_NAME, "score")))
            score = int(re.search(r'\d+', score_elem.text.strip()).group())
            log(f"📊 提取到分数: {score}")
            return score
        except: 
            pass
        
        try:
            elements = driver.find_elements(By.XPATH, "//*[contains(text(), '分')]")
            for el in elements:
                txt = el.text
                if re.match(r'^\d+$', txt) or re.match(r'^\d+\s*分$', txt):
                     score = int(re.search(r'\d+', txt).group())
                     log(f"📊 提取到分数: {score}")
                     return score
        except: 
            pass
    except Exception as e:
        log(f"❌ 获取分数失败: {e}")
    return None


def perform_exam_process(driver, max_retries=3):
    """
    执行答题流程（从打开中转页到获取分数）
    使用同一个浏览器实例重试
    """
    for exam_attempt in range(max_retries):
        log(f"📝 开始答题流程 (第 {exam_attempt + 1}/{max_retries} 次尝试)...")
        
        try:
            # 步骤 1: 提取链接 (内部重试5次)
            # 这里是针对页面加载超时/资源卡住的专门重试
            real_exam_url = None
            extraction_max_retries = 5
            
            for extract_attempt in range(extraction_max_retries):
                real_exam_url = extract_real_exam_url(driver, retry_attempt=extract_attempt)
                if real_exam_url:
                    break
                log(f"⚠ 提取链接失败，重试 ({extract_attempt+1}/{extraction_max_retries})...")
                time.sleep(3)
                
            if not real_exam_url:
                raise Exception(f"无法提取考试链接 (重试{extraction_max_retries}次均失败)")
            
            # 步骤 2: 直接跳转到真实考试页面
            try:
                driver.get(real_exam_url)
            except TimeoutException:
                log("⚠ 考试页面加载超时，尝试停止加载...")
                driver.execute_script("window.stop();")
            
            # 步骤 3: 点击开始按钮
            if not click_start_exam_button(driver):
                raise Exception("找不到开始按钮")
                
            # 步骤 4: 注入 JS 并等待结果
            if not wait_for_exam_completion_with_js(driver):
                raise Exception("答题超时")
                
            # 步骤 5: 获取分数
            score = get_exam_score(driver)
            
            if score is not None:
                # 只有及格才算真正成功返回
                if score >= 60:
                    return True, score
                else:
                    # 分数不及格处理逻辑
                    if exam_attempt < max_retries - 1:
                        log(f"⚠ 分数 {score} 不及格，正在准备补考... (剩余机会: {max_retries - 1 - exam_attempt})")
                        time.sleep(3)
                        # 强制进入下一次外层循环进行补考
                        continue
                    else:
                        log(f"❌ 不及格重试已达最大次数，最终分数: {score}")
                        return True, score
            else:
                raise Exception("未能获取到分数")
                
        except Exception as e:
            log(f"❌ 答题流程异常: {e}")
            if exam_attempt < max_retries - 1:
                log(f"⏳ 等待3秒后重试答题流程...")
                time.sleep(3)
            else:
                log(f"❌ 答题流程已达最大重试次数")
                return False, None
    
    return False, None


def perform_login_flow(driver, username, password, max_retries=3):
    """
    执行完整的登录流程（包括Session初始化、登录、验证）
    不在此处重建浏览器，只负责逻辑执行
    """
    session_fail_count = 0
    
    for login_attempt in range(max_retries):
        log(f"🔐 开始登录流程 (尝试 {login_attempt + 1}/{max_retries})...")
        
        try:
            # 步骤 1: 打开登录页
            # 增加超时处理
            try:
                driver.get("https://passport.jlc.com")
            except TimeoutException:
                log("⚠ 登录页面加载超时，尝试停止加载继续...")
                driver.execute_script("window.stop();")

            WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
            
            # 步骤 2: 初始化 Session
            if not perform_init_session(driver):
                session_fail_count += 1
                if session_fail_count >= 3:
                    log("❌ 浏览器环境存在异常")
                    raise Exception("初始化 Session 失败")
                raise Exception("初始化 Session 失败")
            
            # 重置失败计数（成功了就清零）
            session_fail_count = 0
            
            # 步骤 3: 获取 CaptchaTicket（全局重试5次，失败直接退出程序）
            captcha_ticket = call_aliv3min_with_timeout()
            if not captcha_ticket:
                # 这里不会执行到，因为 call_aliv3min_with_timeout 失败会直接 sys.exit(1)
                raise Exception("获取 CaptchaTicket 失败")
            
            # 步骤 4: 登录
            status, login_res = login_with_password(driver, username, password, captcha_ticket)
            if status == 'password_error':
                return 'password_error'
            if status != 'success':
                raise Exception("登录失败")
            
            # 步骤 5: 验证登录
            if not verify_login_on_member_page(driver):
                raise Exception("登录验证失败")
            
            log("✅ 登录流程完成")
            return 'success'
            
        except Exception as e:
            log(f"❌ 登录流程异常: {e}")
            if login_attempt < max_retries - 1:
                log(f"⏳ 重试登录流程...")
                time.sleep(3)
                # 不在这里重建 driver，依靠外层循环
            else:
                log(f"❌ 登录流程已达最大重试次数")
                return 'login_failed'
    
    return 'login_failed'


def process_single_account(username, password, account_index, total_accounts):
    """处理单个账号 - 支持多密码重试和断点记忆"""
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
    
    # 构建密码候选列表（去重并保持顺序，优先尝试传入的密码）
    all_passwords = [password]
    for bp in backup_passwords:
        if bp != password:
            all_passwords.append(bp)
    
    result = {
        'account_index': account_index, 
        'status': '未知', 
        'success': False, 
        'score': 0, 
        'highest_score': 0, 
        'failure_reason': None
    }
    
    current_pwd_idx = 0
    max_session_retries = 3  # 定义全流程重试的最大次数（针对非密码错误的异常）
    
    # 外层循环：处理非密码错误导致的“全流程重试”
    for session_attempt in range(max_session_retries):
        
        # 内层循环：遍历密码列表
        while current_pwd_idx < len(all_passwords):
            current_password = all_passwords[current_pwd_idx]
            log(f"🌐 启动浏览器 (账号 {account_index} - 尝试密码 {current_pwd_idx + 1}/{len(all_passwords)})...")
            
            # 显式管理临时目录
            user_data_dir = tempfile.mkdtemp()
            driver = None
            
            try:
                driver = create_chrome_driver(user_data_dir)
            
                # --- 阶段 1: 登录流程 ---
                # perform_login_flow 内部已有3次重试，如果它返回 login_failed，说明环境恶劣
                login_status = perform_login_flow(driver, username, current_password, max_retries=3)
                
                if login_status == 'password_error':
                    log(f"❌ 密码错误: {current_password}，尝试下一个备用密码...")
                    # 明确证明密码错误，永久跳过此密码
                    current_pwd_idx += 1
                    driver.quit()
                    shutil.rmtree(user_data_dir, ignore_errors=True)
                    continue  # 立即进入下一次内层循环尝试新密码
                
                if login_status != 'success':
                    # 登录失败，但不是明确的密码错误（如网络问题、验证码问题等）
                    # 正常进入全流程重试，记忆密码进度（即不增加 current_pwd_idx）
                    log(f"⚠ 登录流程异常 (非密码错误)，准备重新开始全流程...")
                    # 跳出内层循环，让外层循环 (session_attempt) 触发重试
                    # 此时 current_pwd_idx 未改变，下次重试仍用当前密码
                    driver.quit()
                    shutil.rmtree(user_data_dir, ignore_errors=True)
                    break 
                
                # --- 阶段 2: 答题流程 ---
                # 登录成功，开始答题
                # 注意：这里调用修改后的函数，它内部处理了提取链接重试(5次)和分数补考逻辑
                exam_success, score = perform_exam_process(driver, max_retries=3)
                
                if exam_success and score is not None:
                    result['score'] = score
                    result['highest_score'] = score
                    if score >= 60:
                        log(f"🎉 答题通过! 分数: {score}")
                        result['success'] = True
                        result['status'] = '答题成功'
                    else:
                        log(f"😢 分数未达标: {score}")
                        result['status'] = '分数不达标'
                        result['failure_reason'] = f"得分{score}分"
                else:
                    result['status'] = '答题失败'
                    result['failure_reason'] = '答题流程失败'
                
                # 任务完成（无论分数是否达标），退出函数
                driver.quit()
                shutil.rmtree(user_data_dir, ignore_errors=True)
                return result

            except Exception as e:
                log(f"❌ 账号处理异常: {e}")
                if driver: 
                    try: driver.quit()
                    except: pass
                if os.path.exists(user_data_dir):
                    try: shutil.rmtree(user_data_dir, ignore_errors=True)
                    except: pass
                # 发生未捕获异常，视为非密码错误，跳出内层循环进行全流程重试
                break
        
        # 检查是否因为所有密码都试完了才退出内层循环
        if current_pwd_idx >= len(all_passwords):
            log("❌ 所有候选密码均提示错误，放弃该账号")
            result['status'] = '所有密码错误'
            result['failure_reason'] = '所有候选密码均验证失败'
            return result
        
        # 如果还在外层循环中，说明是触发了全流程重试
        if session_attempt < max_session_retries - 1:
            log(f"⏳ 等待5秒后进行第 {session_attempt + 2} 次全流程重试 (从密码 {current_pwd_idx + 1} 继续)...")
            time.sleep(5)
    
    # 外层循环结束，说明多次重试均失败（非密码错误）
    result['status'] = '流程异常'
    result['failure_reason'] = '多次尝试登录或答题均失败(非密码错误)'
    return result


def main():
    if len(sys.argv) < 3:
        print("用法: python jlc.py 账号1,账号2... 密码1,密码2... [失败退出标志]")
        sys.exit(1)
    usernames = sys.argv[1].split(',')
    passwords = sys.argv[2].split(',')
    fail_exit = len(sys.argv) >= 4 and sys.argv[3].lower() == 'true'
    
    if len(usernames) != len(passwords): 
        log("❌ 账号密码数量不匹配")
        sys.exit(1)
    
    # 输出初始信息
    log(f"检测到有 {len(usernames)} 个账号需要答题，失败退出功能已{'开启' if fail_exit else '未开启'}", show_time=False)
    
    # 存储账号信息以便重试
    accounts_list = []
    for i, (u, p) in enumerate(zip(usernames, passwords), 1):
        accounts_list.append({
            'username': u,
            'password': p,
            'index': i,
            'result': None
        })

    # 第一轮运行
    for i, acc in enumerate(accounts_list):
        log(f"\n{'='*40}\n正在处理账号 {acc['index']}\n{'='*40}", show_time=False)
        res = process_single_account(acc['username'], acc['password'], acc['index'], len(usernames))
        acc['result'] = res
        if i < len(accounts_list) - 1: 
            time.sleep(5)
            
    # 最终重试逻辑
    failed_accounts = [acc for acc in accounts_list if not acc['result']['success']]
    
    if failed_accounts:
        log("\n" + "="*40, show_time=False)
        log(f"🔄 检测到 {len(failed_accounts)} 个账号失败，开始最终重试流程", show_time=False)
        log("="*40, show_time=False)
        
        for i, acc in enumerate(failed_accounts):
            idx = acc['index']
            u = acc['username']
            p = acc['password']
            original_result = acc['result']
            original_reason = original_result.get('failure_reason')
            
            log(f"\n🔄 [账号 {idx}] 第一次最终重试 (原失败原因: {original_reason})", show_time=False)
            
            # 第一次重试
            retry_res_1 = process_single_account(u, p, idx, len(usernames))
            
            if retry_res_1['success']:
                log(f"✅ [账号 {idx}] 重试成功", show_time=False)
                acc['result'] = retry_res_1
            else:
                reason_1 = retry_res_1.get('failure_reason')
                log(f"❌ [账号 {idx}] 重试失败 (原因: {reason_1})", show_time=False)
                
                if reason_1 == original_reason:
                    log(f"⚠ [账号 {idx}] 失败原因未改变，放弃继续重试", show_time=False)
                    acc['result'] = retry_res_1
                else:
                    log(f"❓ [账号 {idx}] 失败原因改变 (原: {original_reason} -> 新: {reason_1})，进行最后一次重试", show_time=False)
                    time.sleep(2)
                    
                    # 第二次重试
                    retry_res_2 = process_single_account(u, p, idx, len(usernames))
                    acc['result'] = retry_res_2
                    
                    if retry_res_2['success']:
                        log(f"✅ [账号 {idx}] 第二次重试成功", show_time=False)
                    else:
                        log(f"❌ [账号 {idx}] 第二次重试失败 (原因: {retry_res_2.get('failure_reason')})", show_time=False)

            if i < len(failed_accounts) - 1:
                time.sleep(3)
        
    log("\n" + "="*40, show_time=False)
    log("📊 立创答题结果总结", show_time=False)
    log("="*40, show_time=False)
    
    # 重新提取结果
    all_results = [acc['result'] for acc in accounts_list]
    
    has_failure = False
    for res in all_results:
        if res['success']: 
            log(f"账号{res['account_index']}: 立创题库答题成功✅ 分数:{res['score']}", show_time=False)
        else: 
            has_failure = True
            log(f"账号{res['account_index']}: 立创题库答题失败❌ 原因:{res['failure_reason']}", show_time=False)
    
    if fail_exit and has_failure: 
        sys.exit(1)
    sys.exit(0)

if __name__ == "__main__":
    main()
