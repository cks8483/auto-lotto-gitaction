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
SELECTED_NUMBERS = sys.argv[6:]  # 안 쓰고 있지만 남겨둠


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
            headless=True,  # 디버깅 시 False로 바꾸세요
            args=[
                "--disable-blink-features=AutomationControlled",
            ],
        )

        # PC 환경 강제 (User-Agent + platform)
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

        # navigator.platform = 'Win32' 로 강제
        context.add_init_script(
            """
            Object.defineProperty(navigator, 'platform', {
                get: () => 'Win32',
            });
            """
        )

        page = context.new_page()

        # ============================
        # 1. 로그인 페이지
        # ============================
        login_url = "https://dhlottery.co.kr/user.do?method=login&returnUrl="
        log(f"로그인 페이지 접속: {login_url}")
        page.goto(login_url, wait_until="load")

        # 아이디 / 비밀번호 입력
        log("아이디 / 비밀번호 입력")
        page.fill("#userId", USER_ID)
        page.fill('input[name="password"]', USER_PW)

        # a 태그 로그인 버튼 클릭 (최신 DOM 기준)
        log("로그인 버튼 클릭")
        page.click("a.btn_common.lrg.blu")  # <a class="btn_common lrg blu">로그인</a>

        # 로그인 처리 대기
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except PlaywrightTimeoutError:
            log("networkidle 타임아웃 – 계속 진행 시도")

        # ============================
        # 2. 메인 페이지 이동 + 로그인 확인
        # ============================
        log("메인 페이지로 이동하여 로그인 확인")
        page.goto("https://dhlottery.co.kr/common.do?method=main", wait_until="load")

        # 로그인 후에만 나타나는 정보 영역 찾기
        try:
            info = page.wait_for_selector("ul.information", timeout=5000)
        except PlaywrightTimeoutError:
            error_log(page, "로그인 실패 또는 ul.information 요소를 찾지 못했습니다.")
            raise Exception("로그인 실패로 판단됨 (ul.information 미존재)")

        money_info_text = info.inner_text().split("\n")
        # 일반적으로: [0]=이름, [2]=예치금
        user_name = money_info_text[0].strip()
        deposit_str_raw = money_info_text[2].replace(",", "").replace("원", "").strip()
        deposit = int(deposit_str_raw)

        log(f"로그인 사용자: {user_name}, 예치금: {deposit:,}원")

        # 예치금 체크
        total_price = 1000 * COUNT
        if deposit < total_price:
            hook_slack_btn()
            raise BalanceError(f"예치금 부족: 필요 {total_price:,}원, 보유 {deposit:,}원")

        # ============================
        # 3. 로또 6/45 구매 페이지 이동
        # ============================
        game_url = "https://ol.dhlottery.co.kr/olotto/game/game645.do"
        log(f"구매 페이지 접속: {game_url}")
        page.goto(game_url, wait_until="load")

        # "비정상적인 방법" 팝업 처리 (있을 경우만)
        log("비정상 접근 팝업 처리 시도")
        try:
            popup = page.locator("#popupLayerAlert")
            if popup.is_visible():
                popup.get_by_role("button", name="확인").click()
                log("비정상 접근 팝업 닫음")
        except Exception:
            log("비정상 접근 팝업 없음 또는 처리 실패 – 계속 진행")

        # ============================
        # 4. 혼합선택 / 자동선택 / 구매하기
        # ============================
        log("혼합선택 탭 클릭")
        # 탭이 텍스트로 되어있을 경우
        page.click("text=혼합선택")

        # 구매 개수 선택 (select 박스)
        log(f"구매 개수 선택: {COUNT} 게임")
        # 페이지 구조에 따라 select가 하나뿐이면 아래로 충분
        page.select_option("select", str(COUNT))

        # 자동선택 클릭 (번호 자동)
        log("자동선택 버튼 클릭")
        page.click("text=자동선택")

        # 확인 버튼 클릭
        log("확인 클릭")
        page.click("text=확인")

        # 구매하기 버튼 클릭
        log("구매하기 버튼 클릭")
        page.click('input:has-text("구매하기")')

        # 구매 확인 팝업 처리
        time.sleep(2)
        log("구매 확인 팝업 처리")
        try:
            # "확인" / "취소" 버튼 구조에 따라 조정 가능
            page.click('text=확인 취소 >> input[type="button"]')
        except Exception:
            log("구매 확인 팝업 버튼을 찾지 못했으나, 계속 진행 시도")

        # 구매 완료 후 닫기 버튼
        try:
            page.click('input[name="closeLayer"]')
        except Exception:
            log("구매 결과 레이어 closeLayer 버튼 없음 또는 이미 닫힘")

        log(f"{COUNT}개 복권 구매 성공!")
        hook_slack(
            f"{COUNT}개 복권 구매 성공! \n자세히 보기: https://dhlottery.co.kr/myPage.do?method=notScratchListView"
        )

        # ============================
        # 5. 오늘 구매한 번호 확인
        # ============================
        log("오늘 구매한 번호 조회 시작")

        cookies = page.context.cookies()
        session = Session()
        for cookie in cookies:
            session.cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"])

        url = "https://dhlottery.co.kr/myPage.do"
        querystring = {"method": "lottoBuyList"}
        now_date = get_now().date().strftime("%Y%m%d")
        payload = f"searchStartDate={now_date}&searchEndDate={now_date}&winGrade=2"

        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
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
        html = BeautifulSoup(res.content, "lxml")

        a_tag = html.select_one("tbody > tr:nth-child(1) > td:nth-child(4) > a")
        if not a_tag:
            error_log(page, "오늘 구매 내역을 찾지 못했습니다. (tbody 1행 4열 a 태그 없음)")
            raise Exception("구매 내역 없음 또는 DOM 구조 변경")

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
            # 각 li 안에 줄바꿈으로 숫자가 있을 수 있으니 join
            text = ", ".join(li.inner_text().split("\n"))
            result_msg += text + "\n"

        log(f"이번주 나의 번호:\n{result_msg}")
        hook_slack(f"이번주 나의 행운의 번호는?!\n{result_msg}")

    except BalanceError as be:
        # 예치금 부족
        error_log(page, f"BalanceError: {be}")
    except Exception as e:
        error_log(page, f"Exception 발생: {e}")
    finally:
        if context is not None:
            context.close()
        if browser is not None:
            browser.close()
        log("브라우저/컨텍스트 종료 완료")


# =========================
# 엔트리 포인트
# =========================
if __name__ == "__main__":
    with sync_playwright() as playwright:
        run(playwright)
