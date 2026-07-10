from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

import requests
from playwright.sync_api import (
    BrowserContext,
    Frame,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

BASE_DIR = Path(__file__).resolve().parent
CACHE_FILE = BASE_DIR / "last_grades.json"
STATE_FILE = BASE_DIR / "zzu_jw_state.json"
FAILURE_FILE = BASE_DIR / "failure_count.json"

LOGIN_URL = (
    "https://cas.s.zzu.edu.cn/cas/s/login?"
    "service=https%3A%2F%2Fjwxt.zzu.edu.cn%2Fstudent%2Fsso%2Flogin"
)
HOME_URL = "https://jwxt.zzu.edu.cn/student/home"

ACCOUNT = os.getenv("ZZU_ACCOUNT", "").strip()
PASSWORD = os.getenv("ZZU_PASSWORD", "").strip()
SEND_KEY = os.getenv("SEND_KEY", "").strip()

HEADLESS = os.getenv("HEADLESS", "true").lower() not in {"0", "false", "no"}
PAGE_TIMEOUT = 45_000
MAX_RETRIES = 2


def log(message: str) -> None:
    print(message, flush=True)


def validate_config() -> None:
    missing = []
    if not ACCOUNT:
        missing.append("ZZU_ACCOUNT")
    if not PASSWORD:
        missing.append("ZZU_PASSWORD")
    if not SEND_KEY:
        missing.append("SEND_KEY")
    if missing:
        raise RuntimeError("缺少环境变量：" + "、".join(missing))


def send_wechat(title: str, content: str) -> bool:
    try:
        response = requests.post(
            f"https://sctapi.ftqq.com/{SEND_KEY}.send",
            data={"title": title, "desp": content},
            timeout=20,
        )
        response.raise_for_status()
        result = response.json()
        if result.get("code") == 0:
            log("✓ Server酱推送成功")
            return True
        log(f"Server酱推送失败：{result}")
    except Exception as error:
        log(f"Server酱请求失败：{error}")
    return False


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_old_grades() -> dict[str, str]:
    data = load_json(CACHE_FILE, {})
    return {str(k): str(v) for k, v in data.items()}


def load_failure_count() -> int:
    data = load_json(FAILURE_FILE, {"count": 0})
    return int(data.get("count", 0))


def set_failure_count(count: int) -> None:
    save_json(FAILURE_FILE, {"count": max(0, count)})


def first_visible(page: Page, selectors: list[str]):
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() and locator.is_visible(timeout=1200):
                return locator
        except Exception:
            continue
    return None


def auto_login(page: Page, context: BrowserContext) -> None:
    log("正在登录郑州大学统一身份认证……")
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)

    username = first_visible(
        page,
        [
            'input[placeholder="请输入学号/工号"]',
            'input[name="username"]',
            'input[type="text"]',
        ],
    )
    password = first_visible(
        page,
        [
            'input[type="password"]',
            'input[name="password"]',
        ],
    )
    if username is None or password is None:
        raise RuntimeError(f"没有找到账号或密码输入框，当前地址：{page.url}")

    username.fill(ACCOUNT)
    password.fill(PASSWORD)

    button = first_visible(
        page,
        [
            'button:has-text("登录")',
            'input[type="submit"]',
            '[role="button"]:has-text("登录")',
        ],
    )
    if button is None:
        raise RuntimeError("没有找到登录按钮")

    button.click()

    deadline = time.time() + 90
    while time.time() < deadline:
        if "jwxt.zzu.edu.cn" in page.url and "cas.s.zzu.edu.cn" not in page.url:
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10_000)
            except PlaywrightTimeoutError:
                pass
            context.storage_state(path=str(STATE_FILE))
            log("✓ 登录成功")
            return

        body = ""
        try:
            body = page.locator("body").inner_text(timeout=1500)
        except Exception:
            pass
        if any(word in body for word in ["验证码", "滑块", "动态口令", "认证失败"]):
            raise RuntimeError("登录页面出现验证码、滑块、动态认证或认证失败，云端无法人工处理")

        page.wait_for_timeout(1000)

    raise RuntimeError(f"登录超时，当前地址：{page.url}")


def ensure_logged_in(page: Page, context: BrowserContext) -> None:
    page.goto(HOME_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
    page.wait_for_timeout(1500)

    if "cas.s.zzu.edu.cn" in page.url:
        auto_login(page, context)
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page.wait_for_timeout(1500)

    if "cas.s.zzu.edu.cn" in page.url:
        raise RuntimeError("登录后仍停留在统一认证页面")

    body = page.locator("body").inner_text(timeout=5000)
    if "必须从统一身份认证" in body or "请从统一身份认证" in body:
        auto_login(page, context)
        page.goto(HOME_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        page.wait_for_timeout(1500)

    context.storage_state(path=str(STATE_FILE))


def find_grade_frame(page: Page) -> Frame | None:
    for frame in page.frames:
        if "/grade/sheet/semester-index/" in frame.url:
            return frame
    return None


existing_frame = find_grade_frame(page)
if existing_frame is not None:
    return existing_frame

# ===== 调试信息 =====
print("=" * 60)
print("当前URL：", page.url)
print("当前标题：", page.title())

try:
    body = page.locator("body").inner_text(timeout=5000)
except Exception:
    body = ""

print(body[:3000])
print("=" * 60)

page.screenshot(path="debug.png", full_page=True)

with open("debug.html", "w", encoding="utf-8") as f:
    f.write(page.content())

# 判断是不是掉回登录页
if any(x in body for x in [
    "统一身份认证",
    "请输入学号",
    "请输入密码",
    "登录",
]):
    raise RuntimeError("LOGIN_STATE_EXPIRED")

# 查找"我的成绩"
grade_button = page.get_by_text("我的成绩", exact=True).first

grade_button.wait_for(
    state="visible",
    timeout=15000,
)

grade_button.scroll_into_view_if_needed()

try:
    grade_button.click(timeout=15000)
except PlaywrightTimeoutError:
    grade_button.evaluate("e=>e.click()")

    deadline = time.time() + 35
    while time.time() < deadline:
        frame = find_grade_frame(page)
        if frame:
            return frame
        page.wait_for_timeout(500)

    raise RuntimeError("没有找到“我的成绩”页面 iframe")


def clean_lines(text: str) -> list[str]:
    return [" ".join(line.strip().split()) for line in text.splitlines() if line.strip()]


def is_score(value: str) -> bool:
    value = value.strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        return 0 <= float(value) <= 100
    return value in {
        "通过", "不通过", "优秀", "良好", "中等",
        "及格", "不及格", "合格", "不合格",
    }


def parse_grades(text: str) -> dict[str, str]:
    lines = clean_lines(text)
    grades: dict[str, str] = {}
    ignored = {
        "学生成绩", "成绩单打印", "成绩排名打印", "打印",
        "选择学期：", "课程名称 学分 绩点 成绩 成绩明细",
    }

    for index, line in enumerate(lines):
        if "|" not in line:
            continue
        code = line.split("|", 1)[0].strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", code) or index == 0:
            continue

        course_name = lines[index - 1].strip()
        if not course_name or course_name in ignored:
            continue

        for next_index in range(index + 1, min(index + 8, len(lines))):
            candidate = lines[next_index]
            if "|" in candidate:
                break
            parts = candidate.split()
            if parts and is_score(parts[-1]):
                grades[course_name] = parts[-1]
                break

    return grades


def read_current_grades(page: Page, context: BrowserContext) -> dict[str, str]:
    frame = open_grade_page(page, context)
    frame.locator("body").wait_for(state="visible", timeout=25_000)

    log("正在等待成绩数据加载……")
    deadline = time.time() + 75
    last_text = ""
    reload_attempted = False

    while time.time() < deadline:
        try:
            last_text = frame.locator("body").inner_text(timeout=5000)

            if (
                "初始化数据" not in last_text
                and "课程名称" in last_text
                and "|" in last_text
            ):
                grades = parse_grades(last_text)
                if grades:
                    log(f"✓ 共读取到 {len(grades)} 门课程")
                    return grades

            if (
                not reload_attempted
                and "初始化数据" in last_text
                and "0/0" in last_text
                and time.time() > deadline - 50
            ):
                log("成绩页停留在 0/0，尝试刷新 iframe……")
                frame.reload(wait_until="domcontentloaded")
                reload_attempted = True

        except PlaywrightTimeoutError:
            pass

        page.wait_for_timeout(1000)

    log("成绩页原始文本（前3000字）：")
    log(last_text[:3000])
    raise RuntimeError("成绩数据加载超时或未能解析成绩")


def find_changes(old: dict[str, str], new: dict[str, str]) -> list[str]:
    changes = []
    for course, score in new.items():
        previous = old.get(course)
        if previous is None:
            changes.append(f"{course}：{score}")
        elif previous != score:
            changes.append(f"{course}：{previous} → {score}")
    return changes


def create_context(browser) -> BrowserContext:
    if STATE_FILE.exists():
        try:
            return browser.new_context(storage_state=str(STATE_FILE))
        except Exception as error:
            log(f"旧登录状态读取失败，将重新登录：{error}")
    return browser.new_context()


def run_once() -> None:
    validate_config()
    started = time.time()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=HEADLESS)
        context = create_context(browser)
        page = context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT)

        try:
            current_grades = None
            last_error = None

            for attempt in range(1, MAX_RETRIES + 2):
                try:
                    log(f"开始第 {attempt} 次尝试……")
                    current_grades = read_current_grades(page, context)
                    break
                except Exception as error:
                    last_error = error
                    log(f"第 {attempt} 次尝试失败：{error}")
                    if attempt <= MAX_RETRIES:
                        page.wait_for_timeout(5000)
                        try:
                            page.close()
                        except Exception:
                            pass
                        page = context.new_page()
                        page.set_default_timeout(PAGE_TIMEOUT)

            if current_grades is None:
                raise RuntimeError(f"全部尝试失败：{last_error}")

            old_grades = load_old_grades()
            if not old_grades:
                save_json(CACHE_FILE, current_grades)
                log("首次云端运行：已建立成绩基准，不推送已有成绩")
            else:
                changes = find_changes(old_grades, current_grades)
                if changes:
                    message = "\n\n".join(changes)
                    log("发现成绩变化：\n" + message)
                    if not send_wechat("郑州大学新成绩", message):
                        raise RuntimeError("发现成绩变化，但 Server酱推送失败")
                else:
                    log("未发现成绩变化")
                save_json(CACHE_FILE, current_grades)

            context.storage_state(path=str(STATE_FILE))
            set_failure_count(0)
            log(f"本次检查完成，耗时 {time.time() - started:.1f} 秒")

        finally:
            browser.close()


def main() -> None:
    try:
        run_once()
    except Exception as error:
        count = load_failure_count() + 1
        set_failure_count(count)
        log(f"检查失败（连续第 {count} 次）：{error}")
        traceback.print_exc()

        if count == 5:
            send_wechat(
                "郑大成绩监控连续失败",
                f"程序已连续 5 次检查失败。\n\n最新错误：{error}",
            )
        sys.exit(1)


if __name__ == "__main__":
    main()
