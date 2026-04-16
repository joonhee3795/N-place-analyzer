from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from playwright.sync_api import sync_playwright
import re
import traceback 

# FastAPI 앱 초기화
app = FastAPI(title="네이버 플레이스 진단 API")

# 프론트엔드(HTML)에서 API를 마음껏 호출할 수 있도록 CORS 정책 허용
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
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
            
            context = browser.new_context(
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
            )
            page = context.new_page()
            
            # [핵심 수정] 클라우드 환경에 맞게 대기 시간을 45초로 넉넉하게 늘리고, 무거운 이미지는 기다리지 않게(domcontentloaded) 설정합니다.
            page.goto(target_url, timeout=45000, wait_until="domcontentloaded")
            
            # 화면 어딘가에 '리뷰'라는 단어가 나타날 때까지 대기 (서버가 느릴 수 있으니 여기도 20초 부여)
            try:
                page.wait_for_selector("text=리뷰", timeout=20000)
            except:
                pass 
            
            # 데이터가 화면에 완전히 뿌려질 수 있도록 3초 대기
            page.wait_for_timeout(3000)
            
            # --- [데이터 수집 파트] ---
            og_title_element = page.locator('meta[property="og:title"]')
            og_title = og_title_element.get_attribute('content') if og_title_element.count() > 0 else ""
            page_title = page.title()
            
            if og_title and og_title != "네이버 플레이스":
                scraped_data['shop_name'] = og_title
            else:
                scraped_data['shop_name'] = page_title.split('-')[0].strip() if '-' in page_title else page_title
            
            body_text = page.locator("body").inner_text()
            
            visitor_match = re.search(r'방문자\s*리뷰\s*[\n\r]*\s*([\d,]+)', body_text)
            scraped_data['visitor_reviews'] = int(visitor_match.group(1).replace(',', '')) if visitor_match else 0
            
            blog_match = re.search(r'블로그\s*리뷰\s*[\n\r]*\s*([\d,]+)', body_text)
            scraped_data['blog_reviews'] = int(blog_match.group(1).replace(',', '')) if blog_match else 0
            
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

        # 1. 트래픽 
        traffic_score = min(20, int((v_rev / 1000) * 20)) if v_rev > 0 else 0
        traffic_status = "양호" if traffic_score >= 16 else "보통" if traffic_score >= 8 else "위험"
        metrics['traffic'] = {"score": traffic_score, "status": traffic_status, "label": "트래픽"}

        # 2. 체류시간 
        dwell_score = min(20, int((b_rev / 200) * 20)) if b_rev > 0 else 0
        dwell_status = "양호" if dwell_score >= 16 else "보통" if dwell_score >= 8 else "위험"
        metrics['dwell_time'] = {"score": dwell_score, "status": dwell_status, "label": "체류시간"}

        # 3. 검색적합도 
        seo_score = min(20, int((len(desc) / 150) * 20)) if len(desc) > 0 else 0
        seo_status = "양호" if seo_score >= 16 else "보통" if seo_score >= 8 else "위험"
        metrics['seo'] = {"score": seo_score, "status": seo_status, "label": "검색적합도"}

        # 4. 리뷰품질 
        review_score = 0
        if v_rev > 0:
            ratio = b_rev / v_rev
            if 0.05 <= ratio <= 0.20:
                review_score = 20
            elif 0.02 <= ratio < 0.05 or 0.20 < ratio <= 0.30: 
                review_score = 14
            else: 
                review_score = 8
        review_status = "양호" if review_score >= 16 else "보통" if review_score >= 12 else "위험"
        metrics['review_quality'] = {"score": review_score, "status": review_status, "label": "리뷰품질"}

        # 5. 저장/공유 
        save_score = int((traffic_score * 0.6) + (dwell_score * 0.4))
        save_status = "양호" if save_score >= 16 else "보통" if save_score >= 8 else "위험"
        metrics['save_share'] = {"score": save_score, "status": save_status, "label": "저장/공유"}

        # 총점 합산 
        total_score = traffic_score + dwell_score + seo_score + review_score + save_score
        total_score = min(100, total_score + 5) if total_score > 0 else 0

    return {
        "place_id": place_id,
        "raw_data": scraped_data,
        "analysis": {
            "total_score": total_score,
            "metrics": metrics
        },
        "message": "데이터 수집 및 분석 로직 실행 완료"
    }