from __future__ import annotations

from pathlib import Path
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Frame,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)
import json
import logging
import os
import re
import sys
import time
import traceback

import requests


# ============================================================
# 配置
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "zzu_jw_state.json"
CACHE_FILE = BASE_DIR / "last_grades.json"
LOG_FILE = BASE_DIR / "grade_monitor.log"

LOGIN_URL = (
    "https://cas.s.zzu.edu.cn/cas/s/login?"
    "service=https%3A%2F%2Fjwxt.zzu.edu.cn%2Fstudent%2Fsso%2Flogin"
)

HOME_URL = "https://jwxt.zzu.edu.cn/student/home"

SEND_KEY = os.getenv("SEND_KEY", "").strip()

CHECK_INTERVAL = 600
HEADLESS = os.getenv("HEADLESS", "false").strip().lower() in {
    "1", "true", "yes", "on"
}
IS_GITHUB_ACTIONS = os.getenv("GITHUB_ACTIONS", "").strip().lower() == "true"
BROWSER_CHANNEL = None if IS_GITHUB_ACTIONS else "msedge"
PAGE_TIMEOUT = 30000

DEBUG_SCREENSHOT = BASE_DIR / "debug.png"
DEBUG_HTML = BASE_DIR / "debug.html"


# ============================================================
# 日志
# ============================================================

def setup_logger() -> logging.Logger:
    logger = logging.getLogger("zzu_grade_monitor")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger


LOGGER = setup_logger()


def log(message: str, level: str = "info") -> None:
    print(message)

    if level == "error":
        LOGGER.error(message)
    elif level == "warning":
        LOGGER.warning(message)
    else:
        LOGGER.info(message)


# ============================================================
# 推送
# ============================================================

def send_wechat(title: str, content: str) -> bool:
    if not SEND_KEY or "请填写" in SEND_KEY:
        log("尚未填写 Server酱 SendKey，跳过推送。", "warning")
        return False

    url = f"https://sctapi.ftqq.com/{SEND_KEY}.send"

    try:
        response = requests.post(
            url,
            data={"title": title, "desp": content},
            timeout=15,
        )
        response.raise_for_status()
        result = response.json()

        if result.get("code") == 0:
            log("微信推送成功。")
            return True

        log(f"微信推送失败：{result}", "warning")

    except requests.exceptions.Timeout:
        log("Server酱请求超时。", "warning")
    except requests.exceptions.RequestException as error:
        log(f"Server酱请求失败：{error}", "warning")
    except ValueError:
        log("Server酱返回内容无法解析。", "warning")

    return False


# ============================================================
# 缓存
# ============================================================

def load_old_grades() -> dict[str, str]:
    if not CACHE_FILE.exists():
        return {}

    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        return {str(key): str(value) for key, value in data.items()}
    except Exception as error:
        log(f"读取成绩缓存失败：{error}", "warning")
        return {}


def save_grades(grades: dict[str, str]) -> None:
    CACHE_FILE.write_text(
        json.dumps(grades, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ============================================================
# 通用工具
# ============================================================

def format_duration(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days}天")
    if hours or days:
        parts.append(f"{hours}小时")
    if minutes or hours or days:
        parts.append(f"{minutes}分钟")
    parts.append(f"{secs}秒")
    return "".join(parts)


def countdown(seconds: int) -> None:
    remaining = max(0, int(seconds))

    while remaining > 0:
        minutes, secs = divmod(remaining, 60)
        print(
            f"\r下一次检查倒计时：{minutes:02d}:{secs:02d}",
            end="",
            flush=True,
        )
        time.sleep(1)
        remaining -= 1

    print("\r下一次检查倒计时：00:00")


def is_cas_page(page: Page) -> bool:
    return "cas.s.zzu.edu.cn" in page.url


def page_has_fake_login_message(page: Page) -> bool:
    try:
        text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        return False

    keywords = [
        "必须从统一身份认证",
        "请从统一身份认证",
        "统一认证登录",
        "统一身份认证登录",
    ]
    return any(keyword in text for keyword in keywords)


def save_debug_files(page: Page, reason: str) -> None:
    """保存 GitHub Actions 或本地失败现场。"""
    print("=" * 60)
    print(f"调试原因：{reason}")
    print(f"当前网址：{page.url}")

    try:
        print(f"页面标题：{page.title()}")
    except Exception as error:
        print(f"无法读取页面标题：{error}")

    try:
        body_text = page.locator("body").inner_text(timeout=5000)
    except Exception as error:
        body_text = f"无法读取页面文本：{error}"

    print("当前页面文本：")
    print("-" * 60)
    print(body_text[:5000])
    print("-" * 60)

    try:
        page.screenshot(path=str(DEBUG_SCREENSHOT), full_page=True)
        print(f"已保存截图：{DEBUG_SCREENSHOT}")
    except Exception as error:
        print(f"保存截图失败：{error}")

    try:
        DEBUG_HTML.write_text(page.content(), encoding="utf-8")
        print(f"已保存HTML：{DEBUG_HTML}")
    except Exception as error:
        print(f"保存HTML失败：{error}")

    print("=" * 60)


# ============================================================
# 浏览器状态
# ============================================================

def create_context(browser: Browser) -> BrowserContext:
    if not STATE_FILE.exists():
        raise RuntimeError(
            f"未找到登录状态文件：{STATE_FILE}\n"
            "请先运行 login_save.py。"
        )

    try:
        log("正在载入已保存的登录状态……")
        return browser.new_context(storage_state=str(STATE_FILE))
    except Exception as error:
        raise RuntimeError(
            f"无法读取登录状态文件：{error}\n"
            "请重新运行 login_save.py。"
        ) from error


def enter_jwxt_through_cas(
    page: Page,
    context: BrowserContext,
) -> None:
    """
    每轮都从 CAS 的 service 地址进入。
    Cookie 有效时，CAS 会自动跳回 JWXT。
    Cookie 无效时，会停在 CAS 登录页，不再尝试在监控脚本里自动填密码。
    """
    log("正在通过统一认证入口进入教务系统……")

    page.goto(
        LOGIN_URL,
        wait_until="domcontentloaded",
        timeout=PAGE_TIMEOUT,
    )

    deadline = time.time() + 30

    while time.time() < deadline:
        if (
            "jwxt.zzu.edu.cn" in page.url
            and "cas.s.zzu.edu.cn" not in page.url
        ):
            try:
                page.wait_for_load_state(
                    "domcontentloaded",
                    timeout=10000,
                )
            except PlaywrightTimeoutError:
                pass

            page.wait_for_timeout(1500)

            if page_has_fake_login_message(page):
                raise RuntimeError("FAKE_LOGIN_PAGE")

            context.storage_state(path=str(STATE_FILE))
            return

        if is_cas_page(page):
            try:
                password_box = page.locator(
                    'input[type="password"]'
                ).first
                if password_box.count() > 0:
                    try:
                        password_box.wait_for(
                            state="visible",
                            timeout=1000,
                        )
                        raise RuntimeError("LOGIN_STATE_EXPIRED")
                    except PlaywrightTimeoutError:
                        pass
            except PlaywrightTimeoutError:
                pass

        page.wait_for_timeout(500)

    raise RuntimeError(
        f"统一认证跳转超时，当前地址：{page.url}"
    )


# ============================================================
# 成绩解析
# ============================================================

def clean_lines(text: str) -> list[str]:
    result: list[str] = []

    for line in text.splitlines():
        cleaned = " ".join(line.strip().split())
        if cleaned:
            result.append(cleaned)

    return result


def is_score(value: str) -> bool:
    value = value.strip()

    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        number = float(value)
        return 0 <= number <= 100

    return value in {
        "通过",
        "不通过",
        "优秀",
        "良好",
        "中等",
        "及格",
        "不及格",
        "合格",
        "不合格",
    }


def parse_grades(text: str) -> dict[str, str]:
    lines = clean_lines(text)
    grades: dict[str, str] = {}

    ignored_names = {
        "学生成绩",
        "成绩单打印",
        "成绩排名打印",
        "打印",
        "选择学期：",
        "课程名称 学分 绩点 成绩 成绩明细",
    }

    for index, line in enumerate(lines):
        if "|" not in line:
            continue

        course_code = line.split("|")[0].strip()

        if not re.fullmatch(r"[A-Za-z0-9_-]+", course_code):
            continue

        if index == 0:
            continue

        course_name = lines[index - 1].strip()

        if not course_name or course_name in ignored_names:
            continue

        score: str | None = None

        for next_index in range(
            index + 1,
            min(index + 7, len(lines)),
        ):
            candidate = lines[next_index]

            if "|" in candidate:
                break

            parts = candidate.split()

            if not parts:
                continue

            possible_score = parts[-1]

            if is_score(possible_score):
                score = possible_score
                break

        if score is not None:
            grades[course_name] = score

    return grades


# ============================================================
# 成绩页面操作
# ============================================================

def find_grade_frame(page: Page) -> Frame | None:
    for frame in page.frames:
        if "/grade/sheet/semester-index/" in frame.url:
            return frame
    return None


def open_grade_page(
    page: Page,
    context: BrowserContext,
) -> Frame:
    enter_jwxt_through_cas(page, context)

    # CAS 回跳后的页面未必正好在首页，因此显式进入首页。
    page.goto(
        HOME_URL,
        wait_until="domcontentloaded",
        timeout=PAGE_TIMEOUT,
    )
    page.wait_for_timeout(1500)

    if is_cas_page(page):
        raise RuntimeError("LOGIN_STATE_EXPIRED")

    if page_has_fake_login_message(page):
        raise RuntimeError("FAKE_LOGIN_PAGE")

    existing_frame = find_grade_frame(page)
    if existing_frame is not None:
        return existing_frame

    # 优先使用页面文本定位，避免首页卡片 class 改动后失效。
    grade_button = page.get_by_text("我的成绩", exact=True).first

    try:
        grade_button.wait_for(
            state="visible",
            timeout=25000,
        )
    except PlaywrightTimeoutError as error:
        save_debug_files(page, "首页未找到“我的成绩”入口")

        try:
            body_text = page.locator("body").inner_text(timeout=5000)
        except Exception:
            body_text = ""

        login_keywords = [
            "统一身份认证",
            "请输入学号",
            "请输入密码",
            "账号登录",
            "扫码登录",
        ]
        if is_cas_page(page) or any(
            keyword in body_text for keyword in login_keywords
        ):
            raise RuntimeError("LOGIN_STATE_EXPIRED") from error

        raise RuntimeError(
            f"首页未找到“我的成绩”入口，当前地址：{page.url}"
        ) from error

    grade_button.scroll_into_view_if_needed()

    try:
        grade_button.click(timeout=15000)
    except PlaywrightTimeoutError:
        grade_button.evaluate("element => element.click()")

    deadline = time.time() + 30

    while time.time() < deadline:
        frame = find_grade_frame(page)
        if frame is not None:
            return frame
        page.wait_for_timeout(500)

    raise RuntimeError("没有找到成绩页面 iframe")


def read_current_grades(
    page: Page,
    context: BrowserContext,
) -> dict[str, str]:
    frame = open_grade_page(page, context)

    frame.locator("body").wait_for(
        state="visible",
        timeout=20000,
    )

    log("已经进入成绩页面，等待成绩数据加载……")

    deadline = time.time() + 60
    last_text = ""
    reload_attempted = False

    while time.time() < deadline:
        try:
            last_text = frame.locator("body").inner_text(
                timeout=5000,
            )

            if any(
                keyword in last_text
                for keyword in [
                    "必须从统一身份认证",
                    "请从统一身份认证",
                    "统一身份认证登录",
                ]
            ):
                raise RuntimeError("FAKE_LOGIN_PAGE")

            if (
                "初始化数据" not in last_text
                and "课程名称" in last_text
                and "|" in last_text
            ):
                break

            if (
                not reload_attempted
                and "初始化数据" in last_text
                and "0/0" in last_text
                and time.time() > deadline - 45
            ):
                log(
                    "成绩页长时间停留在0/0，尝试刷新 iframe。",
                    "warning",
                )
                frame.reload(wait_until="domcontentloaded")
                reload_attempted = True

        except PlaywrightTimeoutError:
            pass

        page.wait_for_timeout(1000)

    else:
        log("等待60秒后，成绩仍未加载完成。", "warning")
        print("-" * 60)
        print(last_text[:5000])
        print("-" * 60)
        raise RuntimeError("成绩数据加载超时")

    grades = parse_grades(last_text)

    if not grades:
        log("成绩页面已经加载，但未能解析成绩。", "warning")
        print("-" * 60)
        print(last_text[:5000])
        print("-" * 60)
        raise RuntimeError("没有解析出任何成绩")

    return grades


# ============================================================
# 成绩比较
# ============================================================

def find_changes(
    old_grades: dict[str, str],
    new_grades: dict[str, str],
) -> list[str]:
    changes: list[str] = []

    for course_name, new_score in new_grades.items():
        old_score = old_grades.get(course_name)

        if old_score is None:
            changes.append(f"{course_name}：{new_score}")
        elif str(old_score) != str(new_score):
            changes.append(
                f"{course_name}：{old_score} → {new_score}"
            )

    return changes


# ============================================================
# 主程序
# ============================================================

def main() -> None:
    started_at = time.time()
    check_count = 0
    success_count = 0
    failed_count = 0
    push_count = 0

    print("=" * 60)
    print("郑州大学成绩监控已启动")
    print("本脚本不保存账号密码，只使用 login_save.py 生成的登录状态")
    print("每轮均从统一认证入口进入，避免直接访问教务系统产生假登录页")
    print(f"检查间隔：{CHECK_INTERVAL // 60}分钟")
    print("第一次运行只建立基准，不推送已有成绩")
    print("按 Ctrl+C 可以停止")
    print("=" * 60)

    with sync_playwright() as playwright:
        launch_options = {
            "headless": HEADLESS,
        }
        if BROWSER_CHANNEL:
            launch_options["channel"] = BROWSER_CHANNEL

        log(
            "浏览器模式："
            + (
                "GitHub Actions Chromium 无头模式"
                if IS_GITHUB_ACTIONS
                else f"本地 {BROWSER_CHANNEL or 'Chromium'} 模式"
            )
        )

        browser = playwright.chromium.launch(**launch_options)

        context = create_context(browser)
        page = context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT)

        try:
            while True:
                check_count += 1
                check_started_at = time.time()

                print()
                print("=" * 60)
                print(f"第 {check_count} 次检查")
                print(
                    "当前时间：",
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                )

                try:
                    current_grades = read_current_grades(
                        page,
                        context,
                    )
                    old_grades = load_old_grades()

                    print("当前最新学期成绩：")
                    for course_name, score in current_grades.items():
                        print(f"  {course_name}：{score}")

                    if not old_grades:
                        save_grades(current_grades)
                        log("第一次运行，已保存当前成绩作为基准。")
                        log("本次不会推送已有成绩。")
                    else:
                        changes = find_changes(
                            old_grades,
                            current_grades,
                        )

                        if changes:
                            log("发现成绩变化：")
                            for item in changes:
                                print(f"  {item}")
                                LOGGER.info(f"成绩变化：{item}")

                            push_success = send_wechat(
                                "郑州大学新成绩",
                                "\n\n".join(changes),
                            )

                            if push_success:
                                push_count += 1
                                save_grades(current_grades)
                                log("成绩缓存已更新。")
                            else:
                                log(
                                    "推送失败，本次不更新缓存，下次继续尝试。",
                                    "warning",
                                )
                        else:
                            log("无事发生。")
                            save_grades(current_grades)

                    context.storage_state(path=str(STATE_FILE))
                    success_count += 1

                except RuntimeError as error:
                    failed_count += 1
                    error_text = str(error)
                    log(f"本轮检查失败：{error_text}", "error")

                    if error_text not in {
                        "LOGIN_STATE_EXPIRED",
                        "FAKE_LOGIN_PAGE",
                    }:
                        save_debug_files(page, error_text)

                    if error_text in {
                        "LOGIN_STATE_EXPIRED",
                        "FAKE_LOGIN_PAGE",
                    }:
                        send_wechat(
                            "郑大成绩监控登录状态失效",
                            "请重新运行 login_save.py 获取登录状态。",
                        )
                        print()
                        print("登录状态已失效。")
                        print("请关闭本程序，重新运行 login_save.py。")

                except PlaywrightTimeoutError as error:
                    failed_count += 1
                    log(f"页面加载超时：{error}", "error")

                except Exception as error:
                    failed_count += 1
                    log(f"本轮检查出现异常：{error}", "error")
                    traceback.print_exc()
                    LOGGER.error(traceback.format_exc())

                check_elapsed = time.time() - check_started_at
                total_elapsed = time.time() - started_at

                print()
                print(f"本次检查耗时：{check_elapsed:.1f}秒")
                print(f"累计运行：{format_duration(total_elapsed)}")
                print(
                    f"检查统计：共{check_count}次，"
                    f"成功{success_count}次，"
                    f"失败{failed_count}次，"
                    f"推送{push_count}次"
                )
                print()

                # GitHub Actions 已由 cron 每10分钟启动一次，
                # 每次检查完成后应立即退出，不能在任务内继续倒计时。
                if IS_GITHUB_ACTIONS:
                    log("GitHub Actions 单次检查完成，程序退出。")
                    break

                countdown(CHECK_INTERVAL)

        finally:
            try:
                context.storage_state(path=str(STATE_FILE))
            except Exception:
                pass

            browser.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，成绩监控已手动停止。")
    except Exception as error:
        print(f"\n程序启动失败：{error}")
        traceback.print_exc()
        sys.exit(1)
