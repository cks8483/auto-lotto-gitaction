import re
import sys
import time
from datetime import datetime

import pytz
from requests import post, Response, Session
from playwright.sync_api import Playwright, sync_playwright, TimeoutError as PlaywrightTimeoutError
from bs4 import BeautifulSoup


# =========================
# 명령행 인자
# =========================
RUN_FILE_NAME = sys.argv[0]

USER_ID = sys.argv[1]          # 동행복권 ID
USER_PW = sys.argv[2]          # 동행복권 PW
SLACK_BOT_TOKEN = sys.argv[3]  # Slack Bot Token
SLACK_CHANNEL = sys.argv[4]    # Slack 채널 (예: #lotto)
COUNT = int(sys.argv[5])       # 구매 개수
FIXED_NUMBER  = sys.argv[6]  # 고정 숫자


# =========================
# 예외 정의
# =========================
class BalanceError(Exception):
    pass


# =========================
# 공통 유틸
# =========================
def get_now() -> datetime:
    korea_tz = pytz.timezone("Asia/Seoul")
    return datetime.now(pytz.utc).astimezone(korea_tz)


def hook_slack(message: str) -> Response:
    korea_time_str = get_now().strftime("%Y-%m-%d %H:%M:%S")
    payload = {
        "text": f"> {korea_time_str} *로또 자동 구매 봇 로그* \n{message}",
        "channel": SLACK_CHANNEL,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
    }
    return post("https://slack.com/api/chat.postMessage", json=payload, headers=headers)


def hook_slack_btn() -> Response:
    """예치금 부족 시 버튼 포함 Slack 알림"""
    korea_time_str = get_now().strftime("%Y-%m-%d %H:%M:%S")
    payload = {
        "channel": SLACK_CHANNEL,
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"> {korea_time_str} *로또 자동 구매 봇 알림* \n예치금이 부족합니다! 충전을 해주세요!",
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "충전하러 가기",
                            "emoji": True,
                        },
                        "url": "https://dhlottery.co.kr/payment.do?method=payment",
                        "action_id": "button_action",
                    }
                ],
            },
        ],
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
    }
    return post("https://slack.com/api/chat.postMessage", json=payload, headers=headers)


def log(msg: str):
    print("[LOG]", msg)
    # 필요 없으면 아래 주석 처리
    # hook_slack(msg)


def error_log(page, msg: str):
    """에러 발생 시: Slack + 콘솔 + 스크린샷 + HTML"""
    print("[ERROR]", msg)
    hook_slack(f"[ERROR] {msg}")

    if page is None:
        return

    try:
        page.screenshot(path="error.png", full_page=True)
        print("Screenshot saved: error.png")
    except Exception as e:
        print("Screenshot failed:", e)

    try:
        html = page.content()
        with open("error.html", "w", encoding="utf-8") as f:
            f.write(html)
        print("HTML saved: error.html")
    except Exception as e:
        print("HTML save failed:", e)



def buy_lotto_fixed_number(page, count, fixed_number):
    """
    반자동 구매
    (혼합선택 → 초기화 → 적용수량 → fixed_number 클릭 → 자동선택 → 확인 → 구매하기)
    """
    
    # 1) 혼합선택 탭 클릭
    page.click("text=혼합선택")
    page.wait_for_timeout(500)  # 탭 전환 대기

    # 2) 초기화
    page.click("text=초기화")
    page.wait_for_timeout(300)

    # 3) 구매 개수(적용 수량)
    page.select_option("#amoundApply", str(count))
    page.wait_for_timeout(300)

    # 4) 고정번호 클릭 - label을 직접 클릭
    try:
        # label을 통한 클릭 시도
        label_selector = f'label[for="check645num{fixed_number}"]'
        label = page.locator(label_selector)
        label.scroll_into_view_if_needed()
        page.wait_for_timeout(500)  # 스크롤 완료 대기
        label.click(force=True)
        print(f"고정번호 {fixed_number} label 클릭 성공")
    except Exception as e:
        print(f"label 클릭 실패, checkbox 직접 클릭 시도: {e}")
        # checkbox 직접 클릭 시도
        checkbox = page.locator(f"#check645num{fixed_number}")
        page.evaluate(f"document.getElementById('check645num{fixed_number}').scrollIntoView({{block: 'center'}})")
        page.wait_for_timeout(500)
        checkbox.click(force=True)

    page.wait_for_timeout(500)

    # 5) 자동선택 (label for 구조)
    auto_label = page.locator('label[for="checkAutoSelect"]')
    auto_label.scroll_into_view_if_needed()
    page.wait_for_timeout(300)
    auto_label.click(force=True)
    page.wait_for_timeout(500)

    # 6) 선택 확정 버튼
    confirm_btn = page.locator("#btnSelectNum")
    confirm_btn.scroll_into_view_if_needed()
    page.wait_for_timeout(300)
    confirm_btn.click()
    page.wait_for_timeout(1000)  # 번호 적용 대기

    # 7) 구매하기
    buy_btn = page.locator("#btnBuy")
    buy_btn.scroll_into_view_if_needed()
    page.wait_for_timeout(300)
    buy_btn.click()
    page.wait_for_timeout(1000)

    # 8) 구매확인 팝업 처리
    try:
        confirm_popup = page.locator('#popupLayerConfirm input[value="확인"]')
        confirm_popup.wait_for(state="visible", timeout=5000)
        confirm_popup.click()
        page.wait_for_timeout(1000)
    except:
        pass

    print(f"반자동 구매 완료 → 고정번호 {fixed_number}, {count}게임")




# =========================
# Playwright 메인 로직
# =========================
# =========================
# Playwright 메인 로직
# =========================
def run(playwright: Playwright) -> None:
    browser = None
    context = None
    page = None

    try:
        log("브라우저 실행 중…")

        browser = playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 960},
            device_scale_factor=1,
            is_mobile=False,
            has_touch=False,
        )

        context.add_init_script("""
            Object.defineProperty(navigator, 'platform', {
                get: () => 'Win32',
            });
        """)

        page = context.new_page()

        # ============================
        # 1. 로그인 페이지
        # ============================
        login_url = "https://dhlottery.co.kr/user.do?method=login&returnUrl="
        log(f"로그인 페이지 접속: {login_url}")
        page.goto(login_url, wait_until="load")

        log("아이디 / 비밀번호 입력")
        page.fill("#userId", USER_ID)
        page.fill('input[name="password"]', USER_PW)

        log("로그인 버튼 클릭")
        page.click("a.btn_common.lrg.blu")

        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except PlaywrightTimeoutError:
            log("networkidle 타임아웃 – 계속 진행 시도")

        # ============================
        # 2. 로그인 후 메인 확인
        # ============================
        log("메인 페이지 이동하여 로그인 확인")
        page.goto("https://dhlottery.co.kr/common.do?method=main", wait_until="load")

        try:
            info = page.wait_for_selector("ul.information", timeout=5000)
        except PlaywrightTimeoutError:
            error_log(page, "로그인 실패 또는 ul.information 요소 없음")
            raise Exception("로그인 실패로 판단됨")

        money_info_text = info.inner_text().split("\n")
        user_name = money_info_text[0].strip()
        deposit = int(money_info_text[2].replace(",", "").replace("원", "").strip())

        log(f"로그인 사용자: {user_name}, 예치금: {deposit:,}원")

        total_price = 1000 * COUNT
        if deposit < total_price:
            hook_slack_btn()
            raise BalanceError(f"예치금 부족: 필요 {total_price:,}원, 보유 {deposit:,}원")

        # ============================
        # 3. 구매 페이지 이동
        # ============================
        game_url = "https://ol.dhlottery.co.kr/olotto/game/game645.do"
        log(f"구매 페이지 접속: {game_url}")
        page.goto(game_url, wait_until="load")

        try:
            popup = page.locator("#popupLayerAlert")
            if popup.is_visible():
                popup.get_by_role("button", name="확인").click()
                log("비정상 접근 팝업 닫음")
        except:
            pass
        

        # ============================
        # ★★★ 반자동 구매 실행 ★★★
        # ============================

        log(f"반자동 구매 실행 (필수번호={FIXED_NUMBER})")
        buy_lotto_fixed_number(page, COUNT, FIXED_NUMBER)
        hook_slack(f"반자동 구매 완료: {COUNT}게임 / 필수번호={FIXED_NUMBER}")

        # ============================
        # 5. 오늘 구매한 번호 조회
        # ============================
        log("오늘 구매한 번호 조회 시작")

        cookies = page.context.cookies()
        session = Session()
        for cookie in cookies:
            session.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"])

        url = "https://dhlottery.co.kr/myPage.do"
        querystring = {"method": "lottoBuyList"}
        now_date = get_now().strftime("%Y%m%d")
        payload = f"searchStartDate={now_date}&searchEndDate={now_date}&winGrade=2"

        headers = {
            "Accept": "text/html",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://dhlottery.co.kr",
            "Referer": "https://dhlottery.co.kr/myPage.do?method=lottoBuyListView",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        }

        res = session.post(url, data=payload, headers=headers, params=querystring)
        html = BeautifulSoup(res.content, "html.parser")

        a_tag = html.select_one("tbody > tr:nth-child(1) > td:nth-child(4) > a")
        if not a_tag:
            error_log(page, "구매내역을 찾지 못했습니다.")
            raise Exception("구매내역 없음")

        detail_info = re.findall(r"\d+", a_tag.get("href"))

        detail_url = (
            "https://dhlottery.co.kr/myPage.do"
            f"?method=lotto645Detail&orderNo={detail_info[0]}"
            f"&barcode={detail_info[1]}&issueNo={detail_info[2]}"
        )
        log(f"상세 페이지 이동: {detail_url}")
        page.goto(detail_url, wait_until="load")

        log("번호 추출")
        result_msg = ""
        for li in page.query_selector_all("div.selected li"):
            text = ", ".join(li.inner_text().split("\n"))
            result_msg += text + "\n"

        log(f"이번주 번호:\n{result_msg}")
        hook_slack(f"이번주 반자동 구매 번호:\n{result_msg}")

    except BalanceError as be:
        error_log(page, f"BalanceError: {be}")
    except Exception as e:
        error_log(page, f"Exception 발생: {e}")
    finally:
        if context: context.close()
        if browser: browser.close()
        log("브라우저 종료 완료")



# =========================
# 엔트리 포인트
# =========================
if __name__ == "__main__":
    with sync_playwright() as playwright:
        run(playwright)
