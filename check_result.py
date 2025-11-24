import re
import os
import sys
import time
from datetime import datetime
from typing import List

import pytz
from requests import post, Response, Session
from playwright.sync_api import Playwright, sync_playwright
from bs4 import BeautifulSoup

# 환경변수 로드
if len(sys.argv) < 6:
    USER_ID = os.getenv('LOTTO_USER_ID')
    USER_PW = os.getenv('LOTTO_USER_PW')
    SLACK_BOT_TOKEN = os.getenv('SLACK_BOT_TOKEN')
    SLACK_CHANNEL = os.getenv('SLACK_CHANNEL')
    COUNT = os.getenv('LOTTO_COUNT', '5')
else:
    USER_ID = sys.argv[1]
    USER_PW = sys.argv[2]
    SLACK_BOT_TOKEN = sys.argv[3]
    SLACK_CHANNEL = sys.argv[4]
    COUNT = sys.argv[5]

SLACK_API_URL = "https://slack.com/api/chat.postMessage"


def get_now() -> datetime:
    korea_tz = pytz.timezone("Asia/Seoul")
    return datetime.now(pytz.utc).astimezone(korea_tz)


def get_check_lucky_number(lucky_numbers: List[str], my_numbers: List[str]) -> str:
    return_msg = ""
    for my_num in my_numbers:
        if my_num in lucky_numbers:
            return_msg += f" [ {my_num} ] "
        else:
            return_msg += f" {my_num} "
    return return_msg


def hook_slack(message: str) -> Response:
    korea_time_str = get_now().strftime("%Y-%m-%d %H:%M:%S")
    payload = {
        "text": f"> {korea_time_str} *로또 자동 구매 봇 알림* \n{message}",
        "channel": SLACK_CHANNEL,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
    }
    return post(SLACK_API_URL, json=payload, headers=headers)


def log(msg: str):
    print(f"[LOG] {msg}")


def error_log(page, msg: str):
    print(f"[ERROR] {msg}")
    hook_slack(f"[ERROR] {msg}")
    
    if page:
        try:
            page.screenshot(path="error.png", full_page=True)
            print("Screenshot saved: error.png")
        except Exception as e:
            print(f"Screenshot failed: {e}")
        
        try:
            with open("error.html", "w", encoding="utf-8") as f:
                f.write(page.content())
            print("HTML saved: error.html")
        except Exception as e:
            print(f"HTML save failed: {e}")


def run(playwright: Playwright) -> None:
    browser = None
    context = None
    page = None
    
    try:
        # ================================================================ #
        # 초기 세팅 및 로그인
        # ================================================================ #
        log("브라우저 실행 중...")
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        
        log("로그인 페이지 접속")
        page.goto("https://dhlottery.co.kr/user.do?method=login")
        page.fill('[placeholder="아이디"]', USER_ID)
        page.fill('[placeholder="비밀번호"]', USER_PW)
        
        log("로그인 시도")
        with page.expect_navigation():
            page.press('form[name="jform"] >> text=로그인', "Enter")
        time.sleep(4)

        # ================================================================ #
        # 당첨 번호 파싱
        # ================================================================ #
        log("메인 페이지에서 당첨 번호 확인")
        page.goto("https://dhlottery.co.kr/common.do?method=main")
        page.wait_for_selector("#article div.content", timeout=10000)
        
        result_info = page.query_selector("#article div.content")
        retry_cnt = 0
        while not result_info and retry_cnt < 3:
            log(f"당첨 번호 파싱 재시도 {retry_cnt + 1}/3")
            time.sleep(1)
            result_info = page.query_selector("#article div.content")
            retry_cnt += 1
        
        if not result_info:
            raise Exception("당첨 번호를 찾을 수 없습니다")
        
        result_info = result_info.inner_text().split("이전")[0].replace("\n", " ")
        log(f"로또 결과: {result_info}")
        hook_slack(f"로또 결과: {result_info}")

        lucky_number = (
            result_info.split("당첨번호")[-1]
            .split("1등")[0]
            .strip()
            .replace("보너스번호 ", "")
            .replace(" ", ",")
        )
        lucky_number = lucky_number.split(",")
        log(f"당첨 번호: {lucky_number}")

        # ================================================================ #
        # 구매 내역 조회 (Requests 사용)
        # ================================================================ #
        log("구매 내역 조회 시작")
        
        # Playwright 쿠키를 Session에 전달
        cookies = page.context.cookies()
        session = Session()
        
        # 쿠키 디버깅 출력
        log(f"전달할 쿠키 개수: {len(cookies)}")
        for cookie in cookies:
            session.cookies.set(
                cookie["name"], 
                cookie["value"], 
                domain=cookie["domain"],
                path=cookie.get("path", "/")
            )
            log(f"쿠키 설정: {cookie['name']} = {cookie['value'][:20]}...")  # 값 일부만 출력
        
        url = "https://dhlottery.co.kr/myPage.do"
        querystring = {"method": "lottoBuyList"}
        now_date = get_now().date().strftime("%Y%m%d")
        log(f"조회 날짜: {now_date}")
        
        payload = f"searchStartDate={now_date}&searchEndDate={now_date}&winGrade=2"
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ko,en;q=0.9",
            "Cache-Control": "max-age=0",
            "Connection": "keep-alive",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://dhlottery.co.kr",
            "Referer": "https://dhlottery.co.kr/myPage.do?method=lottoBuyListView",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        }
        
        # POST 요청 실행
        log("POST 요청 전송 중...")
        res = session.post(url, data=payload, headers=headers, params=querystring, allow_redirects=False)
        
        log(f"응답 상태 코드: {res.status_code}")
        log(f"응답 URL: {res.url}")
        log(f"응답 헤더: {dict(res.headers)}")
        
        # 리다이렉트 처리
        if res.status_code in [301, 302, 303, 307, 308]:
            redirect_url = res.headers.get("Location")
            log(f"리다이렉트 감지: {redirect_url}")
            res = session.get(redirect_url, headers=headers)
            log(f"리다이렉트 후 상태 코드: {res.status_code}")
        
        # HTML 저장 (디버깅용)
        with open("purchase_list.html", "w", encoding="utf-8") as f:
            f.write(res.text)
        log("구매 내역 HTML 저장: purchase_list.html")
        
        # HTML 파싱
        html = BeautifulSoup(res.content, "lxml")
        
        # a 태그 찾기
        a_tag = html.select_one("tbody > tr:nth-child(1) > td:nth-child(4) > a")
        
        if not a_tag:
            error_log(page, "구매 내역을 찾을 수 없습니다")
            
            tbody = html.select_one("tbody")
            if tbody:
                log(f"tbody 내용: {tbody.get_text()}")
            else:
                log("tbody 자체가 없습니다")
            
            nodata = html.select_one(".nodata")
            if nodata:
                log(f"구매 내역 없음: {nodata.get_text()}")
                hook_slack("오늘 구매한 로또가 없습니다.")
                return
            
            raise Exception("구매 내역 파싱 실패")
        
        a_tag_href = a_tag.get("href")
        log(f"구매 상세 링크: {a_tag_href}")
        
        detail_info = re.findall(r"\d+", a_tag_href)
        
        if len(detail_info) < 3:
            raise Exception(f"상세 정보 파싱 실패: {detail_info}")
        
        # 상세 페이지 접근 (Playwright 사용)
        detail_url = (
            f"https://dhlottery.co.kr/myPage.do?method=lotto645Detail"
            f"&orderNo={detail_info[0]}&barcode={detail_info[1]}&issueNo={detail_info[2]}"
        )
        log(f"상세 페이지 이동: {detail_url}")
        page.goto(detail_url)
        
        page.wait_for_selector("div.selected li", timeout=5000)
        
        result_msg = ""
        results = page.query_selector_all("div.selected li")
        
        if not results:
            error_log(page, "구매한 번호를 찾을 수 없습니다")
            raise Exception("번호 파싱 실패")
        
        for result in results:
            my_lucky_number = result.inner_text().split("\n")
            result_msg += (
                my_lucky_number[0]
                + get_check_lucky_number(lucky_number, my_lucky_number[1:])
                + "\n"
            )
        
        log(f"이번주 나의 번호:\n{result_msg}")
        hook_slack(f"> 이번주 나의 행운의 번호 결과는?!?!?!\n{result_msg}")

    except Exception as exc:
        error_log(page, f"예외 발생: {exc}")
        raise exc
    finally:
        if context:
            context.close()
        if browser:
            browser.close()
        log("브라우저 종료 완료")


with sync_playwright() as playwright:
    run(playwright)