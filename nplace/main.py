from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware # [추가됨] 브라우저 통신(CORS) 허용을 위한 모듈
from pydantic import BaseModel
from playwright.sync_api import sync_playwright
import re
import traceback 

# FastAPI 앱 초기화
app = FastAPI(title="네이버 플레이스 진단 API")

# [추가됨] 프론트엔드(HTML)에서 API를 마음껏 호출할 수 있도록 CORS 정책 허용
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # 실제 운영 시에는 내 도메인("https://mywebsite.com")만 넣는 것이 좋습니다.
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 클라이언트(웹)에서 받을 데이터 형식 정의
class PlaceRequest(BaseModel):
    url: str

# 기본 주소로 접속 시 안내 메시지
@app.get("/")
def read_root():
    return {"message": "네이버 플레이스 진단 API 서버가 정상 작동 중입니다. 테스트하려면 브라우저 주소창에 /docs 를 붙여서 접속하세요."}

@app.post("/analyze")
def analyze_place(req: PlaceRequest):
    """
    프론트엔드에서 URL을 보내면 실행되는 핵심 함수입니다.
    """
    # 1. URL에서 고유 ID 추출
    match = re.search(r'/(\d{6,})', req.url)
    if not match:
        raise HTTPException(status_code=400, detail="유효한 네이버 플레이스 URL이 아닙니다.")
    
    place_id = match.group(1)
    
    # 2. 크롤링하기 쉬운 모바일맵 URL로 변환
    target_url = f"https://m.place.naver.com/place/{place_id}/home"
    
    scraped_data = {}
    
    # 3. Playwright를 이용한 백그라운드 브라우저 실행 및 크롤링
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            
            # [핵심 수정 1] 봇 차단 방지: 실제 아이폰으로 접속하는 것처럼 브라우저를 속입니다.
            context = browser.new_context(
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
            )
            page = context.new_page()
            
            # 해당 URL로 이동
            page.goto(target_url, timeout=15000)
            
            # [핵심 수정 2] 화면 어딘가에 '리뷰'라는 단어가 나타날 때까지 대기 (데이터가 로딩되었다는 증거)
            try:
                page.wait_for_selector("text=리뷰", timeout=10000)
            except:
                pass # 못 찾더라도 다음 단계로 진행
            
            # 데이터가 화면에 완전히 뿌려질 수 있도록 3초간 확실하게 대기합니다.
            page.wait_for_timeout(3000)
            
            # --- [데이터 수집 파트] ---
            
            # (1) 상호명 추출: 여러 방식을 동원해 진짜 상호명을 빼옵니다.
            og_title_element = page.locator('meta[property="og:title"]')
            og_title = og_title_element.get_attribute('content') if og_title_element.count() > 0 else ""
            page_title = page.title()
            
            # 메타태그나 타이틀에서 껍데기 이름이 아닌 진짜 이름을 선별합니다.
            if og_title and og_title != "네이버 플레이스":
                scraped_data['shop_name'] = og_title
            else:
                scraped_data['shop_name'] = page_title.split('-')[0].strip() if '-' in page_title else page_title
            
            # (2) 리뷰 수 추출: 화면 텍스트 모두 긁어오기
            body_text = page.locator("body").inner_text()
            
            # [핵심 수정 3] 정규식 강화: '방문자리뷰'와 '숫자' 사이에 줄바꿈이나 띄어쓰기가 있어도 잡아냅니다.
            visitor_match = re.search(r'방문자\s*리뷰\s*[\n\r]*\s*([\d,]+)', body_text)
            scraped_data['visitor_reviews'] = int(visitor_match.group(1).replace(',', '')) if visitor_match else 0
            
            blog_match = re.search(r'블로그\s*리뷰\s*[\n\r]*\s*([\d,]+)', body_text)
            scraped_data['blog_reviews'] = int(blog_match.group(1).replace(',', '')) if blog_match else 0
            
            # [추가됨] 검색적합도 분석을 위해 매장 소개글(og:description) 추출
            og_desc_element = page.locator('meta[property="og:description"]')
            scraped_data['description'] = og_desc_element.get_attribute('content') if og_desc_element.count() > 0 else ""
                
            scraped_data['status'] = "success"
            
            browser.close()
            
    except Exception as e:
        scraped_data['status'] = "error"
        scraped_data['message'] = str(e)
        scraped_data['traceback'] = traceback.format_exc()

    # --- [점수 계산 파트 (마케팅 알고리즘 고도화)] ---
    total_score = 0
    metrics = {}

    if scraped_data.get('status') == 'success':
        v_rev = scraped_data.get('visitor_reviews', 0)
        b_rev = scraped_data.get('blog_reviews', 0)
        desc = scraped_data.get('description', '')

        # 1. 트래픽 (1페이지 평균 방문자리뷰 1,000개 기준 비례 점수)
        traffic_score = min(20, int((v_rev / 1000) * 20)) if v_rev > 0 else 0
        traffic_status = "양호" if traffic_score >= 16 else "보통" if traffic_score >= 8 else "위험"
        metrics['traffic'] = {"score": traffic_score, "status": traffic_status, "label": "트래픽"}

        # 2. 체류시간 (1페이지 평균 블로그리뷰 200개 기준 비례 점수)
        dwell_score = min(20, int((b_rev / 200) * 20)) if b_rev > 0 else 0
        dwell_status = "양호" if dwell_score >= 16 else "보통" if dwell_score >= 8 else "위험"
        metrics['dwell_time'] = {"score": dwell_score, "status": dwell_status, "label": "체류시간"}

        # 3. 검색적합도 (소개글 150자 이상 기준 비례 점수 - 키워드 세팅 여부 간접 확인)
        seo_score = min(20, int((len(desc) / 150) * 20)) if len(desc) > 0 else 0
        seo_status = "양호" if seo_score >= 16 else "보통" if seo_score >= 8 else "위험"
        metrics['seo'] = {"score": seo_score, "status": seo_status, "label": "검색적합도"}

        # 4. 리뷰품질 (블로그리뷰 / 방문자리뷰 황금 비율: 5% ~ 20% 사이일 때 최고점)
        review_score = 0
        if v_rev > 0:
            ratio = b_rev / v_rev
            if 0.05 <= ratio <= 0.20:  # 황금 비율 (자연스러운 바이럴)
                review_score = 20
            elif 0.02 <= ratio < 0.05 or 0.20 < ratio <= 0.30: # 체험단 의존 혹은 리뷰 관리 약간 부족
                review_score = 14
            else: # 극단적 불균형 (어뷰징 의심 혹은 마케팅 전무)
                review_score = 8
        review_status = "양호" if review_score >= 16 else "보통" if review_score >= 12 else "위험"
        metrics['review_quality'] = {"score": review_score, "status": review_status, "label": "리뷰품질"}

        # 5. 저장/공유 (트래픽과 체류시간의 종합 활성도 반영)
        save_score = int((traffic_score * 0.6) + (dwell_score * 0.4))
        save_status = "양호" if save_score >= 16 else "보통" if save_score >= 8 else "위험"
        metrics['save_share'] = {"score": save_score, "status": save_status, "label": "저장/공유"}

        # 총점 합산 (보정: 기본기가 아주 없는 매장이 아니라면 최소한의 희망을 주기 위해 기본점수 5점 세팅)
        total_score = traffic_score + dwell_score + seo_score + review_score + save_score
        total_score = min(100, total_score + 5) if total_score > 0 else 0

    # 4. 분석 결과 반환 (프론트엔드로 보내줄 최종 데이터 세트)
    return {
        "place_id": place_id,
        "raw_data": scraped_data,
        "analysis": {
            "total_score": total_score,
            "metrics": metrics
        },
        "message": "데이터 수집 및 분석 로직 실행 완료"
    }